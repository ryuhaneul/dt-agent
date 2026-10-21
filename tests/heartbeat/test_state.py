"""Heartbeat state recovery and wake persistence contracts."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import Envelope, Origin
from ductor_bot.config import AgentConfig, HeartbeatConfig
from ductor_bot.heartbeat.models import HeartbeatOutcome, HeartbeatStatus
from ductor_bot.heartbeat.observer import HeartbeatObserver, _KeyState
from ductor_bot.session.key import SessionKey

KEY = SessionKey.telegram(100)
MARKER = ("claude", "sid-100", 3)
ACK = HeartbeatOutcome(HeartbeatStatus.ACK, marker=MARKER)


def _observer(path: Path, *, auto_targets: bool = False, interval: int = 30) -> HeartbeatObserver:
    config = AgentConfig(
        heartbeat=HeartbeatConfig(
            enabled=True,
            suspend_on_ack=True,
            auto_targets=auto_targets,
            interval_minutes=interval,
            quiet_start=0,
            quiet_end=0,
            group_targets=[],
        ),
        allowed_user_ids=[100],
    )
    obs = HeartbeatObserver(config)
    obs.set_state_path(path)
    session = SimpleNamespace(
        session_key=KEY,
        provider=MARKER[0],
        session_id=MARKER[1],
        message_count=MARKER[2],
        last_active=datetime.now(UTC).isoformat(),
        activity_at=datetime.now(UTC).isoformat(),
        activity_turns=MARKER[2],
    )
    obs.set_session_source(AsyncMock(return_value=[session]), AsyncMock(return_value=session))
    return obs


async def _seed_ack(obs: HeartbeatObserver) -> None:
    await obs._ensure_loaded()
    obs.set_heartbeat_handler(AsyncMock(return_value=ACK))
    await obs._run_for_chat(KEY.chat_id)
    assert obs._state[KEY.storage_key].acked == MARKER
    assert await obs._persist_now()
    await asyncio.gather(*obs._persist_tasks)


def _transport() -> AsyncMock:
    transport = AsyncMock()
    transport.transport_name = "tg"
    transport.deliver = AsyncMock()
    return transport


def _bus(obs: HeartbeatObserver) -> tuple[MessageBus, AsyncMock]:
    bus = MessageBus()
    transport = _transport()
    bus.register_transport(transport)

    async def wake(envelope: Envelope) -> None:
        await obs.wake(
            SessionKey.for_transport(envelope.transport, envelope.chat_id, envelope.topic_id)
        )

    bus.set_wake_hook(wake)
    return bus, transport


async def test_restart_restores_dormant_marker(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat_state.json"
    first = _observer(path)
    await _seed_ack(first)

    restored = _observer(path)
    handler = AsyncMock(return_value=ACK)
    restored.set_heartbeat_handler(handler)
    await restored._tick()
    handler.assert_not_awaited()
    assert restored._state[KEY.storage_key].acked == MARKER
    assert (tmp_path / "heartbeat_state.initialized").exists()


async def test_reset_clears_ack_and_backoff_but_keeps_quarantine(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat_state.json"
    obs = _observer(path)
    await obs._ensure_loaded()
    obs._state[KEY.storage_key] = _KeyState(
        acked=MARKER,
        quarantine_sid="bad-sid",
        quarantine_notified=True,
        fail_count=2,
        retry_after=12345.0,
        tracked=True,
    )
    obs.reset_suspend_state()
    await asyncio.gather(*obs._persist_tasks)
    state = obs._state[KEY.storage_key]
    assert (state.acked, state.fail_count, state.retry_after) == (None, 0, None)
    assert (state.quarantine_sid, state.quarantine_notified) == ("bad-sid", True)

    restored = _observer(path)
    await restored._ensure_loaded()
    assert restored._state[KEY.storage_key] == state


async def test_config_fp_change_invalidates_old_ack_after_unsaved_reset(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat_state.json"
    old = _observer(path)
    await old._ensure_loaded()
    old._state[KEY.storage_key] = _KeyState(
        acked=MARKER, quarantine_sid="bad-sid", fail_count=1, retry_after=123.0, tracked=True
    )
    assert await old._persist_now()
    old._config.heartbeat.interval_minutes = 60
    old.reset_suspend_state()
    for task in old._persist_tasks:
        task.cancel()
    await asyncio.gather(*old._persist_tasks, return_exceptions=True)

    changed = _observer(path, interval=60)
    await changed._ensure_loaded()
    state = changed._state[KEY.storage_key]
    assert (state.acked, state.fail_count, state.retry_after) == (None, 0, None)
    assert state.quarantine_sid == "bad-sid"
    saved = json.loads(path.read_text())
    assert saved["config_fp"] == changed._config_fp()

    unchanged = _observer(path, interval=60)
    await unchanged._ensure_loaded()
    assert unchanged._state[KEY.storage_key].quarantine_sid == "bad-sid"


async def test_same_config_fp_keeps_ack(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat_state.json"
    old = _observer(path)
    await _seed_ack(old)
    same = _observer(path)
    await same._ensure_loaded()
    assert same._state[KEY.storage_key].acked == MARKER
    assert len(json.loads(path.read_text())["config_fp"]) == 64


async def test_first_start_saves_empty_state_then_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "heartbeat_state.json"
    first = _observer(path)
    marker = tmp_path / "heartbeat_state.initialized"
    observed: list[bool] = []

    async def inspect_marker(_path: Path) -> None:
        observed.append(path.exists())

    monkeypatch.setattr(first, "_create_initialized", inspect_marker)
    await first._ensure_loaded()
    assert observed == [True]
    assert path.exists()
    assert not marker.exists()
    assert first._recovery_pending is False

    second = _observer(path)
    await second._ensure_loaded()
    assert marker.exists()
    assert second._recovery_pending is False


async def test_normal_state_without_marker_restores_it_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat_state.json"
    first = _observer(path)
    await _seed_ack(first)
    (tmp_path / "heartbeat_state.initialized").unlink()
    before = path.read_bytes()

    restored = _observer(path)
    await restored._ensure_loaded()
    assert restored._state[KEY.storage_key].acked == MARKER
    assert path.read_bytes() == before
    assert (tmp_path / "heartbeat_state.initialized").exists()


async def test_missing_state_with_marker_sets_recovery_pending(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat_state.json"
    (tmp_path / "heartbeat_state.initialized").touch()
    obs = _observer(path, auto_targets=False)
    await obs._ensure_loaded()
    assert obs._recovery_pending is True
    assert json.loads(path.read_text())["recovery_pending"] is True

    await obs._tick()
    assert obs._recovery_pending is True
    restarted = _observer(path, auto_targets=False)
    await restarted._ensure_loaded()
    assert restarted._recovery_pending is True


async def test_corrupt_state_is_preserved_and_sets_recovery(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat_state.json"
    path.write_text("{broken")
    obs = _observer(path)
    await obs._ensure_loaded()
    corrupt = await asyncio.to_thread(lambda: list(tmp_path.glob("heartbeat_state.json.corrupt-*")))
    assert len(corrupt) == 1
    assert corrupt[0].read_text() == "{broken"
    assert obs._recovery_pending is True
    assert json.loads(path.read_text())["recovery_pending"] is True
    assert (tmp_path / "heartbeat_state.initialized").exists()


async def test_bus_wake_is_saved_before_delivery_and_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "heartbeat_state.json"
    obs = _observer(path)
    await _seed_ack(obs)
    bus, transport = _bus(obs)
    env = Envelope(origin=Origin.TASK_RESULT, chat_id=100, needs_injection=False)
    with caplog.at_level("INFO", logger="ductor_bot.heartbeat.observer"):
        await bus.submit(env)
    transport.deliver.assert_awaited_once_with(env)
    lines = [r.message for r in caplog.records if r.message.startswith("Heartbeat wake key=")]
    assert lines == [f"Heartbeat wake key={KEY.storage_key} via=bus"]

    restored = _observer(path)
    await restored._ensure_loaded()
    assert restored._state[KEY.storage_key].acked is None
    handler = AsyncMock(return_value=ACK)
    restored.set_heartbeat_handler(handler)
    await restored._tick()
    handler.assert_awaited_once()


async def test_cancelled_injection_keeps_wake_persisted(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat_state.json"
    obs = _observer(path)
    await _seed_ack(obs)
    bus, _transport_mock = _bus(obs)
    injector = AsyncMock()
    injector.inject_prompt = AsyncMock(side_effect=asyncio.CancelledError)
    bus.set_injector(injector)
    env = Envelope(origin=Origin.INTERAGENT, chat_id=100, needs_injection=True, prompt="result")
    with pytest.raises(asyncio.CancelledError):
        await bus.submit(env)

    restored = _observer(path)
    await restored._ensure_loaded()
    assert restored._state[KEY.storage_key].acked is None


async def test_failed_save_removes_stale_ack_and_delivery_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "heartbeat_state.json"
    obs = _observer(path)
    await _seed_ack(obs)
    bus, transport = _bus(obs)

    def fail_save(*_args: object) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patcher:
        patcher.setattr("ductor_bot.heartbeat.observer.atomic_json_save", fail_save)
        env = Envelope(origin=Origin.TASK_RESULT, chat_id=100, needs_injection=False)
        with caplog.at_level("ERROR", logger="ductor_bot.heartbeat.observer"):
            await bus.submit(env)
    transport.deliver.assert_awaited_once_with(env)
    errors = [r for r in caplog.records if "removed stale state" in r.message]
    assert len(errors) == 1
    assert not path.exists()
    assert (tmp_path / "heartbeat_state.initialized").exists()

    restored = _observer(path)
    await restored._ensure_loaded()
    assert KEY.storage_key not in restored._state
    assert restored._recovery_pending is True


async def test_failed_save_and_unlink_sets_dirty_then_retries_on_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "heartbeat_state.json"
    obs = _observer(path)
    await _seed_ack(obs)
    bus, transport = _bus(obs)

    def fail_save(*_args: object) -> None:
        raise OSError("disk full")

    original_unlink = Path.unlink

    def fail_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == path:
            raise OSError("unlink denied")
        original_unlink(self, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr("ductor_bot.heartbeat.observer.atomic_json_save", fail_save)
        patcher.setattr(Path, "unlink", fail_unlink)
        with caplog.at_level("ERROR", logger="ductor_bot.heartbeat.observer"):
            env = Envelope(origin=Origin.TASK_RESULT, chat_id=100, needs_injection=False)
            await bus.submit(env)
    transport.deliver.assert_awaited_once_with(env)
    assert obs._dirty is True
    assert "save and removal failed" in caplog.text
    assert json.loads(path.read_text())["state"][KEY.storage_key]["acked"] == list(MARKER)

    obs.set_heartbeat_handler(AsyncMock(return_value=HeartbeatOutcome(HeartbeatStatus.SKIPPED)))
    await obs._tick()
    assert obs._dirty is False
    assert json.loads(path.read_text())["state"][KEY.storage_key]["acked"] is None


async def test_wake_logs_only_when_clearing_ack_or_backoff(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    obs = _observer(tmp_path / "heartbeat_state.json")
    await obs._ensure_loaded()
    with caplog.at_level("INFO", logger="ductor_bot.heartbeat.observer"):
        obs.notify_activity(KEY)
        obs._state[KEY.storage_key].acked = MARKER
        obs.notify_activity(KEY)
        obs.notify_activity(KEY)
        obs._state[KEY.storage_key].retry_after = 123.0
        obs.reset_suspend_state()
    lines = [r.message for r in caplog.records if r.message.startswith("Heartbeat wake key=")]
    assert lines == [
        f"Heartbeat wake key={KEY.storage_key} via=user",
        f"Heartbeat wake key={KEY.storage_key} via=reload",
    ]
    await obs._persist_now()


async def test_inflight_and_wake_generation_work_with_suspend_disabled(tmp_path: Path) -> None:
    obs = _observer(tmp_path / "heartbeat_state.json")
    obs._config.heartbeat.suspend_on_ack = False
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(*_args: object) -> HeartbeatOutcome:
        entered.set()
        await release.wait()
        return ACK

    execute = AsyncMock(side_effect=handler)
    obs.set_heartbeat_handler(execute)
    first = asyncio.create_task(obs._run_for_chat(KEY.chat_id))
    await entered.wait()
    await obs._run_for_chat(KEY.chat_id)
    assert execute.await_count == 1
    assert obs._generation[KEY] == 0

    obs.notify_activity(KEY)
    assert obs._generation[KEY] == 1
    release.set()
    await first
    assert obs._inflight == set()
    assert obs._generation == {}
    assert obs._state[KEY.storage_key].tracked is True
    assert obs._state[KEY.storage_key].acked is None


async def test_save_retries_once_before_fail_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "heartbeat_state.json"
    obs = _observer(path)
    await obs._ensure_loaded()
    obs._state[KEY.storage_key] = _KeyState(acked=MARKER, tracked=True)
    from ductor_bot.heartbeat import observer as observer_module

    real_save = observer_module.atomic_json_save
    attempts = 0

    def fail_once(destination: Path, payload: dict[str, object]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transient")
        real_save(destination, payload)

    monkeypatch.setattr(observer_module, "atomic_json_save", fail_once)
    assert await obs._persist_now()
    assert attempts == 2
    assert obs._dirty is False
    assert json.loads(path.read_text())["state"][KEY.storage_key]["acked"] == list(MARKER)


async def _seed_preload_state_file(path: Path, *, enabled: bool = True, auto: bool = False) -> None:
    seed = _observer(path, auto_targets=auto)
    seed._config.heartbeat.enabled = enabled
    seed._config.allowed_user_ids = [100, 200, 300]
    await seed._ensure_loaded()
    seed._state[KEY.storage_key] = _KeyState(acked=MARKER, tracked=True)
    seed._state["tg:200"] = _KeyState(tracked=True, fail_count=2, retry_after=9999999999.0)
    seed._state["tg:300"] = _KeyState(
        quarantine_sid="sid-300", quarantine_notified=True, tracked=True
    )
    assert await seed._persist_now()


async def test_preload_notify_restores_other_state_and_old_tracked_target(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat_state.json"
    await _seed_preload_state_file(path, auto=True)
    restored = _observer(path, auto_targets=True)
    restored._config.allowed_user_ids = [100, 200, 300]
    old = SimpleNamespace(
        session_key=SessionKey.telegram(200),
        provider="claude",
        session_id="sid-200",
        message_count=2,
        last_active=(datetime.now(UTC) - timedelta(days=8)).isoformat(),
        activity_at=(datetime.now(UTC) - timedelta(days=8)).isoformat(),
        activity_turns=2,
    )
    quarantined = SimpleNamespace(
        session_key=SessionKey.telegram(300),
        provider="claude",
        session_id="sid-300",
        message_count=2,
        last_active=datetime.now(UTC).isoformat(),
        activity_at=datetime.now(UTC).isoformat(),
        activity_turns=2,
    )
    primary = (await restored._list_sessions())[0]
    restored.set_session_source(
        AsyncMock(return_value=[primary, old, quarantined]),
        AsyncMock(return_value=primary),
    )
    restored.notify_activity(KEY)
    await asyncio.gather(*restored._persist_tasks)

    assert restored._state[KEY.storage_key].acked is None
    assert restored._state["tg:200"].retry_after == 9999999999.0
    assert restored._state["tg:200"].tracked is True
    assert restored._state["tg:300"].quarantine_sid == "sid-300"
    saved = json.loads(await asyncio.to_thread(path.read_text))
    assert set(saved["state"]) == {"tg:100", "tg:200", "tg:300"}
    assert saved["state"][KEY.storage_key]["acked"] is None
    targets = await restored._collect_targets()
    assert old.session_key in {target.key for target in targets}


async def test_preload_reset_preserves_quarantine_and_tracked(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat_state.json"
    await _seed_preload_state_file(path)
    restored = _observer(path)
    restored._config.allowed_user_ids = [100, 200, 300]
    restored.reset_suspend_state()
    await asyncio.gather(*restored._persist_tasks)

    assert set(restored._state) == {"tg:100", "tg:200", "tg:300"}
    assert restored._state[KEY.storage_key].acked is None
    assert restored._state["tg:200"].fail_count == 0
    assert restored._state["tg:200"].retry_after is None
    assert restored._state["tg:200"].tracked is True
    assert restored._state["tg:300"].quarantine_sid == "sid-300"
    saved = json.loads(await asyncio.to_thread(path.read_text))
    assert saved["state"]["tg:200"]["retry_after"] is None
    assert saved["state"]["tg:300"]["quarantine_sid"] == "sid-300"


async def test_disabled_start_then_notify_does_not_overwrite_unloaded_state(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat_state.json"
    await _seed_preload_state_file(path, enabled=False)
    restored = _observer(path)
    restored._config.heartbeat.enabled = False
    restored._config.allowed_user_ids = [100, 200, 300]
    await restored.start()
    assert restored._loaded is False
    restored.notify_activity(KEY)
    await asyncio.gather(*restored._persist_tasks)

    saved = json.loads(await asyncio.to_thread(path.read_text))
    assert set(saved["state"]) == {"tg:100", "tg:200", "tg:300"}
    assert saved["state"][KEY.storage_key]["acked"] is None
    assert saved["state"]["tg:200"]["retry_after"] == 9999999999.0
    assert saved["state"]["tg:300"]["quarantine_sid"] == "sid-300"


async def test_preload_bus_wake_waits_for_restored_state_save(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat_state.json"
    await _seed_preload_state_file(path)
    restored = _observer(path)
    restored._config.allowed_user_ids = [100, 200, 300]
    await restored.wake(KEY)

    saved = json.loads(await asyncio.to_thread(path.read_text))
    assert saved["state"][KEY.storage_key]["acked"] is None
    assert saved["state"]["tg:200"]["retry_after"] == 9999999999.0
    assert saved["state"]["tg:300"]["quarantine_sid"] == "sid-300"


async def test_persist_now_loads_existing_state_before_write(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat_state.json"
    await _seed_preload_state_file(path)
    before = await asyncio.to_thread(path.read_bytes)
    restored = _observer(path)
    restored._config.allowed_user_ids = [100, 200, 300]
    assert await restored._persist_now()
    assert await asyncio.to_thread(path.read_bytes) == before
    assert set(restored._state) == {"tg:100", "tg:200", "tg:300"}


async def test_corrupt_rename_failure_still_marks_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "heartbeat_state.json"
    await asyncio.to_thread(path.write_text, "{broken")
    original_rename = Path.rename

    def fail_rename(self: Path, target: Path) -> Path:
        if self == path:
            raise OSError("rename denied")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_rename)
    restored = _observer(path)
    with caplog.at_level("ERROR", logger="ductor_bot.heartbeat.observer"):
        await restored._ensure_loaded()
    assert restored._state == {}
    assert restored._recovery_pending is True
    saved = json.loads(await asyncio.to_thread(path.read_text))
    assert saved["recovery_pending"] is True
    assert saved["state"] == {}
    assert "recovery will overwrite it" in caplog.text
