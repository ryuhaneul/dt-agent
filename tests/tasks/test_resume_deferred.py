"""Resume requests wait for the previous run's result delivery to finish."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.cli.types import AgentRequest, AgentResponse
from ductor_bot.tasks.hub import INTERRUPTED_BY_RESTART, TaskHub
from ductor_bot.tasks.models import TaskSubmit
from ductor_bot.tasks.registry import TaskRegistry


@pytest.fixture
async def state(tmp_path):
    registry = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
    current = SimpleNamespace(
        first_started=asyncio.Event(),
        finish_first=asyncio.Event(),
        delivering=asyncio.Event(),
        release_delivery=asyncio.Event(),
        next_started=asyncio.Event(),
        finish_next=asyncio.Event(),
        calls=0,
        active=0,
        maximum=0,
        outcome="done",
        on_delivery=None,
    )

    async def execute(_request):
        current.calls += 1
        first = current.calls == 1
        current.active += 1
        current.maximum = max(current.maximum, current.active)
        try:
            (current.first_started if first else current.next_started).set()
            await (current.finish_first if first else current.finish_next).wait()
            if first and current.outcome == "failed":
                raise RuntimeError("Synthetic execution failure")
            return AgentResponse(result="Synthetic result", session_id="task-session")
        finally:
            current.active -= 1

    async def deliver(result):
        if not current.delivering.is_set():
            if current.on_delivery is not None:
                current.on_delivery(result)
            current.delivering.set()
            await current.release_delivery.wait()

    cli = MagicMock()
    cli.execute = AsyncMock(side_effect=execute)
    cli.resolve_provider.return_value = ("claude", "opus")
    hub = TaskHub(
        registry,
        MagicMock(workspace=tmp_path),
        cli_service=cli,
        config=SimpleNamespace(enabled=True, max_parallel=5, timeout_seconds=60),
    )
    hub._prepare_request = AsyncMock(return_value=AgentRequest(prompt="Synthetic", chat_id=42))
    hub.set_result_handler("main", deliver)
    current.hub, current.registry, current.cli = hub, registry, cli
    current.task_id = hub.submit(
        TaskSubmit(
            42,
            "Synthetic work",
            1,
            None,
            "main",
            provider_override="claude",
            model_override="opus",
        )
    )
    current.first = hub._in_flight[current.task_id]
    registry.update_status(current.task_id, "running", session_id="prior-session")
    await asyncio.wait_for(current.first_started.wait(), 10)
    try:
        yield current
    finally:
        current.finish_first.set()
        current.release_delivery.set()
        current.finish_next.set()
        await hub.shutdown()


async def finish_delivery(state):
    state.release_delivery.set()
    await asyncio.wait_for(state.next_started.wait(), 10)
    new = state.hub._in_flight[state.task_id]
    assert new is not state.first
    assert not new.asyncio_task.done()
    assert state.calls == 2
    assert state.maximum == 1
    state.finish_next.set()
    await asyncio.wait_for(new.asyncio_task, 10)
    assert state.task_id not in state.hub._in_flight
    assert state.registry.get(state.task_id).status == "done"


async def test_handler_resume_waits_for_previous_run_and_keeps_ownership(state):
    accepted = []

    def resume(_result):
        accepted.append(state.hub.resume(state.task_id, "Follow up"))

    state.on_delivery = resume
    state.finish_first.set()
    await asyncio.wait_for(state.delivering.wait(), 10)
    assert accepted == [state.task_id]
    assert state.hub._in_flight[state.task_id] is state.first
    assert state.calls == 1
    assert state.registry.get(state.task_id).status == "running"
    await finish_delivery(state)


async def test_live_execution_still_rejects_resume(state):
    state.registry.update_status(state.task_id, "waiting")
    with pytest.raises(ValueError, match="is already running"):
        state.hub.resume(state.task_id, "Too early")
    assert state.calls == 1
    assert not state.first.status_final


async def test_second_pending_resume_is_rejected(state):
    state.finish_first.set()
    await asyncio.wait_for(state.delivering.wait(), 10)
    assert state.hub.resume(state.task_id, "First follow up") == state.task_id
    with pytest.raises(ValueError, match="already has a pending resume"):
        state.hub.resume(state.task_id, "Second follow up")
    assert state.calls == 1
    await finish_delivery(state)


@pytest.mark.parametrize("all_tasks", [False, True])
async def test_cancel_discards_pending_resume(state, *, all_tasks):
    state.finish_first.set()
    await asyncio.wait_for(state.delivering.wait(), 10)
    state.hub.resume(state.task_id, "Follow up")
    if all_tasks:
        assert await state.hub.cancel_all(42) == 1
    else:
        assert await state.hub.cancel(state.task_id)
    assert state.calls == 1
    assert state.registry.get(state.task_id).status == "cancelled"
    assert state.registry.get(state.task_id).completed_at > 0
    state.release_delivery.set()
    await asyncio.gather(*state.hub._pending_deliveries)
    assert state.calls == 1
    assert state.task_id not in state.hub._in_flight


@pytest.mark.parametrize("stop", ["shutdown", "agent"])
async def test_shutdown_records_pending_resume_as_interrupted(state, stop):
    state.finish_first.set()
    await asyncio.wait_for(state.delivering.wait(), 10)
    state.hub.resume(state.task_id, "Follow up")
    if stop == "shutdown":
        state.release_delivery.set()
        await state.hub.shutdown()
    else:
        state.hub.begin_shutdown("main")
        state.release_delivery.set()
        await asyncio.wait_for(state.first.asyncio_task, 10)
    assert state.calls == 1
    entry = state.registry.get(state.task_id)
    assert (entry.status, entry.error, entry.notify_pending) == (
        "failed",
        INTERRUPTED_BY_RESTART,
        True,
    )
    reopened = TaskRegistry(state.registry._path, state.registry._tasks_dir)
    entry = reopened.get(state.task_id)
    assert (entry.status, entry.error, entry.notify_pending) == (
        "failed",
        INTERRUPTED_BY_RESTART,
        True,
    )


@pytest.mark.parametrize("outcome", ["cancelled", "failed"])
async def test_resume_during_unsuccessful_result_delivery(state, outcome):
    state.outcome = outcome
    cancellation = None
    if outcome == "cancelled":
        cancellation = asyncio.create_task(state.hub.cancel(state.task_id))
    else:
        state.finish_first.set()
    await asyncio.wait_for(state.delivering.wait(), 10)
    assert state.registry.get(state.task_id).status == outcome
    assert state.hub.resume(state.task_id, "Try again") == state.task_id
    assert state.calls == 1
    await finish_delivery(state)
    if cancellation is not None:
        assert await asyncio.wait_for(cancellation, 10)


async def done_before_callback(state):
    state.on_delivery = lambda _result: state.hub.resume(state.task_id, "Follow up")
    state.finish_first.set()
    await asyncio.wait_for(state.delivering.wait(), 10)
    state.release_delivery.set()
    # Yield until completion without awaiting and draining the done callback.
    while not state.first.asyncio_task.done():  # noqa: ASYNC110
        await asyncio.sleep(0)
    assert state.hub._in_flight[state.task_id] is state.first
    assert state.task_id in state.hub._pending_resumes


async def test_cancel_discards_resume_before_done_callback(state):
    await done_before_callback(state)
    assert not await state.hub.cancel(state.task_id)
    assert state.task_id not in state.hub._pending_resumes
    for _ in range(3):
        await asyncio.sleep(0)
    assert state.calls == 1
    assert state.registry.get(state.task_id).status == "cancelled"
    assert state.registry.get(state.task_id).completed_at > 0


async def test_cancel_all_discards_resume_before_done_callback(state):
    await done_before_callback(state)
    assert await state.hub.cancel_all(42) == 0
    assert state.task_id not in state.hub._pending_resumes
    for _ in range(3):
        await asyncio.sleep(0)
    assert state.calls == 1
    assert state.registry.get(state.task_id).status == "cancelled"
    assert state.registry.get(state.task_id).completed_at > 0


async def test_shutdown_records_resume_before_done_callback(state):
    await done_before_callback(state)
    await state.hub.shutdown()
    await asyncio.sleep(0)
    assert not state.hub._pending_resumes
    assert not state.hub._in_flight
    assert state.calls == 1
    entry = state.registry.get(state.task_id)
    assert (entry.status, entry.error, entry.notify_pending) == (
        "failed",
        INTERRUPTED_BY_RESTART,
        True,
    )
