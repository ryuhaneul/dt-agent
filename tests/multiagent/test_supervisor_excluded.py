"""Sub-agent dependency exclusions do not stop the main agent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.config import AgentConfig
from ductor_bot.multiagent.models import SubAgentConfig
from ductor_bot.multiagent.supervisor import AgentSupervisor


@pytest.mark.parametrize(
    "reason", [
        "feature matrix is on but module nio is missing",
        "connection details or config error (ValueError)",
    ],
)
async def test_prechecked_sub_is_excluded_after_main_start(
    tmp_path: Path, reason: str,
) -> None:
    supervisor = AgentSupervisor(
        AgentConfig(ductor_home=str(tmp_path)), excluded_subs={"sub": reason},
    )
    supervisor._stacks["main"] = MagicMock()
    supervisor._registry.load = MagicMock(return_value=[
        SubAgentConfig(name="sub", transport="matrix"),
    ])
    supervisor._notify_main_agent = AsyncMock()
    with patch(
        "ductor_bot.multiagent.supervisor.AgentStack.create", new_callable=AsyncMock,
    ) as create:
        await supervisor._sync_sub_agents()
    assert "main" in supervisor.stacks
    assert "sub" not in supervisor.stacks
    create.assert_not_awaited()
    supervisor._notify_main_agent.assert_awaited_once()
    assert reason in supervisor._notify_main_agent.await_args.args[0]


async def test_hot_added_sub_missing_dependency_is_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    supervisor._stacks["main"] = MagicMock()
    supervisor._notify_main_agent = AsyncMock()
    monkeypatch.setattr(
        "ductor_bot.multiagent.supervisor.missing_for", lambda _config: ["nio"],
    )
    sub = SubAgentConfig(
        name="sub", transport="matrix",
        matrix={
            "homeserver": "https://matrix.invalid", "user_id": "@sub:invalid",
            "password": "synthetic",
        },
    )
    with patch(
        "ductor_bot.multiagent.supervisor.AgentStack.create", new_callable=AsyncMock,
    ) as create:
        await supervisor._start_sub_agent(sub)
    create.assert_not_awaited()
    assert "sub" in supervisor._excluded
    supervisor._notify_main_agent.assert_awaited_once()


async def test_supervisor_start_runs_main_and_skips_excluded_sub(
    tmp_path: Path,
) -> None:
    config = AgentConfig(ductor_home=str(tmp_path), tasks={"enabled": False})
    supervisor = AgentSupervisor(config, excluded_subs={"sub": "module nio is missing"})
    supervisor._registry.load = MagicMock(return_value=[
        SubAgentConfig(name="sub", transport="matrix"),
    ])
    supervisor._main_ready.set()
    supervisor._main_done.set()
    main_stack = MagicMock()
    main_stack.bot.on_async_interagent_result = AsyncMock()
    supervisor._notify_main_agent = AsyncMock()
    with (
        patch(
            "ductor_bot.multiagent.supervisor.AgentStack.create",
            new_callable=AsyncMock, return_value=main_stack,
        ) as create,
        patch(
            "ductor_bot.multiagent.internal_api.InternalAgentAPI.start",
            new_callable=AsyncMock, return_value=True,
        ),
        patch(
            "ductor_bot.multiagent.shared_knowledge.SharedKnowledgeSync.start",
            new_callable=AsyncMock,
        ),
        patch(
            "ductor_bot.multiagent.supervisor.FileWatcher.start",
            new_callable=AsyncMock,
        ),
        patch.object(supervisor, "_supervised_run", new_callable=AsyncMock, return_value=0),
    ):
        assert await supervisor.start() == 0
    create.assert_awaited_once_with("main", config, is_main=True)
    assert "main" in supervisor.stacks
    assert "sub" not in supervisor.stacks
    supervisor._notify_main_agent.assert_awaited_once()


async def test_excluded_sub_starts_after_agents_file_is_fixed(tmp_path: Path) -> None:
    import asyncio
    import json

    from ductor_bot.infra.feature_deps import check_feature_deps
    from ductor_bot.workspace.paths import resolve_paths

    agents_path = tmp_path / "agents.json"
    agents_path.write_text(json.dumps([{"name": "sub", "transport": "telegram"}]))
    config = AgentConfig(ductor_home=str(tmp_path))
    excluded = check_feature_deps(config, resolve_paths(ductor_home=tmp_path))
    supervisor = AgentSupervisor(config, excluded_subs=excluded)
    supervisor._stacks["main"] = MagicMock()
    supervisor._notify_main_agent = AsyncMock()
    stack = MagicMock()
    with (
        patch(
            "ductor_bot.multiagent.supervisor.AgentStack.create",
            new_callable=AsyncMock, return_value=stack,
        ) as create,
        patch.object(supervisor, "_supervised_run", new_callable=AsyncMock, return_value=0),
    ):
        await supervisor._on_agents_changed()
        create.assert_not_awaited()
        supervisor._notify_main_agent.assert_not_awaited()

        agents_path.write_text(json.dumps([{
            "name": "sub", "transport": "telegram", "telegram_token": "synthetic",
        }]))
        await supervisor._on_agents_changed()
        await asyncio.sleep(0)

    create.assert_awaited_once()
    assert supervisor.stacks["sub"] is stack
    assert "sub" not in supervisor._excluded
    supervisor._notify_main_agent.assert_not_awaited()


async def test_excluded_sub_repeated_reason_notifies_once_and_changed_reason_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    import json

    from ductor_bot.infra import feature_deps

    agents_path = tmp_path / "agents.json"
    agents_path.write_text(json.dumps([{"name": "sub", "transport": "telegram"}]))
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    supervisor._stacks["main"] = MagicMock()
    supervisor._notify_main_agent = AsyncMock()
    original_import = importlib.import_module

    def import_without_nio(name: str, package: str | None = None) -> object:
        if name == "nio":
            raise ImportError(name)
        return original_import(name, package)

    monkeypatch.setattr(feature_deps.importlib, "import_module", import_without_nio)
    with patch(
        "ductor_bot.multiagent.supervisor.AgentStack.create", new_callable=AsyncMock,
    ) as create:
        await supervisor._on_agents_changed()
        await supervisor._on_agents_changed()
        assert supervisor._notify_main_agent.await_count == 1
        assert "ValueError" in supervisor._excluded["sub"]

        agents_path.write_text(json.dumps([{
            "name": "sub", "transport": "matrix",
            "matrix": {
                "homeserver": "https://matrix.invalid", "user_id": "@sub:invalid",
                "password": "synthetic",
            },
        }]))
        await supervisor._on_agents_changed()
        await supervisor._on_agents_changed()

    create.assert_not_awaited()
    assert supervisor._notify_main_agent.await_count == 2
    assert "nio" in supervisor._excluded["sub"]
    assert "nio" in supervisor._notify_main_agent.await_args.args[0]


async def test_running_sub_is_excluded_then_recovers_when_dependency_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from ductor_bot.infra import feature_deps

    agents_path = tmp_path / "agents.json"
    declaration = {
        "name": "sub", "transport": "telegram", "telegram_token": "synthetic",
    }
    agents_path.write_text(json.dumps([declaration]))
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    supervisor._stacks["main"] = MagicMock()
    supervisor._notify_main_agent = AsyncMock()
    stacks = [MagicMock(), MagicMock()]
    for stack in stacks:
        stack.shutdown = AsyncMock()
    original_import = feature_deps.importlib.import_module

    def without_nio(name: str, package: str | None = None) -> object:
        if name == "nio":
            raise ImportError(name)
        return original_import(name, package)

    monkeypatch.setattr(feature_deps.importlib, "import_module", without_nio)
    with (
        patch(
            "ductor_bot.multiagent.supervisor.AgentStack.create",
            new_callable=AsyncMock, side_effect=stacks,
        ) as create,
        patch.object(supervisor, "_supervised_run", new_callable=AsyncMock, return_value=0),
    ):
        await supervisor._on_agents_changed()
        assert supervisor.stacks["sub"] is stacks[0]

        agents_path.write_text(json.dumps([{**declaration, "transports": ["matrix"]}]))
        await supervisor._on_agents_changed()
        assert "sub" not in supervisor.stacks
        assert "nio" in supervisor._excluded["sub"]
        stacks[0].shutdown.assert_awaited_once()
        supervisor._notify_main_agent.assert_awaited_once()

        await supervisor._on_agents_changed()
        supervisor._notify_main_agent.assert_awaited_once()

        agents_path.write_text(json.dumps([declaration]))
        await supervisor._on_agents_changed()

    assert supervisor.stacks["sub"] is stacks[1]
    assert "sub" not in supervisor._excluded
    assert create.await_count == 2
