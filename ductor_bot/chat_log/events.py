"""Bounded in-process event fan-out for history and web clients."""

from __future__ import annotations

import asyncio
from typing import Any

Event = dict[str, Any]
Subscription = asyncio.Queue[Event | None]


class EventHub:
    """A full subscription receives None and must disconnect and refresh."""

    def __init__(self) -> None:
        self._subscribers: set[Subscription] = set()

    def subscribe(self) -> Subscription:
        queue: Subscription = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: Subscription) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: Event) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                self.unsubscribe(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
            else:
                queue.put_nowait(event)
