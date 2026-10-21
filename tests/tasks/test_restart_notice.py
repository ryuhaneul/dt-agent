"""Shutdown-interrupted tasks persist failure notices for later parent delivery."""

# ruff: noqa: F811  (the registry fixture is imported from the module that defines it)

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.multiagent.supervisor import AgentSupervisor
from ductor_bot.tasks.hub import INTERRUPTED_BY_RESTART, TaskHub
from ductor_bot.tasks.models import TaskEntry, TaskResult
from ductor_bot.tasks.registry import TaskRegistry
from tests.tasks.test_hub import (  # noqa: F401
    _FakeProcess,
    _make_cli_service,
    _make_config,
    _real_cli_service,
    _submit,
    _wait_for,
    registry,
)


def _entry(task_id: str = "t1", **fields: object) -> dict[str, object]:
    base: dict[str, object] = {
        "task_id": task_id,
        "chat_id": 42,
        "parent_agent": "main",
        "name": "Job",
        "prompt_preview": "p",
        "provider": "claude",
        "model": "opus",
        "status": "failed",
    }
    base.update(fields)
    return base


def _registry_with(tmp_path: Path, entries: list[TaskEntry]) -> TaskRegistry:
    registry = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
    for entry in entries:
        (tmp_path / "tasks" / entry.task_id).mkdir(parents=True, exist_ok=True)
        registry._entries[entry.task_id] = entry
    return registry


# -- the record --------------------------------------------------------------------------------------------


def test_old_record_without_the_fields_loads_as_not_pending() -> None:
    entry = TaskEntry.from_dict(_entry())

    assert entry.notify_pending is False
    assert entry.notify_attempts == 0


def test_fields_round_trip_and_bad_values_count_as_not_pending() -> None:
    entry = TaskEntry.from_dict(_entry(notify_pending=True, notify_attempts=2, thread_id=9))
    again = TaskEntry.from_dict(entry.to_dict())
    assert (again.notify_pending, again.notify_attempts, again.thread_id) == (True, 2, 9)

    broken = TaskEntry.from_dict(_entry(notify_pending="yes", notify_attempts="many"))
    assert (broken.notify_pending, broken.notify_attempts) == (False, 0)
    assert TaskEntry.from_dict(_entry(notify_attempts=True)).notify_attempts == 0
    assert TaskEntry.from_dict(_entry(notify_attempts=-3)).notify_attempts == 0


def test_task_running_at_an_unclean_stop_is_pending_after_the_next_load(tmp_path: Path) -> None:
    first = _registry_with(tmp_path, [TaskEntry.from_dict(_entry(status="running"))])
    first._persist()

    loaded = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")

    entry = loaded.get("t1")
    assert entry is not None
    assert (entry.status, entry.notify_pending) == ("failed", True)
    assert entry.error == "Bot restarted while task was running"


# -- keeping them ------------------------------------------------------------------------------------------


def test_retention_does_not_remove_a_task_whose_parent_is_not_told(tmp_path: Path) -> None:
    old = time.time() - 30 * 24 * 3600
    registry = _registry_with(
        tmp_path,
        [
            TaskEntry.from_dict(_entry("pending", notify_pending=True, completed_at=old)),
            TaskEntry.from_dict(_entry("plain", completed_at=old)),
        ],
    )

    removed = registry.cleanup_finished_retention(max_age_hours=24, keep_last=0)

    assert removed == 1
    assert registry.get("pending") is not None
    assert registry.get("plain") is None


def test_retention_count_limit_does_not_remove_it_either(tmp_path: Path) -> None:
    now = time.time()
    registry = _registry_with(
        tmp_path,
        [
            TaskEntry.from_dict(_entry("pending", notify_pending=True, completed_at=now - 500)),
            TaskEntry.from_dict(_entry("a", completed_at=now - 300)),
            TaskEntry.from_dict(_entry("b", completed_at=now - 200)),
        ],
    )

    registry.cleanup_finished_retention(max_age_hours=0, keep_last=1)

    assert registry.get("pending") is not None
    assert registry.get("b") is not None
    assert registry.get("a") is None


def test_explicit_delete_removes_a_pending_task(tmp_path: Path) -> None:
    registry = _registry_with(
        tmp_path, [TaskEntry.from_dict(_entry("pending", notify_pending=True))]
    )

    assert registry.delete("pending") is True
    assert registry.get("pending") is None


async def test_resume_ends_the_wait_to_be_told(registry: TaskRegistry, tmp_path: Path) -> None:
    hub = TaskHub(
        registry,
        MagicMock(workspace=tmp_path),
        cli_service=_make_cli_service(),
        config=_make_config(),
    )
    task_id = hub.submit(_submit())
    await _wait_for(lambda: (registry.get(task_id) or MagicMock(status="")).status == "done")
    # The status is stored before the result delivery finishes; resume only after the run ends.
    await _wait_for(lambda: task_id not in hub._in_flight)
    registry.update_status(
        task_id, "failed", notify_pending=True, notify_attempts=2, error=INTERRUPTED_BY_RESTART
    )

    hub.resume(task_id, "go on")

    entry = registry.get(task_id)
    assert entry is not None
    assert (entry.notify_pending, entry.notify_attempts) == (False, 0)
    await hub.shutdown()


# -- a shutdown cuts a task off ------------------------------------------------------------------------------


def _hub(
    registry: TaskRegistry, tmp_path: Path, service: object
) -> tuple[TaskHub, list[TaskResult]]:
    hub = TaskHub(
        registry,
        MagicMock(workspace=tmp_path),
        cli_service=service,  # type: ignore[arg-type]
        config=_make_config(timeout_seconds=3600.0),
    )
    delivered: list[TaskResult] = []
    hub.set_result_handler("main", AsyncMock(side_effect=delivered.append))
    return hub, delivered


def _count_delivery_attempts(hub: TaskHub) -> list[str]:
    attempts: list[str] = []
    start, deliver = hub._start_final_delivery, hub._deliver

    def counting_start(result: TaskResult):
        attempts.append("start")
        return start(result)

    async def counting_deliver(result: TaskResult) -> None:
        attempts.append("deliver")
        await deliver(result)

    hub._start_final_delivery = counting_start  # type: ignore[method-assign]
    hub._deliver = counting_deliver  # type: ignore[method-assign]
    return attempts


async def test_cli_killed_by_the_shutdown_is_failed_pending_and_not_delivered(
    registry: TaskRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    service = _real_cli_service(process, monkeypatch)
    hub, delivered = _hub(registry, tmp_path, service)
    attempts = _count_delivery_attempts(hub)
    task_id = hub.submit(_submit())
    procs = service._process_registry
    try:
        await _wait_for(lambda: procs.has_active(42))

        hub.begin_shutdown("main")  # what the pre-shutdown hook does
        await procs.kill_all_active()  # then the CLI processes are killed
        await _wait_for(
            lambda: (registry.get(task_id) or MagicMock(status="running")).status != "running"
        )

        entry = registry.get(task_id)
        assert entry is not None
        assert (entry.status, entry.error, entry.notify_pending) == (
            "failed",
            INTERRUPTED_BY_RESTART,
            True,
        )
        assert attempts == []  # no delivery attempt at all
        assert delivered == []
    finally:
        process.die()
        await hub.shutdown()


async def test_asyncio_cancel_by_the_shutdown_is_failed_pending_and_not_delivered(
    registry: TaskRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    service = _real_cli_service(process, monkeypatch)
    hub, delivered = _hub(registry, tmp_path, service)
    attempts = _count_delivery_attempts(hub)
    task_id = hub.submit(_submit())
    procs = service._process_registry
    await _wait_for(lambda: procs.has_active(42))

    await hub.shutdown()  # cancels the task itself, no kill beforehand

    entry = registry.get(task_id)
    assert entry is not None
    assert (entry.status, entry.error, entry.notify_pending) == (
        "failed",
        INTERRUPTED_BY_RESTART,
        True,
    )
    assert attempts == []
    assert delivered == []


async def test_a_response_with_a_session_keeps_it_when_the_shutdown_cut_the_task_off(
    registry: TaskRegistry, tmp_path: Path
) -> None:
    service = _make_cli_service(session_id="sess-9", num_turns=2)
    service.execute.return_value.is_error = True
    service.execute.return_value.returncode = 143
    service.execute.return_value.stale_killed = False
    hub, delivered = _hub(registry, tmp_path, service)
    hub.begin_shutdown("main")

    task_id = hub.submit(_submit())
    await _wait_for(
        lambda: (registry.get(task_id) or MagicMock(status="running")).status != "running"
    )

    entry = registry.get(task_id)
    assert entry is not None
    assert (entry.status, entry.notify_pending) == ("failed", True)
    assert entry.session_id == "sess-9"  # a later notice can offer to resume
    assert entry.num_turns == 2
    assert delivered == []
    await hub.shutdown()


async def test_thread_of_the_task_is_kept(registry: TaskRegistry, tmp_path: Path) -> None:
    service = _make_cli_service()
    service.execute.return_value.is_error = True
    service.execute.return_value.returncode = 143
    service.execute.return_value.stale_killed = False
    hub, _delivered = _hub(registry, tmp_path, service)
    hub.begin_shutdown("main")
    task_id = hub.submit(replace(_submit(), thread_id=17))
    await _wait_for(
        lambda: (registry.get(task_id) or MagicMock(status="running")).status != "running"
    )

    entry = registry.get(task_id)
    assert entry is not None
    assert entry.thread_id == 17
    await hub.shutdown()


async def test_a_task_whose_result_is_being_delivered_is_kept(
    registry: TaskRegistry, tmp_path: Path
) -> None:
    hub, _delivered = _hub(registry, tmp_path, _make_cli_service(result="all done"))
    started = []

    async def slow_handler(result: TaskResult) -> None:
        started.append(result.status)
        await asyncio.sleep(0.3)

    hub.set_result_handler("main", slow_handler)
    task_id = hub.submit(_submit())
    await _wait_for(lambda: bool(started))  # delivery of the finished result has begun

    hub.begin_shutdown("main")
    await hub.shutdown()

    entry = registry.get(task_id)
    assert entry is not None
    assert (entry.status, entry.notify_pending) == ("done", False)
    assert started == ["done"]


async def test_user_cancel_stays_cancelled(
    registry: TaskRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    service = _real_cli_service(process, monkeypatch)
    hub, delivered = _hub(registry, tmp_path, service)
    task_id = hub.submit(_submit())
    procs = service._process_registry
    try:
        await _wait_for(lambda: procs.has_active(42))

        await hub.cancel(task_id)
        await _wait_for(lambda: bool(delivered))

        entry = registry.get(task_id)
        assert entry is not None
        assert (entry.status, entry.notify_pending) == ("cancelled", False)
        assert delivered[0].status == "cancelled"
    finally:
        process.die()
        await hub.shutdown()


async def test_another_agents_shutdown_does_not_touch_the_task(
    registry: TaskRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    service = _real_cli_service(process, monkeypatch)
    hub, delivered = _hub(registry, tmp_path, service)
    task_id = hub.submit(_submit())
    procs = service._process_registry
    try:
        await _wait_for(lambda: procs.has_active(42))

        hub.begin_shutdown("other-agent")
        await procs.kill_for_task(task_id)
        await _wait_for(lambda: bool(delivered))

        entry = registry.get(task_id)
        assert entry is not None
        assert (entry.status, entry.notify_pending) == ("cancelled", False)
    finally:
        process.die()
        await hub.shutdown()


async def test_starting_the_agent_again_clears_its_shutdown_mark(
    registry: TaskRegistry, tmp_path: Path
) -> None:
    hub, _delivered = _hub(registry, tmp_path, _make_cli_service())
    hub.begin_shutdown("main")
    assert hub._is_shutting_down("main")

    hub.set_result_handler("main", AsyncMock())  # the restarted agent is wired again

    assert not hub._is_shutting_down("main")
    await hub.shutdown()


# -- the supervisor connects the hub to the stop ---------------------------------------------------------


async def test_wiring_an_agent_marks_its_tasks_before_its_cli_is_killed(
    registry: TaskRegistry, tmp_path: Path
) -> None:
    hub, _delivered = _hub(registry, tmp_path, _make_cli_service())
    supervisor = AgentSupervisor.__new__(AgentSupervisor)
    supervisor._task_hub = hub
    hooks: list[object] = []
    orch = MagicMock()
    orch.register_pre_shutdown_hook = hooks.append
    stack = MagicMock()
    stack.name = "main"
    stack.bot.orchestrator = orch
    stack.config.allowed_user_ids = []

    supervisor._wire_task_hub(stack)
    assert len(hooks) == 1
    assert not hub._is_shutting_down("main")

    await hooks[0]()  # type: ignore[operator]  # the orchestrator runs it before the kill

    assert hub._is_shutting_down("main")
    assert not hub._is_shutting_down("other")
    await hub.shutdown()


# -- the notice after the next start -----------------------------------------------------------------------


class _Injector:
    """The parent agent's session: records the turns injected into it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def inject_prompt(self, prompt: str, chat_id: int, label: str, **kwargs: object) -> str:
        self.calls.append({"prompt": prompt, "chat_id": chat_id, "label": label, **kwargs})
        return "Your task was interrupted by the restart."


class _Transport:
    """A delivery path named like a messenger; it can be made to fail."""

    def __init__(self, name: str) -> None:
        self.transport_name = name
        self.delivered: list[tuple[int, int | None]] = []
        self.broadcasts = 0
        self.fail = False

    async def deliver(self, envelope: object) -> None:
        if self.fail:
            envelope.delivered = False  # type: ignore[attr-defined]
            return
        self.delivered.append((envelope.chat_id, envelope.topic_id))  # type: ignore[attr-defined]

    async def deliver_broadcast(self, envelope: object) -> None:
        self.broadcasts += 1


def _bus_with(names: tuple[str, ...]) -> tuple[object, _Injector, dict[str, _Transport]]:
    from ductor_bot.bus.bus import MessageBus

    bus = MessageBus()
    injector = _Injector()
    bus.set_injector(injector)  # type: ignore[arg-type]
    transports = {name: _Transport(name) for name in names}
    for transport in transports.values():
        bus.register_transport(transport)  # type: ignore[arg-type]
    return bus, injector, transports


def _bot(kind: str, bus: object) -> object:
    from ductor_bot.messenger.matrix.bot import MatrixBot
    from ductor_bot.messenger.slack.bot import SlackBot
    from ductor_bot.messenger.telegram.app import TelegramBot

    cls = {"tg": TelegramBot, "mx": MatrixBot, "sl": SlackBot}[kind]
    bot = object.__new__(cls)
    bot._bus = bus  # type: ignore[attr-defined]
    bot._config = MagicMock(allowed_user_ids=[42])  # type: ignore[attr-defined]
    return bot


def _pending_hub(tmp_path: Path, **fields: object) -> tuple[TaskHub, TaskRegistry]:
    entry = TaskEntry.from_dict(
        _entry(
            "t1",
            notify_pending=True,
            thread_id=5,
            session_id="sess-1",
            name="Nightly build",
            **fields,
        )
    )
    registry = _registry_with(tmp_path, [entry])
    hub = TaskHub(
        registry,
        MagicMock(workspace=tmp_path),
        cli_service=_make_cli_service(),
        config=_make_config(),
    )
    return hub, registry


@pytest.mark.parametrize("kind", ["tg", "mx", "sl"])
async def test_notice_goes_through_the_parents_transport_to_the_chat_and_topic_once(
    tmp_path: Path, kind: str
) -> None:
    bus, injector, transports = _bus_with(("tg", "mx", "sl"))
    bot = _bot(kind, bus)
    hub, registry = _pending_hub(tmp_path)

    told = await hub.report_interrupted("main", bot.notify_task_interrupted)  # type: ignore[attr-defined]

    assert told == 1
    assert len(injector.calls) == 1
    call = injector.calls[0]
    assert (call["transport"], call["chat_id"], call["topic_id"]) == (kind, 42, 5)
    assert "Nightly build" in str(call["prompt"])
    assert "interrupted by restart" in str(call["prompt"])
    assert "resume_task.py t1" in str(call["prompt"])  # a session exists: it can be continued
    assert transports[kind].delivered == [(42, 5)]
    for name, transport in transports.items():
        assert transport.broadcasts == 0
        if name != kind:
            assert transport.delivered == []
    entry = registry.get("t1")
    assert entry is not None
    assert entry.notify_pending is False


async def test_no_session_means_no_resume_advice(tmp_path: Path) -> None:
    bus, injector, _transports = _bus_with(("tg",))
    hub, registry = _pending_hub(tmp_path)
    registry.update_status("t1", "failed", session_id="")

    await hub.report_interrupted("main", _bot("tg", bus).notify_task_interrupted)  # type: ignore[attr-defined]

    assert "resume_task.py" not in str(injector.calls[0]["prompt"])


async def test_a_second_start_does_not_tell_again(tmp_path: Path) -> None:
    bus, injector, transports = _bus_with(("tg",))
    hub, _registry = _pending_hub(tmp_path)
    notify = _bot("tg", bus).notify_task_interrupted  # type: ignore[attr-defined]

    await hub.report_interrupted("main", notify)
    again = await hub.report_interrupted("main", notify)

    assert again == 0
    assert len(injector.calls) == 1
    assert transports["tg"].delivered == [(42, 5)]


async def test_only_the_tasks_of_that_agent_are_told(tmp_path: Path) -> None:
    bus, injector, _transports = _bus_with(("tg",))
    hub, registry = _pending_hub(tmp_path)
    other = TaskEntry.from_dict(_entry("t2", parent_agent="helper", notify_pending=True))
    (tmp_path / "tasks" / "t2").mkdir(parents=True)
    registry._entries["t2"] = other

    await hub.report_interrupted("main", _bot("tg", bus).notify_task_interrupted)  # type: ignore[attr-defined]

    assert len(injector.calls) == 1
    assert registry.get("t2").notify_pending is True  # type: ignore[union-attr]


async def test_multi_transport_agent_is_told_through_its_primary_transport(tmp_path: Path) -> None:
    from ductor_bot.messenger.multi import MultiBotAdapter

    bus, injector, transports = _bus_with(("tg", "mx"))
    matrix, telegram = _bot("mx", bus), _bot("tg", bus)
    multi = object.__new__(MultiBotAdapter)
    multi._primary = matrix  # type: ignore[attr-defined]
    multi._secondaries = [telegram]  # type: ignore[attr-defined]
    multi._all = [matrix, telegram]  # type: ignore[attr-defined]
    hub, _registry = _pending_hub(tmp_path)

    await hub.report_interrupted("main", multi.notify_task_interrupted)

    assert injector.calls[0]["transport"] == "mx"
    assert transports["mx"].delivered == [(42, 5)]
    assert transports["tg"].delivered == []
    assert transports["tg"].broadcasts == 0


async def test_broadcast_fallback_does_not_count_as_told(tmp_path: Path) -> None:
    # the parent's own transport is not registered: the bus broadcasts through another one
    bus, _injector, transports = _bus_with(("tg",))
    hub, registry = _pending_hub(tmp_path)
    notify = _bot("mx", bus).notify_task_interrupted  # type: ignore[attr-defined]

    told = await hub.report_interrupted("main", notify)

    assert told == 0
    assert transports["tg"].broadcasts == 1  # the bus did fall back ...
    entry = registry.get("t1")
    assert entry is not None
    assert (entry.notify_pending, entry.notify_attempts) == (True, 1)  # ... and it is kept


async def test_failed_tries_are_counted_and_the_third_one_gives_up(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bus, _injector, transports = _bus_with(("tg",))
    transports["tg"].fail = True
    hub, registry = _pending_hub(tmp_path)
    notify = _bot("tg", bus).notify_task_interrupted  # type: ignore[attr-defined]
    entry = registry.get("t1")
    assert entry is not None

    await hub.report_interrupted("main", notify)
    assert (entry.notify_pending, entry.notify_attempts) == (True, 1)
    await hub.report_interrupted("main", notify)
    assert (entry.notify_pending, entry.notify_attempts) == (True, 2)
    with caplog.at_level("WARNING"):
        await hub.report_interrupted("main", notify)

    assert (entry.notify_pending, entry.notify_attempts) == (False, 3)
    assert any("Giving up telling the parent" in r.message for r in caplog.records)


async def test_a_notifier_that_raises_counts_as_a_failed_try(tmp_path: Path) -> None:
    hub, registry = _pending_hub(tmp_path)

    async def broken(_result: object) -> bool:
        msg = "boom"
        raise RuntimeError(msg)

    assert await hub.report_interrupted("main", broken) == 0

    entry = registry.get("t1")
    assert entry is not None
    assert (entry.notify_pending, entry.notify_attempts) == (True, 1)


# -- the supervisor starts the notice after the result handler is wired ---------------------------------


def _supervisor_for(hub: TaskHub) -> AgentSupervisor:
    supervisor = AgentSupervisor.__new__(AgentSupervisor)
    supervisor._task_hub = hub
    supervisor._stopping = False
    supervisor._notice_tasks = {}
    return supervisor


def _stack(notify: object) -> MagicMock:
    stack = MagicMock()
    stack.name = "main"
    stack.bot.notify_task_interrupted = notify
    stack.config.allowed_user_ids = []
    return stack


async def test_start_notice_runs_once_after_the_handler_is_wired_without_holding_the_start(
    tmp_path: Path,
) -> None:
    hub, registry = _pending_hub(tmp_path)
    supervisor = _supervisor_for(hub)
    gate = asyncio.Event()
    seen: list[bool] = []

    async def notify(_result: TaskResult) -> bool:
        seen.append("main" in hub._result_handlers)  # the parent's handler was connected before
        await gate.wait()
        return True

    stack = _stack(notify)
    supervisor._wire_task_hub(stack)
    supervisor._report_interrupted_tasks(stack)  # returns at once, the notice is not awaited

    await _wait_for(lambda: bool(seen))
    entry = registry.get("t1")
    assert entry is not None
    assert entry.notify_pending is True  # still being told
    gate.set()
    await _wait_for(lambda: not entry.notify_pending)

    assert seen == [True]
    await _wait_for(lambda: supervisor._notice_tasks == {})
    await hub.shutdown()


async def test_no_notice_task_without_pending_tasks_or_while_stopping(tmp_path: Path) -> None:
    hub, registry = _pending_hub(tmp_path)
    supervisor = _supervisor_for(hub)
    stack = _stack(AsyncMock(return_value=True))

    supervisor._report_interrupted_tasks(stack)  # the result handler is not wired yet
    assert supervisor._notice_tasks == {}
    supervisor._wire_task_hub(stack)

    supervisor._stopping = True
    supervisor._report_interrupted_tasks(stack)
    assert supervisor._notice_tasks == {}

    supervisor._stopping = False
    registry.update_status("t1", "failed", notify_pending=False)
    supervisor._report_interrupted_tasks(stack)
    assert supervisor._notice_tasks == {}
    await hub.shutdown()


async def test_stopping_the_agent_cancels_its_unfinished_notice(tmp_path: Path) -> None:
    hub, registry = _pending_hub(tmp_path)
    supervisor = _supervisor_for(hub)
    started = asyncio.Event()

    async def notify(_result: TaskResult) -> bool:
        started.set()
        await asyncio.sleep(3600)
        return True

    stack = _stack(notify)
    supervisor._wire_task_hub(stack)
    supervisor._report_interrupted_tasks(stack)
    await asyncio.wait_for(started.wait(), 5)
    task = supervisor._notice_tasks["main"]

    supervisor._cancel_notice("main")
    await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    entry = registry.get("t1")
    assert entry is not None
    assert (entry.notify_pending, entry.notify_attempts) == (True, 0)  # told at the next start
    await hub.shutdown()
