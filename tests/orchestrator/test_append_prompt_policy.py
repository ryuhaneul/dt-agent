"""Appended system prompt policy with Claude's per-turn snapshot refresh."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.cli import claude_features
from ductor_bot.cli.types import AgentRequest, AgentResponse, CLIResponse
from ductor_bot.config import AgentConfig
from ductor_bot.multiagent.models import SubAgentConfig, merge_sub_agent_config
from ductor_bot.multiagent.supervisor import AgentSupervisor
from ductor_bot.orchestrator import flows
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.flows import build_append_prompt
from ductor_bot.session.key import SessionKey
from ductor_bot.tasks.hub import TaskHub
from ductor_bot.tasks.registry import TaskRegistry
from ductor_bot.workspace.paths import DuctorPaths

MEMORY = "# Main Memory"


@pytest.fixture(autouse=True)
def _flag_support():
    """The installed CLI is not probed: tests state what it supports."""
    claude_features._supported = True
    yield
    claude_features._supported = None


@pytest.fixture
def snapshot_orch(workspace: tuple[DuctorPaths, AgentConfig]) -> Orchestrator:
    paths, config = workspace
    config.allowed_user_ids = [12345]
    orch = Orchestrator(config, paths)
    object.__setattr__(
        orch._cli_service,
        "execute",
        AsyncMock(return_value=CLIResponse(session_id="s", result="r")),
    )
    return orch


# -- build_append_prompt -----------------------------------------------------------------


@pytest.mark.parametrize("is_new", [True, False])
async def test_claude_off_assembles_memory_on_every_turn(snapshot_orch, is_new: bool) -> None:
    append, flag = await build_append_prompt(snapshot_orch, provider="claude", is_new=is_new)
    assert flag is True
    assert append is not None
    assert MEMORY in append


async def test_claude_on_keeps_the_new_session_policy(snapshot_orch) -> None:
    snapshot_orch._config.claude_system_prompt_snapshot = "on"
    resumed, resumed_flag = await build_append_prompt(
        snapshot_orch, provider="claude", is_new=False
    )
    fresh, fresh_flag = await build_append_prompt(snapshot_orch, provider="claude", is_new=True)
    assert (resumed, resumed_flag) == (None, False)
    assert fresh is not None
    assert MEMORY in fresh
    assert fresh_flag is False


async def test_unsupported_cli_falls_back_once_with_the_old_policy(
    snapshot_orch, caplog: pytest.LogCaptureFixture
) -> None:
    claude_features._supported = None
    with patch.object(claude_features, "_probe", return_value=False):
        caplog.set_level("WARNING")
        resumed, flag = await build_append_prompt(snapshot_orch, provider="claude", is_new=False)
        await build_append_prompt(snapshot_orch, provider="claude", is_new=False)
        fresh, _ = await build_append_prompt(snapshot_orch, provider="claude", is_new=True)
    assert (resumed, flag) == (None, False)
    assert fresh is not None
    assert MEMORY in fresh
    assert sum("--system-prompt-snapshot" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("provider", ["codex", "gemini", "grok"])
async def test_other_providers_keep_their_policy(snapshot_orch, provider: str) -> None:
    resumed, resumed_flag = await build_append_prompt(
        snapshot_orch, provider=provider, is_new=False
    )
    fresh, fresh_flag = await build_append_prompt(snapshot_orch, provider=provider, is_new=True)
    assert (resumed, resumed_flag, fresh_flag) == (None, False, False)
    assert fresh is not None
    assert MEMORY in fresh


async def test_named_session_policy_never_carries_the_memory(snapshot_orch) -> None:
    for provider in ("claude", "codex"):
        for is_new in (True, False):
            append, _flag = await build_append_prompt(
                snapshot_orch, provider=provider, is_new=is_new, include_mainmemory_policy=False
            )
            assert append is None  # no files block configured either
    _append, flag = await build_append_prompt(
        snapshot_orch, provider="claude", is_new=False, include_mainmemory_policy=False
    )
    assert flag is True


async def test_files_block_is_appended_on_every_turn_in_every_mode(snapshot_orch) -> None:
    (snapshot_orch.paths.workspace / "PERSONA.md").write_text("persona text")
    snapshot_orch._config.append_system_prompt_files = ["PERSONA.md"]
    for mode in ("off", "on"):
        snapshot_orch._config.claude_system_prompt_snapshot = mode
        append, _ = await build_append_prompt(snapshot_orch, provider="claude", is_new=False)
        assert append is not None
        assert "persona text" in append
        assert (MEMORY in append) is (mode == "off")


# -- the foreground and named turns ------------------------------------------------------


async def test_foreground_turn_marks_the_request_and_keeps_memory_on_resume(snapshot_orch) -> None:
    key = SessionKey(chat_id=1)
    request, session = await flows._prepare_normal(snapshot_orch, key, "hello")
    session.session_id = "sid-1"
    await snapshot_orch._sessions.update_session(session)

    resumed, _ = await flows._prepare_normal(snapshot_orch, key, "again")

    assert request.claude_snapshot_off is True
    assert resumed.resume_session == "sid-1"
    assert resumed.claude_snapshot_off is True
    assert MEMORY in (resumed.append_system_prompt or "")


async def test_foreground_turn_on_mode_has_no_flag_and_no_memory_on_resume(snapshot_orch) -> None:
    snapshot_orch._config.claude_system_prompt_snapshot = "on"
    key = SessionKey(chat_id=1)
    _first, session = await flows._prepare_normal(snapshot_orch, key, "hello")
    session.session_id = "sid-1"
    await snapshot_orch._sessions.update_session(session)

    resumed, _ = await flows._prepare_normal(snapshot_orch, key, "again")

    assert resumed.claude_snapshot_off is False
    assert resumed.append_system_prompt is None


# -- inter-agent turns and tasks: flag only ---------------------------------------------


async def test_interagent_turn_gets_the_flag_but_keeps_its_append_policy(snapshot_orch) -> None:
    await snapshot_orch.handle_interagent_message("main", "do it")
    request = snapshot_orch._cli_service.execute.call_args[0][0]
    assert request.claude_snapshot_off is True
    assert MEMORY not in (request.append_system_prompt or "")


async def test_interagent_turn_without_the_flag_in_on_mode(snapshot_orch) -> None:
    snapshot_orch._config.claude_system_prompt_snapshot = "on"
    await snapshot_orch.handle_interagent_message("main", "do it")
    assert snapshot_orch._cli_service.execute.call_args[0][0].claude_snapshot_off is False


# -- result injection turns --------------------------------------------------------------


async def _active_session(orch: Orchestrator, provider: str = "claude", model: str = "opus"):
    key = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(key, provider=provider, model=model)
    session.session_id = "sid-1"
    await orch._sessions.update_session(session)


async def _injected(orch: Orchestrator, *, isolated: bool = False) -> AgentRequest:
    execute = AsyncMock(return_value=AgentResponse(result="r", session_id="sid-1"))
    object.__setattr__(orch._cli_service, "execute", execute)
    await orch.inject_prompt("result text", 1, "label", isolated=isolated)
    return orch._cli_service.execute.call_args[0][0]


async def test_injection_into_the_user_session_refreshes_like_a_normal_turn(snapshot_orch) -> None:
    await _active_session(snapshot_orch)
    request = await _injected(snapshot_orch)
    assert request.resume_session == "sid-1"
    assert request.claude_snapshot_off is True
    assert MEMORY in (request.append_system_prompt or "")


async def test_injection_without_an_active_session_starts_a_new_one_with_memory(
    snapshot_orch,
) -> None:
    request = await _injected(snapshot_orch)
    assert request.resume_session is None
    assert MEMORY in (request.append_system_prompt or "")


@pytest.mark.parametrize("why", ["mode-on", "unsupported"])
async def test_injection_keeps_the_old_policy_without_the_refresh(snapshot_orch, why: str) -> None:
    if why == "mode-on":
        snapshot_orch._config.claude_system_prompt_snapshot = "on"
    else:
        claude_features._supported = False
    await _active_session(snapshot_orch)
    request = await _injected(snapshot_orch)
    assert request.claude_snapshot_off is False
    assert request.append_system_prompt is None  # no files block configured, resumed


async def test_injection_into_a_non_claude_session_is_unchanged(snapshot_orch) -> None:
    await _active_session(snapshot_orch, provider="codex", model="gpt-5.2-codex")
    request = await _injected(snapshot_orch)
    assert request.provider_override == "codex"
    assert request.claude_snapshot_off is False
    assert request.append_system_prompt is None


async def test_isolated_injection_gets_the_file_blocks_and_the_flag_only(snapshot_orch) -> None:
    (snapshot_orch.paths.workspace / "PERSONA.md").write_text("persona text")
    snapshot_orch._config.append_system_prompt_files = ["PERSONA.md"]
    await _active_session(snapshot_orch)
    request = await _injected(snapshot_orch, isolated=True)
    assert request.resume_session is None
    assert request.claude_snapshot_off is True
    assert "persona text" in (request.append_system_prompt or "")
    assert MEMORY not in (request.append_system_prompt or "")


async def test_isolated_injection_has_no_flag_in_on_mode(snapshot_orch) -> None:
    snapshot_orch._config.claude_system_prompt_snapshot = "on"
    assert (await _injected(snapshot_orch, isolated=True)).claude_snapshot_off is False


def _task_hub(tmp_path: Path, provider: str, **kwargs: str) -> tuple[TaskHub, MagicMock]:
    registry = TaskRegistry(registry_path=tmp_path / "t.json", tasks_dir=tmp_path / "tasks")
    cli = MagicMock()
    cli.resolve_provider = MagicMock(return_value=(provider, "m"))
    config = MagicMock(enabled=True, max_parallel=5, timeout_seconds=60.0)
    hub = TaskHub(registry, MagicMock(workspace=tmp_path), cli_service=cli, config=config, **kwargs)
    hub._instruction_context = lambda _entry: None  # type: ignore[method-assign]
    return hub, cli


def _entry(parent: str = "main") -> MagicMock:
    return MagicMock(
        parent_agent=parent,
        provider="",
        model="",
        reasoning_effort="",
        task_id="t1",
        chat_id=1,
        thread_id=None,
    )


@pytest.mark.parametrize(
    ("provider", "modes", "parent", "expected"),
    [
        ("claude", {}, "main", True),
        ("codex", {}, "main", False),
        ("claude", {"sub": "on"}, "sub", False),  # the agent's own mode, not main's
        ("claude", {"sub": "on"}, "main", True),
    ],
)
async def test_task_requests_get_only_the_flag(
    tmp_path: Path, provider: str, modes: dict[str, str], parent: str, expected: bool
) -> None:
    hub, cli = _task_hub(tmp_path, provider)
    for agent, mode in modes.items():
        hub.set_agent_snapshot_mode(agent, mode)
    request = await hub._prepare_request(_entry(parent), "do", cli, None)
    assert request.claude_snapshot_off is expected
    assert MEMORY not in (request.append_system_prompt or "")


async def test_task_hub_default_mode_comes_from_its_own_setting(tmp_path: Path) -> None:
    hub, cli = _task_hub(tmp_path, "claude", claude_system_prompt_snapshot="on")
    assert (await hub._prepare_request(_entry(), "do", cli, None)).claude_snapshot_off is False


# -- single-shot paths are untouched ----------------------------------------------------


def test_only_the_planned_modules_know_the_request_mark() -> None:
    root = Path(flows.__file__).resolve().parents[1]
    users = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "claude_snapshot_off" in path.read_text(encoding="utf-8")
    }
    assert users == {
        "cli/service.py",
        "cli/types.py",
        "orchestrator/flows.py",
        "orchestrator/injection.py",
        "tasks/hub.py",
    }
    assert AgentRequest(prompt="cron or title turn").claude_snapshot_off is False


# -- configuration -----------------------------------------------------------------------


def test_setting_default_and_validation() -> None:
    assert AgentConfig().claude_system_prompt_snapshot == "off"
    assert AgentConfig(claude_system_prompt_snapshot="on").claude_system_prompt_snapshot == "on"
    with pytest.raises(ValueError, match="claude_system_prompt_snapshot"):
        AgentConfig(claude_system_prompt_snapshot="maybe")  # type: ignore[arg-type]
    assert "claude_system_prompt_snapshot" not in SubAgentConfig(name="w").model_dump(
        exclude_none=True
    )


@pytest.mark.parametrize(
    ("main", "sub", "expected"),
    [("off", None, "off"), ("on", None, "on"), ("off", "on", "on"), ("on", "off", "off")],
)
def test_sub_agent_inherits_and_overrides(
    tmp_path: Path, main: str, sub: str | None, expected: str
) -> None:
    sub_cfg = SubAgentConfig(name="w", claude_system_prompt_snapshot=sub)
    merged = merge_sub_agent_config(
        AgentConfig(claude_system_prompt_snapshot=main),  # type: ignore[arg-type]
        sub_cfg,
        tmp_path,
    )
    assert merged.claude_system_prompt_snapshot == expected


async def test_effective_value_is_mirrored_into_the_sub_config_file(tmp_path: Path) -> None:
    supervisor = AgentSupervisor(
        AgentConfig(
            ductor_home=str(tmp_path), interagent_port=0, claude_system_prompt_snapshot="on"
        )
    )
    (tmp_path / "agents.json").write_text(
        json.dumps([{"name": "sub1", "transport": "telegram", "telegram_token": "tok:1"}])
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
    assert update.await_args.kwargs["claude_system_prompt_snapshot"] == "on"


async def test_supervisor_hands_the_task_hub_each_agents_mode() -> None:
    supervisor = AgentSupervisor(AgentConfig(claude_system_prompt_snapshot="off"))
    hub = MagicMock()
    supervisor._task_hub = hub
    stack = MagicMock()
    stack.name = "sub1"
    stack.config = AgentConfig(claude_system_prompt_snapshot="on")
    supervisor._wire_task_hub(stack)
    hub.set_agent_snapshot_mode.assert_called_once_with("sub1", "on")
