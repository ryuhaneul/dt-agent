"""Tests for the observer manager."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.config import AgentConfig, update_config_file
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.observers import ObserverManager
from ductor_bot.workspace.paths import DuctorPaths


def _manager() -> ObserverManager:
    return ObserverManager(MagicMock(), MagicMock())


async def test_skips_observers_for_missing_optional_clis() -> None:
    manager = _manager()

    with (
        patch("ductor_bot.orchestrator.observers.GeminiCacheObserver") as gemini_cls,
        patch("ductor_bot.orchestrator.observers.GrokCacheObserver") as grok_cls,
        patch("ductor_bot.orchestrator.observers.CodexCacheObserver") as codex_cls,
    ):
        cache = await manager.init_model_caches(
            installed_providers=frozenset({"claude"}),
            on_gemini_refresh=MagicMock(),
            on_grok_refresh=MagicMock(),
        )

    gemini_cls.assert_not_called()
    grok_cls.assert_not_called()
    codex_cls.assert_not_called()
    assert manager.gemini_cache_obs is None
    assert manager.grok_cache_obs is None
    assert manager.codex_cache_obs is None
    assert cache.models == []


async def test_starts_observers_for_installed_optional_clis() -> None:
    manager = _manager()
    gemini_observer = MagicMock()
    gemini_observer.start = AsyncMock()
    grok_observer = MagicMock()
    grok_observer.start = AsyncMock()
    codex_observer = MagicMock()
    codex_observer.start = AsyncMock()
    codex_observer.get_cache.return_value = CodexModelCache("", [])

    with (
        patch(
            "ductor_bot.orchestrator.observers.GeminiCacheObserver",
            return_value=gemini_observer,
        ),
        patch(
            "ductor_bot.orchestrator.observers.GrokCacheObserver",
            return_value=grok_observer,
        ),
        patch(
            "ductor_bot.orchestrator.observers.CodexCacheObserver",
            return_value=codex_observer,
        ),
    ):
        await manager.init_model_caches(
            installed_providers=frozenset({"claude", "codex", "gemini", "grok"}),
            on_gemini_refresh=MagicMock(),
            on_grok_refresh=MagicMock(),
        )

    gemini_observer.start.assert_awaited_once()
    grok_observer.start.assert_awaited_once()
    assert manager.gemini_cache_obs is gemini_observer
    assert manager.grok_cache_obs is grok_observer
    assert manager.codex_cache_obs is codex_observer
    codex_observer.start.assert_awaited_once()


async def test_installed_but_unauthenticated_provider_still_gets_observer() -> None:
    """INSTALLED (not AUTHENTICATED) is enough — the set is auth-detection based."""
    manager = _manager()
    codex_observer = MagicMock()
    codex_observer.start = AsyncMock()
    codex_observer.get_cache.return_value = CodexModelCache("", [])

    with (
        patch("ductor_bot.orchestrator.observers.GeminiCacheObserver") as gemini_cls,
        patch("ductor_bot.orchestrator.observers.GrokCacheObserver") as grok_cls,
        patch(
            "ductor_bot.orchestrator.observers.CodexCacheObserver",
            return_value=codex_observer,
        ),
    ):
        await manager.init_model_caches(
            installed_providers=frozenset({"claude", "codex"}),
            on_gemini_refresh=MagicMock(),
            on_grok_refresh=MagicMock(),
        )

    gemini_cls.assert_not_called()
    grok_cls.assert_not_called()
    codex_observer.start.assert_awaited_once()


@pytest.mark.parametrize(
    ("agent_name", "heartbeat_block", "expected_enabled"),
    [
        ("worker", "example", False),
        ("worker", "missing", False),
        ("worker", "partial", False),
        ("main", "example", True),
    ],
)
async def test_agent_heartbeat_config_reload(
    tmp_path: Path, agent_name: str, heartbeat_block: str, expected_enabled: bool
) -> None:
    paths = DuctorPaths(tmp_path)
    disk_config = json.loads(paths.config_example_path.read_text(encoding="utf-8"))
    disk_config["model"] = "sonnet"
    cfg = AgentConfig.model_validate({**disk_config, "heartbeat": {"enabled": False}})
    if heartbeat_block == "missing":
        disk_config.pop("heartbeat")
    elif heartbeat_block == "partial":
        disk_config["heartbeat"] = {"interval_minutes": 60}
    paths.config_path.parent.mkdir()
    paths.config_path.write_text(json.dumps(disk_config), encoding="utf-8")

    orch = Orchestrator(cfg, paths, agent_name=agent_name)
    manager = orch._observers
    heartbeat = manager.heartbeat
    heartbeat.set_heartbeat_handler(AsyncMock())
    await heartbeat._ensure_loaded()
    on_hot = MagicMock(wraps=orch._on_config_hot_reload)
    await manager.start_config_reloader(on_hot_reload=on_hot, on_restart_needed=MagicMock())
    try:
        reloader = manager._config_reloader
        assert reloader is not None
        assert not heartbeat.running
        update_config_file(paths.config_path, model="opus")
        new_mtime = reloader._last_mtime + 1
        os.utime(paths.config_path, (new_mtime, new_mtime))
        await reloader._check()
        await asyncio.sleep(0)

        assert cfg.model == "opus"
        assert heartbeat.running is expected_enabled
        assert cfg.heartbeat.enabled is expected_enabled
        on_hot.assert_called_once()
        assert ("heartbeat" in on_hot.call_args.args[1]) is expected_enabled
    finally:
        await manager.stop_all()
