"""Private answers use the shared conversation lock without publishing replies."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from ductor_bot.bus.bus import MessageBus
from ductor_bot.cli.slot_rotation import build_fallback_prefix, build_unparsable_prefix
from ductor_bot.cli.types import AgentResponse
from ductor_bot.i18n import init
from ductor_bot.orchestrator import injection
from ductor_bot.orchestrator.injection import answer_task_question
from tests.multiagent.test_delegation_context import agents, seed_topic  # noqa: F401


@pytest.fixture
def wired(agents):  # noqa: F811 - imported pytest fixture
    bus, main, worker = agents
    messages = MessageBus()
    adapter = AsyncMock()
    adapter.transport_name = "tg"
    messages.register_transport(adapter)
    for orch in (main, worker):
        orch.wire_observers_to_bus(messages)
    return bus, main, worker, messages, adapter


async def ask(orch, *, origin=None, alive=None, deadline=None, chat_id=-777, transport="tg"):
    return await answer_task_question(
        orch,
        "sample-task",
        "Sample task",
        "private-question-sentinel",
        chat_id,
        42,
        transport,
        origin if origin is not None else lambda: "source-42",
        alive if alive is not None else lambda: True,
        deadline if deadline is not None else asyncio.get_running_loop().time() + 5,
    )


@pytest.mark.parametrize("agent_index", [1, 2])
async def test_answer_is_private_and_uses_active_session(wired, agent_index, caplog):
    bus, _, _, messages, adapter = wired
    orch = wired[agent_index]
    key, _ = await seed_topic(orch)
    orch._cli_service.execute.return_value = AgentResponse(
        result="private-answer-sentinel", session_id="after-answer"
    )
    caplog.set_level("INFO", logger=injection.__name__)

    assert await ask(orch) == ("private-answer-sentinel", "")
    request = orch._cli_service.execute.call_args.args[0]
    assert request.resume_session == "source-42"
    assert request.provider_override == "claude"
    assert request.process_label == "task-question:sample-task"
    assert request.chat_id == key.chat_id
    assert request.topic_id == key.topic_id
    assert request.transport == "tg"
    for fragment in (
        "Background task 'Sample task' (sample-task)",
        "private-question-sentinel",
        "which you started from this conversation",
        "Answer from this conversation's context.",
        "Your reply goes only to the task",
        "it is not posted to the user chat",
        "do not create, resume or cancel tasks",
        "do not message other agents",
        "do not commit, push, deploy or edit files",
        "reply with a line containing only NEEDS_USER",
    ):
        assert fragment in request.prompt
    assert (await orch._sessions.get_active(key)).session_id == "after-answer"
    assert not messages.lock_pool.get(key.lock_key).locked()
    assert orch._named_sessions.list_active(12345) == []
    adapter.deliver.assert_not_awaited()
    adapter.deliver_broadcast.assert_not_awaited()
    for stack in bus._agents.values():
        stack.bot.notification_service.notify.assert_not_awaited()
        stack.bot.notification_service.notify_all.assert_not_awaited()
    records = [r.getMessage() for r in caplog.records if r.name == injection.__name__]
    assert len(records) == 1
    assert re.fullmatch(
        r"Task question task=sample-task result=answered reason= waited_ms=\d+ elapsed_ms=\d+",
        records[0],
    )
    assert "private-question-sentinel" not in caplog.text
    assert "private-answer-sentinel" not in caplog.text


@pytest.mark.parametrize("case", ["pool", "chat"])
async def test_no_session_does_not_execute(wired, case):
    _, main, _, _, _ = wired
    await seed_topic(main)
    if case == "pool":
        main._lock_pool = None
    assert await ask(main, chat_id=0 if case == "chat" else -777) == (None, "no_session")
    main._cli_service.execute.assert_not_awaited()


@pytest.mark.parametrize("limited_by", ["lock", "deadline"])
async def test_lock_timeout_preserves_other_owner(wired, monkeypatch, limited_by):
    _, main, _, messages, _ = wired
    key, _ = await seed_topic(main)
    lock = messages.lock_pool.get(key.lock_key)
    monkeypatch.setattr(
        injection, "_ASYNC_QUESTION_LOCK_WAIT", 0.02 if limited_by == "lock" else 60
    )
    await lock.acquire()
    try:
        deadline = asyncio.get_running_loop().time() + (1 if limited_by == "lock" else 0.02)
        assert await ask(main, deadline=deadline) == (None, "busy")
        assert lock.locked()
        main._cli_service.execute.assert_not_awaited()
    finally:
        lock.release()


@pytest.mark.parametrize("disconnect_while_waiting", [False, True])
async def test_disconnected_client_never_executes_and_releases_lock(
    wired, disconnect_while_waiting
):
    _, main, _, messages, _ = wired
    key, _ = await seed_topic(main)
    lock = messages.lock_pool.get(key.lock_key)
    alive = True
    if disconnect_while_waiting:
        await lock.acquire()
    pending = asyncio.create_task(ask(main, alive=lambda: alive))
    if disconnect_while_waiting:
        await asyncio.sleep(0.01)
        assert not pending.done()
    alive = False
    if disconnect_while_waiting:
        lock.release()
    assert await asyncio.wait_for(pending, 1) == (None, "client_gone")
    assert not lock.locked()
    main._cli_service.execute.assert_not_awaited()


@pytest.mark.parametrize("origin", ["", "other-session"])
async def test_origin_must_match_active_session(wired, origin):
    _, main, _, messages, _ = wired
    key, _ = await seed_topic(main)
    assert await ask(main, origin=lambda: origin) == (None, "session_mismatch")
    main._cli_service.execute.assert_not_awaited()
    assert not messages.lock_pool.get(key.lock_key).locked()


@pytest.mark.parametrize("case", ["missing", "empty"])
async def test_missing_active_session_never_executes(wired, case):
    _, main, _, _, _ = wired
    key, _ = await seed_topic(main)
    if case == "missing":
        main.paths.sessions_path.unlink()
    else:
        await main._sessions.reset_provider_session(key, "claude", "sonnet")
    assert await ask(main) == (None, "session_mismatch")
    main._cli_service.execute.assert_not_awaited()


async def test_provider_switch_and_return(wired):
    _, main, _, _, _ = wired
    key, _ = await seed_topic(main)
    active, _ = await main._sessions.resolve_session(key, provider="codex", model="gpt-5.5")
    active.session_id = "different-provider-session"
    await main._sessions.update_session(active)
    assert await ask(main) == (None, "session_mismatch")
    main._cli_service.execute.assert_not_awaited()
    await main._sessions.resolve_session(key, provider="claude", model="sonnet")
    assert await ask(main) == ("main answer", "")
    assert main._cli_service.execute.call_args.args[0].resume_session == "source-42"


async def test_origin_is_resolved_after_acquiring_lock(wired):
    _, main, _, messages, _ = wired
    key, _ = await seed_topic(main)
    lock = messages.lock_pool.get(key.lock_key)
    origin = ""
    reads = []

    def resolve():
        assert lock.locked()
        reads.append(origin)
        return origin

    await lock.acquire()
    pending = asyncio.create_task(ask(main, origin=resolve))
    await asyncio.sleep(0.01)
    assert reads == []
    origin = "source-42"
    lock.release()
    assert await asyncio.wait_for(pending, 1) == ("main answer", "")
    assert reads == ["source-42"]
    assert not lock.locked()


@pytest.mark.parametrize("phase", ["waiting", "reading", "executing"])
async def test_cancellation_releases_only_our_lock(wired, phase):
    _, main, _, messages, _ = wired
    key, _ = await seed_topic(main)
    lock = messages.lock_pool.get(key.lock_key)
    started = asyncio.Event()

    async def block(_arg):
        started.set()
        await asyncio.Event().wait()

    if phase == "waiting":
        await lock.acquire()
    elif phase == "reading":
        main._sessions.get_active = block
    else:
        main._cli_service.execute.side_effect = block
    pending = asyncio.create_task(ask(main))
    if phase == "waiting":
        await asyncio.sleep(0.01)
    else:
        await asyncio.wait_for(started.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert lock.locked() == (phase == "waiting")
    if phase != "executing":
        main._cli_service.execute.assert_not_awaited()
    if phase == "waiting":
        lock.release()


async def test_execution_deadline_cancels_and_releases(wired):
    _, main, _, messages, _ = wired
    key, _ = await seed_topic(main)
    cancelled = asyncio.Event()

    async def execute(_request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    main._cli_service.execute.side_effect = execute
    assert await ask(main, deadline=asyncio.get_running_loop().time() + 0.03) == (None, "deadline")
    assert cancelled.is_set()
    assert not messages.lock_pool.get(key.lock_key).locked()


async def test_expired_deadline_never_executes(wired):
    _, main, _, messages, _ = wired
    key, _ = await seed_topic(main)
    assert await ask(main, deadline=asyncio.get_running_loop().time() - 1) == (None, "deadline")
    main._cli_service.execute.assert_not_awaited()
    assert not messages.lock_pool.get(key.lock_key).locked()


@pytest.mark.parametrize("phase", ["reading", "executing", "response"])
async def test_errors_fall_back_without_content_in_log(wired, phase, caplog):
    _, main, _, messages, _ = wired
    key, _ = await seed_topic(main)
    caplog.set_level("INFO", logger=injection.__name__)
    if phase == "reading":
        main._sessions.get_active = AsyncMock(side_effect=OSError("private-error-sentinel"))
    elif phase == "executing":
        main._cli_service.execute.side_effect = RuntimeError("private-error-sentinel")
    else:
        main._cli_service.execute.return_value = AgentResponse(
            result="private-error-sentinel", is_error=True
        )
    assert await ask(main) == (None, "error")
    assert not messages.lock_pool.get(key.lock_key).locked()
    assert "private-question-sentinel" not in caplog.text
    assert "private-error-sentinel" not in caplog.text
    assert re.search(
        r"Task question task=sample-task result=fallback reason=error waited_ms=\d+ elapsed_ms=\d+",
        caplog.text,
    )


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ("NEEDS_USER", "needs_user"),
        ("explanation\n needs_user \nmore", "needs_user"),
        ("prefix\nNeEdS_UsEr", "needs_user"),
        ("", "empty"),
        (" \n\t ", "empty"),
        ("Use NEEDS_USER as a literal value.", ""),
        ("  exact answer\n", ""),
    ],
)
async def test_answer_classification_preserves_success_text(wired, answer, reason):
    _, main, _, _, _ = wired
    await seed_topic(main)
    main._cli_service.execute.return_value = AgentResponse(result=answer)
    assert await ask(main) == (None if reason else answer, reason)


@pytest.mark.parametrize("language", ["ko", "en"])
@pytest.mark.parametrize("prefix_kind", ["fallback", "unparsable"])
async def test_needs_user_after_real_localized_provider_prefix(wired, language, prefix_kind):
    _, main, _, _, _ = wired
    await seed_topic(main)
    init(language)
    try:
        utc = ZoneInfo("UTC")
        if prefix_kind == "fallback":
            prefix = build_fallback_prefix(
                "claude", "claude-alt", datetime(2026, 1, 1, tzinfo=utc), utc
            )
        else:
            prefix = build_unparsable_prefix("synthetic stderr")
    finally:
        init("en")
    assert prefix.startswith("[Claude:")
    main._cli_service.execute.return_value = AgentResponse(result=prefix + " needs_user \n")
    assert await ask(main) == (None, "needs_user")


async def test_orchestrator_method_delegates(wired):
    _, main, _, _, _ = wired
    await seed_topic(main)
    assert await main.answer_task_question(
        "sample-task",
        "Sample task",
        "Question",
        -777,
        42,
        "tg",
        lambda: "source-42",
        lambda: True,
        asyncio.get_running_loop().time() + 5,
    ) == ("main answer", "")
