"""Real bus-to-orchestrator heartbeat integration contracts."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import Envelope, Origin
from ductor_bot.cli.types import AgentResponse
from ductor_bot.session.key import SessionKey

if TYPE_CHECKING:
    import pytest

    from ductor_bot.orchestrator.core import Orchestrator


class _Transport:
    transport_name = "tg"

    def __init__(self) -> None:
        self.delivered: list[Envelope] = []

    async def deliver(self, envelope: Envelope) -> None:
        self.delivered.append(envelope)

    async def deliver_broadcast(self, envelope: Envelope) -> None:
        self.delivered.append(envelope)


async def _seed_dormant(orch: Orchestrator) -> tuple[SessionKey, AsyncMock]:
    config = orch._config
    config.allowed_user_ids = [1]
    config.heartbeat.enabled = True
    config.heartbeat.auto_targets = True
    config.heartbeat.suspend_on_ack = True
    config.heartbeat.cooldown_minutes = 0
    config.heartbeat.quiet_start = 0
    config.heartbeat.quiet_end = 0

    key = SessionKey.telegram(1)
    session, _ = await orch._sessions.resolve_session(key, provider="claude", model="opus")
    session.session_id = "sid-1"
    session.message_count = 3
    session.last_active = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    await orch._sessions.update_session(session)

    execute = AsyncMock(
        return_value=AgentResponse(
            result="HEARTBEAT_OK",
            session_id="sid-1",
            is_error=False,
        )
    )
    orch._cli_service.execute = execute
    observer = orch._observers.heartbeat
    await observer._tick()
    assert execute.await_count == 1
    saved = await orch._sessions.get_active(key)
    assert saved is not None
    assert observer._state[key.storage_key].acked == (
        saved.provider,
        saved.session_id,
        saved.message_count,
    )
    execute.reset_mock()
    await observer._tick()
    execute.assert_not_awaited()
    return key, execute


def _wire_bus(orch: Orchestrator) -> tuple[MessageBus, _Transport]:
    bus = MessageBus()
    transport = _Transport()
    bus.register_transport(transport)
    orch.wire_observers_to_bus(bus)
    return bus, transport


async def test_real_process_registry_busy_check_is_topic_scoped(
    orch: Orchestrator,
) -> None:
    config = orch._config
    config.allowed_group_ids = [-1001]
    config.heartbeat.enabled = True
    config.heartbeat.auto_targets = True
    config.heartbeat.suspend_on_ack = False
    config.heartbeat.cooldown_minutes = 0
    config.heartbeat.quiet_start = 0
    config.heartbeat.quiet_end = 0

    key = SessionKey.telegram(-1001, 7)
    session, _ = await orch._sessions.resolve_session(key, provider="claude", model="opus")
    session.session_id = "sid-7"
    session.last_active = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    await orch._sessions.update_session(session)

    process_other = MagicMock(spec=asyncio.subprocess.Process)
    process_other.pid = 701
    process_other.returncode = None
    orch._process_registry.register(-1001, process_other, "task:other", topic_id=8)
    assert orch.is_chat_busy(-1001, 7) is False
    assert orch.is_chat_busy(-1001, 8) is True
    assert orch.is_chat_busy(-1001, None) is True

    execute = AsyncMock(
        return_value=AgentResponse(
            result="HEARTBEAT_OK",
            session_id="sid-7",
            is_error=False,
        )
    )
    orch._cli_service.execute = execute
    observer = orch._observers.heartbeat
    await observer._run_for_chat(-1001, 7, source="auto")
    execute.assert_awaited_once()

    process_same = MagicMock(spec=asyncio.subprocess.Process)
    process_same.pid = 702
    process_same.returncode = None
    orch._process_registry.register(-1001, process_same, "task:same", topic_id=7)
    execute.reset_mock()
    await observer._run_for_chat(-1001, 7, source="auto")
    execute.assert_not_awaited()
    assert observer._reasons[key.storage_key] == "busy"


async def test_cancelled_task_result_wakes_dormant_key_through_real_bus(
    orch: Orchestrator,
) -> None:
    key, execute = await _seed_dormant(orch)
    bus, transport = _wire_bus(orch)
    before = await orch._sessions.get_active(key)
    assert before is not None
    envelope = Envelope(
        origin=Origin.TASK_RESULT,
        chat_id=key.chat_id,
        status="cancelled",
        result_text="Task cancelled",
        needs_injection=False,
    )
    await bus.submit(envelope)
    assert transport.delivered == [envelope]
    after = await orch._sessions.get_active(key)
    assert after is not None
    assert (after.session_id, after.message_count) == (before.session_id, before.message_count)
    assert orch._observers.heartbeat._state[key.storage_key].acked is None
    execute.assert_not_awaited()

    await orch._observers.heartbeat._tick()
    assert execute.await_count == 1
    await orch._observers.heartbeat._tick()
    assert execute.await_count == 1


async def test_failed_interagent_injection_still_wakes_next_tick(
    orch: Orchestrator,
) -> None:
    key, execute = await _seed_dormant(orch)
    bus, transport = _wire_bus(orch)
    before = await orch._sessions.get_active(key)
    assert before is not None
    execute.side_effect = RuntimeError("injection failed")
    envelope = Envelope(
        origin=Origin.INTERAGENT,
        chat_id=key.chat_id,
        prompt="Delegate result",
        needs_injection=True,
    )
    await bus.submit(envelope)
    assert envelope.is_error
    assert transport.delivered == []
    assert envelope.result_text == ""
    after = await orch._sessions.get_active(key)
    assert after is not None
    assert (after.session_id, after.message_count) == (before.session_id, before.message_count)
    assert orch._observers.heartbeat._state[key.storage_key].acked is None

    execute.side_effect = None
    execute.reset_mock()
    await orch._observers.heartbeat._tick()
    assert execute.await_count == 1
    await orch._observers.heartbeat._tick()
    assert execute.await_count == 1


async def test_cli_log_counts_actual_calls_and_pre_cli_exception_has_only_run_log(
    orch: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key, execute = await _seed_dormant(orch)
    observer = orch._observers.heartbeat
    observer.notify_activity(key)
    execute.reset_mock()
    with caplog.at_level(logging.INFO):
        await observer._tick()
    cli_lines = [r.message for r in caplog.records if r.message.startswith("Heartbeat CLI key=")]
    run_lines = [r.message for r in caplog.records if r.message.startswith("Heartbeat run key=")]
    assert len(cli_lines) == execute.await_count == 1
    assert len(run_lines) == 1
    assert f"key={key.storage_key}" in cli_lines[0]

    observer.notify_activity(key)
    lookup = AsyncMock(side_effect=RuntimeError("session lookup failed"))
    monkeypatch.setattr(orch._sessions, "get_active", lookup)
    execute.reset_mock()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await observer._tick()
    lookup.assert_awaited_once_with(key)
    execute.assert_not_awaited()
    assert not any(r.message.startswith("Heartbeat CLI key=") for r in caplog.records)
    run_lines = [r.message for r in caplog.records if r.message.startswith("Heartbeat run key=")]
    assert len(run_lines) == 1
    assert "outcome=exception reason=RuntimeError marker=-" in run_lines[0]


async def test_suspend_off_keeps_auto_target_provider_reason_and_transition_logs(
    orch: Orchestrator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = orch._config
    config.allowed_user_ids = [1]
    config.heartbeat.auto_targets = True
    config.heartbeat.suspend_on_ack = False
    config.heartbeat.cooldown_minutes = 0
    config.heartbeat.quiet_start = 0
    config.heartbeat.quiet_end = 0

    key = SessionKey.telegram(1)
    session, _ = await orch._sessions.resolve_session(key, provider="codex", model="gpt-5.2-codex")
    session.session_id = "codex-sid"
    await orch._sessions.update_session(session)
    execute = AsyncMock(
        return_value=AgentResponse(result="invalid session", session_id="", is_error=True)
    )
    orch._cli_service.execute = execute
    observer = orch._observers.heartbeat

    with caplog.at_level(logging.INFO):
        await observer._tick()
        await observer._tick()
    assert execute.await_count == 1
    request = execute.await_args.args[0]
    assert request.provider_override == "codex"
    assert request.model_override == "gpt-5.2-codex"
    assert observer._state[key.storage_key].quarantine_sid == "codex-sid"
    messages = [record.message for record in caplog.records]
    assert messages.count("Heartbeat tg:1: ran") == 1
    assert messages.count("Heartbeat tg:1: quarantined") == 1
    assert sum("outcome=failed reason=invalid_session" in line for line in messages) == 1
