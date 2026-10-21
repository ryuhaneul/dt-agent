"""Task receipts use agent settings even when foreground CLI settings omit them."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig
from ductor_bot.config_reload import ConfigReloader
from ductor_bot.multiagent.stack import AgentStack
from ductor_bot.multiagent.supervisor import AgentSupervisor
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.tasks import hub as hub_module
from ductor_bot.tasks.hub import TaskHub
from ductor_bot.tasks.models import TaskSubmit
from ductor_bot.tasks.registry import TaskRegistry

STAMP = 1500000000.0


@pytest.mark.parametrize("name", ["main", "sub"])
@pytest.mark.parametrize("mode", ["submit", "resume", "answer"])
@pytest.mark.parametrize("hot_reload", [False, True])
async def test_supervisor_wiring_uses_real_agent_timezone_for_task_input(
    workspace, monkeypatch, name, mode, hot_reload
):
    paths, _ = workspace
    # The hot-reload variant starts in UTC and switches through ConfigReloader.
    config = AgentConfig(user_timezone="UTC" if hot_reload else "Asia/Seoul")
    orch = Orchestrator(config, paths, agent_name=name)
    # Foreground CLI settings carry the configured agent timezone at startup.
    assert orch.cli_service._config.user_timezone == config.user_timezone
    clock = SimpleNamespace(wall=STAMP)
    monkeypatch.setattr(
        hub_module, "time", SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: 100.0)
    )
    execute = AsyncMock(return_value=AgentResponse(result="ok", session_id="synthetic"))
    monkeypatch.setattr(orch.cli_service, "execute", execute)
    monkeypatch.setattr(orch.cli_service, "resolve_provider", lambda _: ("claude", "sonnet"))
    registry = TaskRegistry(paths.tasks_registry_path, paths.tasks_dir)
    # Main's fallback deliberately differs; a sub must receive its own setting.
    supervisor = AgentSupervisor(AgentConfig(user_timezone="UTC"))
    hub = TaskHub(registry, paths, config=config.tasks)
    supervisor._task_hub = hub
    bot = SimpleNamespace(
        orchestrator=orch, on_task_result=AsyncMock(), on_task_question=AsyncMock()
    )
    supervisor._wire_task_hub(AgentStack(name, config, paths, bot))
    if hot_reload:
        config_path = paths.config_path
        updated = config.model_copy(update={"user_timezone": "Asia/Seoul"})
        config_path.write_text(json.dumps(updated.model_dump(mode="json")), encoding="utf-8")
        reloader = ConfigReloader(config_path, orch.config)
        reloader._last_mtime = 0.0  # open the mtime gate like the poll loop would
        await reloader._check()
        assert orch.config.user_timezone == "Asia/Seoul"
    try:
        task_id = hub.submit(TaskSubmit(42, "Inspect synthetic system", 1, None, name))
        await hub._in_flight[task_id].asyncio_task
        await asyncio.sleep(0)
        if mode != "submit":
            if mode == "answer":
                registry.update_status(task_id, "waiting", last_question="Which target?")
            clock.wall += 60
            hub.resume(task_id, "Synthetic parent answer" if mode == "answer" else "Continue")
            await hub._in_flight[task_id].asyncio_task
        request = execute.await_args.args[0]
        minute = "40" if mode == "submit" else "41"
        assert request.cli_prompt.splitlines()[0] == f"[Received: 2017-07-14 11:{minute}:00 KST]"
        assert request.cli_prompt.count("[Received:") == 1
        assert "[Received:" not in request.prompt
        assert "[Received:" not in paths.tasks_registry_path.read_text()
    finally:
        await hub.shutdown()


async def test_single_agent_explicit_timezone_and_missing_or_invalid_fallback(
    workspace, monkeypatch
):
    paths, _ = workspace
    monkeypatch.setattr(
        hub_module, "time", SimpleNamespace(time=lambda: STAMP, monotonic=lambda: 100.0)
    )
    cli = SimpleNamespace(
        execute=AsyncMock(return_value=AgentResponse(result="ok")),
        resolve_provider=lambda _: ("claude", "sonnet"),
    )
    for supplied, zone, expected in [
        (True, "Asia/Seoul", "2017-07-14 11:40:00 KST"),
        (False, None, "2017-07-14 02:40:00 UTC"),
        (True, "invalid/zone", "2017-07-14 02:40:00 UTC"),
        (True, "", "2017-07-14 02:40:00 UTC"),
        (True, None, "2017-07-14 02:40:00 UTC"),
    ]:
        registry = TaskRegistry(paths.tasks_registry_path, paths.tasks_dir)
        kwargs = {"user_timezone": zone} if supplied else {}
        hub = TaskHub(registry, paths, config=AgentConfig().tasks, cli_service=cli, **kwargs)
        try:
            task_id = hub.submit(TaskSubmit(42, "Synthetic instruction", 1, None, "main"))
            await hub._in_flight[task_id].asyncio_task
            assert (
                cli.execute.await_args.args[0].cli_prompt.splitlines()[0]
                == f"[Received: {expected}]"
            )
        finally:
            await hub.shutdown()


async def test_callable_fallback_timezone_follows_later_changes(workspace, monkeypatch):
    paths, _ = workspace
    monkeypatch.setattr(
        hub_module, "time", SimpleNamespace(time=lambda: STAMP, monotonic=lambda: 100.0)
    )
    cli = SimpleNamespace(
        execute=AsyncMock(return_value=AgentResponse(result="ok")),
        resolve_provider=lambda _: ("claude", "sonnet"),
    )
    zone = {"value": "UTC"}
    registry = TaskRegistry(paths.tasks_registry_path, paths.tasks_dir)
    hub = TaskHub(
        registry,
        paths,
        config=AgentConfig().tasks,
        cli_service=cli,
        user_timezone=lambda: zone["value"],
    )
    try:
        for value, expected in [
            ("UTC", "02:40:00 UTC"),
            ("Asia/Seoul", "11:40:00 KST"),
            ("bad/zone", "02:40:00 UTC"),
        ]:
            zone["value"] = value
            task_id = hub.submit(TaskSubmit(42, "Synthetic instruction", 1, None, "main"))
            await hub._in_flight[task_id].asyncio_task
            first_line = cli.execute.await_args.args[0].cli_prompt.splitlines()[0]
            assert first_line == f"[Received: 2017-07-14 {expected}]"
    finally:
        await hub.shutdown()
