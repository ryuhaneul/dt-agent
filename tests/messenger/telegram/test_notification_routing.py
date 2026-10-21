"""Configurable startup notification routing."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.config import AgentConfig, NotificationsConfig, NotificationTarget
from ductor_bot.messenger.telegram.sender import SendRichOpts


def _make_bot(
    monkeypatch: pytest.MonkeyPatch,
    *,
    startup_targets: list[NotificationTarget] | None = None,
    allowed_user_ids: list[int] | None = None,
) -> tuple[Any, AsyncMock, AsyncMock, AsyncMock]:
    """Construct a TelegramBot-like object with notify_startup.

    Patches ``send_rich`` at module level so every call is observable via
    AsyncMock. The shared ``NotificationService.notify_all`` is mocked so
    tests can assert the fallback path fired (or did not).
    """
    from ductor_bot.messenger.telegram import app as app_module

    fake_send_rich = AsyncMock()
    monkeypatch.setattr(app_module, "send_rich", fake_send_rich)

    cfg = AgentConfig(
        telegram_token="test-token",
        allowed_user_ids=allowed_user_ids or [111, 222],
        notifications=NotificationsConfig(
            startup_targets=startup_targets or [],
        ),
    )

    # Shim: construct TelegramBot without its heavy __init__ side effects.
    bot = app_module.TelegramBot.__new__(app_module.TelegramBot)
    bot._config = cfg
    bot._bot = MagicMock()

    notify_all_mock = AsyncMock()
    broadcast_mock = AsyncMock()
    bot._notification_service = MagicMock()
    bot._notification_service.notify_all = notify_all_mock
    # broadcast is a method on TelegramBot — replace for isolation.
    bot.broadcast = broadcast_mock  # type: ignore[method-assign]

    return bot, fake_send_rich, notify_all_mock, broadcast_mock


async def test_notify_startup_falls_back_when_no_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, send_rich, notify_all, _ = _make_bot(monkeypatch)

    await bot.notify_startup("hello world")

    notify_all.assert_awaited_once_with("hello world")
    send_rich.assert_not_called()


async def test_notify_startup_uses_configured_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, send_rich, notify_all, _ = _make_bot(
        monkeypatch,
        startup_targets=[NotificationTarget(enabled=True, chat_id=-100999, topic_id=68)],
    )

    await bot.notify_startup("startup note")

    notify_all.assert_not_called()
    assert send_rich.await_count == 1
    call = send_rich.await_args_list[0]
    assert call.args[1] == -100999
    assert call.args[2] == "startup note"
    opts = call.args[3]
    assert isinstance(opts, SendRichOpts)
    assert opts.thread_id == 68


async def test_notify_startup_skips_disabled_and_missing_chat_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, send_rich, notify_all, _ = _make_bot(
        monkeypatch,
        startup_targets=[
            NotificationTarget(enabled=False, chat_id=-100111, topic_id=None),  # disabled
            NotificationTarget(enabled=True, chat_id=None, topic_id=None),  # no chat_id
            NotificationTarget(enabled=True, chat_id=-100222, topic_id=None),  # valid
        ],
    )

    await bot.notify_startup("startup note")

    notify_all.assert_not_called()
    assert send_rich.await_count == 1
    assert send_rich.await_args_list[0].args[1] == -100222


async def test_notify_startup_silences_when_all_targets_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit notification targets that are all disabled suppress broadcast delivery."""
    bot, send_rich, notify_all, _ = _make_bot(
        monkeypatch,
        startup_targets=[
            NotificationTarget(enabled=False, chat_id=-100111, topic_id=None),
            NotificationTarget(enabled=False, chat_id=-100222, topic_id=None),
        ],
    )

    await bot.notify_startup("startup note")

    notify_all.assert_not_called()
    send_rich.assert_not_called()


async def test_notify_startup_swallows_per_target_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, send_rich, _, _ = _make_bot(
        monkeypatch,
        startup_targets=[
            NotificationTarget(enabled=True, chat_id=-100111, topic_id=None),  # will fail
            NotificationTarget(enabled=True, chat_id=-100222, topic_id=None),  # succeeds
        ],
    )
    # First call raises, second succeeds.
    send_rich.side_effect = [RuntimeError("nope"), None]

    await bot.notify_startup("note")

    # Both targets attempted — the first error did not short-circuit.
    assert send_rich.await_count == 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
