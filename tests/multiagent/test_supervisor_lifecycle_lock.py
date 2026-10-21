"""Sub-agent lifecycle changes are serialized by one lock and never run an old stack again."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.config import AgentConfig
from ductor_bot.infra.restart import EXIT_RESTART
from ductor_bot.log_context import ctx_chat_id, ctx_operation, set_log_context
from ductor_bot.multiagent.models import SubAgentConfig
from ductor_bot.multiagent.supervisor import AgentSupervisor

SUB = "worker"
WAIT = 5.0  # generous bound for operations that must complete (not a drain time)


class _FakeStack:
    """A stack whose run() blocks until it is cancelled, or ends with a scripted exit code."""

    def __init__(self, world: _World, name: str, config: AgentConfig, serial: int) -> None:
        self.world = world
        self.name = name
        self.config = config
        self.serial = serial
        self.paths = SimpleNamespace(mainmemory_path=Path("unused"))
        self.is_main = False
        self.bot = MagicMock()
        self.bot.orchestrator = None
        self.runs = 0
        self.shutdowns = 0
        self.seen_context: dict[str, object] = {}
        self.started = asyncio.Event()

    async def run(self) -> int:
        self.runs += 1
        self.seen_context = {"operation": ctx_operation.get(None), "chat": ctx_chat_id.get(None)}
        self.started.set()
        if self.runs == 1 and self.world.exit_first_run:
            self.world.exit_first_run = False
            return EXIT_RESTART
        await asyncio.Event().wait()
        return 0

    async def shutdown(self, _budget: object = None) -> None:
        self.shutdowns += 1


class _World:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path
        self.config = AgentConfig(
            ductor_home=str(tmp_path), telegram_token="main-token", allowed_user_ids=[1]
        )
        self.supervisor = AgentSupervisor(self.config)
        self.supervisor._running = True
        self.created: list[_FakeStack] = []
        self.create_delay = 0.0
        self.create_gate: asyncio.Event | None = None
        self.create_entered = asyncio.Event()
        self.exit_first_run = False
        self.stack_class: type[_FakeStack] = _FakeStack
        self.honour_stop = True
        self.release: asyncio.Event | None = None

    def declare(self, token: str = "tok:1") -> None:
        (self.home / "agents.json").write_text(
            json.dumps([{"name": SUB, "telegram_token": token}]), encoding="utf-8"
        )

    async def create(self, name: str, config: AgentConfig, *, is_main: bool = False) -> _FakeStack:
        self.create_entered.set()
        if self.create_gate is not None:
            await self.create_gate.wait()
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        stack = self.stack_class(self, name, config, len(self.created) + 1)
        self.created.append(stack)
        return stack

    @property
    def alive(self) -> list[_FakeStack]:
        """Stacks that were created and not shut down."""
        return [s for s in self.created if s.shutdowns == 0]


@pytest.fixture
def world(tmp_path: Path):
    w = _World(tmp_path)
    with patch("ductor_bot.multiagent.supervisor.AgentStack.create", w.create):
        yield w


async def _finish(world: _World) -> None:
    await asyncio.wait_for(world.supervisor.stop_all(), WAIT)


# -- concurrent lifecycle changes ---------------------------------------------------------


async def test_file_change_and_manual_restart_at_the_same_time_leave_one_stack(
    world: _World,
) -> None:
    world.create_delay = 0.02  # a window in which an unserialized second start would overlap
    world.declare("tok:1")
    await world.supervisor._on_agents_changed()
    assert len(world.alive) == 1

    world.declare("tok:2")  # the token changed: the watcher restarts the agent ...
    await asyncio.wait_for(
        asyncio.gather(
            world.supervisor._on_agents_changed(),
            world.supervisor.restart_agent(SUB),  # ... and so does the command
        ),
        WAIT,
    )

    sup = world.supervisor
    assert len(world.alive) == 1  # one stack
    assert sup._stacks[SUB] is world.alive[0]
    live_tasks = [t for t in asyncio.all_tasks() if t.get_name() == f"agent:{SUB}" and not t.done()]
    assert live_tasks == [sup._tasks[SUB]]  # one polling task
    await _finish(world)


async def test_two_manual_restarts_at_the_same_time_leave_one_stack(world: _World) -> None:
    world.create_delay = 0.02
    world.declare()
    await world.supervisor._on_agents_changed()

    await asyncio.wait_for(
        asyncio.gather(world.supervisor.restart_agent(SUB), world.supervisor.restart_agent(SUB)),
        WAIT,
    )

    assert len(world.alive) == 1
    await _finish(world)


# -- stop_all while a restart is running -----------------------------------------------------


async def test_stop_all_waits_for_a_running_restart_and_nothing_survives(world: _World) -> None:
    world.declare()
    await world.supervisor._on_agents_changed()
    world.create_gate = asyncio.Event()
    world.create_entered.clear()

    restart = asyncio.create_task(world.supervisor.restart_agent(SUB))
    await asyncio.wait_for(
        world.create_entered.wait(), WAIT
    )  # the restart is between stop and start
    stop_all = asyncio.create_task(world.supervisor.stop_all())
    await asyncio.sleep(0.05)
    assert not stop_all.done()  # it waits for the restart instead of missing the new stack

    world.create_gate.set()
    await asyncio.wait_for(asyncio.gather(restart, stop_all), WAIT)  # no deadlock

    sup = world.supervisor
    assert world.alive == []
    assert SUB not in sup._stacks
    assert SUB not in sup._tasks


async def test_nothing_starts_after_stop_all(world: _World) -> None:
    world.declare()
    await world.supervisor._on_agents_changed()
    await _finish(world)
    created = len(world.created)

    message = await world.supervisor.start_agent_by_name(SUB)
    restarted = await world.supervisor.restart_agent(SUB)
    await world.supervisor._on_agents_changed()

    assert "not started" in message
    assert "not started" in restarted
    assert len(world.created) == created
    assert SUB not in world.supervisor._stacks


# -- starting a name that already runs ---------------------------------------------------------


async def test_starting_a_running_agent_is_refused(world: _World) -> None:
    world.declare()
    await world.supervisor._on_agents_changed()
    sup = world.supervisor

    message = await sup.start_agent_by_name(SUB)
    direct = await sup._start_sub_agent(SubAgentConfig(name=SUB, telegram_token="tok:1"))

    assert "already running" in message
    assert (direct.ok, direct.reason) == (False, "already running")
    assert len(world.created) == 1
    assert SUB not in sup._excluded  # a refused start is no failure of the agent
    await _finish(world)


async def test_failed_start_cleans_up_without_deadlock(world: _World) -> None:
    world.declare()
    bus = MagicMock()
    bus.drain_agent = AsyncMock(return_value=True)
    bus.register.side_effect = RuntimeError("register failed")
    world.supervisor._bus = bus

    message = await asyncio.wait_for(world.supervisor.start_agent_by_name(SUB), WAIT)

    assert "not started" in message
    assert world.alive == []  # the half-started stack was shut down
    assert SUB not in world.supervisor._stacks
    assert SUB in world.supervisor._excluded
    assert not world.supervisor._agents_lock.locked()


async def test_a_failing_shutdown_is_logged_and_does_not_stop_the_stop(
    world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    world.declare()
    await world.supervisor._on_agents_changed()
    stack = world.supervisor._stacks[SUB]
    stack.shutdown = AsyncMock(side_effect=RuntimeError("secret detail"))  # type: ignore[method-assign]

    with caplog.at_level("WARNING", logger="ductor_bot.multiagent.supervisor"):
        await asyncio.wait_for(world.supervisor.stop_agent(SUB), WAIT)

    assert SUB not in world.supervisor._stacks
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "shutdown failed (RuntimeError)" in text
    assert "secret detail" not in text


# -- a restart requested by the agent itself ---------------------------------------------------


async def test_exit_restart_rebuilds_without_deadlock_and_never_reruns_the_old_stack(
    world: _World,
) -> None:
    world.declare()
    world.exit_first_run = True
    sup = world.supervisor

    await sup._on_agents_changed()
    first = sup._stacks[SUB]
    await asyncio.wait_for(first.started.wait(), WAIT)
    for _ in range(100):  # the task rebuilds the stack by itself
        if sup._stacks.get(SUB) is not first:
            break
        await asyncio.sleep(0.01)
    second = sup._stacks[SUB]
    await asyncio.wait_for(second.started.wait(), WAIT)

    assert second is not first
    assert (first.runs, first.shutdowns) == (1, 1)  # the old stack ran once and was shut down
    assert second.runs == 1
    assert not sup._agents_lock.locked()
    await asyncio.wait_for(sup.stop_agent(SUB), WAIT)  # and a stop afterwards is not blocked
    assert world.alive == []


async def test_a_stop_while_the_agent_waits_to_rebuild_cancels_it(world: _World) -> None:
    world.declare()
    sup = world.supervisor
    await sup._on_agents_changed()
    stack = sup._stacks[SUB]
    await asyncio.wait_for(stack.started.wait(), WAIT)

    async with sup._agents_lock:  # another lifecycle change is in progress
        waiting = asyncio.create_task(sup._handle_restart_exit(SUB, stack, sup._health[SUB]))
        await asyncio.sleep(0.02)
        assert not waiting.done()
        waiting.cancel()  # what a stop or restart does to the agent's task
        with pytest.raises(asyncio.CancelledError):
            await waiting

    assert stack.shutdowns == 0  # the old stack was left to whoever got the lock
    assert len(world.created) == 1
    await _finish(world)


# -- the log context of a new agent task ------------------------------------------------------------


async def test_a_new_agent_task_does_not_inherit_the_callers_log_context(world: _World) -> None:
    world.declare()
    set_log_context(operation="msg", chat_id=42)  # the context of the command that restarts it

    await world.supervisor.restart_agent(SUB)
    stack = world.supervisor._stacks[SUB]
    await asyncio.wait_for(stack.started.wait(), WAIT)

    assert stack.seen_context == {"operation": None, "chat": None}
    await _finish(world)


# -- graceful stop ---------------------------------------------------------------------------


class _StopAwareStack(_FakeStack):
    """A bot that supports request_stop(): run() then returns EXIT_RESTART, or ignores it."""

    def __init__(self, world: _World, name: str, config: AgentConfig, serial: int) -> None:
        super().__init__(world, name, config, serial)
        self.stop_requested = asyncio.Event()
        self.cancelled = False
        self.bot.request_stop = self._request_stop

    def _request_stop(self) -> bool:
        self.stop_requested.set()
        return True

    async def run(self) -> int:
        self.runs += 1
        self.started.set()
        try:
            if self.world.honour_stop:
                await self.stop_requested.wait()
                return EXIT_RESTART  # the code that would trigger a rebuild if it were handled
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            if self.world.release is not None:
                await self.world.release.wait()  # cleanup that takes longer than the cap
            raise
        return 0


def _declare_two(world: _World) -> list[str]:
    names = ["worker-a", "worker-b"]
    (world.home / "agents.json").write_text(
        json.dumps([{"name": n, "telegram_token": f"tok:{n}"} for n in names]), encoding="utf-8"
    )
    return names


async def test_run_returning_the_restart_code_during_a_stop_neither_deadlocks_nor_restarts(
    world: _World,
) -> None:
    world.stack_class = _StopAwareStack
    world.declare()
    await world.supervisor._on_agents_changed()
    stack = world.alive[0]
    await asyncio.wait_for(stack.started.wait(), WAIT)
    task = world.supervisor._tasks[SUB]

    await asyncio.wait_for(world.supervisor.stop_agent(SUB), WAIT)  # no deadlock on the lock

    assert task.done()
    assert not stack.cancelled  # it returned on its own, nobody cancelled it
    assert len(world.created) == 1  # no rebuild
    assert stack.runs == 1
    assert stack.shutdowns == 1
    assert SUB not in world.supervisor._stacks
    assert world.supervisor._stopping_names == set()
    await _finish(world)


async def test_a_bot_that_ignores_the_stop_request_is_cancelled_after_the_cap(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ductor_bot.multiagent.supervisor._GRACEFUL_STOP_SECONDS", 0.2)
    world.stack_class = _StopAwareStack
    world.honour_stop = False
    world.declare()
    await world.supervisor._on_agents_changed()
    stack = world.alive[0]
    await asyncio.wait_for(stack.started.wait(), WAIT)
    task = world.supervisor._tasks[SUB]

    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(world.supervisor.stop_agent(SUB), WAIT)
    elapsed = asyncio.get_running_loop().time() - started

    assert stack.stop_requested.is_set()
    assert stack.cancelled
    assert task.done()
    assert 0.15 <= elapsed < 2.0
    assert stack.shutdowns == 1
    await _finish(world)


async def test_a_cancelled_bot_that_does_not_finish_does_not_hold_the_stop_forever(
    world: _World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("ductor_bot.multiagent.supervisor._GRACEFUL_STOP_SECONDS", 0.1)
    monkeypatch.setattr("ductor_bot.multiagent.supervisor._CANCEL_WAIT_SECONDS", 0.3)
    world.stack_class = _StopAwareStack
    world.honour_stop = False
    world.release = asyncio.Event()
    world.declare()
    await world.supervisor._on_agents_changed()
    stack = world.alive[0]
    await asyncio.wait_for(stack.started.wait(), WAIT)
    task = world.supervisor._tasks[SUB]

    with caplog.at_level("WARNING"):
        await asyncio.wait_for(world.supervisor.stop_agent(SUB), WAIT)

    assert not task.done()  # it did not finish, yet the stop went on
    assert stack.shutdowns == 1
    assert SUB not in world.supervisor._stacks
    assert any("did not end within the shutdown budget" in r.message for r in caplog.records)
    world.release.set()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), WAIT)
    await _finish(world)


async def test_sequential_sub_stops_share_one_budget(world: _World) -> None:
    from ductor_bot.infra.shutdown_budget import ShutdownBudget

    world.stack_class = _StopAwareStack
    world.honour_stop = False
    names = _declare_two(world)
    await world.supervisor._on_agents_changed()
    for stack in world.alive:
        await asyncio.wait_for(stack.started.wait(), WAIT)
    budget = ShutdownBudget(0.4)
    loop = asyncio.get_running_loop()

    async with world.supervisor._agents_lock:
        first_started = loop.time()
        await world.supervisor._stop_agent_locked(names[0], budget)
        first = loop.time() - first_started
        second_started = loop.time()
        await world.supervisor._stop_agent_locked(names[1], budget)
        second = loop.time() - second_started

    assert 0.3 <= first < 1.5  # the whole budget went into waiting for the first agent
    assert second < 0.3  # the second one found it used up and was cancelled at once
    assert all(stack.cancelled for stack in world.created)
    await _finish(world)
