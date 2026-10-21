"""Reply targets and silent async inter-agent delivery."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock

from ductor_bot.multiagent.bus import (
    AsyncInterAgentResult,
    AsyncInterAgentTask,
    AsyncSendOptions,
    InterAgentBus,
    _make_result,
)


def _make_stack(orch_result: str = "ok") -> MagicMock:
    stack = MagicMock()
    orch = MagicMock()
    orch.handle_interagent_message = AsyncMock(return_value=(orch_result, "session", ""))
    stack.bot.orchestrator = orch
    stack.bot.notification_service = MagicMock()
    stack.bot.notification_service.notify = AsyncMock()
    stack.bot.notification_service.notify_all = AsyncMock()
    stack.config = MagicMock()
    stack.config.allowed_user_ids = [42]
    stack.config.interagent_notify_received = True
    return stack


async def test_send_async_populates_reply_to_and_silent_on_task() -> None:
    bus = InterAgentBus()
    bus.register("target", _make_stack())
    bus.register("reply-target", _make_stack())
    bus.set_async_result_handler("reply-target", AsyncMock())

    opts = AsyncSendOptions(reply_to="reply-target", silent=True)
    task_id = bus.send_async("sender", "target", "Hello", opts=opts)
    assert task_id is not None

    # Task is in flight briefly; we can grab it before it finishes.
    # The in-flight dict keys on task_id.
    task: AsyncInterAgentTask | None = None
    if task_id in bus._async_tasks:
        task = bus._async_tasks[task_id]

    # Even if the task already completed, the fields must have been set.
    # Drain and assert on whichever object we can still inspect.
    if task is None:
        # Wait briefly then reconstruct expectations — the best we can do
        # is confirm the opts round-tripped into send_async by inspecting
        # the delivered result handler's payload.
        delivered: list[AsyncInterAgentResult] = []
        bus.set_async_result_handler("sender", AsyncMock(side_effect=delivered.append))
        # Re-submit to observe the task instance synchronously.
        task_id = bus.send_async("sender", "target", "Hello", opts=opts)
        assert task_id is not None
        task = bus._async_tasks[task_id]

    assert task is not None
    assert task.reply_to == "reply-target"
    assert task.silent is True

    # Cleanup — don't leak pending tasks.
    await bus.cancel_all_async()


async def test_notify_recipient_skipped_on_silent() -> None:
    bus = InterAgentBus()
    stack = _make_stack()
    bus.register("target", stack)

    task = AsyncInterAgentTask(
        task_id="t1",
        sender="sender",
        recipient="target",
        message="hi",
        silent=True,
    )
    await bus._notify_recipient(task)

    stack.bot.notification_service.notify.assert_not_called()
    stack.bot.notification_service.notify_all.assert_not_called()


async def test_notify_recipient_skipped_on_reply_to_set() -> None:
    bus = InterAgentBus()
    stack = _make_stack()
    bus.register("target", stack)

    task = AsyncInterAgentTask(
        task_id="t2",
        sender="sender",
        recipient="target",
        message="hi",
        reply_to="some-agent",
    )
    await bus._notify_recipient(task)

    stack.bot.notification_service.notify.assert_not_called()
    stack.bot.notification_service.notify_all.assert_not_called()


async def test_notify_recipient_fires_normally_when_both_unset() -> None:
    bus = InterAgentBus()
    stack = _make_stack()
    bus.register("target", stack)

    task = AsyncInterAgentTask(
        task_id="t3",
        sender="sender",
        recipient="target",
        message="hi",
    )
    await bus._notify_recipient(task)

    stack.bot.notification_service.notify.assert_awaited_once()


async def test_deliver_async_result_uses_reply_to_when_set() -> None:
    bus = InterAgentBus()
    delivered: list[AsyncInterAgentResult] = []
    handler = AsyncMock(side_effect=delivered.append)
    bus.set_async_result_handler("reply-target", handler)

    # sender="unknown" (SSH scenario) — would miss handler without reply_to.
    result = AsyncInterAgentResult(
        task_id="t4",
        sender="unknown",
        recipient="target",
        message_preview="hi",
        result_text="done",
        reply_to="reply-target",
    )
    await bus._deliver_async_result(result)

    assert len(delivered) == 1
    assert delivered[0].task_id == "t4"


async def test_deliver_async_result_falls_back_to_sender_when_reply_to_empty() -> None:
    bus = InterAgentBus()
    delivered: list[AsyncInterAgentResult] = []
    handler = AsyncMock(side_effect=delivered.append)
    bus.set_async_result_handler("original-sender", handler)

    result = AsyncInterAgentResult(
        task_id="t5",
        sender="original-sender",
        recipient="target",
        message_preview="hi",
        result_text="done",
    )
    await bus._deliver_async_result(result)

    assert len(delivered) == 1
    assert delivered[0].task_id == "t5"


async def test_deliver_async_result_no_handler_logs_warning_and_does_not_raise() -> None:
    """reply_to not registered AND sender not registered => dropped cleanly."""
    bus = InterAgentBus()
    result = AsyncInterAgentResult(
        task_id="t6",
        sender="unknown",
        recipient="target",
        message_preview="hi",
        result_text="done",
        reply_to="also-unknown",
    )
    # Must not raise even though no handler exists for either name.
    await bus._deliver_async_result(result)


async def test_end_to_end_reply_to_routes_to_configured_handler() -> None:
    """Full send_async flow with reply_to delivers to the reply_to handler."""
    bus = InterAgentBus()
    bus.register("target", _make_stack("done-text"))
    bus.register("reply-target", _make_stack())

    delivered: list[AsyncInterAgentResult] = []
    bus.set_async_result_handler("reply-target", AsyncMock(side_effect=delivered.append))

    task_id = bus.send_async(
        "unknown", "target", "Hi", opts=AsyncSendOptions(reply_to="reply-target")
    )
    assert task_id is not None
    # Allow the async task to finish.
    await asyncio.sleep(0.1)

    assert len(delivered) == 1
    assert delivered[0].reply_to == "reply-target"
    assert delivered[0].result_text == "done-text"


async def test_async_send_options_defaults_are_backward_compatible() -> None:
    opts = AsyncSendOptions()
    assert opts.reply_to == ""
    assert opts.silent is False


def test_routing_field_defaults_and_result_serialization() -> None:
    opts = AsyncSendOptions()
    task = AsyncInterAgentTask(task_id="task", sender="main", recipient="sub", message="work")
    result = _make_result(task, 0)
    for item in (opts, task, result):
        assert item.result_origin == ""
        assert item.broadcast_ref == ""

    task.result_origin = "cron-broadcast"
    task.broadcast_ref = "cron-job-1"
    result = _make_result(task, 0)
    restored = AsyncInterAgentResult(**json.loads(json.dumps(asdict(result))))
    assert restored == result
    assert (restored.result_origin, restored.broadcast_ref) == ("cron-broadcast", "cron-job-1")
