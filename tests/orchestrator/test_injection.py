"""Tests for _inject_prompt provider/model resolution from the active session."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.cli.types import AgentRequest, AgentResponse
from ductor_bot.orchestrator.injection import _inject_prompt
from ductor_bot.workspace.paths import DuctorPaths


def _make_orch(active: Any) -> MagicMock:
    orch = MagicMock()
    orch._sessions.get_active = AsyncMock(return_value=active)
    orch._cli_service.execute = AsyncMock(
        return_value=AgentResponse(result="ok", session_id="any", is_error=False)
    )
    orch._config.cli_timeout = 60
    return orch


def _captured_request(orch: MagicMock) -> AgentRequest:
    return cast("AgentRequest", orch._cli_service.execute.await_args.args[0])


class TestInjectPromptProviderOverride:
    """`_inject_prompt` passes the active session's provider/model into AgentRequest."""

    async def test_uses_active_session_provider_model(self) -> None:
        active = MagicMock(session_id="sid", provider="codex", model="gpt-5.5")
        orch = _make_orch(active)

        with patch("ductor_bot.orchestrator.injection._update_session", new=AsyncMock()):
            await _inject_prompt(orch, "hi", chat_id=1, process_label="task_result:x")

        req = _captured_request(orch)
        assert req.provider_override == "codex"
        assert req.model_override == "gpt-5.5"
        assert req.resume_session == "sid"

    async def test_creates_the_session_when_there_is_no_active_one(self) -> None:
        orch = _make_orch(None)
        created = MagicMock(session_id="", provider="claude", model="opus", reasoning_effort="")
        orch.resolve_runtime_target = MagicMock(return_value=("opus", "claude"))
        orch._sessions.resolve_session = AsyncMock(return_value=(created, True))

        with (
            patch("ductor_bot.orchestrator.injection._update_session", new=AsyncMock()) as update,
            # A new session reads MAINMEMORY; this mocked orchestrator has no workspace.
            patch("ductor_bot.orchestrator.flows.read_mainmemory", return_value=""),
        ):
            await _inject_prompt(orch, "hi", chat_id=1, process_label="task_result:x")

        req = _captured_request(orch)
        assert req.provider_override == "claude"  # the created session's own target
        assert req.model_override == "opus"
        assert req.resume_session is None
        update.assert_awaited_once()  # the returned id is stored on the created session

    async def test_claude_active_session(self) -> None:
        active = MagicMock(session_id="sid", provider="claude", model="opus")
        orch = _make_orch(active)

        with patch("ductor_bot.orchestrator.injection._update_session", new=AsyncMock()):
            await _inject_prompt(orch, "hi", chat_id=1, process_label="task_result:x")

        req = _captured_request(orch)
        assert req.provider_override == "claude"
        assert req.model_override == "opus"


async def test_inject_prompt_appends_configured_files(tmp_path: Path) -> None:
    """_inject_prompt injects append_system_prompt_files from the agent workspace."""
    paths = DuctorPaths(ductor_home=tmp_path)
    paths.workspace.mkdir(parents=True, exist_ok=True)
    (paths.workspace / "PERSONA.md").write_text("You are helpful.")

    active = MagicMock(session_id="sid", provider="claude", model="opus")
    orch = _make_orch(active)
    orch.paths = paths
    orch._config.append_system_prompt_files = ["PERSONA.md"]

    with patch("ductor_bot.orchestrator.injection._update_session", new=AsyncMock()):
        await _inject_prompt(orch, "hi", chat_id=1, process_label="task_result:x")

    req = _captured_request(orch)
    assert req.append_system_prompt is not None
    assert "You are helpful." in req.append_system_prompt


async def test_inject_prompt_no_files_leaves_append_none(tmp_path: Path) -> None:
    """Empty append_system_prompt_files -> append_system_prompt stays None."""
    paths = DuctorPaths(ductor_home=tmp_path)
    paths.workspace.mkdir(parents=True, exist_ok=True)

    orch = _make_orch(MagicMock(session_id="sid", provider="claude", model="opus"))
    orch.paths = paths
    orch._config.append_system_prompt_files = []

    with patch("ductor_bot.orchestrator.injection._update_session", new=AsyncMock()):
        await _inject_prompt(orch, "hi", chat_id=1, process_label="task_result:x")

    req = _captured_request(orch)
    assert req.append_system_prompt is None



async def test_isolated_injection_does_not_resume_or_update_dm_session() -> None:
    dm = MagicMock(session_id="dm-secret-session", provider="claude", model="opus")
    orch = _make_orch(dm)
    with patch("ductor_bot.orchestrator.injection._update_session", new=AsyncMock()) as update:
        first = await _inject_prompt(
            orch, "broadcast one", chat_id=0, process_label="interagent:first",
            isolated=True, result_origin="cron-broadcast", broadcast_ref="job-1",
        )
        second = await _inject_prompt(
            orch, "broadcast two", chat_id=0, process_label="interagent:second",
            isolated=True, result_origin="cron-broadcast", broadcast_ref="job-1",
        )
    assert (first, second) == ("ok", "ok")
    orch._sessions.get_active.assert_not_awaited()
    update.assert_not_awaited()
    requests = [call.args[0] for call in orch._cli_service.execute.await_args_list]
    assert [req.resume_session for req in requests] == [None, None]
    assert [req.chat_id for req in requests] == [0, 0]
    assert all(req.result_origin == "cron-broadcast" for req in requests)
    assert all(req.broadcast_ref == "job-1" for req in requests)
    assert dm.session_id == "dm-secret-session"
