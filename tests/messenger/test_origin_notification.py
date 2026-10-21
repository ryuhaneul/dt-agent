"""Origin-scoped notifications never fan out or fall back to a DM."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest

from ductor_bot.messenger.matrix.bot import MatrixNotificationService
from ductor_bot.messenger.notifications import CompositeNotificationService
from ductor_bot.messenger.slack.bot import SlackNotificationService
from ductor_bot.messenger.telegram.app import TelegramNotificationService


async def test_composite_notifies_only_matching_transport() -> None:
    composite = CompositeNotificationService()
    services = []
    for name in ("tg", "mx", "sl"):
        service = MagicMock()
        service.transport_name = name
        service.notify_origin = AsyncMock(return_value=True)
        service.notify = AsyncMock()
        service.notify_all = AsyncMock()
        composite.add(service)
        services.append(service)

    assert await composite.notify_origin("tg", -100777, "task", topic_id=10) is True
    services[0].notify_origin.assert_awaited_once_with("tg", -100777, "task", topic_id=10)
    for service in services[1:]:
        service.notify_origin.assert_not_awaited()
    for service in services:
        service.notify.assert_not_awaited()
        service.notify_all.assert_not_awaited()


async def test_composite_unknown_transport_does_not_broadcast() -> None:
    composite = CompositeNotificationService()
    service = MagicMock()
    service.transport_name = "tg"
    service.notify_origin = AsyncMock()
    service.notify = AsyncMock()
    service.notify_all = AsyncMock()
    composite.add(service)
    assert await composite.notify_origin("mx", 777, "task") is False
    service.notify_origin.assert_not_awaited()
    service.notify.assert_not_awaited()
    service.notify_all.assert_not_awaited()


async def test_telegram_origin_keeps_topic() -> None:
    service = TelegramNotificationService(MagicMock(), MagicMock())
    with patch(
        "ductor_bot.messenger.telegram.app.send_rich", new_callable=AsyncMock, return_value=True
    ) as send:
        assert await service.notify_origin("tg", -100777, "task", topic_id=10) is True
    args = send.await_args.args
    assert args[1:3] == (-100777, "task")
    assert args[3].thread_id == 10


async def test_matrix_unknown_room_does_not_broadcast() -> None:
    bot = MagicMock()
    bot.id_map.int_to_room.return_value = None
    service = MatrixNotificationService(bot)
    service.notify_all = AsyncMock()
    assert await service.notify_origin("mx", 777, "task") is False
    service.notify_all.assert_not_awaited()
    bot.client.room_send.assert_not_called()


async def test_slack_invalid_thread_does_not_fall_back_to_channel() -> None:
    bot = MagicMock()
    bot.id_map.int_to_channel.return_value = "C123"
    bot.id_map.int_to_thread.return_value = ("C-other", "123.456")
    bot._send_rich = AsyncMock(return_value="sent")
    service = SlackNotificationService(bot)
    service.notify_all = AsyncMock()
    assert await service.notify_origin("sl", 777, "task", topic_id=10) is False
    bot._send_rich.assert_not_awaited()
    service.notify_all.assert_not_awaited()


async def test_slack_origin_keeps_thread() -> None:
    bot = MagicMock()
    bot.id_map.int_to_channel.return_value = "C123"
    bot.id_map.int_to_thread.return_value = ("C123", "123.456")
    bot._send_rich = AsyncMock(return_value="sent")
    service = SlackNotificationService(bot)
    assert await service.notify_origin("sl", 777, "task", topic_id=10) is True
    bot._send_rich.assert_awaited_once_with("C123", "task", thread_ts="123.456")


async def test_telegram_unreachable_origin_does_not_broadcast() -> None:
    service = TelegramNotificationService(MagicMock(), MagicMock())
    service.notify_all = AsyncMock()
    error = TelegramBadRequest(method=MagicMock(), message="chat not found")
    with patch("ductor_bot.messenger.telegram.app.send_rich", side_effect=error):
        assert await service.notify_origin("tg", 777, "task", topic_id=10) is False
    service.notify_all.assert_not_awaited()
