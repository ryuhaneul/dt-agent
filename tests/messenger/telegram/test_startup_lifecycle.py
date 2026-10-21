"""Tests for startup lifecycle detection and auto-recovery in _on_startup."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from ductor_bot.config import AgentConfig
from ductor_bot.infra.inflight import InflightTracker, InflightTurn
from ductor_bot.infra.startup_state import StartupInfo, StartupKind
from ductor_bot.text.response_format import (
    recovery_notification_text,
    startup_notification_text,
)

if TYPE_CHECKING:
    import pytest


class TestStartupNotification:
    def test_first_start_produces_message(self) -> None:
        text = startup_notification_text(StartupKind.FIRST_START.value)
        assert text
        assert "First start" in text

    def test_reboot_produces_message(self) -> None:
        text = startup_notification_text(StartupKind.SYSTEM_REBOOT.value)
        assert text
        assert "reboot" in text.lower()

    def test_restart_is_silent(self) -> None:
        text = startup_notification_text(StartupKind.SERVICE_RESTART.value)
        assert text == ""


class TestRecoveryNotification:
    def test_foreground_recovery(self) -> None:
        text = recovery_notification_text("foreground", "fix the auth bug")
        assert "Interrupted" in text
        assert "fix the auth bug" in text

    def test_named_session_recovery(self) -> None:
        text = recovery_notification_text("named_session", "deploy", "redowl")
        assert "redowl" in text
        assert "deploy" in text


class TestStartupInfo:
    def test_round_trip(self) -> None:
        info = StartupInfo(
            kind=StartupKind.FIRST_START,
            boot_id="abc-123",
            started_at="2026-03-02T12:00:00+00:00",
        )
        assert info.kind == StartupKind.FIRST_START
        assert info.boot_id == "abc-123"


# -- where the recovery notices go -----------------------------------------------------------

DM = 100
GROUP = -1001
USERS = [DM]


def _turn(chat_id: int, topic_id: int | None) -> InflightTurn:
    return InflightTurn(
        chat_id=chat_id,
        topic_id=topic_id,
        provider="claude",
        model="opus",
        session_id="sess-1",
        prompt_preview="fix the bug",
        started_at=datetime.now(UTC).isoformat(),
        is_recovery=False,
        path="normal",
    )


def _bot(tmp_path: Path, *, named: list[object] | None = None) -> SimpleNamespace:
    service = SimpleNamespace(
        transport_name="tg",
        notify_origin=AsyncMock(return_value=True),
        notify=AsyncMock(),
    )
    orch = SimpleNamespace(
        paths=SimpleNamespace(startup_state_path=tmp_path / "startup.json"),
        inflight_tracker=InflightTracker(tmp_path / "inflight.json"),
        named_sessions=SimpleNamespace(pop_recovered_running=lambda: list(named or [])),
        submit_named_followup_bg=MagicMock(),
    )
    return SimpleNamespace(
        notification_service=service,
        config=AgentConfig(allowed_user_ids=USERS, allowed_group_ids=[GROUP]),
        _orch=orch,
        notify_startup=AsyncMock(),
    )


async def _recover(bot: SimpleNamespace) -> None:
    from ductor_bot.messenger.telegram.startup import _handle_recovery

    await _handle_recovery(bot, {"sentinel": True})  # type: ignore[arg-type]


async def test_a_group_topic_turn_is_announced_once_and_consumed(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    bot._orch.inflight_tracker.begin(_turn(GROUP, 7))

    await _recover(bot)

    notify_origin = bot.notification_service.notify_origin
    notify_origin.assert_awaited_once()
    args, kwargs = notify_origin.await_args
    assert args[:2] == ("tg", GROUP)
    assert "fix the bug" in args[2]
    assert kwargs == {"topic_id": 7}
    bot.notification_service.notify.assert_not_awaited()
    assert not (tmp_path / "inflight.json").exists()


async def test_a_topic_turn_is_announced_in_its_topic_once(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    bot._orch.inflight_tracker.begin(_turn(DM, 7))

    await _recover(bot)

    notify_origin = bot.notification_service.notify_origin
    notify_origin.assert_awaited_once()
    args, kwargs = notify_origin.await_args
    assert args[:2] == ("tg", DM)
    assert "fix the bug" in args[2]
    assert kwargs == {"topic_id": 7}
    bot.notification_service.notify.assert_not_awaited()
    assert bot._orch.inflight_tracker.load_interrupted(max_age_seconds=9999) == []  # consumed


async def test_a_failed_topic_notice_in_a_private_chat_falls_back_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bot = _bot(tmp_path)
    bot._orch.inflight_tracker.begin(_turn(DM, 7))
    bot.notification_service.notify_origin = AsyncMock(side_effect=[False, True])

    with caplog.at_level(logging.INFO, logger="ductor_bot.messenger.telegram.startup"):
        await _recover(bot)

    calls = bot.notification_service.notify_origin.await_args_list
    assert [c.kwargs["topic_id"] for c in calls] == [7, None]
    sent = [r.getMessage() for r in caplog.records if "Recovery notice sent" in r.getMessage()]
    assert sent == ["Recovery notice sent kind=foreground chat=100 topic=7 fallback=True"]


async def test_a_failed_group_topic_notice_is_not_guessed_elsewhere(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from ductor_bot.session.named import NamedSession

    ns = NamedSession(
        name="boldowl",
        chat_id=GROUP,
        topic_id=5,
        provider="claude",
        model="opus",
        session_id="sess-ns-1",
        prompt_preview="build",
        status="idle",
        created_at=1000.0,
    )
    bot = _bot(tmp_path, named=[ns])  # a group named session must not fall back
    bot.notification_service.notify_origin = AsyncMock(return_value=False)

    with caplog.at_level(logging.WARNING, logger="ductor_bot.messenger.telegram.startup"):
        await _recover(bot)

    bot.notification_service.notify_origin.assert_awaited_once()
    bot.notification_service.notify.assert_not_awaited()
    assert any("Recovery notice not sent" in r.getMessage() for r in caplog.records)


async def test_a_turn_without_a_topic_is_announced_once_in_the_chat(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    bot._orch.inflight_tracker.begin(_turn(DM, None))
    bot.notification_service.notify_origin = AsyncMock(return_value=False)

    await _recover(bot)

    bot.notification_service.notify_origin.assert_awaited_once()  # no second attempt
    assert bot.notification_service.notify_origin.await_args.kwargs == {"topic_id": None}


async def test_the_named_follow_up_runs_in_the_session_topic(tmp_path: Path) -> None:
    from ductor_bot.session.named import NamedSession

    ns = NamedSession(
        name="boldowl",
        chat_id=DM,
        topic_id=9,
        provider="claude",
        model="opus",
        session_id="sess-ns-1",
        prompt_preview="build",
        status="idle",
        created_at=1000.0,
        last_prompt="build the feature",
    )
    bot = _bot(tmp_path, named=[ns])

    await _recover(bot)

    followup = bot._orch.submit_named_followup_bg
    followup.assert_called_once_with(DM, "boldowl", "build the feature", message_id=0, thread_id=9)
    assert bot.notification_service.notify_origin.await_args.kwargs == {"topic_id": 9}


async def test_a_notice_that_raises_does_not_stop_the_startup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bot = _bot(tmp_path)
    bot._orch.inflight_tracker.begin(_turn(DM, 7))
    bot._orch.inflight_tracker.begin(_turn(DM, 8))
    bot.notification_service.notify_origin = AsyncMock(side_effect=RuntimeError("secret detail"))

    with caplog.at_level(logging.WARNING, logger="ductor_bot.messenger.telegram.startup"):
        await _recover(bot)  # must not raise

    assert bot.notification_service.notify_origin.await_count == 2  # the next one still runs
    assert bot._orch.inflight_tracker.load_interrupted(max_age_seconds=9999) == []
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in text
    assert "secret detail" not in text
