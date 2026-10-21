"""Tests for multiagent/supervisor.py: AgentSupervisor lifecycle and recovery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.config import AgentConfig, MatrixConfig
from ductor_bot.multiagent.health import AgentHealth
from ductor_bot.multiagent.models import SubAgentConfig
from ductor_bot.multiagent.supervisor import (
    _MAX_RESTART_RETRIES,
    AgentSupervisor,
    StartResult,
    _config_changed,
    _is_transient,
)


@pytest.fixture
def main_config(tmp_path: Path) -> AgentConfig:
    """Create a main config with tmp_path as ductor_home."""
    return AgentConfig(
        ductor_home=str(tmp_path),
        telegram_token="main-token",
        allowed_user_ids=[1],
        interagent_port=0,
    )


@pytest.fixture
def supervisor(main_config: AgentConfig) -> AgentSupervisor:
    return AgentSupervisor(main_config)


def test_config_changed_checks_secondary_matrix_transport() -> None:
    old = AgentConfig(
        transports=["telegram", "matrix"],
        telegram_token="telegram-dummy",
        matrix=MatrixConfig(
            homeserver="https://matrix.invalid",
            user_id="@sub:invalid",
            password="matrix-dummy",
        ),
    )
    same = old.model_copy(deep=True)
    assert _config_changed(same, old) is False

    changed = old.model_copy(deep=True)
    changed.matrix.user_id = "@another:invalid"
    assert _config_changed(changed, old) is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("homeserver", "https://other.invalid"),
        ("password", "other-dummy"),
        ("access_token", "other-token"),
        ("device_id", "OTHER"),
        ("store_path", "other_store"),
    ],
)
def test_config_changed_checks_all_matrix_connection_fields(field: str, value: str) -> None:
    old = AgentConfig(
        transports=["telegram", "matrix"],
        telegram_token="telegram-dummy",
        matrix=MatrixConfig(
            homeserver="https://matrix.invalid",
            user_id="@sub:invalid",
            password="matrix-dummy",
        ),
    )
    changed = old.model_copy(deep=True)
    setattr(changed.matrix, field, value)

    assert _config_changed(changed, old) is True


class TestSupervisorInit:
    """Test initial state."""

    def test_initial_state(self, supervisor: AgentSupervisor) -> None:
        assert supervisor.stacks == {}
        assert supervisor.health == {}
        assert supervisor.bus is None
        assert supervisor._running is False
        assert supervisor._main_generation == 0
        assert supervisor._main_ready_gen is None
        assert not supervisor.is_delivery_ready("main")

    def test_agents_path(self, supervisor: AgentSupervisor, tmp_path: Path) -> None:
        assert supervisor._agents_path == tmp_path / "agents.json"


class TestStartupFailures:
    """Test startup error propagation."""

    async def test_internal_api_start_failure_is_propagated(
        self,
        supervisor: AgentSupervisor,
    ) -> None:
        with (
            patch(
                "ductor_bot.multiagent.internal_api.InternalAgentAPI.start",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("ductor_bot.multiagent.supervisor.AgentStack.create", new_callable=AsyncMock),
            pytest.raises(RuntimeError, match="Internal agent API failed to start"),
        ):
            await supervisor.start()


class TestStopAgent:
    """Test stop_agent() behavior."""

    async def test_stop_main_is_noop(self, supervisor: AgentSupervisor) -> None:
        """Cannot stop main agent via stop_agent()."""
        await supervisor.stop_agent("main")
        # No error, just logged warning

    async def test_stop_nonexistent_is_safe(self, supervisor: AgentSupervisor) -> None:
        """Stopping a non-running agent doesn't crash."""
        await supervisor.stop_agent("nonexistent")

    async def test_stop_cancels_task_and_shutdowns(self, supervisor: AgentSupervisor) -> None:
        """stop_agent() cancels the task and shuts down the stack."""
        mock_stack = MagicMock()
        mock_stack.shutdown = AsyncMock()
        supervisor._stacks["sub1"] = mock_stack

        done_event = asyncio.Event()

        async def fake_task() -> int:
            await done_event.wait()
            return 0

        task = asyncio.create_task(fake_task())
        supervisor._tasks["sub1"] = task

        supervisor._health["sub1"] = AgentHealth(name="sub1", status="running")

        from ductor_bot.multiagent.bus import InterAgentBus

        supervisor._bus = InterAgentBus()
        supervisor._bus.register("sub1", mock_stack)
        supervisor._bus.set_async_result_handler("sub1", AsyncMock())

        await supervisor.stop_agent("sub1")

        assert "sub1" not in supervisor._stacks
        assert "sub1" not in supervisor._tasks
        assert supervisor._health["sub1"].status == "stopped"
        mock_stack.shutdown.assert_called_once()
        assert "sub1" not in supervisor._bus.list_agents()
        assert not supervisor._bus.has_async_result_handler("sub1")


class TestStartAgentByName:
    """Test start_agent_by_name()."""

    async def test_already_running(self, supervisor: AgentSupervisor) -> None:
        supervisor._stacks["sub1"] = MagicMock()
        result = await supervisor.start_agent_by_name("sub1")
        assert "already running" in result

    async def test_not_in_registry(self, supervisor: AgentSupervisor) -> None:
        result = await supervisor.start_agent_by_name("nonexistent")
        assert "not found" in result

    async def test_start_from_registry(self, supervisor: AgentSupervisor, tmp_path: Path) -> None:
        """Agent found in registry is started via _start_sub_agent."""
        agents_data = [{"name": "sub1", "telegram_token": "tok:1"}]
        (tmp_path / "agents.json").write_text(json.dumps(agents_data))

        with patch.object(supervisor, "_start_sub_agent", new_callable=AsyncMock) as mock_start:
            result = await supervisor.start_agent_by_name("sub1")

        assert "started" in result
        mock_start.assert_called_once()
        called_cfg = mock_start.call_args[0][0]
        assert called_cfg.name == "sub1"


class TestRestartAgent:
    """Test restart_agent()."""

    async def test_cannot_restart_main(self, supervisor: AgentSupervisor) -> None:
        result = await supervisor.restart_agent("main")
        assert "Cannot restart main" in result

    async def test_not_in_registry(self, supervisor: AgentSupervisor) -> None:
        result = await supervisor.restart_agent("nonexistent")
        assert "not found" in result

    async def test_restart_stops_and_starts(
        self, supervisor: AgentSupervisor, tmp_path: Path
    ) -> None:
        agents_data = [{"name": "sub1", "telegram_token": "tok:1"}]
        (tmp_path / "agents.json").write_text(json.dumps(agents_data))

        supervisor._stacks["sub1"] = MagicMock()

        with (
            patch.object(supervisor, "_stop_agent_locked", new_callable=AsyncMock) as mock_stop,
            patch.object(supervisor, "_start_sub_agent", new_callable=AsyncMock) as mock_start,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await supervisor.restart_agent("sub1")

        assert "restarted" in result
        mock_stop.assert_called_once_with("sub1")
        mock_start.assert_called_once()

    async def test_restart_stops_before_start(
        self, supervisor: AgentSupervisor, tmp_path: Path
    ) -> None:
        """Restart must fully stop the old agent before starting the new one.

        The new bot handles session takeover itself via ``getUpdates(offset=-1)``
        in ``TelegramBot.run()``, so no artificial delay is needed here.
        """
        agents_data = [{"name": "sub1", "telegram_token": "tok:1"}]
        (tmp_path / "agents.json").write_text(json.dumps(agents_data))

        supervisor._stacks["sub1"] = MagicMock()
        call_order: list[str] = []

        async def record_stop(_name: str) -> None:
            call_order.append("stop")

        async def record_start(_cfg: SubAgentConfig) -> StartResult:
            call_order.append("start")
            return StartResult(ok=True)

        with (
            patch.object(supervisor, "_stop_agent_locked", side_effect=record_stop),
            patch.object(supervisor, "_start_sub_agent", side_effect=record_start),
        ):
            await supervisor.restart_agent("sub1")

        assert call_order == ["stop", "start"]


class TestOnAgentsChanged:
    """Test _on_agents_changed() FileWatcher callback."""

    async def test_starts_new_agents(self, supervisor: AgentSupervisor, tmp_path: Path) -> None:
        agents_data = [{"name": "sub1", "telegram_token": "tok:1"}]
        (tmp_path / "agents.json").write_text(json.dumps(agents_data))

        with patch.object(supervisor, "_start_sub_agent", new_callable=AsyncMock) as mock_start:
            await supervisor._on_agents_changed()

        mock_start.assert_called_once()
        assert mock_start.call_args[0][0].name == "sub1"

    async def test_stops_removed_agents(self, supervisor: AgentSupervisor, tmp_path: Path) -> None:
        # sub1 is currently running but not in agents.json
        supervisor._stacks["sub1"] = MagicMock()
        (tmp_path / "agents.json").write_text("[]")

        with patch.object(supervisor, "_stop_agent_locked", new_callable=AsyncMock) as mock_stop:
            await supervisor._on_agents_changed()

        mock_stop.assert_called_once_with("sub1")

    async def test_restarts_on_token_change(
        self, supervisor: AgentSupervisor, tmp_path: Path
    ) -> None:
        """When token changes in agents.json, the agent is restarted."""
        # Current state: sub1 running with old token
        old_config = AgentConfig(
            telegram_token="old-token", ductor_home=str(tmp_path / "agents/sub1")
        )
        supervisor._stacks["sub1"] = MagicMock(config=old_config)

        # New agents.json has different token
        agents_data = [{"name": "sub1", "telegram_token": "new-token"}]
        (tmp_path / "agents.json").write_text(json.dumps(agents_data))

        with (
            patch.object(supervisor, "_stop_agent_locked", new_callable=AsyncMock) as mock_stop,
            patch.object(supervisor, "_start_sub_agent", new_callable=AsyncMock) as mock_start,
        ):
            await supervisor._on_agents_changed()

        mock_stop.assert_called_once_with("sub1")
        mock_start.assert_called_once()

    async def test_main_agent_not_affected(
        self, supervisor: AgentSupervisor, tmp_path: Path
    ) -> None:
        """Main agent is never stopped/started by _on_agents_changed."""
        supervisor._stacks["main"] = MagicMock()
        (tmp_path / "agents.json").write_text("[]")

        with patch.object(supervisor, "_stop_agent_locked", new_callable=AsyncMock) as mock_stop:
            await supervisor._on_agents_changed()

        mock_stop.assert_not_called()

    async def test_lock_serializes_changes(
        self, supervisor: AgentSupervisor, tmp_path: Path
    ) -> None:
        """Concurrent _on_agents_changed calls are serialized by the lock."""
        (tmp_path / "agents.json").write_text("[]")
        call_order: list[str] = []

        original = supervisor._registry.load

        def slow_load() -> list[SubAgentConfig]:
            call_order.append("enter")
            result = original()
            call_order.append("exit")
            return result

        supervisor._registry.load = slow_load

        await asyncio.gather(
            supervisor._on_agents_changed(),
            supervisor._on_agents_changed(),
        )

        # With lock: enter-exit-enter-exit (serialized)
        # Without lock: could be enter-enter-exit-exit
        assert call_order == ["enter", "exit", "enter", "exit"]


class TestStartSubAgent:
    """Test _start_sub_agent() behavior."""

    async def test_rejects_main_name(self, supervisor: AgentSupervisor) -> None:
        """Cannot create a sub-agent named 'main'."""
        sub_cfg = SubAgentConfig(name="main", telegram_token="tok:1")
        await supervisor._start_sub_agent(sub_cfg)
        assert "main" not in supervisor._stacks or supervisor._stacks.get("main") is None

    async def test_handles_creation_failure(self, supervisor: AgentSupervisor) -> None:
        """If AgentStack.create() fails, sub-agent is not registered."""
        sub_cfg = SubAgentConfig(name="sub1", telegram_token="tok:1")

        with patch(
            "ductor_bot.multiagent.supervisor.AgentStack.create",
            side_effect=RuntimeError("boom"),
        ):
            await supervisor._start_sub_agent(sub_cfg)

        assert "sub1" not in supervisor._stacks


class TestStopAll:
    """Test stop_all() ordered shutdown."""

    async def test_stops_sub_agents_before_main(self, supervisor: AgentSupervisor) -> None:
        """Sub-agents are stopped before the main agent."""
        stop_order: list[str] = []

        main_stack = MagicMock()
        main_stack.shutdown = AsyncMock(side_effect=lambda *_: stop_order.append("main"))
        sub_stack = MagicMock()
        sub_stack.shutdown = AsyncMock(side_effect=lambda *_: stop_order.append("sub1"))

        supervisor._stacks = {"main": main_stack, "sub1": sub_stack}
        supervisor._health = {
            "main": AgentHealth(name="main"),
            "sub1": AgentHealth(name="sub1"),
        }

        # Create tasks that are already done
        supervisor._tasks = {
            "main": asyncio.create_task(asyncio.sleep(999)),
            "sub1": asyncio.create_task(asyncio.sleep(999)),
        }

        from ductor_bot.multiagent.bus import InterAgentBus

        supervisor._bus = InterAgentBus()
        supervisor._bus.register("main", main_stack)
        supervisor._bus.register("sub1", sub_stack)
        supervisor._bus.set_async_result_handler("main", AsyncMock())
        supervisor._bus.set_async_result_handler("sub1", AsyncMock())
        supervisor._watcher = MagicMock()
        supervisor._watcher.stop = AsyncMock()

        await supervisor.stop_all()

        # sub1 should be stopped before main
        assert stop_order.index("sub1") < stop_order.index("main")
        assert supervisor._running is False
        assert not supervisor._bus.has_async_result_handler("main")
        assert not supervisor._bus.has_async_result_handler("sub1")

    async def test_cancellation_unblocks_start(self, supervisor: AgentSupervisor) -> None:
        """Cancelling the supervisor.start() task (simulating SIGINT handler)
        must not hang — CancelledError must propagate from _main_done.wait()
        so the finally block in run_telegram() can call stop_all()."""
        main_stack = MagicMock()
        main_stack.shutdown = AsyncMock()
        main_stack.bot = MagicMock()
        main_stack.bot.on_async_interagent_result = AsyncMock()
        main_stack.is_main = True

        # stack.run() must block forever (simulating normal polling)
        async def _block_forever() -> int:
            await asyncio.sleep(9999)
            return 0

        main_stack.run = _block_forever

        supervisor._watcher = MagicMock()
        supervisor._watcher.start = AsyncMock()
        supervisor._watcher.stop = AsyncMock()

        with (
            patch.object(supervisor, "_sync_sub_agents", new_callable=AsyncMock),
            patch(
                "ductor_bot.multiagent.supervisor.AgentStack.create",
                new_callable=AsyncMock,
                return_value=main_stack,
            ),
            patch(
                "ductor_bot.multiagent.shared_knowledge.SharedKnowledgeSync",
            ) as mock_sks_cls,
        ):
            mock_sks = MagicMock()
            mock_sks.start = AsyncMock()
            mock_sks.stop = AsyncMock()
            mock_sks_cls.return_value = mock_sks

            task = asyncio.create_task(supervisor.start())
            # Let start() reach _main_done.wait()
            await asyncio.sleep(0.05)
            assert not task.done()

            # Simulate SIGINT: cancel the task (same as _request_shutdown)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # After CancelledError, the caller (run_telegram) would call stop_all()
        await supervisor.stop_all()
        assert supervisor._running is False


class TestHandleCrash:
    """Test _handle_crash() recovery logic."""

    async def test_main_crash_terminates(self, supervisor: AgentSupervisor) -> None:
        """Main agent crash on a fatal (non-transient) error terminates the supervisor."""
        health = AgentHealth(name="main")
        supervisor._health["main"] = health
        stack = MagicMock()

        _, _, should_return = await supervisor._handle_crash(
            "main", stack, health, 1, RuntimeError("fatal error")
        )
        assert should_return is True
        assert supervisor._main_done.is_set()

    async def test_main_transient_network_retries(self, supervisor: AgentSupervisor) -> None:
        """Main startup retries transient network errors with backoff and rebuild."""
        health = AgentHealth(name="main")
        supervisor._health["main"] = health
        stack = MagicMock()
        stack.shutdown = AsyncMock()

        exc = type("TelegramNetworkError", (Exception,), {})("connection reset")

        with (
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch.object(
                supervisor, "_rebuild_stack", new_callable=AsyncMock, return_value=MagicMock()
            ) as mock_rebuild,
        ):
            _stack, _count, should_return = await supervisor._handle_crash(
                "main", stack, health, 1, exc
            )

        assert should_return is False
        # _capped_backoff(1) = 5 seconds
        mock_sleep.assert_called_once_with(5)
        mock_rebuild.assert_called_once()
        assert not supervisor._main_done.is_set()
        assert not supervisor._main_ready.is_set()
        assert health.status == "starting"

    async def test_sub_agent_max_retries_exceeded(self, supervisor: AgentSupervisor) -> None:
        """After max retries, sub-agent is given up."""
        health = AgentHealth(name="sub1")
        supervisor._health["sub1"] = health
        stack = MagicMock()
        supervisor._stacks["main"] = MagicMock()
        supervisor._stacks["main"].config.allowed_user_ids = []

        retry_count = _MAX_RESTART_RETRIES + 1
        _, _, should_return = await supervisor._handle_crash(
            "sub1", stack, health, retry_count, RuntimeError("keeps crashing")
        )
        assert should_return is True

    async def test_sub_agent_recoverable(self, supervisor: AgentSupervisor) -> None:
        """Sub-agent crash with retries left triggers backoff and rebuild."""
        health = AgentHealth(name="sub1")
        supervisor._health["sub1"] = health
        stack = MagicMock()
        stack.shutdown = AsyncMock()

        with (
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch.object(
                supervisor, "_rebuild_stack", new_callable=AsyncMock, return_value=MagicMock()
            ),
        ):
            _new_stack, _new_count, should_return = await supervisor._handle_crash(
                "sub1", stack, health, 1, RuntimeError("transient error")
            )

        assert should_return is False
        # Backoff: 5 * 2^(1-1) = 5 seconds
        mock_sleep.assert_called_once_with(5)
        assert health.status == "starting"


class TestHandleRestartExit:
    """Test _handle_restart_exit() behavior."""

    async def test_main_restart_signals_done(self, supervisor: AgentSupervisor) -> None:
        health = AgentHealth(name="main")
        supervisor._health["main"] = health
        stack = MagicMock()

        _, should_return = await supervisor._handle_restart_exit("main", stack, health)
        assert should_return is True
        assert supervisor._main_done.is_set()

    async def test_sub_restart_does_hot_reload(self, supervisor: AgentSupervisor) -> None:
        health = AgentHealth(name="sub1")
        supervisor._health["sub1"] = health
        stack = MagicMock()
        stack.shutdown = AsyncMock()

        new_stack = MagicMock()
        with (
            patch.object(
                supervisor, "_rebuild_stack", new_callable=AsyncMock, return_value=new_stack
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result_stack, should_return = await supervisor._handle_restart_exit(
                "sub1", stack, health
            )

        assert should_return is False
        assert result_stack is new_stack
        stack.shutdown.assert_called_once()
        assert health.status == "starting"

    async def test_sub_restart_shuts_down_before_rebuild(self, supervisor: AgentSupervisor) -> None:
        """Hot-reload must shut down the old stack before rebuilding.

        Session takeover is handled by the new bot via ``getUpdates(offset=-1)``
        in ``TelegramBot.run()``, so no sleep delay is needed between
        shutdown and rebuild.
        """
        health = AgentHealth(name="sub1")
        supervisor._health["sub1"] = health
        stack = MagicMock()
        call_order: list[str] = []

        async def record_shutdown(_budget: object = None) -> None:
            call_order.append("shutdown")

        stack.shutdown = record_shutdown

        async def record_rebuild(_name: str, _old: object) -> MagicMock:
            call_order.append("rebuild")
            return MagicMock()

        with patch.object(supervisor, "_rebuild_stack", side_effect=record_rebuild):
            await supervisor._handle_restart_exit("sub1", stack, health)

        assert call_order == ["shutdown", "rebuild"]


class TestAbortAllAgents:
    """Test abort_all_agents() kills processes on every stack."""

    async def test_kills_across_all_stacks(self, supervisor: AgentSupervisor) -> None:
        """abort_all_agents kills processes on main + sub-agents."""
        main_registry = MagicMock()
        main_registry.kill_all_active = AsyncMock(return_value=2)
        main_orch = MagicMock()
        main_orch.process_registry = main_registry
        main_orch.bg_observer = None
        main_bot = MagicMock()
        main_bot.orchestrator = main_orch
        main_stack = MagicMock()
        main_stack.bot = main_bot

        sub_registry = MagicMock()
        sub_registry.kill_all_active = AsyncMock(return_value=1)
        sub_orch = MagicMock()
        sub_orch.process_registry = sub_registry
        sub_orch.bg_observer = None
        sub_bot = MagicMock()
        sub_bot.orchestrator = sub_orch
        sub_stack = MagicMock()
        sub_stack.bot = sub_bot

        supervisor._stacks = {"main": main_stack, "sub1": sub_stack}

        from ductor_bot.multiagent.bus import InterAgentBus

        supervisor._bus = InterAgentBus()

        killed = await supervisor.abort_all_agents()
        assert killed == 3
        main_registry.kill_all_active.assert_called_once()
        sub_registry.kill_all_active.assert_called_once()

    async def test_no_stacks_returns_zero(self, supervisor: AgentSupervisor) -> None:
        killed = await supervisor.abort_all_agents()
        assert killed == 0

    async def test_none_orchestrator_skipped(self, supervisor: AgentSupervisor) -> None:
        """Stacks with None orchestrator are safely skipped."""
        bot = MagicMock()
        bot.orchestrator = None
        stack = MagicMock()
        stack.bot = bot
        supervisor._stacks = {"main": stack}

        killed = await supervisor.abort_all_agents()
        assert killed == 0

    async def test_includes_bus_cancel(self, supervisor: AgentSupervisor) -> None:
        """Bus async tasks are also cancelled."""
        from ductor_bot.multiagent.bus import InterAgentBus

        supervisor._bus = InterAgentBus()
        supervisor._bus.cancel_all_async = AsyncMock(return_value=2)

        killed = await supervisor.abort_all_agents()
        assert killed == 2
        supervisor._bus.cancel_all_async.assert_called_once()


class TestIsTransient:
    """Test _is_transient() network-error classification."""

    def test_connection_error_is_transient(self) -> None:
        assert _is_transient(ConnectionError("reset")) is True

    def test_timeout_error_is_transient(self) -> None:
        assert _is_transient(TimeoutError("timed out")) is True

    def test_telegram_network_error_name_is_transient(self) -> None:
        """Matched by class name — supervisor must not import aiogram."""
        exc = type("TelegramNetworkError", (Exception,), {})("net down")
        assert _is_transient(exc) is True

    def test_fatal_error_is_not_transient(self) -> None:
        """Auth/conflict errors (and anything unknown) must terminate the supervisor."""
        exc = type("TelegramUnauthorizedError", (Exception,), {})("bad token")
        assert _is_transient(exc) is False


async def test_main_generation_requires_current_wired_injector(
    supervisor: AgentSupervisor,
) -> None:
    from ductor_bot.bus.bus import MessageBus

    stack = MagicMock()
    stack.is_main = True
    stack.bot._bus = MessageBus()
    orch = stack.bot.orchestrator
    supervisor._stacks["main"] = stack
    supervisor._main_generation = 1
    supervisor._inject_supervisor_hook(stack)
    hook = stack.bot.register_startup_hook.call_args.args[0]

    await hook()
    assert not supervisor.is_delivery_ready("main")
    assert supervisor._main_ready_gen is None

    stack.bot._bus.set_injector(orch)
    await hook()
    assert supervisor.is_delivery_ready("main")
    assert supervisor._main_ready_gen == 1

    replacement = MagicMock()
    replacement.is_main = True
    replacement.config = stack.config
    replacement.bot._bus = MessageBus()
    supervisor._stacks["main"] = replacement
    supervisor._main_generation = 2
    supervisor._main_ready_gen = None
    await hook()
    assert not supervisor.is_delivery_ready("main")
    supervisor._inject_supervisor_hook(replacement)
    new_hook = replacement.bot.register_startup_hook.call_args.args[0]
    replacement.bot._bus.set_injector(replacement.bot.orchestrator)
    await new_hook()
    assert supervisor.is_delivery_ready("main")


async def test_main_rebuild_invalidates_previous_generation(
    supervisor: AgentSupervisor,
) -> None:
    old_stack = MagicMock()
    old_stack.is_main = True
    new_stack = MagicMock()
    supervisor._main_generation = 1
    supervisor._main_ready_gen = 1
    supervisor._stacks["main"] = old_stack
    with patch(
        "ductor_bot.multiagent.supervisor.AgentStack.create",
        new_callable=AsyncMock,
        return_value=new_stack,
    ):
        assert await supervisor._rebuild_stack("main", old_stack) is new_stack
    assert supervisor._main_generation == 2
    assert supervisor._main_ready_gen is None
    assert not supervisor.is_delivery_ready("main")


async def test_main_fatal_invalidates_readiness(
    supervisor: AgentSupervisor,
) -> None:
    health = AgentHealth(name="main")
    supervisor._health["main"] = health
    supervisor._main_generation = 1
    supervisor._main_ready_gen = 1
    assert supervisor.is_delivery_ready("main")
    await supervisor._handle_crash("main", MagicMock(), health, 1, RuntimeError("fatal"))
    assert supervisor._main_fatal
    assert supervisor._main_ready_gen is None
    assert not supervisor.is_delivery_ready("main")
    assert supervisor._main_ready.is_set()


def test_subagent_readiness_requires_running_orchestrator(
    supervisor: AgentSupervisor,
) -> None:
    stack = MagicMock()
    stack.config.transport = "telegram"
    stack.bot.orchestrator = None
    health = AgentHealth(name="sub")
    supervisor._stacks["sub"] = stack
    supervisor._health["sub"] = health
    assert not supervisor.is_delivery_ready("sub")
    health.mark_running()
    assert not supervisor.is_delivery_ready("sub")
    stack.bot.orchestrator = MagicMock()
    assert supervisor.is_delivery_ready("sub")
    stack.config.transport = "headless"
    assert not supervisor.is_delivery_ready("sub")
    stack.config.transport = "telegram"
    health.mark_stopped()
    assert not supervisor.is_delivery_ready("sub")


async def test_main_transient_crash_revokes_readiness_before_backoff(
    supervisor: AgentSupervisor,
) -> None:
    health = AgentHealth(name="main")
    supervisor._health["main"] = health
    supervisor._main_generation = 1
    supervisor._main_ready_gen = 1
    assert supervisor.is_delivery_ready("main")

    async def inspect_backoff(_seconds: int) -> None:
        assert supervisor._main_ready_gen is None
        assert not supervisor.is_delivery_ready("main")

    with (
        patch("asyncio.sleep", side_effect=inspect_backoff),
        patch.object(
            supervisor, "_rebuild_stack", new_callable=AsyncMock, return_value=MagicMock(),
        ),
    ):
        await supervisor._handle_crash("main", MagicMock(), health, 1, ConnectionError())


@pytest.mark.parametrize("action", ["start", "restart"])
async def test_manual_start_returns_actual_failure(
    supervisor: AgentSupervisor, tmp_path: Path, action: str,
) -> None:
    (tmp_path / "agents.json").write_text(json.dumps([
        {"name": "sub1", "transport": "matrix"},
    ]))
    if action == "restart":
        stack = MagicMock()
        stack.shutdown = AsyncMock()
        supervisor._stacks["sub1"] = stack
    result = await (
        supervisor.start_agent_by_name("sub1")
        if action == "start" else supervisor.restart_agent("sub1")
    )
    assert result == "Agent 'sub1' not started: item #0 config error (ValueError)"
    assert "sub1" not in supervisor.stacks


@pytest.mark.parametrize("action", ["start", "restart"])
async def test_invalid_registry_entry_returns_exclusion_reason(
    supervisor: AgentSupervisor, tmp_path: Path, action: str,
) -> None:
    (tmp_path / "agents.json").write_text(json.dumps([
        {"name": "sub1", "allowed_user_ids": "invalid"},
    ]))
    supervisor._excluded["sub1"] = "config error (ValidationError)"
    result = await (
        supervisor.start_agent_by_name("sub1")
        if action == "start" else supervisor.restart_agent("sub1")
    )
    assert result == "Agent 'sub1' not started: config error (ValidationError)"


@pytest.mark.parametrize("action", ["start", "restart"])
async def test_manual_start_checks_raw_transport_dependencies(
    supervisor: AgentSupervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    from ductor_bot.infra import feature_deps

    (tmp_path / "agents.json").write_text(json.dumps([{
        "name": "sub1", "transport": "telegram", "telegram_token": "tok:1",
        "transports": ["matrix"],
    }]))
    original_import = feature_deps.importlib.import_module

    def without_nio(name: str, package: str | None = None):
        if name == "nio":
            raise ImportError(name)
        return original_import(name, package)

    monkeypatch.setattr(feature_deps.importlib, "import_module", without_nio)
    supervisor._stacks["main"] = MagicMock()
    supervisor._notify_main_agent = AsyncMock()
    if action == "restart":
        old = MagicMock()
        old.shutdown = AsyncMock()
        supervisor._stacks["sub1"] = old

    with patch(
        "ductor_bot.multiagent.supervisor.AgentStack.create", new_callable=AsyncMock,
    ) as create:
        result = await (
            supervisor.start_agent_by_name("sub1")
            if action == "start" else supervisor.restart_agent("sub1")
        )

    assert result.startswith("Agent 'sub1' not started: ")
    assert "nio" in result
    assert "sub1" not in supervisor.stacks
    assert "nio" in supervisor._excluded["sub1"]
    create.assert_not_awaited()
    supervisor._notify_main_agent.assert_awaited_once()


async def test_config_update_failure_cleans_created_stack(
    supervisor: AgentSupervisor, tmp_path: Path,
) -> None:
    (tmp_path / "agents.json").write_text(json.dumps([
        {"name": "sub1", "transport": "telegram", "telegram_token": "tok:1"},
    ]))
    config_path = tmp_path / "agents" / "sub1" / "config" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}")
    stack = MagicMock()
    stack.shutdown = AsyncMock()
    supervisor._stacks["main"] = MagicMock()
    supervisor._notify_main_agent = AsyncMock()

    with (
        patch(
            "ductor_bot.multiagent.supervisor.AgentStack.create",
            new_callable=AsyncMock, return_value=stack,
        ),
        patch(
            "ductor_bot.multiagent.supervisor.update_config_file_async",
            new_callable=AsyncMock, side_effect=OSError("synthetic"),
        ),
    ):
        result = await supervisor.start_agent_by_name("sub1")

    assert result == "Agent 'sub1' not started: agent start error (OSError)"
    stack.shutdown.assert_awaited_once()
    assert "sub1" not in supervisor._stacks
    assert "sub1" not in supervisor._tasks
    assert "OSError" in supervisor._excluded["sub1"]


async def test_watcher_continues_after_hot_reload_callback_failure(
    supervisor: AgentSupervisor, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    import os

    from ductor_bot.infra.file_watcher import FileWatcher

    path = tmp_path / "agents.json"
    path.write_text("[]")
    base_ns = path.stat().st_mtime_ns
    failed = asyncio.Event()
    started = asyncio.Event()
    original_callback = supervisor._on_agents_changed

    async def callback() -> None:
        if not failed.is_set():
            failed.set()
            raise OSError("synthetic")
        await original_callback()

    async def create_stack(*_args, **_kwargs):
        started.set()
        return MagicMock()

    watcher = FileWatcher(path, callback, interval=0.01)
    await watcher.update_mtime()
    supervisor._notify_main_agent = AsyncMock()
    try:
        with (
            patch(
                "ductor_bot.multiagent.supervisor.AgentStack.create",
                new_callable=AsyncMock, side_effect=create_stack,
            ),
            patch.object(supervisor, "_supervised_run", new_callable=AsyncMock, return_value=0),
        ):
            await watcher.start()
            path.write_text(json.dumps([
                {"name": "sub1", "transport": "telegram", "telegram_token": "tok:1"},
            ]))
            os.utime(path, ns=(base_ns + 1_000_000_000, base_ns + 1_000_000_000))
            await asyncio.wait_for(failed.wait(), 1)
            path.write_text(json.dumps([
                {"name": "sub1", "transport": "telegram", "telegram_token": "tok:1"},
            ]))
            os.utime(path, ns=(base_ns + 2_000_000_000, base_ns + 2_000_000_000))
            await asyncio.wait_for(started.wait(), 1)
    finally:
        await watcher.stop()

    assert "sub1" in supervisor.stacks
    assert "File watcher callback failed" in caplog.text


async def test_shared_sync_failure_unregisters_created_stack(
    supervisor: AgentSupervisor, tmp_path: Path,
) -> None:
    (tmp_path / "agents.json").write_text(json.dumps([
        {"name": "sub1", "transport": "telegram", "telegram_token": "tok:1"},
    ]))
    stack = MagicMock()
    stack.shutdown = AsyncMock()
    supervisor._shared_knowledge = MagicMock()
    supervisor._shared_knowledge.sync_agent = AsyncMock(side_effect=OSError("synthetic"))
    supervisor._notify_main_agent = AsyncMock()
    from ductor_bot.multiagent.bus import InterAgentBus
    supervisor._bus = InterAgentBus()

    with (
        patch(
            "ductor_bot.multiagent.supervisor.AgentStack.create",
            new_callable=AsyncMock, return_value=stack,
        ),
        patch.object(supervisor, "_supervised_run", new_callable=AsyncMock, return_value=0),
    ):
        result = await supervisor.start_agent_by_name("sub1")

    assert result == "Agent 'sub1' not started: agent start error (OSError)"
    stack.shutdown.assert_awaited_once()
    assert "sub1" not in supervisor._stacks
    assert "sub1" not in supervisor._tasks
    assert "sub1" not in supervisor._bus.list_agents()
    assert not supervisor._bus.has_async_result_handler("sub1")


async def test_restart_replaces_async_result_handler(
    supervisor: AgentSupervisor, tmp_path: Path,
) -> None:
    from ductor_bot.multiagent.bus import InterAgentBus

    (tmp_path / "agents.json").write_text(json.dumps([
        {"name": "sub1", "transport": "telegram", "telegram_token": "tok:1"},
    ]))
    old = MagicMock()
    old.shutdown = AsyncMock()
    new = MagicMock()
    new.shutdown = AsyncMock()
    supervisor._stacks["sub1"] = old
    supervisor._bus = InterAgentBus()
    supervisor._bus.register("sub1", old)
    supervisor._bus.set_async_result_handler("sub1", old.bot.on_async_interagent_result)

    async def create_after_cleanup(*_args: object, **_kwargs: object) -> MagicMock:
        assert not supervisor._bus.has_async_result_handler("sub1")
        assert "sub1" not in supervisor._bus.list_agents()
        return new

    with (
        patch(
            "ductor_bot.multiagent.supervisor.AgentStack.create",
            new_callable=AsyncMock, side_effect=create_after_cleanup,
        ),
        patch.object(supervisor, "_supervised_run", new_callable=AsyncMock, return_value=0),
    ):
        result = await supervisor.restart_agent("sub1")

    assert result == "Agent 'sub1' restarted."
    old.shutdown.assert_awaited_once()
    assert supervisor.stacks["sub1"] is new
    assert supervisor._bus.has_async_result_handler("sub1")


async def test_effective_memory_lint_is_written_to_the_sub_config_file(
    supervisor: AgentSupervisor, tmp_path: Path,
) -> None:
    (tmp_path / "agents.json").write_text(json.dumps([
        {
            "name": "sub1", "transport": "telegram", "telegram_token": "tok:1",
            "memory_lint": {"mode": "full", "schedule": "0 9 * * 1"},
        },
    ]))
    config_path = tmp_path / "agents" / "sub1" / "config" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}")
    stack = MagicMock()
    stack.shutdown = AsyncMock()
    supervisor._stacks["main"] = MagicMock()
    supervisor._notify_main_agent = AsyncMock()

    with (
        patch(
            "ductor_bot.multiagent.supervisor.AgentStack.create",
            new_callable=AsyncMock, return_value=stack,
        ),
        patch(
            "ductor_bot.multiagent.supervisor.update_config_file_async",
            new_callable=AsyncMock, side_effect=OSError("synthetic"),
        ) as update,
    ):
        await supervisor.start_agent_by_name("sub1")

    lint = update.await_args.kwargs["memory_lint"]
    assert (lint["mode"], lint["schedule"], lint["every_hours"]) == ("full", "0 9 * * 1", 168)

