"""A restarting sub-agent: handlers of the old stack end before the new stack reads its state."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest  # noqa: TC002 -- annotations only

import ductor_bot.multiagent.supervisor as supervisor_module
from ductor_bot.bus.envelope import DeliveryOutcome
from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig
from ductor_bot.multiagent.bus import AsyncSendOptions, InterAgentBus
from ductor_bot.multiagent.health import AgentHealth
from ductor_bot.multiagent.supervisor import AgentSupervisor
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.workspace.paths import DuctorPaths


class Cli:
    """Synthetic CLI shared by every stack of one test; a prompt can be held back."""

    def __init__(self) -> None:
        self.by_stack: dict[str, list[str]] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.started: dict[str, asyncio.Event] = {}

    def gate(self, text: str) -> asyncio.Event:
        return self.gates.setdefault(text, asyncio.Event())

    def execute_for(self, stack_name: str):
        async def execute(request):
            text = request.prompt.split("[INTER-AGENT MESSAGE", 1)[1].split("\n")[2]
            self.by_stack.setdefault(stack_name, []).append(text)
            self.started.setdefault(text, asyncio.Event()).set()
            if text in self.gates:
                await self.gates[text].wait()
            return AgentResponse(result=f"done {text}", session_id=f"sid-{text}")

        return execute


def _stack(home: Path, generation: str, cli: Cli):
    """A stack on *home*; every generation of the worker uses the same home, as a restart does."""
    home.mkdir(parents=True, exist_ok=True)
    config = AgentConfig(allowed_user_ids=[12345], model="sonnet")
    orch = Orchestrator(config, DuctorPaths(home), agent_name="worker")
    orch._cli_service.execute = cli.execute_for(generation)
    stack = MagicMock()
    stack.bot.orchestrator = orch
    stack.config = config
    stack.shutdown = AsyncMock()
    stack.bot.notification_service.notify = AsyncMock()
    stack.bot.notification_service.notify_all = AsyncMock()
    return stack


def _world(tmp_path: Path):
    cli = Cli()
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path), interagent_port=0))
    bus = InterAgentBus()
    bus.set_agent_expected_checker(supervisor.is_agent_expected)
    supervisor._bus = bus
    supervisor._declared = {"worker"}
    main = _stack(tmp_path / "main", "main", cli)
    first = _stack(tmp_path / "worker", "gen1", cli)
    bus.register("main", main)
    bus.register("worker", first)
    supervisor._stacks["main"] = main
    supervisor._stacks["worker"] = first
    results: list = []

    async def handler(result):
        results.append(result)
        return DeliveryOutcome(ok=True, injected=True, delivered=True)

    bus.set_async_result_handler("main", handler)
    return cli, supervisor, bus, first, results


def _new_generation(tmp_path: Path, supervisor: AgentSupervisor, bus: InterAgentBus, cli: Cli):
    stack = _stack(tmp_path / "worker", "gen2", cli)
    supervisor._stacks["worker"] = stack
    bus.register("worker", stack)
    return stack


def _send(bus: InterAgentBus, text: str, lane: str) -> str:
    task_id = bus.send_async(
        "main", "worker", f"\n{text}\n", opts=AsyncSendOptions(lane=lane, silent=True)
    )
    assert task_id
    return task_id


async def _until(condition, what: str, timeout: float = 5.0) -> None:  # noqa: ASYNC109
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, f"timed out: {what}"
        await asyncio.sleep(0.01)


def _lanes_on_disk(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / "worker" / "named_sessions.json"
    entries = json.loads(path.read_text())["sessions"] if path.exists() else []
    return {e["name"].rsplit(".l", 1)[-1]: e["session_id"] for e in entries}


async def test_stop_waits_for_the_running_handler_so_both_lanes_survive(tmp_path: Path) -> None:
    cli, supervisor, bus, _first, results = _world(tmp_path)
    cli.gate("a")
    _send(bus, "a", "a")
    await _until(lambda: "a" in cli.started, "lane a running")

    stopping = asyncio.create_task(supervisor.stop_agent("worker"))
    await asyncio.sleep(0.1)
    assert not stopping.done(), "stop_agent returned while an admitted handler was running"

    cli.gate("a").set()
    await asyncio.wait_for(stopping, 5)

    _new_generation(tmp_path, supervisor, bus, cli)
    _send(bus, "b", "b")
    await _until(lambda: len(results) == 2, "both results delivered")

    assert _lanes_on_disk(tmp_path) == {"a": "sid-a", "b": "sid-b"}


async def test_late_old_generation_write_is_dropped_after_the_drain_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(supervisor_module, "_GENERATION_DRAIN_SECONDS", 0.1)
    cli, supervisor, bus, _first, results = _world(tmp_path)
    cli.gate("a")
    _send(bus, "a", "a")
    await _until(lambda: "a" in cli.started, "lane a running")

    caplog.set_level(logging.WARNING)
    await asyncio.wait_for(supervisor.stop_agent("worker"), 5)  # gives up on the handler
    _new_generation(tmp_path, supervisor, bus, cli)
    _send(bus, "b", "b")
    await _until(lambda: len(results) == 1, "lane b delivered")
    assert _lanes_on_disk(tmp_path) == {"a": "", "b": "sid-b"}

    cli.gate("a").set()  # the old generation's handler now ends
    await _until(lambda: len(results) == 2, "lane a delivered")

    # The old generation never writes its answer (lane a keeps the id it had when it was dropped).
    assert _lanes_on_disk(tmp_path) == {"a": "", "b": "sid-b"}
    assert any("write skipped" in r.getMessage() for r in caplog.records)
    assert any("still running" in r.getMessage() for r in caplog.records)


async def test_restart_exit_blocks_the_old_stack_and_runs_waiting_requests_on_the_new_one(
    tmp_path: Path,
) -> None:
    cli, supervisor, bus, first, results = _world(tmp_path)
    cli.gate("a")
    _send(bus, "a", "a")
    await _until(lambda: "a" in cli.started, "lane a running")
    _send(bus, "a2", "a")  # waits behind a in the same lane (FIFO)
    in_shutdown = asyncio.Event()
    leave_shutdown = asyncio.Event()

    async def shutdown(_budget: object = None) -> None:  # the old stack stopping: a new request arrives meanwhile
        in_shutdown.set()
        await leave_shutdown.wait()
        cli.gate("a").set()

    first.shutdown = shutdown

    async def rebuild(_name, _old):
        return _new_generation(tmp_path, supervisor, bus, cli)

    supervisor._rebuild_stack = rebuild  # type: ignore[method-assign]
    restart = asyncio.create_task(
        supervisor._handle_restart_exit("worker", first, AgentHealth(name="worker"))
    )
    await asyncio.wait_for(in_shutdown.wait(), 5)
    _send(bus, "c", "c")  # a new delegation during the old stack's shutdown
    await asyncio.sleep(0.2)
    assert "c" not in cli.by_stack.get("gen1", []), "new request ran on the stopping stack"

    leave_shutdown.set()
    await asyncio.wait_for(restart, 5)
    await _until(lambda: len(results) == 3, "all three results delivered")

    assert cli.by_stack["gen1"] == ["a"]
    assert sorted(cli.by_stack["gen2"]) == ["a2", "c"]  # waiting requests kept, on the new stack
    assert _lanes_on_disk(tmp_path) == {"a": "sid-a2", "c": "sid-c"}


async def test_a_request_admitted_during_the_hop_does_not_start_on_the_stopping_stack(
    tmp_path: Path,
) -> None:
    cli, supervisor, bus, first, _results = _world(tmp_path)
    old_orch = first.bot.orchestrator
    supervisor._mark_stopping("worker")

    confirming = asyncio.create_task(bus._confirm_orchestrator("worker", old_orch, 5))
    await asyncio.sleep(0.1)
    assert not confirming.done()  # the old orchestrator is not accepted any more

    new = _new_generation(tmp_path, supervisor, bus, cli)
    assert await asyncio.wait_for(confirming, 5) is new.bot.orchestrator


async def test_drain_agent_reports_whether_the_handlers_finished(tmp_path: Path) -> None:
    cli, _supervisor, bus, _first, _results = _world(tmp_path)
    assert await bus.drain_agent("worker", 0.1) is True  # nothing running

    cli.gate("a")
    _send(bus, "a", "a")
    await _until(lambda: "a" in cli.started, "lane a running")
    assert await bus.drain_agent("worker", 0.1) is False

    waiting = asyncio.create_task(bus.drain_agent("worker", 5))
    await asyncio.sleep(0.05)
    assert not waiting.done()
    cli.gate("a").set()
    assert await asyncio.wait_for(waiting, 5) is True
