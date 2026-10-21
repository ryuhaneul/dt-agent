"""Tests for the heartbeat observer."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import time_machine

from ductor_bot.config import AgentConfig, HeartbeatConfig, HeartbeatTarget
from ductor_bot.heartbeat.models import HeartbeatOutcome, HeartbeatStatus
from ductor_bot.heartbeat.observer import HeartbeatObserver, _KeyState
from ductor_bot.orchestrator.flows import _strip_ack_token
from ductor_bot.session.key import SessionKey
from ductor_bot.utils.quiet_hours import is_quiet_hour

ACK = HeartbeatOutcome(HeartbeatStatus.ACK, marker=("claude", "sid", 1))
SKIPPED = HeartbeatOutcome(HeartbeatStatus.SKIPPED)


def _alert(text: str) -> HeartbeatOutcome:
    return HeartbeatOutcome(HeartbeatStatus.ALERT, text)


# ---------------------------------------------------------------------------
# Quiet hour logic
# ---------------------------------------------------------------------------


class TestIsQuietHour:
    def test_within_evening_quiet(self) -> None:
        # quiet 21-08: 22 is quiet
        assert is_quiet_hour(22, 21, 8) is True

    def test_within_morning_quiet(self) -> None:
        # quiet 21-08: 3 is quiet
        assert is_quiet_hour(3, 21, 8) is True

    def test_boundary_start_is_quiet(self) -> None:
        assert is_quiet_hour(21, 21, 8) is True

    def test_boundary_end_is_not_quiet(self) -> None:
        # end is exclusive
        assert is_quiet_hour(8, 21, 8) is False

    def test_daytime_is_not_quiet(self) -> None:
        assert is_quiet_hour(14, 21, 8) is False

    def test_no_wrap_quiet_window(self) -> None:
        # quiet 2-6: 4 is quiet, 1 is not
        assert is_quiet_hour(4, 2, 6) is True
        assert is_quiet_hour(1, 2, 6) is False
        assert is_quiet_hour(7, 2, 6) is False

    def test_midnight_in_wrap_window(self) -> None:
        assert is_quiet_hour(0, 21, 8) is True

    def test_same_start_end_means_never_quiet(self) -> None:
        assert is_quiet_hour(12, 8, 8) is False
        assert is_quiet_hour(8, 8, 8) is False


# ---------------------------------------------------------------------------
# ACK token stripping
# ---------------------------------------------------------------------------


class TestStripAckToken:
    def test_exact_token(self) -> None:
        assert _strip_ack_token("HEARTBEAT_OK", "HEARTBEAT_OK") == ""

    def test_token_with_whitespace(self) -> None:
        assert _strip_ack_token("  HEARTBEAT_OK  ", "HEARTBEAT_OK") == ""

    def test_leading_token(self) -> None:
        assert _strip_ack_token("HEARTBEAT_OK Some extra text", "HEARTBEAT_OK") == "Some extra text"

    def test_trailing_token(self) -> None:
        assert _strip_ack_token("Some text HEARTBEAT_OK", "HEARTBEAT_OK") == "Some text"

    def test_no_token(self) -> None:
        assert _strip_ack_token("Hello world", "HEARTBEAT_OK") == "Hello world"

    def test_empty_input(self) -> None:
        assert _strip_ack_token("", "HEARTBEAT_OK") == ""

    def test_token_in_middle_not_stripped(self) -> None:
        result = _strip_ack_token("Before HEARTBEAT_OK After", "HEARTBEAT_OK")
        # Leading strip removes HEARTBEAT_OK, leaving trailing intact
        assert "Before" not in result or "After" in result


# ---------------------------------------------------------------------------
# Observer lifecycle
# ---------------------------------------------------------------------------


def _make_config(*, enabled: bool = True, interval: int = 30) -> AgentConfig:
    return AgentConfig(
        heartbeat=HeartbeatConfig(enabled=enabled, interval_minutes=interval, auto_targets=False),
        allowed_user_ids=[100, 200],
        allowed_group_ids=[-1001, -1002],
    )


class TestHeartbeatObserverSetup:
    def test_defaults_have_no_quiet_hours(self) -> None:
        example = Path(__file__).resolve().parents[2] / "config.example.json"
        example_heartbeat = json.loads(example.read_text(encoding="utf-8"))["heartbeat"]
        for heartbeat in (HeartbeatConfig(), HeartbeatConfig.model_validate(example_heartbeat)):
            obs = HeartbeatObserver(AgentConfig(heartbeat=heartbeat))
            _, _, start, end = obs._resolve_target_settings(HeartbeatTarget(chat_id=-1001))
            assert (heartbeat.quiet_start, heartbeat.quiet_end, start, end) == (0, 0, 0, 0)
            assert all(not is_quiet_hour(hour, start, end) for hour in range(24))

    async def test_disabled_does_not_start_task(self) -> None:
        config = _make_config(enabled=False)
        obs = HeartbeatObserver(config)
        obs.set_heartbeat_handler(AsyncMock())
        await obs.start()
        assert obs._task is None
        await obs.stop()

    async def test_enabled_starts_task(self) -> None:
        config = _make_config(enabled=True)
        obs = HeartbeatObserver(config)
        obs.set_heartbeat_handler(AsyncMock())
        await obs.start()
        assert obs._task is not None
        await obs.stop()
        assert obs._task is None

    async def test_no_handler_does_not_start(self) -> None:
        config = _make_config(enabled=True)
        obs = HeartbeatObserver(config)
        await obs.start()
        assert obs._task is None


class TestHeartbeatObserverTick:
    async def test_tick_calls_handler_for_each_user(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=SKIPPED)
        obs.set_heartbeat_handler(handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        assert handler.call_count == 2
        handler.assert_any_await(100, None, None, None, "tg")
        handler.assert_any_await(200, None, None, None, "tg")

    async def test_tick_skips_busy_chat(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=SKIPPED)
        obs.set_heartbeat_handler(handler)
        obs.set_busy_check(lambda cid, _topic_id: cid == 100)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        handler.assert_awaited_once_with(200, None, None, None, "tg")

    async def test_tick_delivers_alert(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        obs.set_heartbeat_handler(AsyncMock(return_value=_alert("Hey, check this out!")))
        result_handler = AsyncMock()
        obs.set_result_handler(result_handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        assert result_handler.call_count == 2
        result_handler.assert_any_await(100, "Hey, check this out!", None, "tg")
        result_handler.assert_any_await(200, "Hey, check this out!", None, "tg")

    async def test_tick_suppresses_none_result(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        obs.set_heartbeat_handler(AsyncMock(return_value=SKIPPED))
        result_handler = AsyncMock()
        obs.set_result_handler(result_handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        result_handler.assert_not_awaited()

    @pytest.mark.parametrize("hour", [21, 22, 23, 0, 1, 7])
    async def test_tick_skips_during_quiet_hours(self, hour: int) -> None:
        config = _make_config()
        config.heartbeat.quiet_start = 21
        config.heartbeat.quiet_end = 8
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=SKIPPED)
        obs.set_heartbeat_handler(handler)

        with time_machine.travel(datetime(2026, 1, 15, hour, 30, tzinfo=UTC)):
            await obs._tick()

        handler.assert_not_awaited()

    async def test_tick_runs_during_active_hours(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=SKIPPED)
        obs.set_heartbeat_handler(handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        assert handler.call_count == 2

    async def test_handler_exception_does_not_crash(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        obs.set_heartbeat_handler(AsyncMock(side_effect=RuntimeError("boom")))

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

    async def test_tick_propagates_cancelled_from_stale_cleanup(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        obs.set_heartbeat_handler(AsyncMock(return_value=SKIPPED))
        obs.set_stale_cleanup(AsyncMock(side_effect=asyncio.CancelledError()))

        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            pytest.raises(asyncio.CancelledError),
        ):
            await obs._tick()

    async def test_run_for_chat_propagates_cancelled_from_handler(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        obs.set_heartbeat_handler(AsyncMock(side_effect=asyncio.CancelledError()))

        with pytest.raises(asyncio.CancelledError):
            await obs._run_for_chat(100)

    async def test_run_for_chat_propagates_cancelled_from_result_handler(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        obs.set_heartbeat_handler(AsyncMock(return_value=_alert("alert")))
        obs.set_result_handler(AsyncMock(side_effect=asyncio.CancelledError()))

        with pytest.raises(asyncio.CancelledError):
            await obs._run_for_chat(100)


# ---------------------------------------------------------------------------
# Group targets
# ---------------------------------------------------------------------------


class TestHeartbeatGroupTargets:
    async def test_tick_iterates_group_targets(self) -> None:
        config = AgentConfig(
            heartbeat=HeartbeatConfig(enabled=True, auto_targets=False),
            allowed_user_ids=[100],
            allowed_group_ids=[-1001, -1002],
        )
        config.heartbeat.group_targets = [
            HeartbeatTarget(chat_id=-1001, topic_id=42),
            HeartbeatTarget(chat_id=-1002),
        ]
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=SKIPPED)
        obs.set_heartbeat_handler(handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        assert handler.call_count == 3
        handler.assert_any_await(100, None, None, None, "tg")
        # Group targets get resolved prompt/ack from global config
        handler.assert_any_await(
            -1001, 42, config.heartbeat.prompt, config.heartbeat.ack_token, "tg"
        )
        handler.assert_any_await(
            -1002, None, config.heartbeat.prompt, config.heartbeat.ack_token, "tg"
        )

    async def test_tick_group_target_delivers_alert_with_topic_id(self) -> None:
        config = AgentConfig(
            heartbeat=HeartbeatConfig(enabled=True),
            allowed_user_ids=[],
            allowed_group_ids=[-1001],
        )
        config.heartbeat.group_targets = [HeartbeatTarget(chat_id=-1001, topic_id=7)]
        obs = HeartbeatObserver(config)
        obs.set_heartbeat_handler(AsyncMock(return_value=_alert("group alert")))
        result_handler = AsyncMock()
        obs.set_result_handler(result_handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        result_handler.assert_awaited_once_with(-1001, "group alert", 7, "tg")

    async def test_default_group_targets_with_null_chat_id_are_skipped(self) -> None:
        config = _make_config()
        assert all(t.chat_id is None for t in config.heartbeat.group_targets)
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=SKIPPED)
        obs.set_heartbeat_handler(handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        assert handler.call_count == 2

    async def test_topic_id_flows_through_run_for_chat(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        obs.set_heartbeat_handler(AsyncMock(return_value=_alert("alert")))
        result_handler = AsyncMock()
        obs.set_result_handler(result_handler)

        await obs._run_for_chat(-1001, topic_id=42)

        result_handler.assert_awaited_once_with(-1001, "alert", 42, "tg")

    async def test_run_for_chat_passes_prompt_ack_to_handler(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=SKIPPED)
        obs.set_heartbeat_handler(handler)

        await obs._run_for_chat(-1001, prompt="Custom", ack_token="OK")

        handler.assert_awaited_once_with(-1001, None, "Custom", "OK", "tg")


def _set_session_source(obs: HeartbeatObserver) -> None:
    keys = [SessionKey.telegram(chat_id) for chat_id in obs._config.allowed_user_ids]
    keys.extend(
        SessionKey.for_transport(target.transport, target.chat_id, target.topic_id)
        for target in obs._config.heartbeat.group_targets
        if target.enabled and target.chat_id is not None
    )
    sessions = [SimpleNamespace(session_key=key) for key in keys]
    active = SimpleNamespace(
        provider="claude", session_id="sid", message_count=1, activity_turns=1
    )
    obs.set_session_source(AsyncMock(return_value=sessions), AsyncMock(return_value=active))


def _acked_keys(obs: HeartbeatObserver) -> set[str]:
    return {key for key, state in obs._state.items() if state.acked is not None}


class TestSuspendOnAck:
    def _observer(self) -> HeartbeatObserver:
        config = AgentConfig(
            heartbeat=HeartbeatConfig(enabled=True, suspend_on_ack=True),
            allowed_user_ids=[100],
        )
        obs = HeartbeatObserver(config)
        _set_session_source(obs)
        return obs

    async def test_ack_suspends_until_matching_activity(self) -> None:
        obs = self._observer()
        handler = AsyncMock(return_value=ACK)
        obs.set_heartbeat_handler(handler)

        await obs._run_for_chat(100)
        await obs._run_for_chat(100)
        assert handler.await_count == 1
        assert SessionKey.telegram(100).storage_key in _acked_keys(obs)

        obs.notify_activity(SessionKey.telegram(100))
        await obs._run_for_chat(100)
        assert handler.await_count == 2

    async def test_failed_and_skipped_results_do_not_suspend(self) -> None:
        obs = self._observer()
        handler = AsyncMock(
            side_effect=[
                HeartbeatOutcome(HeartbeatStatus.FAILED),
                HeartbeatOutcome(HeartbeatStatus.SKIPPED),
            ]
        )
        obs.set_heartbeat_handler(handler)

        await obs._run_for_chat(100)
        obs.notify_activity(SessionKey.telegram(100))
        await obs._run_for_chat(100)
        assert handler.await_count == 2
        assert _acked_keys(obs) == set()

    async def test_activity_wakes_only_exact_session_key(self) -> None:
        obs = self._observer()
        handler = AsyncMock(return_value=ACK)
        obs.set_heartbeat_handler(handler)
        key = SessionKey.for_transport("mx", 100, 7)
        await obs._run_for_chat(100, 7, transport="mx")

        obs.notify_activity(SessionKey.telegram(100, 7))
        obs.notify_activity(SessionKey.for_transport("mx", 100, 8))
        assert key.storage_key in _acked_keys(obs)
        obs.notify_activity(key)
        assert key.storage_key not in _acked_keys(obs)

    async def test_activity_during_inflight_prevents_lost_wakeup(self) -> None:
        obs = self._observer()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(*_args: object) -> HeartbeatOutcome:
            entered.set()
            await release.wait()
            return ACK

        obs.set_heartbeat_handler(handler)
        task = asyncio.create_task(obs._run_for_chat(100))
        await entered.wait()
        obs.notify_activity(SessionKey.telegram(100))
        release.set()
        await task

        assert _acked_keys(obs) == set()
        assert obs._generation == {}
        assert obs._inflight == set()

    async def test_duplicate_target_cannot_enter_concurrently(self) -> None:
        obs = self._observer()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def handler(*_args: object) -> HeartbeatOutcome:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return SKIPPED

        obs.set_heartbeat_handler(handler)
        first = asyncio.create_task(obs._run_for_chat(100))
        await entered.wait()
        await obs._run_for_chat(100)
        release.set()
        await first
        assert calls == 1
        assert obs._generation == {}
        assert obs._inflight == set()

    async def test_reload_invalidates_ack_without_reopening_inflight(self) -> None:
        obs = self._observer()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def handler(*_args: object) -> HeartbeatOutcome:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return ACK

        obs.set_heartbeat_handler(handler)
        first = asyncio.create_task(obs._run_for_chat(100))
        await entered.wait()
        obs.reset_suspend_state()
        await obs._run_for_chat(100)
        assert calls == 1
        release.set()
        await first
        assert _acked_keys(obs) == set()
        assert obs._generation == {}
        assert obs._inflight == set()

    async def test_disable_during_inflight_prevents_suspend(self) -> None:
        obs = self._observer()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(*_args: object) -> HeartbeatOutcome:
            entered.set()
            await release.wait()
            return ACK

        obs.set_heartbeat_handler(handler)
        task = asyncio.create_task(obs._run_for_chat(100))
        await entered.wait()
        obs._config.heartbeat.suspend_on_ack = False
        obs.reset_suspend_state()
        release.set()
        await task
        assert _acked_keys(obs) == set()
        assert obs._generation == {}
        assert obs._inflight == set()

    async def test_cancelled_handler_cleans_state(self) -> None:
        obs = self._observer()
        obs.set_heartbeat_handler(AsyncMock(side_effect=asyncio.CancelledError()))
        with pytest.raises(asyncio.CancelledError):
            await obs._run_for_chat(100)
        assert obs._generation == {}
        assert obs._inflight == set()

    async def test_callback_error_cleans_state_without_suspending(self) -> None:
        obs = self._observer()
        obs.set_heartbeat_handler(AsyncMock(return_value=_alert("alert")))
        obs.set_result_handler(AsyncMock(side_effect=RuntimeError("delivery failed")))
        await obs._run_for_chat(100)
        assert _acked_keys(obs) == set()
        assert obs._generation == {}
        assert obs._inflight == set()

    async def test_explicit_off_does_not_suspend(self) -> None:
        config = _make_config()
        config.heartbeat.suspend_on_ack = False
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=ACK)
        obs.set_heartbeat_handler(handler)
        await obs._run_for_chat(100)
        await obs._run_for_chat(100)
        assert handler.await_count == 2
        assert _acked_keys(obs) == set()
        assert obs._state[SessionKey.telegram(100).storage_key].tracked is True
        assert obs._generation == {}
        assert obs._inflight == set()

    async def test_group_target_uses_same_suspend_contract(self) -> None:
        config = AgentConfig(
            heartbeat=HeartbeatConfig(
                enabled=True,
                auto_targets=False,
                suspend_on_ack=True,
                quiet_start=0,
                quiet_end=0,
                group_targets=[HeartbeatTarget(chat_id=-1001, topic_id=7)],
            ),
            allowed_user_ids=[],
            allowed_group_ids=[-1001],
        )
        obs = HeartbeatObserver(config)
        _set_session_source(obs)
        handler = AsyncMock(return_value=ACK)
        obs.set_heartbeat_handler(handler)
        await obs._tick_group_targets()
        await obs._tick_group_targets()
        assert handler.await_count == 1
        assert SessionKey.telegram(-1001, 7).storage_key in _acked_keys(obs)


async def test_handler_error_cleans_suspend_state() -> None:
    config = AgentConfig(
        heartbeat=HeartbeatConfig(enabled=True, suspend_on_ack=True),
        allowed_user_ids=[100],
    )
    obs = HeartbeatObserver(config)
    obs.set_heartbeat_handler(AsyncMock(side_effect=RuntimeError("heartbeat failed")))
    await obs._run_for_chat(100)
    assert _acked_keys(obs) == set()
    assert obs._generation == {}
    assert obs._inflight == set()


async def test_custom_interval_target_uses_suspend_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = HeartbeatTarget(chat_id=-1001, topic_id=7, interval_minutes=60)
    config = AgentConfig(
        heartbeat=HeartbeatConfig(
            enabled=True,
            suspend_on_ack=True,
            quiet_start=0,
            quiet_end=0,
            group_targets=[target],
        ),
        allowed_user_ids=[],
        allowed_group_ids=[-1001],
    )
    obs = HeartbeatObserver(config)
    _set_session_source(obs)
    handler = AsyncMock(return_value=ACK)
    obs.set_heartbeat_handler(handler)
    obs._running = True
    sleeps = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await obs._target_loop(target, 60)
    assert handler.await_count == 1
    assert SessionKey.telegram(-1001, 7).storage_key in _acked_keys(obs)
    assert obs._generation == {}
    assert obs._inflight == set()


async def test_stop_preserves_ack_and_clears_inflight() -> None:
    obs = TestSuspendOnAck()._observer()
    key = SessionKey.telegram(100)
    obs._state[key.storage_key] = _KeyState(acked=ACK.marker, tracked=True)
    obs._generation[key] = 3
    obs._inflight.add(key)
    await obs.stop()
    assert key.storage_key in _acked_keys(obs)
    assert obs._generation == {}
    assert obs._inflight == set()
