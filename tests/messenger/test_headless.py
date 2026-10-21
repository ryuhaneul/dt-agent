"""Headless sub-agent startup, silent delivery, and shared session locking."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.__main__ import _validate_transports
from ductor_bot.bus.bus import MessageBus
from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig, MemoryCompactionConfig, MemoryFlushConfig
from ductor_bot.messenger.headless import HeadlessBot
from ductor_bot.messenger.protocol import BotProtocol
from ductor_bot.messenger.registry import create_bot
from ductor_bot.multiagent.models import SubAgentConfig, merge_sub_agent_config
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.observers import ObserverManager
from ductor_bot.session.key import SessionKey
from ductor_bot.session.manager import ProviderSessionData, SessionData
from ductor_bot.workspace.paths import DuctorPaths


def test_credential_free_sub_agent_uses_headless_bot(tmp_path: Path) -> None:
    main = AgentConfig(ductor_home=str(tmp_path / "main"), transports=["telegram"])
    sub = SubAgentConfig(name="worker")
    config = merge_sub_agent_config(main, sub, tmp_path / "agents" / "worker")

    assert config.transports == ["headless"]
    bot = create_bot(config, agent_name="worker")
    assert isinstance(bot, HeadlessBot)
    assert isinstance(bot, BotProtocol)
    assert bot.config is config


async def test_headless_run_wires_shared_lock_pool_and_serializes_flush(tmp_path: Path) -> None:
    config = AgentConfig(
        ductor_home=str(tmp_path / "worker"),
        transports=["headless"],
        memory_flush=MemoryFlushConfig(dedup_seconds=0),
        memory_compaction=MemoryCompactionConfig(enabled=False),
    )
    bus = MessageBus()
    bot = create_bot(config, agent_name="worker", bus=bus)
    assert isinstance(bot, HeadlessBot)
    orch = Orchestrator(config, DuctorPaths(ductor_home=tmp_path / "worker"), agent_name="worker")
    orch.shutdown = AsyncMock()  # type: ignore[method-assign]
    ready = asyncio.Event()

    async def startup_hook() -> None:
        assert bot.orchestrator is orch
        assert orch._memory_flusher is not None
        assert orch._memory_flusher._lock_pool is bus.lock_pool
        ready.set()

    bot.register_startup_hook(startup_hook)
    with patch.object(Orchestrator, "create", new=AsyncMock(return_value=orch)) as create:
        running = asyncio.create_task(bot.run())
        await asyncio.wait_for(ready.wait(), timeout=1)
        create.assert_awaited_once_with(config, agent_name="worker", lock_pool=bus.lock_pool)

        flusher = orch._memory_flusher
        assert flusher is not None
        active = 0
        peak = 0
        entered = asyncio.Event()
        release = asyncio.Event()

        async def execute(_request: object) -> AgentResponse:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            entered.set()
            try:
                await release.wait()
            finally:
                active -= 1
            return AgentResponse(result="")

        orch._cli_service.execute = AsyncMock(side_effect=execute)  # type: ignore[method-assign]
        key = SessionKey(chat_id=101)
        session = SessionData(chat_id=101, provider="claude", model="opus")
        session.provider_sessions["claude"] = ProviderSessionData(
            session_id="dummy-session", message_count=3
        )

        async def flush() -> None:
            flusher.mark_boundary(key)
            await flusher.maybe_flush(key, session)

        first = asyncio.create_task(flush())
        second = asyncio.create_task(flush())
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

        assert peak == 1
        assert orch._cli_service.execute.await_count == 2
        await bot.shutdown()
        assert await asyncio.wait_for(running, timeout=1) == 0
        orch.shutdown.assert_awaited_once()


async def test_headless_observers_skip_chat_outputs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager = ObserverManager(
        AgentConfig(ductor_home=str(tmp_path), transports=["headless"]),
        DuctorPaths(ductor_home=tmp_path),
    )
    cron = SimpleNamespace(start=AsyncMock())
    webhook = SimpleNamespace(start=AsyncMock())
    manager.cron = cron
    manager.webhook = webhook
    manager.heartbeat.start = AsyncMock()  # type: ignore[method-assign]
    manager.cleanup.start = AsyncMock()  # type: ignore[method-assign]
    with (
        caplog.at_level("INFO", logger="ductor_bot.orchestrator.observers"),
        patch("ductor_bot.orchestrator.observers.watch_rule_files", new_callable=AsyncMock),
        patch("ductor_bot.orchestrator.observers.watch_skill_sync", new_callable=AsyncMock),
    ):
        await manager.start_all()
        await asyncio.sleep(0)

    assert "Headless agent: cron/heartbeat/webhook disabled" in caplog.text
    assert manager._rule_sync_task is not None
    assert manager._skill_sync_task is not None
    cron.start.assert_not_awaited()
    manager.heartbeat.start.assert_not_awaited()
    webhook.start.assert_not_awaited()
    manager.cleanup.start.assert_awaited_once()


async def test_headless_notifications_are_silent(tmp_path: Path) -> None:
    bot = HeadlessBot(AgentConfig(ductor_home=str(tmp_path), transports=["headless"]))
    await bot.notification_service.notify(123, "dummy")
    await bot.notification_service.notify_all("dummy")


def test_main_rejects_headless_transport() -> None:
    with pytest.raises(SystemExit) as exc:
        _validate_transports(AgentConfig(transports=["headless"]))
    assert exc.value.code == 1
