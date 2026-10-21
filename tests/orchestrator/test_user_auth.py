"""Foreground user turns reject missing auth before executing a conversation CLI."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from ductor_bot.cli import auth as cli_auth
from ductor_bot.cli.auth import AuthResult, AuthStatus, check_provider_auth
from ductor_bot.cli.types import AgentResponse
from ductor_bot.i18n import LANGUAGES, t
from ductor_bot.i18n.loader import TranslationStore
from ductor_bot.orchestrator.flows import (
    _user_auth_warning,
    named_session_flow,
    named_session_streaming,
)
from ductor_bot.session import SessionKey


def auth_result(provider, authenticated=False):
    return AuthResult(provider, AuthStatus.AUTHENTICATED if authenticated else AuthStatus.INSTALLED)


@pytest.mark.parametrize("transport", ["tg", "mx", "slack", "api"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("prompt", ["Hello", "[image: photo.png] Describe this", "/unknown_button"])
async def test_all_user_transports_and_prompt_kinds_block_cli(orch, transport, streaming, prompt):
    check = Mock(return_value=auth_result("claude"))
    delta = AsyncMock()
    with patch.object(cli_auth, "check_provider_auth", check):
        key = SessionKey(transport=transport, chat_id=1, topic_id=42)
        if streaming:
            result = await orch.handle_message_streaming(key, prompt, on_text_delta=delta)
        else:
            result = await orch.handle_message(key, prompt)
    assert result.text == t("session.no_auth", provider="claude")
    check.assert_called_once_with("claude")
    orch._cli_service.execute.assert_not_awaited()
    orch._cli_service.execute_streaming.assert_not_awaited()
    delta.assert_not_awaited()
    assert not result.stream_fallback


@pytest.mark.parametrize("streaming", [False, True])
async def test_uses_active_session_provider_instead_of_authenticated_config_default(orch, streaming):
    key = SessionKey(chat_id=1)
    await orch._sessions.resolve_session(key, provider="codex", model="gpt-5.2-codex")
    check = Mock(side_effect=lambda provider: auth_result(provider, provider == "claude"))
    with patch.object(cli_auth, "check_provider_auth", check):
        handler = orch.handle_message_streaming if streaming else orch.handle_message
        result = await handler(key, "Follow up")
    assert "./scripts/login.sh codex" in result.text
    check.assert_called_once_with("codex")
    orch._cli_service.execute.assert_not_awaited()
    orch._cli_service.execute_streaming.assert_not_awaited()


async def test_model_directive_uses_its_provider(orch):
    # This ID is supplied by runtime Codex discovery in production.
    orch._providers._known_model_ids |= frozenset({"gpt-5.2-codex"})
    with patch.object(cli_auth, "check_provider_auth", return_value=auth_result("codex")) as check:
        result = await orch.handle_message(SessionKey(chat_id=1), "@gpt-5.2-codex Hello")
    check.assert_called_once_with("codex")
    assert "./scripts/login.sh codex" in result.text
    orch._cli_service.execute.assert_not_awaited()


@pytest.mark.parametrize("streaming", [False, True])
async def test_login_file_appearing_during_runtime_unblocks_next_message(orch, tmp_path, monkeypatch, streaming):
    monkeypatch.setattr(cli_auth.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("DUCTOR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli_auth, "_claude_cli_logged_in", lambda: False)
    # Replace the suite's auth mock with the actual filesystem checker.
    monkeypatch.setattr(cli_auth, "check_provider_auth", check_provider_auth)
    response = AgentResponse(result="Logged-in response", session_id="session-after-login")
    orch._cli_service.execute.return_value = response
    orch._cli_service.execute_streaming.return_value = response
    handler = orch.handle_message_streaming if streaming else orch.handle_message
    key = SessionKey(chat_id=1)
    first = await handler(key, "Before login")
    assert "./scripts/login.sh claude" in first.text
    orch._cli_service.execute.assert_not_awaited()
    orch._cli_service.execute_streaming.assert_not_awaited()
    credentials = tmp_path / ".claude" / ".credentials.json"
    credentials.parent.mkdir()
    credentials.write_text("{}")  # Synthetic presence marker, no credentials.
    second = await handler(key, "After login")
    assert second.text == "Logged-in response"
    executor = orch._cli_service.execute_streaming if streaming else orch._cli_service.execute
    executor.assert_awaited_once()
    credentials.unlink()
    third = await handler(key, "After logout")
    assert "./scripts/login.sh claude" in third.text
    assert executor.await_count == 1


@pytest.mark.parametrize("streaming", [False, True])
async def test_named_followup_checks_named_provider_without_marking_running(orch, streaming):
    ns = orch._named_sessions.create(1, "codex", "gpt-5.2-codex", "Setup")
    orch._named_sessions.update_after_response(1, ns.name, "named-session-id")
    handler = named_session_streaming if streaming else named_session_flow
    with patch.object(cli_auth, "check_provider_auth", return_value=auth_result("codex")) as check:
        result = await handler(orch, SessionKey(chat_id=1), ns.name, "Button or followup")
    check.assert_called_once_with("codex")
    assert "./scripts/login.sh codex" in result.text
    assert ns.status == "idle"
    assert ns.session_id == "named-session-id"
    orch._cli_service.execute.assert_not_awaited()
    orch._cli_service.execute_streaming.assert_not_awaited()


async def test_command_and_directive_only_do_not_require_auth(orch):
    with patch.object(cli_auth, "check_provider_auth") as check:
        reset = await orch.handle_message(SessionKey(chat_id=1), "/new")
        hint = await orch.handle_message(SessionKey(chat_id=1), "@opus")
    assert "reset" in reset.text.lower()
    assert "for that message only" in hint.text
    check.assert_not_called()


async def test_injected_automation_prompt_preserves_cli_execution(orch):
    orch._cli_service.execute.return_value = AgentResponse(result="Automation response")
    with patch.object(cli_auth, "check_provider_auth") as check:
        result = await orch.inject_prompt("Scheduled prompt", 1, "cron")
    assert result == "Automation response"
    check.assert_not_called()
    orch._cli_service.execute.assert_awaited_once()


async def test_configured_gemini_key_is_an_auth_source(orch):
    orch._config.gemini_api_key = "synthetic-test-key"
    with patch.object(cli_auth, "check_provider_auth") as check:
        assert await _user_auth_warning(orch, "gemini") is None
    check.assert_not_called()


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini", "grok"])
def test_provider_check_only_probes_selected_provider(provider, monkeypatch):
    checks = {name: Mock(return_value=auth_result(name, True)) for name in cli_auth._CHECKERS}
    for name, checker in checks.items():
        monkeypatch.setitem(cli_auth._CHECKERS, name, checker)
    assert check_provider_auth(provider).is_authenticated
    checks[provider].assert_called_once_with()
    for name, checker in checks.items():
        if name != provider:
            checker.assert_not_called()


@pytest.mark.parametrize("language", LANGUAGES)
async def test_auth_messages_use_host_login_script_in_all_languages(orch, language):
    with patch("ductor_bot.i18n._store", TranslationStore(language)), patch.object(
        cli_auth, "check_provider_auth", return_value=auth_result("codex")
    ):
        warning = await _user_auth_warning(orch, "codex")
        assert warning is not None
        assert "./scripts/login.sh codex" in warning.text
        for key in ("welcome.no_auth", "model.no_auth", "session.error_auth"):
            assert "./scripts/login.sh <provider>" in t(key)
            assert "claude auth" not in t(key)
            assert "codex auth" not in t(key)
