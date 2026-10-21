"""Execution, permission, and result contracts for heartbeat targets."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ductor_bot.config import AgentConfig, HeartbeatConfig, HeartbeatTarget
from ductor_bot.heartbeat.models import HeartbeatOutcome, HeartbeatStatus
from ductor_bot.heartbeat.observer import HeartbeatObserver, _KeyState
from ductor_bot.session.key import SessionKey
from ductor_bot.session.manager import SessionData

MARKER = ("claude", "session-abcdef", 3)


def _session(chat_id: int, *, topic_id: int | None = None, transport: str = "tg") -> SessionData:
    return SessionData(
        chat_id=chat_id,
        topic_id=topic_id,
        transport=transport,
        provider=MARKER[0],
        session_id=MARKER[1],
        message_count=MARKER[2],
        last_active=datetime.now(UTC).isoformat(),
    )


def _observer(
    sessions: list[SessionData],
    *,
    auto_targets: bool = True,
    suspend_on_ack: bool = True,
    manual: list[HeartbeatTarget] | None = None,
) -> HeartbeatObserver:
    config = AgentConfig(
        heartbeat=HeartbeatConfig(
            enabled=True,
            auto_targets=auto_targets,
            suspend_on_ack=suspend_on_ack,
            interval_minutes=1,
            quiet_start=0,
            quiet_end=0,
            group_targets=manual or [],
        ),
        allowed_user_ids=[100],
        allowed_group_ids=[-1001],
    )
    observer = HeartbeatObserver(config)

    async def list_all() -> list[SessionData]:
        return list(sessions)

    async def get_active(key: SessionKey) -> SessionData | None:
        return next((item for item in sessions if item.session_key == key), None)

    observer.set_session_source(list_all, get_active)
    return observer


def _outcome(status: HeartbeatStatus, *, reason: str = "", text: str = "") -> HeartbeatOutcome:
    return HeartbeatOutcome(status, text=text, marker=MARKER, reason=reason)


async def test_ack_dormancy_and_marker_change() -> None:
    session = _session(100)
    obs = _observer([session])
    handler = AsyncMock(return_value=_outcome(HeartbeatStatus.ACK))
    obs.set_heartbeat_handler(handler)
    await obs._tick()
    assert obs._state["tg:100"].acked == MARKER
    await obs._tick()
    assert handler.await_count == 1
    session.message_count += 1  # a system turn: still dormant
    await obs._tick()
    assert handler.await_count == 1
    session.activity_turns += 1  # a user turn wakes the check
    await obs._tick()
    assert handler.await_count == 2


@pytest.mark.parametrize("change", ["user", "bus", "marker"])
async def test_ack_races_do_not_mark_dormant(change: str) -> None:
    session = _session(100)
    obs = _observer([session])
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(*_args: object) -> HeartbeatOutcome:
        started.set()
        await release.wait()
        return _outcome(HeartbeatStatus.ACK)

    obs.set_heartbeat_handler(handler)
    task = asyncio.create_task(obs._run_for_chat(100))
    await started.wait()
    if change == "user":
        obs.notify_activity(session.session_key)
    elif change == "bus":
        await obs.wake(session.session_key)
    else:
        session.message_count += 1
    release.set()
    await task
    assert obs._state["tg:100"].acked is None


async def test_quarantine_notice_follows_the_language() -> None:
    from ductor_bot.i18n import init

    obs = _observer([_session(100)])
    obs.set_heartbeat_handler(
        AsyncMock(return_value=_outcome(HeartbeatStatus.FAILED, reason="invalid_session"))
    )
    delivered = AsyncMock()
    obs.set_result_handler(delivered)
    init("ko")
    try:
        await obs._run_for_chat(100)
    finally:
        init("en")

    assert "하트비트 점검을 멈췄어요" in delivered.await_args.args[1]


async def test_invalid_session_quarantines_once_then_new_session_resumes() -> None:
    session = _session(100)
    obs = _observer([session])
    handler = AsyncMock(return_value=_outcome(HeartbeatStatus.FAILED, reason="invalid_session"))
    delivered = AsyncMock()
    obs.set_heartbeat_handler(handler)
    obs.set_result_handler(delivered)
    await obs._run_for_chat(100)
    state = obs._state["tg:100"]
    assert (state.quarantine_sid, state.quarantine_notified) == (MARKER[1], True)
    assert delivered.await_count == 1
    assert "Heartbeat checks stopped" in delivered.await_args.args[1]
    await obs._run_for_chat(100)
    assert handler.await_count == 1
    session.session_id = "new-session"
    await obs._run_for_chat(100)
    assert handler.await_count == 2
    assert state.quarantine_sid is None
    assert state.quarantine_notified is False
    handler.return_value = HeartbeatOutcome(
        HeartbeatStatus.FAILED,
        marker=(MARKER[0], "new-session", MARKER[2]),
        reason="invalid_session",
    )
    await obs._run_for_chat(100)
    assert delivered.await_count == 2


async def test_cli_error_backoff_is_1_2_4_4_and_wake_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(100)
    obs = _observer([session])
    obs.set_heartbeat_handler(
        AsyncMock(return_value=_outcome(HeartbeatStatus.FAILED, reason="cli_error"))
    )
    now = 1000.0
    monkeypatch.setattr("ductor_bot.heartbeat.observer.time.time", lambda: now)
    for failure, multiplier in enumerate([1, 2, 4, 4], 1):
        await obs._run_for_chat(100)
        state = obs._state["tg:100"]
        assert state.fail_count == failure
        assert state.retry_after == now + multiplier * 60
        now = state.retry_after
    obs.notify_activity(session.session_key)
    assert (state.fail_count, state.retry_after) == (0, None)


async def test_allowlist_removed_after_selection_or_during_alert() -> None:
    session = _session(-1001, topic_id=7)
    obs = _observer([session])
    handler = AsyncMock(return_value=_outcome(HeartbeatStatus.ALERT, text="alert"))
    delivered = AsyncMock()
    obs.set_heartbeat_handler(handler)
    obs.set_result_handler(delivered)
    targets = await obs._collect_targets()
    assert len(targets) == 1
    obs._config.allowed_group_ids.clear()
    await obs._tick_group_targets(targets)
    handler.assert_not_awaited()

    obs._config.allowed_group_ids.append(-1001)

    async def remove_allowlist(*_args: object) -> HeartbeatOutcome:
        obs._config.allowed_group_ids.clear()
        return _outcome(HeartbeatStatus.ALERT, text="alert")

    obs.set_heartbeat_handler(remove_allowlist)
    await obs._run_for_chat(-1001, 7, source="auto")
    delivered.assert_not_awaited()


async def test_other_topic_busy_does_not_skip_heartbeat() -> None:
    session = _session(-1001, topic_id=7)
    obs = _observer([session])
    handler = AsyncMock(return_value=HeartbeatOutcome(HeartbeatStatus.SKIPPED, reason="cooldown"))
    obs.set_heartbeat_handler(handler)
    obs.set_busy_check(lambda chat_id, topic_id: (chat_id, topic_id) == (-1001, 8))

    await obs._run_for_chat(-1001, 7, source="auto")

    handler.assert_awaited_once_with(-1001, 7, None, None, "tg")
    assert obs._reasons["tg:-1001:7"] == "cooldown"


async def test_same_topic_busy_skips_heartbeat() -> None:
    session = _session(-1001, topic_id=7)
    obs = _observer([session])
    handler = AsyncMock(return_value=HeartbeatOutcome(HeartbeatStatus.SKIPPED))
    obs.set_heartbeat_handler(handler)
    obs.set_busy_check(lambda chat_id, topic_id: (chat_id, topic_id) == (-1001, 7))

    await obs._run_for_chat(-1001, 7, source="auto")

    handler.assert_not_awaited()
    assert obs._reasons["tg:-1001:7"] == "busy"


async def test_manual_transport_permission_rules() -> None:
    tg = _session(-1002, topic_id=7)
    mx = _session(200, transport="mx")
    manual = [
        HeartbeatTarget(chat_id=-1002, topic_id=7),
        HeartbeatTarget(chat_id=200, transport="mx"),
    ]
    obs = _observer([tg, mx], auto_targets=False, manual=manual)
    obs._config.allowed_user_ids.clear()
    handler = AsyncMock(return_value=HeartbeatOutcome(HeartbeatStatus.SKIPPED, reason="cooldown"))
    obs.set_heartbeat_handler(handler)
    await obs._tick_group_targets()
    handler.assert_awaited_once_with(200, None, obs._hb.prompt, obs._hb.ack_token, "mx")


@pytest.mark.parametrize("wake", ["user", "bus", "session"])
@pytest.mark.parametrize("reason", ["cli_error", "invalid_session"])
async def test_failed_result_cannot_restore_state_after_wake(wake: str, reason: str) -> None:
    session = _session(100)
    obs = _observer([session], suspend_on_ack=False)
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(*_args: object) -> HeartbeatOutcome:
        started.set()
        await release.wait()
        return _outcome(HeartbeatStatus.FAILED, reason=reason)

    obs.set_heartbeat_handler(handler)
    task = asyncio.create_task(obs._run_for_chat(100))
    await started.wait()
    if wake == "user":
        obs.notify_activity(session.session_key)
    elif wake == "bus":
        await obs.wake(session.session_key)
    else:
        session.session_id = "new-session"
    release.set()
    await task
    state = obs._state["tg:100"]
    if wake == "session" and reason == "cli_error":
        assert state.retry_after is not None
    else:
        assert state.retry_after is None
    assert state.quarantine_sid is None


async def test_suspend_off_runs_twice_and_prevents_inflight_duplicate() -> None:
    session = _session(100)
    obs = _observer([session], suspend_on_ack=False)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(*_args: object) -> HeartbeatOutcome:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _outcome(HeartbeatStatus.ACK)

    obs.set_heartbeat_handler(handler)
    task = asyncio.create_task(obs._run_for_chat(100))
    await started.wait()
    await obs._run_for_chat(100)
    assert calls == 1
    release.set()
    await task
    await obs._run_for_chat(100)
    assert calls == 2
    assert obs._state["tg:100"].acked is None


async def test_d5_transitions_and_run_logs(caplog: pytest.LogCaptureFixture) -> None:
    session = _session(100)
    obs = _observer([session])
    handler = AsyncMock(return_value=HeartbeatOutcome(HeartbeatStatus.SKIPPED, reason="cooldown"))
    obs.set_heartbeat_handler(handler)
    with caplog.at_level(logging.INFO, logger="ductor_bot.heartbeat.observer"):
        await obs._run_for_chat(100)
        await obs._run_for_chat(100)
        handler.return_value = _outcome(HeartbeatStatus.ACK)
        await obs._run_for_chat(100)
        await obs._run_for_chat(100)
    messages = [record.message for record in caplog.records]
    assert messages.count("Heartbeat tg:100: cooldown") == 1
    assert messages.count("Heartbeat tg:100: ran") == 1
    assert messages.count("Heartbeat tg:100: dormant") == 1
    runs = [line for line in messages if line.startswith("Heartbeat run key=")]
    assert len(runs) == 1
    assert "outcome=ack" in runs[0]
    assert "marker=claude:session-:3" in runs[0]
    assert sum(line.startswith("Heartbeat dormant key=") for line in messages) == 1


async def test_no_session_reason_logs_only_on_transition(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sessions: list[SessionData] = []
    obs = _observer(sessions, auto_targets=False)

    async def handler(*_args: object) -> HeartbeatOutcome:
        return HeartbeatOutcome(
            HeartbeatStatus.SKIPPED,
            reason="session_ready" if sessions else "no_session",
        )

    obs.set_heartbeat_handler(handler)
    with caplog.at_level(logging.INFO, logger="ductor_bot.heartbeat.observer"):
        await obs._tick()
        await obs._tick()
        assert [record.message for record in caplog.records].count(
            "Heartbeat tg:100: no_session"
        ) == 1
        sessions.append(_session(100))
        await obs._tick()
        sessions.clear()
        await obs._tick()
    messages = [record.message for record in caplog.records]
    assert messages.count("Heartbeat tg:100: no_session") == 2
    assert messages.count("Heartbeat tg:100: session_ready") == 1

    obs._config.allowed_user_ids.clear()
    await obs._collect_targets()
    assert "tg:100" not in obs._reasons


async def test_quarantine_notice_retries_without_cli_after_send_error() -> None:
    session = _session(100)
    obs = _observer([session])
    handler = AsyncMock(return_value=_outcome(HeartbeatStatus.FAILED, reason="invalid_session"))
    delivered = AsyncMock(side_effect=[RuntimeError("send failed"), None])
    obs.set_heartbeat_handler(handler)
    obs.set_result_handler(delivered)

    await obs._tick()
    state = obs._state["tg:100"]
    assert state.quarantine_sid == MARKER[1]
    assert state.quarantine_notified is False
    assert handler.await_count == 1
    assert delivered.await_count == 1

    await obs._tick()
    assert handler.await_count == 1
    assert delivered.await_count == 2
    assert state.quarantine_notified is True

    await obs._tick()
    assert handler.await_count == 1
    assert delivered.await_count == 2


async def test_quarantine_notice_retries_after_permission_returns() -> None:
    session = _session(100)
    obs = _observer([session])
    handler = AsyncMock(return_value=_outcome(HeartbeatStatus.FAILED, reason="invalid_session"))
    delivered = AsyncMock()
    obs.set_heartbeat_handler(handler)
    obs.set_result_handler(delivered)

    async def revoke_during_run(*_args: object) -> HeartbeatOutcome:
        obs._config.allowed_user_ids.clear()
        return _outcome(HeartbeatStatus.FAILED, reason="invalid_session")

    obs.set_heartbeat_handler(revoke_during_run)
    await obs._run_for_chat(100, source="auto")
    assert obs._state["tg:100"].quarantine_notified is False
    delivered.assert_not_awaited()

    obs._config.allowed_user_ids.append(100)
    obs.set_heartbeat_handler(handler)
    await obs._tick()
    handler.assert_not_awaited()
    delivered.assert_awaited_once()
    assert obs._state["tg:100"].quarantine_notified is True


async def test_old_quarantine_notice_cannot_mark_new_session_notified() -> None:
    session = _session(100)
    obs = _observer([session])
    handler = AsyncMock(return_value=_outcome(HeartbeatStatus.FAILED, reason="invalid_session"))
    obs.set_heartbeat_handler(handler)
    sending = asyncio.Event()
    release = asyncio.Event()
    sent = 0

    async def deliver(*_args: object) -> None:
        nonlocal sent
        sent += 1
        if sent == 1:
            sending.set()
            await release.wait()

    obs.set_result_handler(deliver)
    old_run = asyncio.create_task(obs._run_for_chat(100))
    await sending.wait()
    state = obs._state["tg:100"]
    session.session_id = "new-session"
    await obs._run_for_chat(100)
    assert state.quarantine_sid is None
    assert state.quarantine_notified is False
    assert handler.await_count == 1
    release.set()
    await old_run
    assert state.quarantine_notified is False

    handler.return_value = HeartbeatOutcome(
        HeartbeatStatus.FAILED,
        marker=(MARKER[0], "new-session", MARKER[2]),
        reason="invalid_session",
    )
    await obs._run_for_chat(100)
    assert state.quarantine_sid == "new-session"
    assert state.quarantine_notified is True
    assert sent == 2


async def test_concurrent_quarantine_retries_send_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _session(100)
    obs = _observer([session])
    obs._state["tg:100"] = _KeyState(
        tracked=True, quarantine_sid=MARKER[1], quarantine_notified=False
    )
    handler = AsyncMock()
    obs.set_heartbeat_handler(handler)
    sending = asyncio.Event()
    release = asyncio.Event()
    delivered = AsyncMock()

    async def deliver(*args: object) -> None:
        await delivered(*args)
        sending.set()
        await release.wait()

    obs.set_result_handler(deliver)
    with caplog.at_level(logging.INFO, logger="ductor_bot.heartbeat.observer"):
        first = asyncio.create_task(obs._run_for_chat(100))
        await sending.wait()
        await obs._run_for_chat(100)
        assert [record.message for record in caplog.records].count(
            "Heartbeat tg:100: inflight"
        ) == 1
        release.set()
        await first
    delivered.assert_awaited_once()
    handler.assert_not_awaited()
    assert obs._state["tg:100"].quarantine_notified is True


async def test_quarantine_retry_blocks_cli_while_notice_is_sending() -> None:
    session = _session(100)
    obs = _observer([session])
    obs._state["tg:100"] = _KeyState(
        tracked=True, quarantine_sid=MARKER[1], quarantine_notified=False
    )
    handler = AsyncMock(return_value=_outcome(HeartbeatStatus.ACK))
    obs.set_heartbeat_handler(handler)
    sending = asyncio.Event()
    release = asyncio.Event()

    async def deliver(*_args: object) -> None:
        sending.set()
        await release.wait()

    obs.set_result_handler(deliver)
    first = asyncio.create_task(obs._run_for_chat(100))
    await sending.wait()
    session.session_id = "new-session"
    await obs._run_for_chat(100)
    handler.assert_not_awaited()
    release.set()
    await first
    assert obs._state["tg:100"].quarantine_notified is False
    assert not obs._inflight


async def test_dormant_log_requires_successful_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from ductor_bot.heartbeat import observer as observer_module

    session = _session(100)
    path = tmp_path / "failed" / "heartbeat_state.json"
    obs = _observer([session])
    obs.set_state_path(path)
    await obs._ensure_loaded()
    obs.set_heartbeat_handler(AsyncMock(return_value=_outcome(HeartbeatStatus.ACK)))

    def fail_save(*_args: object) -> None:
        raise OSError("disk error")

    with monkeypatch.context() as patch:
        patch.setattr(observer_module, "atomic_json_save", fail_save)
        with caplog.at_level(logging.INFO, logger="ductor_bot.heartbeat.observer"):
            await obs._run_for_chat(100)
            await asyncio.gather(*obs._persist_tasks)
    assert not path.exists()
    assert not any(record.message.startswith("Heartbeat dormant key=") for record in caplog.records)

    path = tmp_path / "saved" / "heartbeat_state.json"
    saved = _observer([session])
    saved.set_state_path(path)
    saved.set_heartbeat_handler(AsyncMock(return_value=_outcome(HeartbeatStatus.ACK)))
    acked_at_log: list[list[object]] = []

    class CaptureSavedMarker(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage().startswith("Heartbeat dormant key="):
                acked_at_log.append(json.loads(path.read_text())["state"]["tg:100"]["acked"])

    logger = logging.getLogger("ductor_bot.heartbeat.observer")
    capture = CaptureSavedMarker()
    logger.addHandler(capture)
    try:
        with caplog.at_level(logging.INFO, logger="ductor_bot.heartbeat.observer"):
            await saved._run_for_chat(100)
    finally:
        logger.removeHandler(capture)
    assert acked_at_log == [list(MARKER)]


async def test_run_log_is_one_per_topic_and_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sessions = [_session(-1001, topic_id=7), _session(-1001, topic_id=8)]
    obs = _observer(sessions)
    obs.set_heartbeat_handler(AsyncMock(return_value=_outcome(HeartbeatStatus.ALERT, text="a")))
    with caplog.at_level(logging.INFO, logger="ductor_bot.heartbeat.observer"):
        await obs._tick()
        obs.set_heartbeat_handler(AsyncMock(side_effect=ValueError("before CLI")))
        await obs._run_for_chat(-1001, 7, source="auto")
    runs = [
        record.message
        for record in caplog.records
        if record.message.startswith("Heartbeat run key=")
    ]
    assert len(runs) == 3
    assert any("key=tg:-1001:7" in line for line in runs)
    assert any("key=tg:-1001:8" in line for line in runs)
    assert sum("outcome=exception reason=ValueError marker=-" in line for line in runs) == 1


async def test_wake_and_dormant_log_only_on_real_change(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _session(100)
    obs = _observer([session])
    obs.set_heartbeat_handler(AsyncMock(return_value=_outcome(HeartbeatStatus.ACK)))
    with caplog.at_level(logging.INFO, logger="ductor_bot.heartbeat.observer"):
        obs.notify_activity(session.session_key)
        await obs._run_for_chat(100)
        obs.notify_activity(session.session_key)
        obs.notify_activity(session.session_key)
    messages = [record.message for record in caplog.records]
    assert sum(line.startswith("Heartbeat dormant key=") for line in messages) == 1
    assert messages.count("Heartbeat wake key=tg:100 via=user") == 1


async def test_manual_tg_removed_during_alert_is_not_delivered() -> None:
    session = _session(-1001, topic_id=7)
    obs = _observer(
        [session], auto_targets=False, manual=[HeartbeatTarget(chat_id=-1001, topic_id=7)]
    )
    obs._config.allowed_user_ids.clear()
    delivered = AsyncMock()
    obs.set_result_handler(delivered)

    async def handler(*_args: object) -> HeartbeatOutcome:
        obs._config.allowed_group_ids.clear()
        return _outcome(HeartbeatStatus.ALERT, text="alert")

    obs.set_heartbeat_handler(handler)
    await obs._tick_group_targets()
    delivered.assert_not_awaited()


async def test_alert_is_delivered_even_after_generation_changes() -> None:
    session = _session(100)
    obs = _observer([session])
    delivered = AsyncMock()
    obs.set_result_handler(delivered)

    async def handler(*_args: object) -> HeartbeatOutcome:
        obs.notify_activity(session.session_key)
        return _outcome(HeartbeatStatus.ALERT, text="alert")

    obs.set_heartbeat_handler(handler)
    await obs._run_for_chat(100)
    delivered.assert_awaited_once()


async def test_old_tracked_backoff_and_quarantine_are_respected() -> None:
    session = _session(100)
    obs = _observer([session])
    key = session.session_key
    obs._state[key.storage_key] = _KeyState(tracked=True, retry_after=time.time() + 1000)
    handler = AsyncMock(return_value=_outcome(HeartbeatStatus.ACK))
    obs.set_heartbeat_handler(handler)
    await obs._tick()
    handler.assert_not_awaited()
    obs._state[key.storage_key].retry_after = time.time() - 1
    await obs._tick()
    handler.assert_awaited_once()


async def test_old_tracked_restarts_with_backoff_and_quarantine(tmp_path: Path) -> None:
    old = datetime.now(UTC) - timedelta(days=8)
    backoff_session = _session(100)
    quarantine_session = _session(-1001, topic_id=7)
    backoff_session.last_active = old.isoformat()
    quarantine_session.last_active = old.isoformat()
    backoff_session.activity_at = old.isoformat()
    quarantine_session.activity_at = old.isoformat()
    sessions = [backoff_session, quarantine_session]
    path = tmp_path / "heartbeat_state.json"
    first = _observer(sessions)
    first.set_state_path(path)
    await first._ensure_loaded()
    first._state["tg:100"] = _KeyState(tracked=True, fail_count=2, retry_after=time.time() + 1000)
    first._state["tg:-1001:7"] = _KeyState(
        tracked=True, quarantine_sid=MARKER[1], quarantine_notified=True
    )
    assert await first._persist_now()

    restored = _observer(sessions)
    restored.set_state_path(path)
    handler = AsyncMock(return_value=_outcome(HeartbeatStatus.ACK))
    restored.set_heartbeat_handler(handler)
    await restored._tick()
    handler.assert_not_awaited()
    assert restored._state["tg:100"].fail_count == 2
    assert restored._state["tg:-1001:7"].quarantine_sid == MARKER[1]
    restored._state["tg:100"].retry_after = time.time() - 1
    await restored._tick()
    handler.assert_awaited_once_with(100, None, None, None, "tg")


async def test_wake_during_ack_has_no_dormant_log_until_next_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _session(100)
    obs = _observer([session])
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(*_args: object) -> HeartbeatOutcome:
        started.set()
        await release.wait()
        return _outcome(HeartbeatStatus.ACK)

    obs.set_heartbeat_handler(handler)
    with caplog.at_level(logging.INFO, logger="ductor_bot.heartbeat.observer"):
        task = asyncio.create_task(obs._run_for_chat(100))
        await started.wait()
        obs.notify_activity(session.session_key)
        release.set()
        await task
        assert not any(
            record.message.startswith("Heartbeat dormant key=") for record in caplog.records
        )
        await obs._run_for_chat(100)
    messages = [record.message for record in caplog.records]
    assert sum(line.startswith("Heartbeat run key=") for line in messages) == 2
    assert sum(line.startswith("Heartbeat dormant key=") for line in messages) == 1


async def test_custom_interval_loop_uses_result_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = HeartbeatTarget(chat_id=-1001, topic_id=7, interval_minutes=5)
    session = _session(-1001, topic_id=7)
    obs = _observer([session], auto_targets=False, manual=[target])
    obs._running = True
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    monkeypatch.setattr("ductor_bot.heartbeat.observer.time.time", lambda: 1000.0)

    async def handler(*_args: object) -> HeartbeatOutcome:
        obs._running = False
        return _outcome(HeartbeatStatus.FAILED, reason="cli_error")

    obs.set_heartbeat_handler(handler)
    await obs._target_loop(target, 5)
    assert obs._state["tg:-1001:7"].retry_after == 1300.0
