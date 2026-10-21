"""Tests for sessions.json migration from legacy to prefixed keys."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ductor_bot.config import AgentConfig
from ductor_bot.session.key import SessionKey
from ductor_bot.session.manager import SessionData, SessionManager

if TYPE_CHECKING:
    import pytest


def _make_manager(tmp_path: Path, **overrides: Any) -> SessionManager:
    cfg = AgentConfig(**overrides)
    return SessionManager(sessions_path=tmp_path / "sessions.json", config=cfg)


def _write_sessions(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _read_sessions(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "sessions.json"
    return json.loads(path.read_text(encoding="utf-8"))


class TestSessionMigration:
    async def test_load_legacy_flat_key_migrates(self, tmp_path: Path) -> None:
        """Legacy key '987654321' becomes 'tg:987654321' on load."""
        _write_sessions(
            tmp_path,
            {
                "987654321": {
                    "chat_id": 987654321,
                    "provider": "claude",
                    "model": "opus",
                },
            },
        )
        mgr = _make_manager(tmp_path)
        session = await mgr.get_active(SessionKey(chat_id=987654321))
        assert session is not None
        assert session.chat_id == 987654321
        assert session.transport == "tg"

    async def test_load_legacy_topic_key_migrates(self, tmp_path: Path) -> None:
        """Legacy key '123:45' becomes 'tg:123:45' on load."""
        _write_sessions(
            tmp_path,
            {
                "123:45": {
                    "chat_id": 123,
                    "provider": "claude",
                    "model": "opus",
                },
            },
        )
        mgr = _make_manager(tmp_path)
        session = await mgr.get_active(SessionKey(chat_id=123, topic_id=45))
        assert session is not None
        assert session.chat_id == 123
        assert session.topic_id == 45
        assert session.transport == "tg"

    async def test_load_legacy_negative_chat_id_migrates(self, tmp_path: Path) -> None:
        """Legacy key '-100123' becomes 'tg:-100123' on load."""
        _write_sessions(
            tmp_path,
            {
                "-100123": {
                    "chat_id": -100123,
                    "provider": "claude",
                    "model": "opus",
                },
            },
        )
        mgr = _make_manager(tmp_path)
        session = await mgr.get_active(SessionKey(chat_id=-100123))
        assert session is not None
        assert session.chat_id == -100123
        assert session.transport == "tg"

    async def test_load_new_keys_preserved(self, tmp_path: Path) -> None:
        """Prefixed keys load without transformation."""
        _write_sessions(
            tmp_path,
            {
                "tg:123": {
                    "chat_id": 123,
                    "transport": "tg",
                    "provider": "claude",
                    "model": "opus",
                },
            },
        )
        mgr = _make_manager(tmp_path)
        session = await mgr.get_active(SessionKey(chat_id=123))
        assert session is not None
        assert session.chat_id == 123
        assert session.transport == "tg"

    async def test_save_uses_prefixed_keys(self, tmp_path: Path) -> None:
        """Saved sessions.json always uses transport-prefixed keys."""
        mgr = _make_manager(tmp_path)
        await mgr.resolve_session(key=SessionKey(chat_id=42))

        data = _read_sessions(tmp_path)
        assert "tg:42" in data
        assert "42" not in data

    async def test_save_after_legacy_load_uses_prefixed_keys(self, tmp_path: Path) -> None:
        """Loading legacy keys and saving re-writes them as prefixed."""
        _write_sessions(
            tmp_path,
            {
                "999": {
                    "chat_id": 999,
                    "provider": "claude",
                    "model": "opus",
                },
            },
        )
        mgr = _make_manager(tmp_path)
        session = await mgr.get_active(SessionKey(chat_id=999))
        assert session is not None

        # Trigger a save by updating the session
        await mgr.update_session(session)

        data = _read_sessions(tmp_path)
        assert "tg:999" in data
        assert "999" not in data

    async def test_mixed_format_migration(self, tmp_path: Path) -> None:
        """Both old and new keys in same file load correctly."""
        _write_sessions(
            tmp_path,
            {
                "100": {
                    "chat_id": 100,
                    "provider": "claude",
                    "model": "opus",
                },
                "tg:200": {
                    "chat_id": 200,
                    "transport": "tg",
                    "provider": "codex",
                    "model": "gpt-5.2-codex",
                },
                "mx:300": {
                    "chat_id": 300,
                    "transport": "mx",
                    "provider": "claude",
                    "model": "opus",
                },
                "-100500:7": {
                    "chat_id": -100500,
                    "provider": "claude",
                    "model": "opus",
                },
            },
        )
        mgr = _make_manager(tmp_path)

        s1 = await mgr.get_active(SessionKey(chat_id=100))
        assert s1 is not None
        assert s1.transport == "tg"

        s2 = await mgr.get_active(SessionKey(chat_id=200))
        assert s2 is not None
        assert s2.transport == "tg"
        assert s2.provider == "codex"

        s3 = await mgr.get_active(SessionKey(transport="mx", chat_id=300))
        assert s3 is not None
        assert s3.transport == "mx"

        s4 = await mgr.get_active(SessionKey(chat_id=-100500, topic_id=7))
        assert s4 is not None
        assert s4.topic_id == 7
        assert s4.transport == "tg"

    async def test_legacy_transport_field_injected(self, tmp_path: Path) -> None:
        """Legacy entries without 'transport' get it from the parsed key."""
        _write_sessions(
            tmp_path,
            {
                "42": {
                    "chat_id": 42,
                    "provider": "claude",
                    "model": "opus",
                },
            },
        )
        mgr = _make_manager(tmp_path)
        session = await mgr.get_active(SessionKey(chat_id=42))
        assert session is not None
        assert session.transport == "tg"
        assert session.session_key == SessionKey(transport="tg", chat_id=42)

    async def test_list_all_after_migration(self, tmp_path: Path) -> None:
        """list_all returns all sessions regardless of original key format."""
        _write_sessions(
            tmp_path,
            {
                "1": {
                    "chat_id": 1,
                    "provider": "claude",
                    "model": "opus",
                },
                "tg:2": {
                    "chat_id": 2,
                    "transport": "tg",
                    "provider": "claude",
                    "model": "opus",
                },
            },
        )
        mgr = _make_manager(tmp_path)
        all_sessions = await mgr.list_all()
        assert len(all_sessions) == 2
        chat_ids = {s.chat_id for s in all_sessions}
        assert chat_ids == {1, 2}

    async def test_resolve_after_legacy_load(self, tmp_path: Path) -> None:
        """resolve_session finds and reuses a migrated legacy session."""
        _write_sessions(
            tmp_path,
            {
                "555": {
                    "chat_id": 555,
                    "session_id": "legacy-sid",
                    "provider": "claude",
                    "model": "opus",
                },
            },
        )
        mgr = _make_manager(tmp_path, idle_timeout_minutes=30)
        session, is_new = await mgr.resolve_session(key=SessionKey(chat_id=555))
        assert is_new is False
        assert session.session_id == "legacy-sid"
        assert session.transport == "tg"


class TestOldTokenAndCostFiguresAreDropped:
    """Records written before the token/cost tracking was removed still load; the figures go."""

    @staticmethod
    def _figures_in(text: str) -> bool:
        return "total_cost_usd" in text or "total_tokens" in text

    async def test_flat_legacy_record_keeps_ids_counts_and_drops_figures(
        self, tmp_path: Path
    ) -> None:
        _write_sessions(
            tmp_path,
            {
                "tg:1": {
                    "chat_id": 1,
                    "provider": "claude",
                    "model": "opus",
                    "reasoning_effort": "high",
                    "session_id": "sid-flat",
                    "message_count": 3,
                    "total_cost_usd": 1.5,
                    "total_tokens": 999,
                    "activity_at": "2025-01-01T00:00:00+00:00",
                    "activity_turns": 2,
                },
            },
        )
        mgr = _make_manager(tmp_path)
        session = await mgr.get_active(SessionKey(chat_id=1))
        assert session is not None
        await mgr.update_session(session, counts_as_activity=False)

        saved = _read_sessions(tmp_path)
        assert not self._figures_in((tmp_path / "sessions.json").read_text(encoding="utf-8"))
        record = saved["tg:1"]
        assert record["reasoning_effort"] == "high"
        assert record["activity_turns"] == 2
        assert record["provider_sessions"]["claude"] == {
            "session_id": "sid-flat",
            "message_count": 4,
        }

    async def test_nested_record_keeps_every_provider_and_drops_figures(
        self, tmp_path: Path
    ) -> None:
        _write_sessions(
            tmp_path,
            {
                "tg:2": {
                    "chat_id": 2,
                    "provider": "codex",
                    "model": "gpt-5.2-codex",
                    "provider_sessions": {
                        "claude": {
                            "session_id": "claude-sid",
                            "message_count": 5,
                            "total_cost_usd": 0.4,
                            "total_tokens": 400,
                        },
                        "codex": {
                            "session_id": "codex-sid",
                            "message_count": 2,
                            "total_cost_usd": 0.2,
                            "total_tokens": 200,
                        },
                    },
                },
            },
        )
        mgr = _make_manager(tmp_path)
        session = await mgr.get_active(SessionKey(chat_id=2))
        assert session is not None
        await mgr.update_session(session)

        buckets = _read_sessions(tmp_path)["tg:2"]["provider_sessions"]
        assert buckets["claude"] == {"session_id": "claude-sid", "message_count": 5}
        assert buckets["codex"] == {"session_id": "codex-sid", "message_count": 3}
        assert not self._figures_in((tmp_path / "sessions.json").read_text(encoding="utf-8"))

    def test_figures_alone_do_not_make_a_provider_bucket(self) -> None:
        session = SessionData(chat_id=1, total_cost_usd=0.2, total_tokens=5)
        assert session.provider_sessions == {}

    async def test_no_unknown_key_warning_for_the_dropped_figures(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_sessions(
            tmp_path,
            {"tg:3": {"chat_id": 3, "session_id": "s", "total_cost_usd": 1, "total_tokens": 2}},
        )
        mgr = _make_manager(tmp_path)
        with caplog.at_level("WARNING"):
            assert await mgr.get_active(SessionKey(chat_id=3)) is not None
        assert "unknown keys" not in caplog.text

    async def test_new_file_read_with_old_defaults_gives_zero(self, tmp_path: Path) -> None:
        """Rolling back: the old reader defaults the missing figures to 0."""
        mgr = _make_manager(tmp_path)
        session, _ = await mgr.resolve_session(key=SessionKey(chat_id=4))
        session.session_id = "sid-4"
        await mgr.update_session(session)

        bucket = _read_sessions(tmp_path)["tg:4"]["provider_sessions"]["claude"]
        assert bucket.get("total_cost_usd", 0.0) == 0.0
        assert bucket.get("total_tokens", 0) == 0
        assert bucket["session_id"] == "sid-4"
