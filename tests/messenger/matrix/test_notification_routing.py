"""Unit tests for Matrix startup notification routing.

Matrix treats explicit-disabled startup target lists as silence.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.config import (
    AgentConfig,
    MatrixConfig,
    NotificationsConfig,
    NotificationTarget,
)


def _make_bot(
    *,
    startup_targets: list[NotificationTarget] | None = None,
) -> tuple[Any, AsyncMock, AsyncMock, AsyncMock]:
    """Construct a MatrixBot-like object without heavy __init__.

    Returns ``(bot, notify_mock, notify_all_mock, broadcast_mock)``.
    """
    from ductor_bot.messenger.matrix import bot as bot_module

    cfg = AgentConfig(
        telegram_token="test-token",
        matrix=MatrixConfig(
            homeserver="https://example.invalid",
            user_id="@test:example.invalid",
            access_token="dummy",
        ),
        notifications=NotificationsConfig(
            startup_targets=startup_targets or [],
        ),
    )

    bot = bot_module.MatrixBot.__new__(bot_module.MatrixBot)
    bot._config = cfg

    notify_mock = AsyncMock()
    notify_all_mock = AsyncMock()
    broadcast_mock = AsyncMock()

    ns = MagicMock()
    ns.notify = notify_mock
    ns.notify_all = notify_all_mock
    bot._notification_service = ns
    # broadcast is a bound method on MatrixBot; replace for isolation.
    bot.broadcast = broadcast_mock  # type: ignore[method-assign]

    return bot, notify_mock, notify_all_mock, broadcast_mock


async def test_notify_startup_falls_back_when_no_targets() -> None:
    bot, notify, notify_all, _ = _make_bot()

    await bot.notify_startup("hello")

    notify_all.assert_awaited_once_with("hello")
    notify.assert_not_called()


async def test_notify_startup_uses_configured_targets() -> None:
    bot, notify, notify_all, _ = _make_bot(
        startup_targets=[NotificationTarget(enabled=True, chat_id=9001, topic_id=None)],
    )

    await bot.notify_startup("startup")

    notify_all.assert_not_called()
    notify.assert_awaited_once_with(9001, "startup")


async def test_notify_startup_silences_when_all_targets_disabled() -> None:
    """Explicit notification targets that are all disabled suppress broadcast delivery."""
    bot, notify, notify_all, _ = _make_bot(
        startup_targets=[NotificationTarget(enabled=False, chat_id=123, topic_id=None)],
    )

    await bot.notify_startup("startup note")

    notify_all.assert_not_called()
    notify.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
