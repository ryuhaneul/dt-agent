"""Target collection and lost-state recovery for heartbeat checks."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from ductor_bot.config import AgentConfig, HeartbeatConfig, HeartbeatTarget
from ductor_bot.heartbeat.models import HeartbeatOutcome, HeartbeatStatus
from ductor_bot.heartbeat.observer import HeartbeatObserver, _KeyState, _Target
from ductor_bot.session.key import SessionKey
from ductor_bot.session.manager import SessionData

if TYPE_CHECKING:
    import pytest


def _session(
    chat_id: int,
    *,
    topic_id: int | None = None,
    transport: str = "tg",
    sid: str = "sid",
    age_hours: int = 1,
    last_active: str | None = None,
) -> SessionData:
    return SessionData(
        chat_id=chat_id,
        topic_id=topic_id,
        transport=transport,
        provider="claude",
        model="opus",
        session_id=sid,
        last_active=last_active or (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat(),
    )


def _observer(
    sessions: list[SessionData],
    *,
    auto_targets: bool = True,
    allowed_users: list[int] | None = None,
    allowed_groups: list[int] | None = None,
    manual: list[HeartbeatTarget] | None = None,
    path: Path | None = None,
) -> HeartbeatObserver:
    config = AgentConfig(
        heartbeat=HeartbeatConfig(
            enabled=True,
            auto_targets=auto_targets,
            suspend_on_ack=True,
            group_targets=manual or [],
            quiet_start=0,
            quiet_end=0,
        ),
        allowed_user_ids=allowed_users if allowed_users is not None else [100],
        allowed_group_ids=allowed_groups if allowed_groups is not None else [-1001],
    )
    observer = HeartbeatObserver(config)
    if path is not None:
        observer.set_state_path(path)

    async def list_all() -> list[SessionData]:
        return list(sessions)

    async def get_active(key: SessionKey) -> SessionData | None:
        return next((session for session in sessions if session.session_key == key), None)

    observer.set_session_source(list_all, get_active)
    return observer


def _keys(targets: list[_Target]) -> set[str]:
    return {target.key.storage_key for target in targets}


async def test_auto_targets_include_allowed_tg_sessions_within_window(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sessions = [
        _session(-1001, topic_id=7),
        _session(100),
        _session(100, topic_id=8),
        _session(-1002, topic_id=9),
        _session(200),
        _session(100, topic_id=11, transport="mx"),
        _session(-1001, topic_id=12, sid=""),
        _session(-1001, topic_id=13, age_hours=24 * 8),
    ]
    obs = _observer(sessions)
    with caplog.at_level("INFO", logger="ductor_bot.heartbeat.observer"):
        first = await obs._collect_targets()
        second = await obs._collect_targets()
        sessions.remove(sessions[2])
        third = await obs._collect_targets()

    expected = {"tg:-1001:7", "tg:100", "tg:100:8"}
    assert _keys(first) == _keys(second) == expected
    assert all(target.source == "auto" for target in first)
    assert _keys(third) == expected - {"tg:100:8"}
    lines = [r.message for r in caplog.records if r.message.startswith("Heartbeat auto targets:")]
    assert lines == ["Heartbeat auto targets: 3", "Heartbeat auto targets: 2"]


async def test_default_auto_targets_select_recent_dm_and_each_allowed_group_topic() -> None:
    example = Path(__file__).parents[2] / "config.example.json"
    example_heartbeat = json.loads(
        await asyncio.to_thread(example.read_text, encoding="utf-8")
    )["heartbeat"]
    sessions = [
        _session(100),
        _session(-1001, topic_id=7),
        _session(-1001, topic_id=8),
        _session(200),
        _session(-1002, topic_id=9),
        _session(-1001, topic_id=10, age_hours=24 * 8),
        _session(100, topic_id=11, age_hours=24 * 8),
        _session(-1001, topic_id=12, sid=""),
    ]
    for heartbeat in (HeartbeatConfig(), HeartbeatConfig.model_validate(example_heartbeat)):
        assert heartbeat.auto_targets is True
        config = AgentConfig(
            heartbeat=heartbeat,
            allowed_user_ids=[100],
            allowed_group_ids=[-1001],
        )
        obs = HeartbeatObserver(config)
        obs.set_session_source(AsyncMock(return_value=sessions), AsyncMock())
        targets = await obs._collect_targets()
        assert _keys(targets) == {"tg:100", "tg:-1001:7", "tg:-1001:8"}
        assert all(target.source == "auto" for target in targets)


async def test_default_ack_suspends_until_user_activity_in_that_session() -> None:
    example = Path(__file__).parents[2] / "config.example.json"
    example_heartbeat = json.loads(
        await asyncio.to_thread(example.read_text, encoding="utf-8")
    )["heartbeat"]
    for heartbeat in (
        HeartbeatConfig(enabled=True),
        HeartbeatConfig.model_validate(example_heartbeat),
    ):
        assert heartbeat.suspend_on_ack is True
        assert heartbeat.ack_token == "HEARTBEAT_OK"
        sessions = [_session(100), _session(-1001, topic_id=7), _session(-1001, topic_id=8)]
        by_key = {session.session_key: session for session in sessions}
        obs = HeartbeatObserver(
            AgentConfig(
                heartbeat=heartbeat,
                allowed_user_ids=[100],
                allowed_group_ids=[-1001],
            )
        )
        obs.set_session_source(
            AsyncMock(return_value=sessions),
            AsyncMock(side_effect=by_key.get),
        )

        async def acknowledge(
            chat_id: int,
            topic_id: int | None,
            _prompt: str | None,
            _ack_token: str | None,
            transport: str,
            *,
            current_sessions: dict[SessionKey, SessionData] = by_key,
        ) -> HeartbeatOutcome:
            session = current_sessions[SessionKey.for_transport(transport, chat_id, topic_id)]
            return HeartbeatOutcome(
                HeartbeatStatus.ACK,
                marker=(session.provider, session.session_id, session.message_count),
            )

        handler = AsyncMock(side_effect=acknowledge)
        obs.set_heartbeat_handler(handler)
        delivery = AsyncMock()
        obs.set_result_handler(delivery)
        await obs._tick()
        assert handler.await_count == 3
        assert all(obs._state[key.storage_key].acked is not None for key in by_key)
        await obs._tick()
        assert handler.await_count == 3
        assert set(obs._reasons.values()) == {"dormant"}
        delivery.assert_not_awaited()

        changed = sessions[1]
        changed.message_count += 1
        obs.notify_activity(changed.session_key)
        assert obs._state[changed.session_key.storage_key].acked is None
        await obs._tick()
        assert handler.await_count == 4
        handler.assert_awaited_with(-1001, 7, None, None, "tg")
        assert all(obs._state[key.storage_key].acked is not None for key in by_key)
        delivery.assert_not_awaited()


async def test_outside_window_remains_target_when_tracked() -> None:
    old = _session(-1001, topic_id=7, age_hours=24 * 8)
    obs = _observer([old])
    obs._state[old.session_key.storage_key] = _KeyState(
        tracked=True, fail_count=2, retry_after=9999999999.0
    )
    targets = await obs._collect_targets()
    assert _keys(targets) == {old.session_key.storage_key}
    assert obs._state[old.session_key.storage_key].retry_after == 9999999999.0


async def test_manual_target_wins_and_disabled_target_opts_out() -> None:
    sessions = [_session(-1001, topic_id=7), _session(-1001, topic_id=8)]
    manual = [
        HeartbeatTarget(chat_id=-1001, topic_id=7, prompt="manual check"),
        HeartbeatTarget(chat_id=-1001, topic_id=8, enabled=False),
    ]
    obs = _observer(sessions, manual=manual)
    targets = await obs._collect_targets()
    assert len(targets) == 1
    assert targets[0].source == "manual"
    assert targets[0].config == manual[0]
    handler = AsyncMock(return_value=HeartbeatOutcome(HeartbeatStatus.SKIPPED))
    obs.set_heartbeat_handler(handler)
    await obs._tick()
    handler.assert_awaited_once_with(-1001, 7, "manual check", obs._hb.ack_token, "tg")


async def test_auto_targets_off_keeps_default_dm_root_and_manual_only() -> None:
    sessions = [_session(100), _session(100, topic_id=7), _session(-1001, topic_id=8)]
    manual = [HeartbeatTarget(chat_id=-1001, topic_id=8)]
    obs = _observer(sessions, auto_targets=False, manual=manual)
    targets = await obs._collect_targets()
    assert _keys(targets) == {"tg:100", "tg:-1001:8"}
    assert {target.key.storage_key: target.source for target in targets} == {
        "tg:100": "auto",
        "tg:-1001:8": "manual",
    }


async def test_state_cleanup_removes_missing_or_disallowed_keys() -> None:
    manual_session = _session(-1002, topic_id=9)
    auto_session = _session(-1001, topic_id=7)
    disallowed = _session(-1003, topic_id=8)
    obs = _observer(
        [manual_session, auto_session, disallowed],
        manual=[HeartbeatTarget(chat_id=-1002, topic_id=9)],
    )
    for key in ("tg:-1002:9", "tg:-1003:8", "tg:-1004:10"):
        obs._state[key] = _KeyState(tracked=True)
    await obs._collect_targets()
    assert set(obs._state) == {"tg:-1002:9"}


async def test_bad_timestamp_excludes_only_that_session_and_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad = _session(-1001, topic_id=7)
    bad.last_active = "not-a-date"
    bad.activity_at = "not-a-date"
    good = _session(100, topic_id=8)
    obs = _observer([bad, good])
    with caplog.at_level("INFO", logger="ductor_bot.heartbeat.observer"):
        first = await obs._collect_targets()
        second = await obs._collect_targets()
    assert _keys(first) == _keys(second) == {"tg:100:8"}
    lines = [r.message for r in caplog.records if "bad_timestamp" in r.message]
    assert lines == ["Heartbeat tg:-1001:7: bad_timestamp"]


async def test_bad_timestamp_logs_again_after_session_disappears(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad = _session(-1001, topic_id=7, last_active="not-a-date")
    sessions = [bad]
    obs = _observer(sessions)
    with caplog.at_level("INFO", logger="ductor_bot.heartbeat.observer"):
        await obs._collect_targets()
        await obs._collect_targets()
        sessions.clear()
        await obs._collect_targets()
        assert bad.session_key.storage_key not in obs._reasons
        sessions.append(bad)
        await obs._collect_targets()
        await obs._collect_targets()
    lines = [record.message for record in caplog.records if "bad_timestamp" in record.message]
    assert lines == ["Heartbeat tg:-1001:7: bad_timestamp"] * 2


async def test_lost_state_recovery_scans_old_tracked_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "heartbeat_state.json"
    old = _session(-1001, topic_id=7, age_hours=24 * 8)
    first = _observer([old], path=path)
    await first._ensure_loaded()
    first._state[old.session_key.storage_key] = _KeyState(
        acked=("claude", old.session_id, old.message_count), tracked=True
    )
    assert await first._persist_now()

    def fail_save(*_args: object) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patcher:
        patcher.setattr("ductor_bot.heartbeat.observer.atomic_json_save", fail_save)
        await first.wake(old.session_key)
    assert not path.exists()

    restored = _observer([old], path=path)
    handler = AsyncMock(return_value=HeartbeatOutcome(HeartbeatStatus.SKIPPED))
    restored.set_heartbeat_handler(handler)
    await restored._tick()
    handler.assert_awaited_once_with(-1001, 7, None, None, "tg")
    assert restored._state[old.session_key.storage_key].tracked is True
    assert restored._recovery_pending is False
    assert json.loads(path.read_text())["recovery_pending"] is False


async def test_recovery_waits_while_auto_off_then_scans_when_enabled(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat_state.json"
    (tmp_path / "heartbeat_state.initialized").touch()
    old = _session(-1001, topic_id=7, age_hours=24 * 8)
    obs = _observer([old], auto_targets=False, allowed_users=[], path=path)
    handler = AsyncMock(return_value=HeartbeatOutcome(HeartbeatStatus.SKIPPED))
    obs.set_heartbeat_handler(handler)
    await obs._tick()
    handler.assert_not_awaited()
    assert obs._recovery_pending is True
    assert json.loads(path.read_text())["recovery_pending"] is True

    obs._config.heartbeat.auto_targets = True
    obs.reset_suspend_state()
    await obs._tick()
    handler.assert_awaited_once_with(-1001, 7, None, None, "tg")
    assert obs._recovery_pending is False
    assert obs._state[old.session_key.storage_key].tracked is True
    await obs._persist_now()
    assert json.loads(path.read_text())["recovery_pending"] is False


async def test_corrupt_state_scans_old_session_once_without_init_marker(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat_state.json"
    path.write_text("{broken")
    old = _session(-1001, topic_id=7, age_hours=24 * 8)
    obs = _observer([old], path=path)
    handler = AsyncMock(return_value=HeartbeatOutcome(HeartbeatStatus.SKIPPED))
    obs.set_heartbeat_handler(handler)

    await obs._tick()
    handler.assert_awaited_once()
    assert obs._recovery_pending is False
    assert obs._state[old.session_key.storage_key].tracked is True
    corrupt = await asyncio.to_thread(lambda: list(tmp_path.glob("heartbeat_state.json.corrupt-*")))
    assert len(corrupt) == 1
    assert await asyncio.to_thread(corrupt[0].read_text) == "{broken"

    obs._state[old.session_key.storage_key].tracked = False
    await obs._tick()
    assert handler.await_count == 1
