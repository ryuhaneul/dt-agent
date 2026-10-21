"""Transport-agnostic notification delivery protocol."""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class NotificationService(Protocol):
    """Transport-agnostic notification delivery.

    Implemented by both TelegramNotificationService and
    MatrixNotificationService so the supervisor and bus can send
    notifications without knowing which transport is active.
    """

    transport_name: str

    async def notify_origin(
        self, transport: str, chat_id: int, text: str, *, topic_id: int | None = None
    ) -> bool:
        """Notify exactly one origin thread, with no fallback."""
        ...

    async def notify(self, chat_id: int, text: str) -> None:
        """Send a notification to a specific chat/room."""
        ...

    async def notify_all(self, text: str) -> None:
        """Send a notification to all authorized users/rooms."""
        ...


class CompositeNotificationService:
    """Fans out notifications to multiple transport-specific services."""

    transport_name = "multi"

    def __init__(self) -> None:
        self._services: list[NotificationService] = []

    def add(self, service: NotificationService) -> None:
        self._services.append(service)

    async def notify_origin(
        self, transport: str, chat_id: int, text: str, *, topic_id: int | None = None
    ) -> bool:
        for svc in self._services:
            if svc.transport_name == transport:
                return await svc.notify_origin(transport, chat_id, text, topic_id=topic_id)
        logger.warning("No notification service for origin transport %s", transport)
        return False

    async def notify(self, chat_id: int, text: str) -> None:
        for svc in self._services:
            await svc.notify(chat_id, text)

    async def notify_all(self, text: str) -> None:
        for svc in self._services:
            await svc.notify_all(text)
