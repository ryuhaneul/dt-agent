"""Tests for the heartbeat flow (orchestrator integration)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import time_machine

from ductor_bot.cli.types import AgentResponse
from ductor_bot.heartbeat.models import HeartbeatStatus
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.flows import _classify_heartbeat_response, heartbeat_flow
from ductor_bot.session.key import SessionKey


def _mock_response(**kwargs: Any) -> AgentResponse:
    defaults: dict[str, Any] = {
        "result": "HEARTBEAT_OK",
        "session_id": "sess-123",
        "is_error": False,
    }
    defaults.update(kwargs)
    return AgentResponse(**defaults)


def _past_cooldown() -> time_machine.travel:
    """Return a time_machine context that jumps 10 minutes into the future."""
    return time_machine.travel(datetime.now(UTC) + timedelta(minutes=10))


@pytest.fixture
def orch(orch: Orchestrator) -> Orchestrator:
    return orch


async def test_heartbeat_ok_returns_ack(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HEARTBEAT_OK response is suppressed."""
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, SessionKey(chat_id=1), "init")

    with _past_cooldown():
        monkeypatch.setattr(
            orch._cli_service,
            "execute",
            AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK")),
        )
        result = await heartbeat_flow(orch, SessionKey(chat_id=1))
        assert result.status is HeartbeatStatus.ACK


async def test_heartbeat_alert_returns_alert(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-OK response returns the alert text."""
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, SessionKey(chat_id=1), "init")

    alert = "Hey! I found something interesting about Python 3.14!"
    with _past_cooldown():
        monkeypatch.setattr(
            orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result=alert))
        )
        result = await heartbeat_flow(orch, SessionKey(chat_id=1))
        assert result.status is HeartbeatStatus.ALERT
        assert result.text == alert


async def test_heartbeat_skips_new_session(orch: Orchestrator) -> None:
    """Heartbeat does nothing if there is no established session."""
    result = await heartbeat_flow(orch, SessionKey(chat_id=999))
    assert result.status is HeartbeatStatus.SKIPPED


async def test_heartbeat_ok_does_not_increment_message_count(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HEARTBEAT_OK should not change message_count."""
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, SessionKey(chat_id=1), "init")

    session_before = await orch._sessions.get_active(SessionKey(chat_id=1))
    assert session_before is not None
    count_before = session_before.message_count

    with _past_cooldown():
        monkeypatch.setattr(
            orch._cli_service,
            "execute",
            AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK")),
        )
        await heartbeat_flow(orch, SessionKey(chat_id=1))

    session_after = await orch._sessions.get_active(SessionKey(chat_id=1))
    assert session_after is not None
    assert session_after.message_count == count_before


async def test_heartbeat_alert_increments_message_count(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alert responses should update session state normally."""
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, SessionKey(chat_id=1), "init")

    session_before = await orch._sessions.get_active(SessionKey(chat_id=1))
    assert session_before is not None
    count_before = session_before.message_count

    alert = "Check out this cool fact!"
    with _past_cooldown():
        monkeypatch.setattr(
            orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result=alert))
        )
        await heartbeat_flow(orch, SessionKey(chat_id=1))

    session_after = await orch._sessions.get_active(SessionKey(chat_id=1))
    assert session_after is not None
    assert session_after.message_count == count_before + 1


async def test_heartbeat_cli_error_returns_failed(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI errors during heartbeat are silently logged, not propagated."""
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, SessionKey(chat_id=1), "init")

    with _past_cooldown():
        monkeypatch.setattr(
            orch._cli_service,
            "execute",
            AsyncMock(return_value=_mock_response(is_error=True, result="Rate limited")),
        )
        result = await heartbeat_flow(orch, SessionKey(chat_id=1))
        assert result.status is HeartbeatStatus.FAILED


async def test_heartbeat_does_not_apply_hooks(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Heartbeat prompt should not have message hooks injected."""
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )

    # Send 5 messages to reach hook threshold
    for _ in range(5):
        await normal(orch, SessionKey(chat_id=1), "msg")

    # Now heartbeat -- the prompt should be the raw heartbeat prompt, no REMINDER
    with _past_cooldown():
        hb_mock = AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK"))
        monkeypatch.setattr(orch._cli_service, "execute", hb_mock)
        await heartbeat_flow(orch, SessionKey(chat_id=1))

        hb_request = hb_mock.call_args[0][0]
        assert "REMINDER" not in hb_request.prompt
        assert "heartbeat" in hb_request.prompt.lower()


async def test_heartbeat_skips_during_cooldown(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Heartbeat is skipped if user was active within cooldown_minutes."""
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, SessionKey(chat_id=1), "just chatting")

    # Immediately after user message -> within cooldown -> skip
    cooldown_mock = AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK"))
    monkeypatch.setattr(orch._cli_service, "execute", cooldown_mock)
    result = await heartbeat_flow(orch, SessionKey(chat_id=1))
    assert result.status is HeartbeatStatus.SKIPPED
    # CLI should NOT have been called since cooldown skipped early
    cooldown_mock.assert_not_awaited()


async def test_heartbeat_runs_after_cooldown(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Heartbeat fires when last_active is older than cooldown_minutes."""
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, SessionKey(chat_id=1), "init")

    # Travel 10 minutes into the future -> past default 5 min cooldown
    future = datetime.now(UTC) + timedelta(minutes=10)
    with time_machine.travel(future):
        after_cooldown_mock = AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK"))
        monkeypatch.setattr(orch._cli_service, "execute", after_cooldown_mock)
        result = await heartbeat_flow(orch, SessionKey(chat_id=1))
        assert result.status is HeartbeatStatus.ACK
        # CLI WAS called (cooldown passed, returned OK)
        after_cooldown_mock.assert_awaited_once()


async def test_heartbeat_uses_session_provider_model_and_effort(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session remains runnable when its provider differs from the global target."""
    orch._providers._available_providers = frozenset({"codex"})

    key = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(key, provider="codex", model="opus")
    session.session_id = "legacy-heartbeat-sid"
    await orch._sessions.update_session(session)
    await orch._sessions.sync_session_target(session, reasoning_effort="high")
    count_before = session.message_count

    with _past_cooldown():
        execute = AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK"))
        monkeypatch.setattr(orch._cli_service, "execute", execute)
        result = await heartbeat_flow(orch, key)

    assert result.status is HeartbeatStatus.ACK
    assert result.marker == ("codex", "legacy-heartbeat-sid", count_before)
    request = execute.call_args.args[0]
    assert request.provider_override == "codex"
    assert request.model_override == "opus"
    assert request.effort_override == "high"
    session_after = await orch._sessions.get_active(key)
    assert session_after is not None
    assert session_after.message_count == count_before


@pytest.mark.parametrize("raw", ["", "   ", "HEARTBEAT_OK HEARTBEAT_OK"])
def test_classifier_rejects_empty_or_repeated_ack(raw: str) -> None:
    outcome = _classify_heartbeat_response(raw, "HEARTBEAT_OK")
    assert outcome.status is HeartbeatStatus.FAILED
    assert outcome.text == ""


def test_classifier_preserves_legacy_alert_delivery() -> None:
    outcome = _classify_heartbeat_response("HEARTBEAT_OK", " HEARTBEAT_OK ")
    assert outcome.status is HeartbeatStatus.ALERT
    assert outcome.text == "HEARTBEAT_OK"


def test_classifier_ack_requires_nonblank_exact_normalized_match() -> None:
    outcome = _classify_heartbeat_response("  HEARTBEAT_OK  ", "HEARTBEAT_OK")
    assert outcome.status is HeartbeatStatus.ACK
    assert outcome.text == ""


async def test_heartbeat_flow_preserves_legacy_spaced_token_alert(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, SessionKey(chat_id=1), "init")

    with _past_cooldown():
        monkeypatch.setattr(
            orch._cli_service,
            "execute",
            AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK")),
        )
        outcome = await heartbeat_flow(
            orch,
            SessionKey(chat_id=1),
            ack_token=" HEARTBEAT_OK ",
        )

    assert outcome.status is HeartbeatStatus.ALERT
    assert outcome.text == "HEARTBEAT_OK"


@pytest.mark.parametrize(
    ("raw", "is_error", "expected_status", "expected_reason"),
    [
        ("HEARTBEAT_OK", False, HeartbeatStatus.ACK, ""),
        ("Alert", False, HeartbeatStatus.ALERT, ""),
        ("", False, HeartbeatStatus.FAILED, "no_ack"),
        ("invalid session", True, HeartbeatStatus.FAILED, "invalid_session"),
        ("Rate limited", True, HeartbeatStatus.FAILED, "cli_error"),
    ],
)
async def test_heartbeat_outcome_reason_marker_and_cli_log(
    orch: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    raw: str,
    is_error: bool,
    expected_status: HeartbeatStatus,
    expected_reason: str,
) -> None:
    from ductor_bot.orchestrator.flows import normal

    key = SessionKey(chat_id=1)
    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, key, "init")
    session = await orch._sessions.get_active(key)
    assert session is not None
    marker = (session.provider, session.session_id, session.message_count)

    with _past_cooldown(), caplog.at_level("INFO", logger="ductor_bot.orchestrator.flows"):
        execute = AsyncMock(return_value=_mock_response(result=raw, is_error=is_error))
        monkeypatch.setattr(orch._cli_service, "execute", execute)
        outcome = await heartbeat_flow(orch, key)

    assert outcome.status is expected_status
    assert outcome.reason == expected_reason
    assert outcome.marker == marker
    cli_lines = [r.message for r in caplog.records if r.message.startswith("Heartbeat CLI key=")]
    assert len(cli_lines) == execute.await_count == 1
    assert key.storage_key in cli_lines[0]
    assert f"provider={session.provider}" in cli_lines[0]


async def test_heartbeat_skip_reasons_and_markers(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ductor_bot.orchestrator.flows import normal

    key = SessionKey(chat_id=1)
    missing = await heartbeat_flow(orch, key)
    assert missing.status is HeartbeatStatus.SKIPPED
    assert missing.reason == "no_session"
    assert missing.marker is None

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, key, "init")
    session = await orch._sessions.get_active(key)
    assert session is not None
    skipped = await heartbeat_flow(orch, key)
    assert skipped.status is HeartbeatStatus.SKIPPED
    assert skipped.reason == "cooldown"
    assert skipped.marker == (session.provider, session.session_id, session.message_count)


async def test_pre_cli_exception_has_no_cli_log(
    orch: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        orch._sessions,
        "get_active",
        AsyncMock(side_effect=RuntimeError("session lookup failed")),
    )
    execute = AsyncMock()
    monkeypatch.setattr(orch._cli_service, "execute", execute)
    with (
        caplog.at_level("INFO", logger="ductor_bot.orchestrator.flows"),
        pytest.raises(RuntimeError, match="session lookup failed"),
    ):
        await heartbeat_flow(orch, SessionKey(chat_id=1))
    execute.assert_not_awaited()
    assert not any(r.message.startswith("Heartbeat CLI key=") for r in caplog.records)


# -- attachments: persona, behavior and main memory ----------------------------------------


@pytest.fixture
def attach_orch(orch: Orchestrator) -> Orchestrator:
    from ductor_bot.cli import claude_features

    claude_features._supported = True
    (orch.paths.workspace / "PERSONA.md").write_text("persona text")
    orch._config.append_system_prompt_files = ["PERSONA.md"]
    yield orch
    claude_features._supported = None


async def _heartbeat_request(orch: Orchestrator, provider: str) -> Any:
    key = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(key, provider=provider, model="m")
    session.session_id = "sid-hb"
    await orch._sessions.update_session(session)
    orch._cli_service.resolve_provider = lambda request: (request.provider_override, "m")
    execute = AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK"))
    orch._cli_service.execute = execute
    with _past_cooldown():
        result = await heartbeat_flow(orch, key)
    assert result.status is HeartbeatStatus.ACK
    return execute.call_args.args[0]


async def test_heartbeat_turn_on_claude_with_snapshot_off_carries_the_attachments(
    attach_orch: Orchestrator,
) -> None:
    request = await _heartbeat_request(attach_orch, "claude")

    assert request.claude_snapshot_off is True
    assert "persona text" in (request.append_system_prompt or "")
    assert "# Main Memory" in (request.append_system_prompt or "")
    assert request.resume_session == "sid-hb"


async def test_heartbeat_turn_on_claude_with_snapshot_on_follows_the_helper_rule(
    attach_orch: Orchestrator,
) -> None:
    attach_orch._config.claude_system_prompt_snapshot = "on"

    request = await _heartbeat_request(attach_orch, "claude")

    assert request.claude_snapshot_off is False
    assert "persona text" in (request.append_system_prompt or "")
    assert "# Main Memory" not in (request.append_system_prompt or "")  # resumed: no memory


@pytest.mark.parametrize("provider", ["codex", "gemini", "grok"])
async def test_heartbeat_turn_on_other_providers_follows_the_helper_rule(
    attach_orch: Orchestrator, provider: str
) -> None:
    request = await _heartbeat_request(attach_orch, provider)

    assert request.claude_snapshot_off is False
    assert "persona text" in (request.append_system_prompt or "")
    assert "# Main Memory" not in (request.append_system_prompt or "")


async def test_heartbeat_ack_with_attachments_leaves_the_session_untouched(
    attach_orch: Orchestrator,
) -> None:
    key = SessionKey(chat_id=1)
    session, _ = await attach_orch._sessions.resolve_session(key, provider="claude", model="m")
    session.session_id = "sid-hb"
    await attach_orch._sessions.update_session(session)
    before = await attach_orch._sessions.get_active(key)
    assert before is not None
    snapshot = (before.last_active, before.message_count, before.activity_turns, before.session_id)

    await _heartbeat_request_for_existing(attach_orch, key)

    after = await attach_orch._sessions.get_active(key)
    assert after is not None
    assert (
        after.last_active,
        after.message_count,
        after.activity_turns,
        after.session_id,
    ) == snapshot


async def _heartbeat_request_for_existing(orch: Orchestrator, key: SessionKey) -> None:
    orch._cli_service.resolve_provider = lambda request: (request.provider_override, "m")
    orch._cli_service.execute = AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK"))
    with _past_cooldown():
        result = await heartbeat_flow(orch, key)
    assert result.status is HeartbeatStatus.ACK
