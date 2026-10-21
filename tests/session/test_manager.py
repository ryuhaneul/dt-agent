"""Tests for JSON-based session manager."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest
import time_machine

from ductor_bot.config import AgentConfig
from ductor_bot.session.key import SessionKey
from ductor_bot.session.manager import ProviderSessionData, SessionData, SessionManager


def _make_manager(tmp_path: Path, **overrides: Any) -> SessionManager:
    cfg = AgentConfig(**overrides)
    return SessionManager(sessions_path=tmp_path / "sessions.json", config=cfg)


async def _simulate_cli_response(
    mgr: SessionManager, session: SessionData, cli_session_id: str
) -> None:
    """Simulate the orchestrator storing the CLI-assigned session ID."""
    session.session_id = cli_session_id
    await mgr.update_session(session)


async def test_resolve_creates_new_session(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    session, is_new = await mgr.resolve_session(key=SessionKey(chat_id=1))
    assert is_new is True
    assert session.chat_id == 1
    assert session.session_id == ""


async def test_resolve_reuses_fresh_session(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, idle_timeout_minutes=30)
    s1, new1 = await mgr.resolve_session(key=SessionKey(chat_id=1))
    await _simulate_cli_response(mgr, s1, "cli-assigned-id")

    s2, new2 = await mgr.resolve_session(key=SessionKey(chat_id=1))
    assert new1 is True
    assert new2 is False
    assert s2.session_id == "cli-assigned-id"


async def test_resolve_treats_empty_session_id_as_new(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, idle_timeout_minutes=30)
    _s1, _ = await mgr.resolve_session(key=SessionKey(chat_id=1))
    # Don't simulate CLI response -- session_id stays empty
    s2, new2 = await mgr.resolve_session(key=SessionKey(chat_id=1))
    assert new2 is True
    assert s2.session_id == ""


async def test_resolve_session_target_creates_missing_session(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    mgr.set_topic_name_resolver(lambda _chat, _topic: "new topic")
    key = SessionKey(chat_id=-100, topic_id=10)

    session, created = await mgr.resolve_session_target(
        key,
        provider="codex",
        model="gpt-5.2-codex",
        reasoning_effort="high",
    )

    assert created is True
    assert session.provider == "codex"
    assert session.model == "gpt-5.2-codex"
    assert session.reasoning_effort == "high"
    assert session.topic_name == "new topic"
    assert session.provider_sessions == {}


async def test_resolve_session_target_retargets_fresh_session(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    key = SessionKey(chat_id=-100, topic_id=11)
    existing, _ = await mgr.resolve_session(key, provider="claude", model="opus")
    existing.session_id = "claude-session"
    await mgr.update_session(existing)
    mgr.set_topic_name_resolver(lambda _chat, _topic: "existing topic")

    session, created = await mgr.resolve_session_target(
        key,
        provider="codex",
        model="gpt-5.2-codex",
        reasoning_effort="high",
    )

    assert created is False
    assert session.provider == "codex"
    assert session.model == "gpt-5.2-codex"
    assert session.reasoning_effort == "high"
    assert session.topic_name == "existing topic"
    assert session.provider_sessions["claude"].session_id == "claude-session"


@time_machine.travel("2025-06-15 12:00:00", tick=False)
async def test_resolve_session_target_idle_stale_replaces_whole_shell(
    tmp_path: Path,
) -> None:
    """Session-wide staleness (idle timeout) wipes the full shell, like resolve_session."""
    mgr = _make_manager(tmp_path, idle_timeout_minutes=30)
    key = SessionKey(chat_id=-100, topic_id=12)
    with time_machine.travel("2025-06-15 12:00:00", tick=False):
        stale, _ = await mgr.resolve_session(key, provider="claude", model="opus")
        stale.session_id = "stale-session"
        stale.provider_sessions["codex"] = ProviderSessionData(message_count=1)
        await mgr.update_session(stale)

    with time_machine.travel("2025-06-15 13:00:00", tick=False):
        session, created = await mgr.resolve_session_target(
            key,
            provider="codex",
            model="gpt-5.2-codex",
        )

    assert created is True
    assert session.provider == "codex"
    assert session.model == "gpt-5.2-codex"
    assert session.provider_sessions == {}


async def test_resolve_session_target_resets_only_stale_target_bucket(
    tmp_path: Path,
) -> None:
    """Target bucket over the cap resets only that bucket; other buckets survive."""
    mgr = _make_manager(tmp_path, max_session_messages=3)
    key = SessionKey(chat_id=-100, topic_id=15)
    existing, _ = await mgr.resolve_session(key, provider="claude", model="opus")
    existing.provider_sessions["claude"] = ProviderSessionData(
        session_id="fresh-claude-session",
        message_count=1,
    )
    existing.provider_sessions["codex"] = ProviderSessionData(
        session_id="stale-codex-session",
        message_count=4,
    )
    await mgr.preserve_session_identity(existing)

    session, created = await mgr.resolve_session_target(
        key,
        provider="codex",
        model="gpt-5.2-codex",
    )

    assert created is True
    assert session.provider == "codex"
    assert session.model == "gpt-5.2-codex"
    # Only the maxed-out codex bucket is dropped; claude history is preserved.
    assert "codex" not in session.provider_sessions
    assert session.provider_sessions["claude"].session_id == "fresh-claude-session"


async def test_resolve_session_target_keeps_other_bucket_when_target_count_stale(
    tmp_path: Path,
) -> None:
    """codex bucket fresh + claude bucket count-stale -> /model claude keeps codex history."""
    mgr = _make_manager(tmp_path, max_session_messages=3)
    key = SessionKey(chat_id=-100, topic_id=17)
    existing, _ = await mgr.resolve_session(key, provider="codex", model="gpt-5.2-codex")
    existing.provider_sessions["codex"] = ProviderSessionData(
        session_id="fresh-codex-session",
        message_count=1,
    )
    existing.provider_sessions["claude"] = ProviderSessionData(
        session_id="stale-claude-session",
        message_count=4,
    )
    await mgr.preserve_session_identity(existing)

    session, created = await mgr.resolve_session_target(
        key,
        provider="claude",
        model="opus",
    )

    assert created is True
    assert session.provider == "claude"
    assert session.model == "opus"
    # Target (claude) bucket reset, but the fresh codex history survives.
    assert "claude" not in session.provider_sessions
    assert session.provider_sessions["codex"].session_id == "fresh-codex-session"


async def test_resolve_session_target_retargets_when_target_bucket_is_fresh(
    tmp_path: Path,
) -> None:
    mgr = _make_manager(tmp_path, max_session_messages=3)
    key = SessionKey(chat_id=-100, topic_id=16)
    existing, _ = await mgr.resolve_session(key, provider="claude", model="opus")
    existing.provider_sessions["claude"] = ProviderSessionData(
        session_id="stale-claude-session",
        message_count=4,
    )
    existing.provider_sessions["codex"] = ProviderSessionData(
        session_id="fresh-codex-session",
        message_count=1,
    )
    await mgr.preserve_session_identity(existing)
    history_before = copy.deepcopy(existing.provider_sessions)

    session, created = await mgr.resolve_session_target(
        key,
        provider="codex",
        model="gpt-5.2-codex",
    )

    assert created is False
    assert session.provider == "codex"
    assert session.model == "gpt-5.2-codex"
    assert session.provider_sessions == history_before


@pytest.mark.parametrize(
    "target_first", [True, False], ids=["target-then-update", "update-then-target"]
)
async def test_resolve_session_target_and_other_topic_update_preserve_both_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_first: bool,
) -> None:
    mgr = _make_manager(tmp_path)
    target_key = SessionKey(chat_id=-100, topic_id=13)
    update_key = SessionKey(chat_id=-100, topic_id=14)
    await mgr.resolve_session(target_key, provider="claude", model="opus")
    update_record, _ = await mgr.resolve_session(update_key, provider="claude", model="opus")
    update_record.session_id = "other-topic-session"
    await mgr.update_session(update_record)

    first_loaded = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    original_load = mgr._load
    first_task: asyncio.Task[None] | None = None
    paused = False

    async def controlled_load() -> dict[str, SessionData]:
        nonlocal paused
        sessions = await original_load()
        if asyncio.current_task() is first_task and not paused:
            paused = True
            first_loaded.set()
            await release_first.wait()
        return sessions

    async def retarget() -> None:
        await mgr.resolve_session_target(
            target_key,
            provider="codex",
            model="gpt-5.2-codex",
        )

    async def update_other() -> None:
        await mgr.update_session(update_record)

    monkeypatch.setattr(mgr, "_load", controlled_load)
    first_operation, second_operation = (
        (retarget, update_other) if target_first else (update_other, retarget)
    )
    first_task = asyncio.create_task(first_operation())
    await first_loaded.wait()

    async def run_second() -> None:
        second_started.set()
        await second_operation()

    second_task = asyncio.create_task(run_second())
    await second_started.wait()
    release_first.set()
    await first_task
    await second_task

    final_target = await mgr.get_active(target_key)
    final_update = await mgr.get_active(update_key)
    assert final_target is not None
    assert final_update is not None
    assert final_target.provider == "codex"
    assert final_target.model == "gpt-5.2-codex"
    assert final_update.session_id == "other-topic-session"
    assert final_update.message_count == 2


@time_machine.travel("2025-06-15 12:00:00", tick=False)
async def test_session_expires_after_idle_timeout(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, idle_timeout_minutes=30)
    s1, _ = await mgr.resolve_session(key=SessionKey(chat_id=1))
    await _simulate_cli_response(mgr, s1, "cli-id-1")

    with time_machine.travel("2025-06-15 12:29:00", tick=False):
        s2, new2 = await mgr.resolve_session(key=SessionKey(chat_id=1))
        assert new2 is False
        assert s2.session_id == "cli-id-1"

    with time_machine.travel("2025-06-15 12:31:00", tick=False):
        s3, new3 = await mgr.resolve_session(key=SessionKey(chat_id=1))
        assert new3 is True
        assert s3.session_id == ""


@time_machine.travel("2025-06-15 03:30:00+00:00", tick=False)
async def test_session_expires_at_daily_reset(tmp_path: Path) -> None:
    mgr = _make_manager(
        tmp_path,
        idle_timeout_minutes=120,
        daily_reset_hour=4,
        daily_reset_enabled=True,
        user_timezone="UTC",
    )
    s1, _ = await mgr.resolve_session(key=SessionKey(chat_id=1))
    await _simulate_cli_response(mgr, s1, "cli-id-1")

    with time_machine.travel("2025-06-15 04:01:00+00:00", tick=False):
        s2, new2 = await mgr.resolve_session(key=SessionKey(chat_id=1))
        assert new2 is True
        assert s2.session_id == ""


@time_machine.travel("2025-06-15 03:30:00+00:00", tick=False)
async def test_daily_reset_disabled_by_default(tmp_path: Path) -> None:
    """When daily_reset_enabled=False (default), the reset_hour has no effect."""
    mgr = _make_manager(tmp_path, idle_timeout_minutes=0, daily_reset_hour=4, user_timezone="UTC")
    s1, _ = await mgr.resolve_session(key=SessionKey(chat_id=1))
    await _simulate_cli_response(mgr, s1, "cli-id-1")

    with time_machine.travel("2025-06-15 04:01:00+00:00", tick=False):
        s2, new2 = await mgr.resolve_session(key=SessionKey(chat_id=1))
        # Reset should NOT happen because daily_reset_enabled defaults to False
        assert new2 is False
        assert s2.session_id == "cli-id-1"


@time_machine.travel("2025-06-15 01:00:00+00:00", tick=False)
async def test_daily_reset_over_midnight(tmp_path: Path) -> None:
    """Session created before yesterday's reset_hour must expire even when now is before today's."""
    # Session created yesterday at 02:00, before the 04:00 reset
    with time_machine.travel("2025-06-14 02:00:00+00:00", tick=False):
        mgr = _make_manager(
            tmp_path,
            idle_timeout_minutes=0,
            daily_reset_hour=4,
            daily_reset_enabled=True,
            user_timezone="UTC",
        )
        s1, _ = await mgr.resolve_session(key=SessionKey(chat_id=1))
        await _simulate_cli_response(mgr, s1, "old-session")

    # Now it's 01:00 today — before today's 04:00 reset, but after yesterday's 04:00 reset
    s2, new2 = await mgr.resolve_session(key=SessionKey(chat_id=1))
    assert new2 is True
    assert s2.session_id == ""


@time_machine.travel("2025-06-15 01:00:00+00:00", tick=False)
async def test_daily_reset_not_triggered_for_recent_session(tmp_path: Path) -> None:
    """Session created after yesterday's reset_hour should survive until today's reset."""
    # Session created yesterday at 06:00 — after the 04:00 reset
    with time_machine.travel("2025-06-14 06:00:00+00:00", tick=False):
        mgr = _make_manager(
            tmp_path,
            idle_timeout_minutes=0,
            daily_reset_hour=4,
            daily_reset_enabled=True,
            user_timezone="UTC",
        )
        s1, _ = await mgr.resolve_session(key=SessionKey(chat_id=1))
        await _simulate_cli_response(mgr, s1, "recent-session")

    # Now it's 01:00 today — before today's 04:00 reset, session should still be fresh
    s2, new2 = await mgr.resolve_session(key=SessionKey(chat_id=1))
    assert new2 is False
    assert s2.session_id == "recent-session"


async def test_update_session_serialized(tmp_path: Path) -> None:
    """Concurrent update_session calls must not lose increments (lost-update guard)."""
    import asyncio as _asyncio

    mgr = _make_manager(tmp_path)
    s, _ = await mgr.resolve_session(key=SessionKey(chat_id=1))
    await _simulate_cli_response(mgr, s, "sess-id")

    # Fire 10 concurrent updates
    await _asyncio.gather(*[mgr.update_session(s) for _ in range(10)])

    # 1 from _simulate_cli_response + 10 from gather = 11; all increments must survive
    reloaded, _ = await mgr.resolve_session(key=SessionKey(chat_id=1))
    assert reloaded.message_count == 11


async def test_provider_switch_resets_session(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s1, _ = await mgr.resolve_session(key=SessionKey(chat_id=1), provider="claude")
    await _simulate_cli_response(mgr, s1, "claude-session-id")

    s2, new2 = await mgr.resolve_session(
        key=SessionKey(chat_id=1), provider="codex", model="gpt-5.2-codex"
    )
    assert new2 is True
    assert s2.session_id == ""
    assert s2.provider == "codex"
    assert s2.model == "gpt-5.2-codex"


async def test_update_session_increments(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s, _ = await mgr.resolve_session(key=SessionKey(chat_id=1))
    assert s.message_count == 0
    await mgr.update_session(s)
    assert s.message_count == 1


async def test_update_session_accumulates(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s, _ = await mgr.resolve_session(key=SessionKey(chat_id=1))
    await mgr.update_session(s)
    await mgr.update_session(s)
    assert s.message_count == 2


async def test_persistence_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    cfg = AgentConfig()

    mgr1 = SessionManager(sessions_path=path, config=cfg)
    s1, _ = await mgr1.resolve_session(key=SessionKey(chat_id=1))
    await _simulate_cli_response(mgr1, s1, "persisted-id")
    await mgr1.update_session(s1)

    mgr2 = SessionManager(sessions_path=path, config=cfg)
    s2, new2 = await mgr2.resolve_session(key=SessionKey(chat_id=1))
    assert new2 is False
    assert s2.session_id == "persisted-id"


async def test_session_data_defaults() -> None:
    s = SessionData(session_id="abc", chat_id=1)
    assert s.provider == "claude"
    assert s.model == "opus"
    assert s.message_count == 0


async def test_model_update_without_provider_switch(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s1, _ = await mgr.resolve_session(
        key=SessionKey(chat_id=1), provider="codex", model="gpt-5.1-codex-mini"
    )
    await _simulate_cli_response(mgr, s1, "codex-session-id")

    s2, is_new = await mgr.resolve_session(
        key=SessionKey(chat_id=1), provider="codex", model="gpt-5.2-codex"
    )
    assert is_new is False
    assert s2.session_id == "codex-session-id"
    assert s2.provider == "codex"
    assert s2.model == "gpt-5.2-codex"


async def test_legacy_session_without_model_is_migrated_on_resolve(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    path.write_text(
        '{"1":{"session_id":"legacy-sid","chat_id":1,"provider":"codex"}}',
        encoding="utf-8",
    )

    mgr = _make_manager(tmp_path, idle_timeout_minutes=30)
    s1, is_new = await mgr.resolve_session(
        key=SessionKey(chat_id=1), provider="codex", model="gpt-5.2-codex"
    )
    assert is_new is False
    assert s1.session_id == "legacy-sid"
    assert s1.provider == "codex"
    assert s1.model == "gpt-5.2-codex"

    persisted = path.read_text(encoding="utf-8")
    assert '"model": "gpt-5.2-codex"' in persisted


async def test_sync_session_target_migrates_missing_model_without_value_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.json"
    path.write_text(
        '{"1":{"session_id":"legacy-sid","chat_id":1,"provider":"claude"}}',
        encoding="utf-8",
    )

    mgr = _make_manager(tmp_path)
    session = await mgr.get_active(SessionKey(chat_id=1))
    assert session is not None
    assert session.model == "opus"

    await mgr.sync_session_target(session, provider="claude", model="opus")
    persisted = path.read_text(encoding="utf-8")
    assert '"model": "opus"' in persisted


async def test_sync_session_target_does_not_overwrite_metrics_from_stale_snapshot(
    tmp_path: Path,
) -> None:
    """sync_session_target must merge target fields without resetting counters."""
    mgr = _make_manager(tmp_path)
    base, _ = await mgr.resolve_session(key=SessionKey(chat_id=1))
    await _simulate_cli_response(mgr, base, "sess-id")

    fresh = await mgr.get_active(SessionKey(chat_id=1))
    stale = await mgr.get_active(SessionKey(chat_id=1))
    assert fresh is not None
    assert stale is not None

    await mgr.update_session(fresh)
    before_sync = await mgr.get_active(SessionKey(chat_id=1))
    assert before_sync is not None
    assert before_sync.message_count == 2

    await mgr.sync_session_target(stale, provider="claude", model="opus")
    after_sync = await mgr.get_active(SessionKey(chat_id=1))
    assert after_sync is not None
    assert after_sync.message_count == 2


async def test_update_session_uses_latest_persisted_counters_from_stale_snapshot(
    tmp_path: Path,
) -> None:
    """update_session should not drop increments when called with stale snapshots."""
    mgr = _make_manager(tmp_path)
    base, _ = await mgr.resolve_session(key=SessionKey(chat_id=1))
    await _simulate_cli_response(mgr, base, "sess-id")

    stale_a = await mgr.get_active(SessionKey(chat_id=1))
    stale_b = await mgr.get_active(SessionKey(chat_id=1))
    assert stale_a is not None
    assert stale_b is not None

    await mgr.update_session(stale_a)
    await mgr.update_session(stale_b)

    final = await mgr.get_active(SessionKey(chat_id=1))
    assert final is not None
    assert final.message_count == 3


async def test_separate_chat_ids(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s1, n1 = await mgr.resolve_session(key=SessionKey(chat_id=1))
    s2, n2 = await mgr.resolve_session(key=SessionKey(chat_id=2))
    assert n1 is True
    assert n2 is True
    assert s1.chat_id != s2.chat_id


# -- topic_name ---------------------------------------------------------------


async def test_topic_name_defaults_to_none() -> None:
    s = SessionData(chat_id=1)
    assert s.topic_name is None


async def test_topic_name_round_trip(tmp_path: Path) -> None:
    """topic_name persists through save/load cycle."""
    mgr = _make_manager(tmp_path)
    s, _ = await mgr.resolve_session(key=SessionKey(chat_id=-100, topic_id=42))
    s.topic_name = "test 1"
    await mgr.update_session(s)

    reloaded = await mgr.get_active(SessionKey(chat_id=-100, topic_id=42))
    assert reloaded is not None
    assert reloaded.topic_name == "test 1"


async def test_topic_name_backward_compat(tmp_path: Path) -> None:
    """Old sessions without topic_name load cleanly."""
    path = tmp_path / "sessions.json"
    path.write_text(
        '{"1":{"chat_id":1,"provider":"claude","model":"opus"}}',
        encoding="utf-8",
    )
    mgr = _make_manager(tmp_path)
    s = await mgr.get_active(SessionKey(chat_id=1))
    assert s is not None
    assert s.topic_name is None


async def test_topic_name_resolver_fills_on_resolve(tmp_path: Path) -> None:
    """When a resolver is set, new sessions get topic_name automatically."""
    mgr = _make_manager(tmp_path)
    mgr.set_topic_name_resolver(lambda _c, _t: "resolved topic")
    s, _ = await mgr.resolve_session(key=SessionKey(chat_id=-100, topic_id=42))
    assert s.topic_name == "resolved topic"


async def test_topic_name_resolver_backfills_existing(tmp_path: Path) -> None:
    """Resolver fills topic_name on existing sessions that lack one."""
    mgr = _make_manager(tmp_path)
    s, _ = await mgr.resolve_session(key=SessionKey(chat_id=-100, topic_id=42))
    await _simulate_cli_response(mgr, s, "sid")
    assert s.topic_name is None

    mgr.set_topic_name_resolver(lambda _c, _t: "backfilled")
    s2, _ = await mgr.resolve_session(key=SessionKey(chat_id=-100, topic_id=42))
    assert s2.topic_name == "backfilled"


async def test_resolver_not_called_for_non_topic_sessions(tmp_path: Path) -> None:
    """Resolver is NOT invoked when topic_id is None."""
    called = False

    def resolver(_c: int, _t: int) -> str:
        nonlocal called
        called = True
        return "should not appear"

    mgr = _make_manager(tmp_path)
    mgr.set_topic_name_resolver(resolver)
    s, _ = await mgr.resolve_session(key=SessionKey(chat_id=1))
    assert s.topic_name is None
    assert not called


# -- list_active_for_chat -----------------------------------------------------


async def test_list_active_for_chat(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    await mgr.resolve_session(key=SessionKey(chat_id=-100))
    await mgr.resolve_session(key=SessionKey(chat_id=-100, topic_id=1))
    await mgr.resolve_session(key=SessionKey(chat_id=-100, topic_id=2))
    await mgr.resolve_session(key=SessionKey(chat_id=-200))

    result = await mgr.list_active_for_chat(-100)
    assert len(result) == 3
    chat_ids = {s.chat_id for s in result}
    assert chat_ids == {-100}


async def test_list_active_for_chat_excludes_stale(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, idle_timeout_minutes=30)
    s1, _ = await mgr.resolve_session(key=SessionKey(chat_id=-100, topic_id=1))
    await _simulate_cli_response(mgr, s1, "sid")

    import time_machine

    with time_machine.travel("2099-01-01 00:00:00", tick=False):
        result = await mgr.list_active_for_chat(-100)
        assert len(result) == 0


# -- move_session ----------------------------------------------------------------------------


async def _session_with_id(
    mgr: SessionManager, key: SessionKey, sid: str, *, provider: str = "claude"
) -> SessionData:
    session, _ = await mgr.resolve_session(key, provider=provider, model="m")
    session.session_id = sid
    await mgr.update_session(session)
    return session


async def test_move_session_keeps_every_bucket_and_the_activity_fields(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    src, dst = SessionKey(chat_id=1, topic_id=5), SessionKey(chat_id=1, topic_id=9)
    session = await _session_with_id(mgr, src, "sid-claude")
    await mgr.reset_provider_session(src, provider="codex", model="m2")
    other = await mgr.get_active(src)
    assert other is not None
    other.session_id = "sid-codex"
    await mgr.update_session(other, counts_as_activity=False)
    before = await mgr.get_active(src)
    assert before is not None

    assert await mgr.move_session(src, dst) is True

    assert await mgr.get_active(src) is None
    moved = await mgr.get_active(dst)
    assert moved is not None
    assert (moved.chat_id, moved.topic_id, moved.transport) == (1, 9, "tg")
    assert {k: v.session_id for k, v in moved.provider_sessions.items()} == {
        "claude": "sid-claude",
        "codex": "sid-codex",
    }
    assert (moved.provider, moved.model) == (before.provider, before.model)
    assert (moved.activity_at, moved.activity_turns) == (before.activity_at, before.activity_turns)
    assert session.session_id == "sid-claude"
    # the record persists under the new key only
    reloaded = _make_manager(tmp_path)
    assert await reloaded.get_active(src) is None
    assert (await reloaded.get_active(dst)) is not None


@pytest.mark.parametrize("existing_provider", ["claude", "codex"])
async def test_move_session_never_replaces_a_resumable_target(
    tmp_path: Path, existing_provider: str
) -> None:
    mgr = _make_manager(tmp_path)
    src, dst = SessionKey(chat_id=1, topic_id=5), SessionKey(chat_id=1, topic_id=9)
    await _session_with_id(mgr, src, "sid-src")
    # the target holds a session id in any provider bucket (also not the active one)
    target = await _session_with_id(mgr, dst, "sid-dst", provider=existing_provider)
    other = "claude" if existing_provider == "codex" else "codex"
    target.provider = other
    await mgr.sync_session_target(target, provider=other)

    assert await mgr.move_session(src, dst) is False

    kept = await mgr.get_active(dst)
    assert kept is not None
    assert [b.session_id for b in kept.provider_sessions.values()] == ["sid-dst"]
    source = await mgr.get_active(src)
    assert source is not None
    assert source.session_id == "sid-src"  # nothing was lost


async def test_move_session_replaces_an_empty_target_shell(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    src, dst = SessionKey(chat_id=1, topic_id=5), SessionKey(chat_id=1, topic_id=9)
    await _session_with_id(mgr, src, "sid-src")
    await mgr.resolve_session(dst)  # a session without any id yet

    assert await mgr.move_session(src, dst) is True

    moved = await mgr.get_active(dst)
    assert moved is not None
    assert moved.session_id == "sid-src"


async def test_move_session_without_a_source_or_to_itself_does_nothing(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    key = SessionKey(chat_id=1, topic_id=5)
    assert await mgr.move_session(key, SessionKey(chat_id=1, topic_id=9)) is False
    await _session_with_id(mgr, key, "sid")
    assert await mgr.move_session(key, key) is False
    assert (await mgr.get_active(key)) is not None


async def test_a_late_update_of_the_old_key_is_the_only_way_it_comes_back(
    tmp_path: Path,
) -> None:
    """A late write through the caller's old session object recreates the old key.

    The report turn is finished and its lock released before the move, so nothing of the
    report flow writes afterwards; this documents what a stray writer would do.
    """
    mgr = _make_manager(tmp_path)
    src, dst = SessionKey(chat_id=1, topic_id=5), SessionKey(chat_id=1, topic_id=9)
    old = await _session_with_id(mgr, src, "sid")
    await mgr.move_session(src, dst)

    await mgr.sync_session_target(old, model="x")  # target syncs never recreate a record
    assert await mgr.get_active(src) is None
    await mgr.update_session(old)  # a counted turn does
    assert await mgr.get_active(src) is not None
    assert (await mgr.get_active(dst)) is not None
