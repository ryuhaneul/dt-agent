"""Restart generation, inter-agent limits and their configuration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

import ductor_bot.multiagent.bus as bus_module
from ductor_bot.bus.envelope import DeliveryOutcome
from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig, InteragentConfig, update_config_file
from ductor_bot.config_reload import ConfigReloader, classify_changes, diff_configs
from ductor_bot.multiagent.bus import AsyncSendOptions, InterAgentBus
from ductor_bot.multiagent.models import SubAgentConfig, merge_sub_agent_config
from ductor_bot.multiagent.session_gate import SessionGateRegistry
from ductor_bot.multiagent.supervisor import AgentSupervisor
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.session.named import interagent_session_name
from ductor_bot.workspace.paths import DuctorPaths


class Cli:
    """Synthetic CLI shared by every stack of one test: one log, blocking per prompt."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.by_stack: dict[str, list[str]] = {}
        self.running = 0
        self.max_running = 0
        self.gates: dict[str, asyncio.Event] = {}

    def gate(self, text: str) -> asyncio.Event:
        return self.gates.setdefault(text, asyncio.Event())

    def execute_for(self, stack_name: str):
        async def execute(request):
            text = request.prompt.split("[INTER-AGENT MESSAGE", 1)[1].split("\n")[2]
            self.by_stack.setdefault(stack_name, []).append(text)
            self.events.append(f"start:{text}")
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            try:
                if text in self.gates:
                    await self.gates[text].wait()
            finally:
                self.running -= 1
                self.events.append(f"end:{text}")
            return AgentResponse(result=f"done {text}", session_id=f"sid-{text}")

        return execute


def _stack(
    tmp_path: Path,
    label: str,
    cli: Cli,
    *,
    name: str = "worker",
    interagent: dict[str, float] | None = None,
) -> SimpleNamespace:
    home = tmp_path / label
    home.mkdir()
    config = AgentConfig(
        allowed_user_ids=[12345],
        model="sonnet",
        interagent=interagent or {},  # type: ignore[arg-type]
    )
    orch = Orchestrator(config, DuctorPaths(home), agent_name=name)
    orch._cli_service.execute = cli.execute_for(label)
    stack = MagicMock()
    stack.bot.orchestrator = orch
    stack.config = config
    stack.shutdown = AsyncMock()
    stack.bot.notification_service.notify = AsyncMock()
    stack.bot.notification_service.notify_all = AsyncMock()
    return stack


def _world(tmp_path: Path, **interagent: float):
    cli = Cli()
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path), interagent_port=0))
    bus = InterAgentBus()
    bus.set_agent_expected_checker(supervisor.is_agent_expected)
    supervisor._bus = bus
    supervisor._declared = {"worker"}
    main = _stack(tmp_path, "main", cli, name="main")
    first = _stack(tmp_path, "gen1", cli, interagent=interagent)
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


def _send(bus: InterAgentBus, text: str, lane: str = "x") -> str:
    task_id = bus.send_async(
        "main", "worker", f"\n{text}\n", opts=AsyncSendOptions(lane=lane, silent=True)
    )
    assert task_id
    return task_id


async def _settle(times: int = 20) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


async def _until(predicate, timeout: float = 5.0) -> None:  # noqa: ASYNC109
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def _done(bus: InterAgentBus) -> None:
    tasks = [t.asyncio_task for t in bus._async_tasks.values() if t.asyncio_task]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
    await _settle()


def _clean(bus: InterAgentBus) -> bool:
    return bus._gates.stats("worker") == (0, 0, 0) and not bus._gates._queues


async def _stop_while_a_runs(supervisor: AgentSupervisor, cli: Cli) -> None:
    """stop_agent waits for the handler that is running (A); let it end meanwhile."""
    stopping = asyncio.create_task(supervisor.stop_agent("worker"))
    await _until(lambda: "worker" in supervisor._bus._unavailable)  # the stop has begun
    cli.gate("A").set()
    await asyncio.wait_for(stopping, 3)


# -- a restart while requests wait ---------------------------------------------------------


async def test_waiting_request_runs_on_the_new_stack_after_a_restart(tmp_path: Path) -> None:
    cli, supervisor, bus, _first, results = _world(tmp_path)
    cli.gate("A")
    _send(bus, "A")
    _send(bus, "B")  # same lane: waits for A
    await _until(lambda: cli.events == ["start:A"])
    await _stop_while_a_runs(supervisor, cli)  # the restart's first half (A ends meanwhile)
    await _settle(50)
    assert cli.by_stack == {"gen1": ["A"]}  # B did not run on the stack being stopped
    assert "worker" not in bus._agents  # between unregister and the new registration
    second = _stack(tmp_path, "gen2", cli)
    bus.register("worker", second)
    supervisor._stacks["worker"] = second
    await _done(bus)
    assert cli.by_stack == {"gen1": ["A"], "gen2": ["B"]}
    assert sorted(r.result_text for r in results) == ["done A", "done B"]
    assert cli.max_running == 1  # never the same session twice at once, across generations
    assert _clean(bus)


async def test_stack_that_is_shutting_down_is_never_used(tmp_path: Path) -> None:
    cli, supervisor, bus, first, results = _world(tmp_path)
    release = asyncio.Event()

    async def slow_shutdown(_budget: object = None) -> None:
        await release.wait()

    first.shutdown = slow_shutdown
    stop = asyncio.create_task(supervisor.stop_agent("worker"))
    await _until(lambda: "worker" in bus._unavailable)
    assert bus._agents["worker"] is first  # still registered, but marked
    _send(bus, "B")
    await _settle(50)
    assert cli.by_stack == {}  # waiting, not run on the old stack
    release.set()
    await stop
    second = _stack(tmp_path, "gen2", cli)
    bus.register("worker", second)
    supervisor._stacks["worker"] = second
    await _done(bus)
    assert cli.by_stack == {"gen2": ["B"]}
    assert results[0].success
    assert "worker" not in bus._unavailable


async def test_a_stack_whose_orchestrator_is_not_up_yet_is_waited_for(tmp_path: Path) -> None:
    cli, _supervisor, bus, first, _results = _world(tmp_path)
    orchestrator = first.bot.orchestrator
    first.bot.orchestrator = None  # registered, still starting
    _send(bus, "B")
    await _settle(50)
    assert cli.by_stack == {}
    first.bot.orchestrator = orchestrator
    await _done(bus)
    assert cli.by_stack == {"gen1": ["B"]}


async def test_a_removed_recipient_ends_the_request_with_a_clear_result(tmp_path: Path) -> None:
    cli, supervisor, bus, _first, results = _world(tmp_path)
    cli.gate("A")
    _send(bus, "A")
    _send(bus, "B")
    await _until(lambda: cli.events == ["start:A"])
    supervisor._declared = set()  # removed from agents.json: not coming back
    await _stop_while_a_runs(supervisor, cli)
    await _done(bus)
    failed = [r for r in results if not r.success]
    assert len(failed) == 1
    assert bus_module.RECIPIENT_GONE in failed[0].error
    assert failed[0].session_name == interagent_session_name("main", 0, None, "x")
    assert cli.by_stack == {"gen1": ["A"]}
    assert _clean(bus)


async def test_an_excluded_recipient_is_not_waited_for(tmp_path: Path) -> None:
    cli, supervisor, bus, _first, results = _world(tmp_path)
    cli.gate("A")
    _send(bus, "A")
    _send(bus, "B")
    await _until(lambda: cli.events == ["start:A"])
    await _stop_while_a_runs(supervisor, cli)
    supervisor._excluded["worker"] = "start failed"  # the restart did not succeed
    started = asyncio.get_running_loop().time()
    await _done(bus)
    assert asyncio.get_running_loop().time() - started < 2
    assert bus_module.RECIPIENT_GONE in next(r for r in results if not r.success).error


async def test_restart_wait_counts_toward_the_wait_limit(tmp_path: Path) -> None:
    cli, supervisor, bus, _first, results = _world(tmp_path, max_queue_wait_seconds=0.5)
    cli.gate("A")
    _send(bus, "A")
    _send(bus, "B")
    await _until(lambda: cli.events == ["start:A"])
    # Declared, so expected back, but it never returns: B waits for the recipient and that
    # wait runs against its limit.
    await _stop_while_a_runs(supervisor, cli)
    await _done(bus)
    failed = [r for r in results if not r.success]
    assert len(failed) == 1
    assert bus_module.QUEUE_WAIT_TIMEOUT in failed[0].error
    assert "Timeout after" not in failed[0].error
    assert failed[0].session_name
    assert _clean(bus)


async def test_sync_send_waits_for_the_restart_within_its_budget(tmp_path: Path) -> None:
    cli, supervisor, bus, _first, _results = _world(tmp_path)
    cli.gate("A")
    _send(bus, "A")
    await _until(lambda: cli.events == ["start:A"])
    waiting = asyncio.create_task(bus.send("main", "worker", "\nsync\n", lane="x", send_timeout=3))
    await _until(
        lambda: (
            bus._gates.pending("worker", 12345, interagent_session_name("main", 0, None, "x")) == 2
        )
    )  # the sync request has its place
    stopping = asyncio.create_task(supervisor.stop_agent("worker"))
    await _until(lambda: "worker" in bus._unavailable)  # the stop has begun
    cli.gate("A").set()  # the running handler ends (the stop waits for it), then A's place is free
    await asyncio.wait_for(stopping, 3)  # the sync request is admitted, its recipient restarting
    await _settle(50)
    assert not waiting.done()
    bus.register("worker", _stack(tmp_path, "gen2", cli))
    reply = await asyncio.wait_for(waiting, 3)
    assert reply.success
    assert cli.by_stack == {"gen1": ["A"], "gen2": ["sync"]}
    assert _clean(bus)


async def test_sync_send_to_a_removed_recipient_fails_at_once(tmp_path: Path) -> None:
    _cli, supervisor, bus, _first, _results = _world(tmp_path)
    supervisor._declared = set()
    await supervisor.stop_agent("worker")
    reply = await bus.send("main", "worker", "\nsync\n")
    assert not reply.success
    assert "not found" in (reply.error or "")  # unknown recipient: the existing fast check


async def test_sync_send_that_loses_its_recipient_while_waiting_fails(tmp_path: Path) -> None:
    cli, supervisor, bus, _first, _results = _world(tmp_path)
    cli.gate("A")
    _send(bus, "A")
    await _until(lambda: cli.events == ["start:A"])
    waiting = asyncio.create_task(bus.send("main", "worker", "\nsync\n", lane="x", send_timeout=3))
    await _until(
        lambda: (
            bus._gates.pending("worker", 12345, interagent_session_name("main", 0, None, "x")) == 2
        )
    )  # the sync request has its place
    supervisor._declared = set()
    stopping = asyncio.create_task(supervisor.stop_agent("worker"))
    await _until(lambda: "worker" in bus._unavailable)  # the stop has begun
    cli.gate("A").set()  # the stop waits for the running handler
    await asyncio.wait_for(stopping, 3)
    reply = await asyncio.wait_for(waiting, 3)
    assert not reply.success
    assert bus_module.RECIPIENT_GONE in (reply.error or "")


def test_supervisor_decides_between_restarting_and_gone(tmp_path: Path) -> None:
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path), interagent_port=0))
    assert supervisor.is_agent_expected("main")
    assert not supervisor.is_agent_expected("nobody")
    supervisor._declared = {"w"}
    assert supervisor.is_agent_expected("w")  # declared, not running: restarting
    supervisor._excluded["w"] = "reason"
    assert not supervisor.is_agent_expected("w")
    del supervisor._excluded["w"]
    supervisor._stacks["w"] = MagicMock()
    supervisor._declared = set()
    assert supervisor.is_agent_expected("w")  # running


async def test_register_clears_the_unavailable_mark_and_unregister_forgets_it(
    tmp_path: Path,
) -> None:
    cli = Cli()
    bus = InterAgentBus()
    bus.register("worker", _stack(tmp_path, "gen1", cli))
    bus.mark_unavailable("worker")
    bus.register("worker", _stack(tmp_path, "gen2", cli))
    assert "worker" not in bus._unavailable
    bus.mark_unavailable("worker")
    bus.unregister("worker")
    assert "worker" not in bus._unavailable


# -- one gate object, limits change its capacity ---------------------------------------------


async def test_a_restart_with_another_limit_changes_the_capacity_of_the_same_gate(
    tmp_path: Path,
) -> None:
    cli = Cli()
    bus = InterAgentBus()
    gates = bus.gates
    bus.register("worker", _stack(tmp_path, "gen1", cli, interagent={"max_parallel_sessions": 1}))
    pool = gates._slots("worker")
    assert pool.capacity == 1
    first = gates.reserve("worker", 12345, "a")
    second = gates.reserve("worker", 12345, "b")
    await first.admit()
    waiter = asyncio.create_task(second.admit())
    await _until(lambda: gates.stats("worker") == (1, 1, 2))
    assert gates.stats("worker") == (1, 1, 2)
    bus.register("worker", _stack(tmp_path, "gen2", cli, interagent={"max_parallel_sessions": 3}))
    await asyncio.wait_for(waiter, 1)  # the waiter was kept and now fits
    assert gates._slots("worker") is pool
    assert gates.limit("worker") == 3
    assert bus.gates is gates
    first.release()
    second.release()
    assert gates.stats("worker") == (0, 0, 0)


def test_registry_limits_are_per_agent_and_default_for_the_rest() -> None:
    registry = SessionGateRegistry(max_parallel=4)
    registry.set_agent_limit("a", 2)
    assert (registry.limit("a"), registry.limit("b")) == (2, 4)
    registry.set_max_parallel(6)  # the default changes, an agent with an own limit does not
    assert (registry.limit("a"), registry.limit("b")) == (2, 6)


# -- configuration ---------------------------------------------------------------------------


def test_defaults_and_validation() -> None:
    cfg = AgentConfig().interagent
    assert (cfg.max_parallel_sessions, cfg.max_queue_wait_seconds) == (4, 3600.0)
    for bad in (
        {"max_parallel_sessions": 0},
        {"max_parallel_sessions": -2},
        {"max_queue_wait_seconds": 0},
        {"max_queue_wait_seconds": -1},
        {"max_parallel_sessions": "many"},
    ):
        with pytest.raises(ValidationError):
            InteragentConfig(**bad)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            AgentConfig(interagent=bad)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            SubAgentConfig.model_validate({"name": "w", "interagent": bad})


def _merged(main: AgentConfig, tmp_path: Path, **sub: object) -> AgentConfig:
    return merge_sub_agent_config(
        main, SubAgentConfig.model_validate({"name": "w", **sub}), tmp_path / "w"
    )


def test_sub_inherits_main_and_overrides_key_by_key(tmp_path: Path) -> None:
    main = AgentConfig(interagent={"max_parallel_sessions": 3, "max_queue_wait_seconds": 100})
    inherited = _merged(main, tmp_path)
    assert (
        inherited.interagent.max_parallel_sessions,
        inherited.interagent.max_queue_wait_seconds,
    ) == (3, 100)
    partial = _merged(main, tmp_path, interagent={"max_parallel_sessions": 2})
    assert (
        partial.interagent.max_parallel_sessions,
        partial.interagent.max_queue_wait_seconds,
    ) == (2, 100)
    wait_only = _merged(main, tmp_path, interagent={"max_queue_wait_seconds": 7})
    assert (
        wait_only.interagent.max_parallel_sessions,
        wait_only.interagent.max_queue_wait_seconds,
    ) == (3, 7)
    both = _merged(
        main, tmp_path, interagent={"max_parallel_sessions": 1, "max_queue_wait_seconds": 5}
    )
    assert (both.interagent.max_parallel_sessions, both.interagent.max_queue_wait_seconds) == (1, 5)
    assert (main.interagent.max_parallel_sessions, main.interagent.max_queue_wait_seconds) == (
        3,
        100,
    )
    assert "interagent" not in SubAgentConfig(name="w").model_dump(exclude_none=True)


async def test_the_effective_values_are_mirrored_into_the_sub_config_file(tmp_path: Path) -> None:
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path), interagent_port=0))
    (tmp_path / "agents.json").write_text(
        json.dumps(
            [
                {
                    "name": "sub1",
                    "transport": "telegram",
                    "telegram_token": "tok:1",
                    "interagent": {"max_parallel_sessions": 2},
                },
            ]
        )
    )
    config_path = tmp_path / "agents" / "sub1" / "config" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}")
    supervisor._stacks["main"] = MagicMock()
    supervisor._notify_main_agent = AsyncMock()  # type: ignore[method-assign]
    with (
        patch(
            "ductor_bot.multiagent.supervisor.AgentStack.create",
            new_callable=AsyncMock,
            return_value=MagicMock(shutdown=AsyncMock()),
        ),
        patch(
            "ductor_bot.multiagent.supervisor.update_config_file_async",
            new_callable=AsyncMock,
            side_effect=OSError("synthetic"),
        ) as update,
    ):
        await supervisor.start_agent_by_name("sub1")
    assert update.await_args.kwargs["interagent"] == {
        "max_parallel_sessions": 2,
        "max_queue_wait_seconds": 3600.0,
    }


def test_mirroring_keeps_every_other_key_of_the_config_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = {
        "custom": {"nested": [1, 2]},
        "provider": "claude",
        "user_timezone": "Asia/Seoul",
        "interagent": {"max_parallel_sessions": 9, "max_queue_wait_seconds": 1.0},
    }
    path.write_text(json.dumps(original))
    update_config_file(
        path, interagent={"max_parallel_sessions": 2, "max_queue_wait_seconds": 3600.0}
    )
    after = json.loads(path.read_text())
    assert after["interagent"] == {"max_parallel_sessions": 2, "max_queue_wait_seconds": 3600.0}
    assert {k: v for k, v in after.items() if k != "interagent"} == {
        k: v for k, v in original.items() if k != "interagent"
    }


def test_supervisor_passes_the_main_limits_to_the_bus() -> None:
    import inspect

    source = inspect.getsource(AgentSupervisor.start)
    assert "max_parallel_sessions=self._main_config.interagent.max_parallel_sessions" in source
    assert "max_queue_wait_seconds=self._main_config.interagent.max_queue_wait_seconds" in source
    assert "_wire_bus_checkers(self._bus)" in source
    assert "set_agent_expected_checker(self.is_agent_expected)" in inspect.getsource(
        AgentSupervisor._wire_bus_checkers
    )


def test_bus_constructor_values_are_the_defaults_for_an_agent_without_config() -> None:
    bus = InterAgentBus(max_parallel_sessions=2, max_queue_wait_seconds=9)
    bare = MagicMock()
    bare.config = SimpleNamespace()
    bus.register("bare", bare)
    assert bus.gates.limit("bare") == 2
    assert bus._queue_wait_for("bare") == 9


async def test_wait_limit_and_slots_follow_the_recipient_configuration(tmp_path: Path) -> None:
    cli = Cli()
    bus = InterAgentBus(max_queue_wait_seconds=9)
    bus.register(
        "worker",
        _stack(
            tmp_path,
            "gen1",
            cli,
            interagent={"max_parallel_sessions": 2, "max_queue_wait_seconds": 0.5},
        ),
    )
    assert bus._queue_wait_for("worker") == 0.5
    assert bus.gates.limit("worker") == 2
    bus.register("main", _stack(tmp_path, "m", cli, name="main"))
    assert bus._queue_wait_for("main") == 3600.0  # its own (default) configuration


# -- hot reload: a warning only, never an automatic restart --------------------------------------


def test_interagent_changes_are_restart_required_not_hot() -> None:
    old = AgentConfig()
    new = AgentConfig(interagent={"max_parallel_sessions": 2})
    hot, restart = classify_changes(diff_configs(old, new))
    assert hot == {}
    assert restart == ["interagent"]


async def test_hot_reload_only_reports_that_a_restart_is_needed(tmp_path: Path) -> None:
    config = AgentConfig()
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config.model_dump(mode="json")))
    on_hot, on_restart = MagicMock(), MagicMock()
    reloader = ConfigReloader(path, config, on_hot_reload=on_hot, on_restart_needed=on_restart)
    reloader._last_mtime = 0.0
    changed = config.model_dump(mode="json")
    changed["interagent"]["max_parallel_sessions"] = 2
    path.write_text(json.dumps(changed))
    await reloader._check()
    on_restart.assert_called_once_with(["interagent"])  # the warning path
    on_hot.assert_not_called()
    assert config.interagent.max_parallel_sessions == 4  # the running value is unchanged


# -- wiring order of the background observer --------------------------------------------------


async def test_background_gate_is_wired_whatever_the_creation_order(tmp_path: Path) -> None:
    from ductor_bot.session.named import NamedSession
    from ductor_bot.workspace.init import init_workspace
    from tests.orchestrator.conftest import setup_framework

    fw_root = tmp_path / "fw"
    setup_framework(fw_root)
    paths = DuctorPaths(
        ductor_home=tmp_path / "home", home_defaults=fw_root / "workspace", framework_root=fw_root
    )
    init_workspace(paths)
    config = AgentConfig()
    config.allowed_user_ids = [12345]
    orch = Orchestrator(config, paths, agent_name="worker")
    cli = MagicMock()
    cli._config = MagicMock()
    cli.execute = AsyncMock(return_value=AgentResponse(result="ok", session_id="s"))
    object.__setattr__(orch, "_cli_service", cli)
    assert orch._observers.background is None  # the observer does not exist yet ...
    gates = SessionGateRegistry(4)
    orch.set_session_gates(gates)  # ... when the gates are connected
    orch._observers.init_task_observers(
        cron_manager=MagicMock(),
        webhook_manager=MagicMock(),
        cli_service=cli,
        codex_cache=MagicMock(),
    )
    name = interagent_session_name("main", 7, 1, "x")
    orch._named_sessions.add(
        NamedSession(
            name=name,
            chat_id=12345,
            provider="claude",
            model="opus",
            session_id="s0",
            prompt_preview="",
            status="idle",
            created_at=1.0,
        )
    )
    orch.submit_named_followup_bg(12345, name, "bg", 1, None)
    assert gates.pending("worker", 12345, name) == 1  # reserved at submission: the gate is wired
    await asyncio.sleep(0.5)
    bg = orch._observers.background
    tasks = [t.asyncio_task for t in bg._tasks.values() if t.asyncio_task]  # type: ignore[union-attr]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    assert gates.pending("worker", 12345, name) == 0
