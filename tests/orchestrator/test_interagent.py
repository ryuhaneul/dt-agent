"""Tests for orchestrator inter-agent Named Session handling."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.cli.types import CLIResponse
from ductor_bot.config import AgentConfig
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.injection import (
    _get_or_create_interagent_session,
    _interagent_chat_id,
)
from ductor_bot.session.named import interagent_session_name
from ductor_bot.workspace.paths import DuctorPaths

_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def orch_ia(workspace: tuple[DuctorPaths, AgentConfig]) -> Orchestrator:
    """Orchestrator with mocked CLIService for inter-agent tests."""
    paths, config = workspace
    config.allowed_user_ids = [12345]
    o = Orchestrator(config, paths, agent_name="codex")
    mock_cli = MagicMock()
    mock_cli._config = MagicMock()
    mock_cli._config.agent_name = "codex"
    mock_cli._config.cli_timeout = 120
    mock_cli.execute = AsyncMock(return_value=CLIResponse(session_id="sess-001", result="done"))
    object.__setattr__(o, "_cli_service", mock_cli)
    return o


class TestInteragentChatId:
    """Test _interagent_chat_id helper."""

    def test_returns_first_allowed_user(self, orch_ia: Orchestrator) -> None:
        assert _interagent_chat_id(orch_ia) == 12345

    def test_returns_zero_when_no_users(self, workspace: tuple[DuctorPaths, AgentConfig]) -> None:
        paths, config = workspace
        config.allowed_user_ids = []
        o = Orchestrator(config, paths)
        assert _interagent_chat_id(o) == 0


class TestGetOrCreateInteragentSession:
    """Test _get_or_create_interagent_session."""

    def test_creates_new_session(self, orch_ia: Orchestrator) -> None:
        ns, is_new, notice = _get_or_create_interagent_session(orch_ia, "main")
        assert is_new is True
        assert notice == ""
        assert ns.name == "ia-main"
        assert ns.chat_id == 12345
        assert ns.status == "running"

    def test_reuses_existing_session(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = _get_or_create_interagent_session(orch_ia, "main")
        ns1.status = "idle"
        ns2, is_new2, notice = _get_or_create_interagent_session(orch_ia, "main")
        assert is_new2 is False
        assert notice == ""
        assert ns2.name == ns1.name

    def test_new_session_flag_resets_existing(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = _get_or_create_interagent_session(orch_ia, "main")
        ns1.status = "idle"
        ns1.session_id = "old-session"

        ns2, is_new, _ = _get_or_create_interagent_session(orch_ia, "main", new_session=True)
        assert is_new is True
        assert ns2.session_id == ""  # Fresh session, no resume ID

    def test_different_senders_get_different_sessions(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = _get_or_create_interagent_session(orch_ia, "alice")
        ns2, _, _ = _get_or_create_interagent_session(orch_ia, "bob")
        assert ns1.name == "ia-alice"
        assert ns2.name == "ia-bob"
        assert ns1.name != ns2.name

    def test_ended_session_creates_new_one(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = _get_or_create_interagent_session(orch_ia, "main")
        ns1.status = "ended"

        ns2, is_new, _ = _get_or_create_interagent_session(orch_ia, "main")
        assert is_new is True
        assert ns2.session_id == ""

    def test_provider_switch_resets_session(self, orch_ia: Orchestrator) -> None:
        # Start with a codex model so the session is created for provider "codex"
        orch_ia._config.model = "gpt-5.3-codex"
        ns1, _, notice1 = _get_or_create_interagent_session(orch_ia, "main")
        assert notice1 == ""
        assert ns1.provider == "codex"
        ns1.status = "idle"
        ns1.session_id = "codex-sess-1"

        # Switch to a claude model → different provider
        orch_ia._config.model = "sonnet"

        ns2, is_new, notice2 = _get_or_create_interagent_session(orch_ia, "main")
        assert is_new is True
        assert ns2.session_id == ""  # Fresh — old codex session discarded
        assert "provider" in notice2.lower()
        assert ns2.provider == "claude"

    def test_same_provider_no_notice(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = _get_or_create_interagent_session(orch_ia, "main")
        ns1.status = "idle"

        _ns2, is_new, notice = _get_or_create_interagent_session(orch_ia, "main")
        assert is_new is False
        assert notice == ""

    def test_scopes_names_by_source_chat_and_topic(self) -> None:
        topic_10 = interagent_session_name("main", 777, 10)
        topic_20 = interagent_session_name("main", 777, 20)
        assert topic_10 != topic_20
        assert topic_10.startswith("ia.main.t10.x")
        assert len(topic_10) <= 40
        assert interagent_session_name("main") == "ia-main"


class TestHandleInteragentMessage:
    """Test handle_interagent_message."""

    async def test_returns_result_and_session_name(self, orch_ia: Orchestrator) -> None:
        result_text, session_name, notice = await orch_ia.handle_interagent_message(
            "main", "Do something"
        )
        assert result_text == "done"
        assert session_name == "ia-main"
        assert notice == ""

    async def test_creates_named_session(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Task 1")
        ns = orch_ia._named_sessions.get(12345, "ia-main")
        assert ns is not None
        assert ns.session_id == "sess-001"
        assert ns.status == "idle"

    async def test_resumes_existing_session(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Task 1")

        # Second call should resume with the session_id
        orch_ia._cli_service.execute = AsyncMock(
            return_value=CLIResponse(session_id="sess-002", result="continued")
        )
        result_text, _, _ = await orch_ia.handle_interagent_message("main", "Task 2")
        assert result_text == "continued"

        # Verify resume_session was passed
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert request.resume_session == "sess-001"

    async def test_new_session_flag_starts_fresh(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Task 1")

        orch_ia._cli_service.execute = AsyncMock(
            return_value=CLIResponse(session_id="sess-new", result="fresh start")
        )
        result_text, _, _ = await orch_ia.handle_interagent_message(
            "main", "New task", new_session=True
        )
        assert result_text == "fresh start"

        # Verify resume_session is None (fresh session)
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert request.resume_session is None

    async def test_same_topic_reuses_scoped_session(self, orch_ia: Orchestrator) -> None:
        _, first_name, _ = await orch_ia.handle_interagent_message(
            "main", "Task 1", source_chat_id=777, source_topic_id=10
        )
        _, second_name, _ = await orch_ia.handle_interagent_message(
            "main", "Task 2", source_chat_id=777, source_topic_id=10
        )
        assert second_name == first_name
        assert orch_ia._cli_service.execute.call_args.args[0].resume_session == "sess-001"

    async def test_different_topics_use_different_scoped_sessions(
        self, orch_ia: Orchestrator
    ) -> None:
        _, topic_10, _ = await orch_ia.handle_interagent_message(
            "main", "Task 1", source_chat_id=777, source_topic_id=10
        )
        _, topic_20, _ = await orch_ia.handle_interagent_message(
            "main", "Task 2", source_chat_id=777, source_topic_id=20
        )
        assert topic_10 != topic_20

    async def test_new_keeps_scoped_destination(self, orch_ia: Orchestrator) -> None:
        _, first_name, _ = await orch_ia.handle_interagent_message(
            "main", "Task 1", source_chat_id=777, source_topic_id=10
        )
        _, new_name, _ = await orch_ia.handle_interagent_message(
            "main",
            "Task 2",
            new_session=True,
            source_chat_id=777,
            source_topic_id=10,
        )
        assert new_name == first_name
        assert orch_ia._cli_service.execute.call_args.args[0].resume_session is None

    async def test_interagent_request_uses_recipient_anchor(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message(
            "main", "Task", source_chat_id=777, source_topic_id=10
        )
        request = orch_ia._cli_service.execute.call_args.args[0]
        assert request.chat_id == 12345
        assert request.chat_id != 777

    async def test_task_delivery_keeps_recipient_anchor(
        self, orch_ia: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ductor_bot.cli.base import CLIConfig
        from ductor_bot.cli.executor import build_subprocess_env

        await orch_ia.handle_interagent_message(
            "main", "Create a task", source_chat_id=777, source_topic_id=10
        )
        request = orch_ia._cli_service.execute.call_args.args[0]
        assert request.chat_id == 12345

        monkeypatch.delenv("DUCTOR_CHAT_ID", raising=False)
        monkeypatch.delenv("DUCTOR_TOPIC_ID", raising=False)
        env = build_subprocess_env(
            CLIConfig(
                working_dir="/tmp/workspace",
                chat_id=request.chat_id,
                topic_id=request.topic_id,
                transport=request.transport,
            )
        )
        assert env is not None
        monkeypatch.setenv("DUCTOR_CHAT_ID", env["DUCTOR_CHAT_ID"])

        tool = _ROOT / "ductor_bot/_home_defaults/workspace/tools/task_tools/create_task.py"
        spec = importlib.util.spec_from_file_location("create_task_tool", tool)
        assert spec is not None
        assert spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        captured: dict[str, object] = {}

        def post_json(_url: str, body: dict[str, object], *, timeout: int) -> dict[str, object]:
            assert timeout == 10
            captured.update(body)
            return {"success": True, "task_id": "task-1"}

        monkeypatch.setattr(
            mod,
            "_load_shared",
            lambda: (lambda _path: "http://example.invalid", post_json, lambda: "codex"),
        )
        monkeypatch.setattr(sys, "argv", ["create_task.py", "do work"])
        mod.main()
        assert captured["chat_id"] == 12345
        assert "topic_id" not in captured

    async def test_interagent_request_carries_origin(self, orch_ia: Orchestrator) -> None:
        """The requesting thread travels with the turn even though the anchor wins."""
        await orch_ia.handle_interagent_message(
            "main",
            "Task",
            source_chat_id=777,
            source_topic_id=10,
            source_transport="mx",
        )
        request = orch_ia._cli_service.execute.call_args.args[0]
        assert request.chat_id == 12345  # anchor unchanged
        assert request.origin_chat_id == 777
        assert request.origin_topic_id == 10
        assert request.origin_transport == "mx"
        assert request.interagent_turn is True

    async def test_origin_defaults_to_own_transport(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message(
            "main", "Task", source_chat_id=777, source_topic_id=10
        )
        request = orch_ia._cli_service.execute.call_args.args[0]
        assert request.origin_transport == "tg"

    async def test_no_source_context_leaves_origin_empty(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Task")
        request = orch_ia._cli_service.execute.call_args.args[0]
        assert request.origin_chat_id == 0
        assert request.origin_topic_id is None
        assert request.interagent_turn is True

    async def test_stale_retry_keeps_origin(self, orch_ia: Orchestrator) -> None:
        """Stale-session retry preserves the inter-agent origin."""
        await orch_ia.handle_interagent_message(
            "main", "Task 1", source_chat_id=777, source_topic_id=10
        )
        orch_ia._cli_service.execute = AsyncMock(
            side_effect=[
                CLIResponse(
                    result="No conversation found with session ID: sess-001",
                    is_error=True,
                ),
                CLIResponse(session_id="sess-002", result="done"),
            ]
        )
        await orch_ia.handle_interagent_message(
            "main", "Task 2", source_chat_id=777, source_topic_id=10
        )
        retry_request = orch_ia._cli_service.execute.call_args.args[0]
        assert retry_request.resume_session is None
        assert retry_request.chat_id == 12345
        assert retry_request.origin_chat_id == 777
        assert retry_request.origin_topic_id == 10
        assert all(
            call.args[0].interagent_turn for call in orch_ia._cli_service.execute.call_args_list
        )

    async def test_origin_reaches_subprocess_env(self, orch_ia: Orchestrator) -> None:
        """End to end: the recipient CLI can see the requesting thread."""
        from ductor_bot.cli.base import CLIConfig
        from ductor_bot.cli.executor import build_subprocess_env

        await orch_ia.handle_interagent_message(
            "main",
            "Task",
            source_chat_id=777,
            source_topic_id=10,
            source_transport="mx",
        )
        request = orch_ia._cli_service.execute.call_args.args[0]
        env = build_subprocess_env(
            CLIConfig(
                working_dir="/tmp/workspace",
                chat_id=request.chat_id,
                topic_id=request.topic_id,
                transport=request.transport,
                origin_chat_id=request.origin_chat_id,
                origin_topic_id=request.origin_topic_id,
                origin_transport=request.origin_transport,
            )
        )
        assert env is not None
        assert env["DUCTOR_CHAT_ID"] == "12345"  # tasks still anchor here
        assert env["DUCTOR_ORIGIN_CHAT_ID"] == "777"
        assert env["DUCTOR_ORIGIN_TOPIC_ID"] == "10"
        assert env["DUCTOR_ORIGIN_TRANSPORT"] == "mx"

    async def test_prompt_contains_interagent_markers(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Hello world")
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert "[INTER-AGENT MESSAGE from 'main'" in request.prompt
        assert "Hello world" in request.prompt
        assert "[END INTER-AGENT MESSAGE]" in request.prompt

    async def test_process_label_set_correctly(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Test")
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert request.process_label == "interagent:ia-main"

    async def test_error_returns_error_text(self, orch_ia: Orchestrator) -> None:
        orch_ia._cli_service.execute = AsyncMock(side_effect=RuntimeError("crash"))
        result_text, session_name, _ = await orch_ia.handle_interagent_message("main", "Crash")
        assert "error" in result_text.lower()
        assert session_name == "ia-main"

    async def test_provider_switch_returns_notice(self, orch_ia: Orchestrator) -> None:
        # Start with codex provider
        orch_ia._config.model = "gpt-5.3-codex"
        await orch_ia.handle_interagent_message("main", "Task 1")

        # Switch to claude provider
        orch_ia._config.model = "sonnet"
        orch_ia._cli_service.execute = AsyncMock(
            return_value=CLIResponse(session_id="claude-sess", result="switched")
        )
        result_text, _, notice = await orch_ia.handle_interagent_message("main", "Task 2")
        assert result_text == "switched"
        assert "provider" in notice.lower()
        # Fresh session → no resume
        call_args = orch_ia._cli_service.execute.call_args
        assert call_args[0][0].resume_session is None

    async def test_session_idle_after_error(self, orch_ia: Orchestrator) -> None:
        orch_ia._cli_service.execute = AsyncMock(side_effect=RuntimeError("crash"))
        await orch_ia.handle_interagent_message("main", "Crash")
        ns = orch_ia._named_sessions.get(12345, "ia-main")
        assert ns is not None
        assert ns.status == "idle"

    async def test_handle_interagent_message_recovers_from_stale_session(
        self, orch_ia: Orchestrator
    ) -> None:
        """A stale inter-agent session is replaced and retried without resume_session, with a recovery notice."""
        # Seed an idle session with a (about to be) stale session_id.
        await orch_ia.handle_interagent_message("main", "Task 1")
        ns = orch_ia._named_sessions.get(12345, "ia-main")
        assert ns is not None
        ns.session_id = "stale-id"
        ns.status = "idle"

        stale_response = CLIResponse(
            result="No conversation found with session ID: stale-id",
            is_error=True,
        )
        fresh_response = CLIResponse(session_id="fresh-sess", result="fresh response")
        orch_ia._cli_service.execute = AsyncMock(side_effect=[stale_response, fresh_response])

        result_text, session_name, notice = await orch_ia.handle_interagent_message(
            "main", "Task 2"
        )

        assert result_text == "fresh response"
        assert session_name == "ia-main"
        assert "stale" in notice.lower()
        assert orch_ia._cli_service.execute.call_count == 2
        first_call_req = orch_ia._cli_service.execute.call_args_list[0][0][0]
        second_call_req = orch_ia._cli_service.execute.call_args_list[1][0][0]
        assert first_call_req.resume_session == "stale-id"
        assert second_call_req.resume_session is None


def test_ask_agent_forwards_source_chat_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _ROOT / "ductor_bot/_home_defaults/workspace/tools/agent_tools/ask_agent.py"
    spec = importlib.util.spec_from_file_location("ask_agent_tool", tool)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {"success": True, "text": "ok"}
    ).encode()
    captured: dict[str, object] = {}

    def urlopen(request: object, timeout: int) -> MagicMock:
        assert timeout == 300
        captured.update(json.loads(request.data.decode()))
        return response

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sys, "argv", ["ask_agent.py", "codex", "hello"])
    monkeypatch.setenv("DUCTOR_AGENT_NAME", "main")
    monkeypatch.setenv("DUCTOR_CHAT_ID", "777")
    monkeypatch.setenv("DUCTOR_TOPIC_ID", "10")
    mod.main()
    assert captured["chat_id"] == 777
    assert captured["topic_id"] == 10


def _load_tool(name: str) -> object:
    tool = _ROOT / f"ductor_bot/_home_defaults/workspace/tools/agent_tools/{name}"
    spec = importlib.util.spec_from_file_location(f"{name}_tool", tool)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_tool(mod: object, argv: list[str], monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {"success": True, "text": "ok", "task_id": "t-1"}
    ).encode()
    captured: dict[str, object] = {}

    def urlopen(request: object, timeout: int) -> MagicMock:
        assert timeout > 0
        captured.update(json.loads(request.data.decode()))
        return response

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sys, "argv", argv)
    mod.main()
    return captured


@pytest.mark.parametrize("tool", ["ask_agent.py", "ask_agent_async.py"])
def test_bus_tools_prefer_origin_over_anchor(tool: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reply sent while handling an inter-agent request goes to the requester."""
    mod = _load_tool(tool)
    monkeypatch.setenv("DUCTOR_AGENT_NAME", "developer")
    monkeypatch.setenv("DUCTOR_CHAT_ID", "12345")
    monkeypatch.delenv("DUCTOR_TOPIC_ID", raising=False)
    monkeypatch.setenv("DUCTOR_TRANSPORT", "tg")
    monkeypatch.setenv("DUCTOR_ORIGIN_CHAT_ID", "-100777")
    monkeypatch.setenv("DUCTOR_ORIGIN_TOPIC_ID", "17185")
    monkeypatch.setenv("DUCTOR_ORIGIN_TRANSPORT", "tg")
    captured = _run_tool(mod, [tool, "main", "report"], monkeypatch)
    assert captured["chat_id"] == -100777
    assert captured["topic_id"] == 17185


@pytest.mark.parametrize("tool", ["ask_agent.py", "ask_agent_async.py"])
def test_bus_tools_fall_back_to_anchor(tool: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an inter-agent origin the agent's own chat is still used."""
    mod = _load_tool(tool)
    monkeypatch.setenv("DUCTOR_AGENT_NAME", "main")
    monkeypatch.setenv("DUCTOR_CHAT_ID", "12345")
    monkeypatch.setenv("DUCTOR_TOPIC_ID", "9")
    monkeypatch.delenv("DUCTOR_ORIGIN_CHAT_ID", raising=False)
    monkeypatch.delenv("DUCTOR_ORIGIN_TOPIC_ID", raising=False)
    captured = _run_tool(mod, [tool, "developer", "do work"], monkeypatch)
    assert captured["chat_id"] == 12345
    assert captured["topic_id"] == 9


@pytest.mark.asyncio
async def test_interagent_async_reply_uses_origin_but_task_keeps_anchor(
    orch_ia: Orchestrator, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bus replies use the requesting topic; task results stay in the sub-agent chat."""
    from ductor_bot.cli.base import CLIConfig
    from ductor_bot.cli.executor import build_subprocess_env

    await orch_ia.handle_interagent_message(
        "main", "Task", source_chat_id=-100777, source_topic_id=17185, source_transport="tg"
    )
    request = orch_ia._cli_service.execute.call_args.args[0]
    env = build_subprocess_env(
        CLIConfig(
            ductor_home=tmp_path,
            agent_name="developer",
            chat_id=request.chat_id,
            transport=request.transport,
            origin_chat_id=request.origin_chat_id,
            origin_topic_id=request.origin_topic_id,
            origin_transport=request.origin_transport,
            interagent_turn=request.interagent_turn,
        )
    )
    for key in (
        "DUCTOR_AGENT_NAME",
        "DUCTOR_CHAT_ID",
        "DUCTOR_TRANSPORT",
        "DUCTOR_ORIGIN_CHAT_ID",
        "DUCTOR_ORIGIN_TOPIC_ID",
        "DUCTOR_ORIGIN_TRANSPORT",
        "DUCTOR_IA_TURN",
    ):
        monkeypatch.setenv(key, env[key])
    monkeypatch.delenv("DUCTOR_TOPIC_ID", raising=False)

    async_tool = _load_tool("ask_agent_async.py")
    bus_body = _run_tool(async_tool, ["ask_agent_async.py", "main", "report"], monkeypatch)
    assert (bus_body["chat_id"], bus_body["topic_id"], bus_body["transport"]) == (
        -100777,
        17185,
        "tg",
    )

    tool_path = _ROOT / "ductor_bot/_home_defaults/workspace/tools/task_tools/create_task.py"
    spec = importlib.util.spec_from_file_location("create_task_tool", tool_path)
    assert spec is not None
    assert spec.loader is not None
    task_tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(task_tool)
    task_body: dict[str, object] = {}

    def post_json(_url: str, body: dict[str, object], *, timeout: int) -> dict[str, object]:
        assert timeout == 10
        task_body.update(body)
        return {"success": True, "task_id": "task-1"}

    monkeypatch.setattr(
        task_tool,
        "_load_shared",
        lambda: (lambda _path: "http://example.invalid", post_json, lambda: "developer"),
    )
    monkeypatch.setattr(sys, "argv", ["create_task.py", "work"])
    task_tool.main()
    assert bus_body["ia_turn"] is True
    assert task_body["chat_id"] == 12345
    assert "topic_id" not in task_body


@pytest.mark.parametrize("tool", ["ask_agent.py", "ask_agent_async.py"])
def test_bus_tools_do_not_send_topic_without_chat(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_tool(tool)
    monkeypatch.setenv("DUCTOR_AGENT_NAME", "developer")
    monkeypatch.setenv("DUCTOR_CHAT_ID", "0")
    monkeypatch.setenv("DUCTOR_TOPIC_ID", "9")
    monkeypatch.setenv("DUCTOR_ORIGIN_CHAT_ID", "0")
    monkeypatch.setenv("DUCTOR_ORIGIN_TOPIC_ID", "17185")
    captured = _run_tool(mod, [tool, "main", "report"], monkeypatch)
    assert "topic_id" not in captured


def test_origin_environment_overrides_inherited_values_and_clears_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ductor_bot.cli.base import CLIConfig
    from ductor_bot.cli.executor import build_subprocess_env

    monkeypatch.setenv("DUCTOR_ORIGIN_CHAT_ID", "999")
    monkeypatch.setenv("DUCTOR_ORIGIN_TOPIC_ID", "888")
    monkeypatch.setenv("DUCTOR_ORIGIN_TRANSPORT", "evil")
    env = build_subprocess_env(
        CLIConfig(
            ductor_home=tmp_path,
            origin_chat_id=777,
            origin_topic_id=10,
            origin_transport="mx",
        ),
        extra_env={"DUCTOR_ORIGIN_CHAT_ID": "555"},
    )
    assert env["DUCTOR_ORIGIN_CHAT_ID"] == "777"
    assert env["DUCTOR_ORIGIN_TOPIC_ID"] == "10"
    assert env["DUCTOR_ORIGIN_TRANSPORT"] == "mx"

    env = build_subprocess_env(CLIConfig(ductor_home=tmp_path))
    assert "DUCTOR_ORIGIN_CHAT_ID" not in env
    assert "DUCTOR_ORIGIN_TOPIC_ID" not in env
    assert "DUCTOR_ORIGIN_TRANSPORT" not in env


def test_ia_turn_flag_controls_environment_independent_of_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ductor_bot.cli.base import CLIConfig
    from ductor_bot.cli.executor import build_subprocess_env

    monkeypatch.setenv("DUCTOR_IA_TURN", "1")
    env = build_subprocess_env(
        CLIConfig(ductor_home=tmp_path, process_label="interagent:envelope-1")
    )
    assert "DUCTOR_IA_TURN" not in env
    env = build_subprocess_env(
        CLIConfig(ductor_home=tmp_path, process_label="main", interagent_turn=True)
    )
    assert env["DUCTOR_IA_TURN"] == "1"


def test_async_tool_marks_ia_turn_without_origin_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_tool("ask_agent_async.py")
    monkeypatch.setenv("DUCTOR_AGENT_NAME", "developer")
    monkeypatch.setenv("DUCTOR_IA_TURN", "1")
    monkeypatch.delenv("DUCTOR_ORIGIN_CHAT_ID", raising=False)
    body = _run_tool(mod, ["ask_agent_async.py", "main", "report"], monkeypatch)
    assert body["ia_turn"] is True


def test_async_tool_leaves_normal_result_injection_unmarked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_tool("ask_agent_async.py")
    monkeypatch.setenv("DUCTOR_AGENT_NAME", "main")
    monkeypatch.delenv("DUCTOR_IA_TURN", raising=False)
    monkeypatch.delenv("DUCTOR_ORIGIN_CHAT_ID", raising=False)
    body = _run_tool(mod, ["ask_agent_async.py", "developer", "next task"], monkeypatch)
    assert "ia_turn" not in body


@pytest.mark.asyncio
async def test_ia_sync_tool_routes_to_origin_session_without_dm(
    orch_ia: Orchestrator, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ductor_bot.cli.base import CLIConfig
    from ductor_bot.cli.executor import build_subprocess_env
    from ductor_bot.multiagent.bus import InterAgentBus

    env = build_subprocess_env(
        CLIConfig(
            ductor_home=tmp_path,
            agent_name="developer",
            chat_id=12345,
            transport="tg",
            origin_chat_id=-100777,
            origin_topic_id=17185,
            origin_transport="tg",
            interagent_turn=True,
        )
    )
    for key in (
        "DUCTOR_AGENT_NAME",
        "DUCTOR_CHAT_ID",
        "DUCTOR_TRANSPORT",
        "DUCTOR_ORIGIN_CHAT_ID",
        "DUCTOR_ORIGIN_TOPIC_ID",
        "DUCTOR_ORIGIN_TRANSPORT",
        "DUCTOR_IA_TURN",
    ):
        monkeypatch.setenv(key, env[key])
    tool = _load_tool("ask_agent.py")
    body = _run_tool(tool, ["ask_agent.py", "main", "Question"], monkeypatch)
    assert body["chat_id"] == -100777
    assert body["topic_id"] == 17185
    assert body["ia_turn"] is True

    stack = MagicMock()
    stack.bot.orchestrator = orch_ia
    stack.bot.notification_service.notify = AsyncMock()
    stack.bot.notification_service.notify_all = AsyncMock()
    bus = InterAgentBus()
    bus.register("main", stack)
    result = await bus.send(
        sender=str(body["from"]),
        recipient=str(body["to"]),
        message=str(body["message"]),
        chat_id=int(body["chat_id"]),
        topic_id=int(body["topic_id"]),
        transport=str(body["transport"]),
    )
    assert result.success is True
    name = interagent_session_name("developer", -100777, 17185)
    assert orch_ia._named_sessions.get(12345, name) is not None
    stack.bot.notification_service.notify.assert_not_awaited()
    stack.bot.notification_service.notify_all.assert_not_awaited()


def test_api_turn_async_delegation_keeps_api_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_tool("ask_agent_async.py")
    monkeypatch.setenv("DUCTOR_AGENT_NAME", "main")
    monkeypatch.setenv("DUCTOR_CHAT_ID", "87654")
    monkeypatch.setenv("DUCTOR_TOPIC_ID", "9")
    monkeypatch.setenv("DUCTOR_TRANSPORT", "api")
    monkeypatch.delenv("DUCTOR_ORIGIN_CHAT_ID", raising=False)
    monkeypatch.delenv("DUCTOR_ORIGIN_TOPIC_ID", raising=False)
    monkeypatch.delenv("DUCTOR_ORIGIN_TRANSPORT", raising=False)
    captured = _run_tool(mod, ["ask_agent_async.py", "worker", "delegate"], monkeypatch)
    assert (captured["chat_id"], captured["topic_id"], captured["transport"]) == (
        87654, 9, "api",
    )
