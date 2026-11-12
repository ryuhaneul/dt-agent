"""Connectivity history and the single ordered outbox worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from aiogram.exceptions import TelegramRetryAfter

from ductor_bot.chat_log.events import EventHub
from ductor_bot.chat_log.outbox import Outbox

if TYPE_CHECKING:
    from ductor_bot.chat_log.outbox import OutboxItem, OutboxState
    from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps

logger = logging.getLogger(__name__)
INTERVAL_SECONDS = 30
PRUNE_INTERVAL_SECONDS = 24 * 60 * 60


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z") if value else None


class TelegramLinkMonitor:
    def __init__(
        self,
        ops: TelegramWebchatOps,
        events: EventHub,
        *,
        on_up: Callable[[], None] | None = None,
    ) -> None:
        self._ops = ops
        self._events = events
        self.on_up = on_up
        self._state: Literal["up", "down", "unknown"] = "unknown"
        self._down_since: datetime | None = None
        self._last_check: datetime | None = None
        self._samples: deque[tuple[datetime, bool]] = deque()
        self.probe_up: bool | None = None
        self.probe_failure_generation = 0
        self._task: asyncio.Task[None] | None = None

    def state(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        start = now.replace(second=0, microsecond=0) - timedelta(minutes=29)
        buckets: list[set[bool]] = [set() for _ in range(30)]
        while self._samples and self._samples[0][0] < now - timedelta(minutes=30):
            self._samples.popleft()
        for at, up in self._samples:
            index = int((at - start).total_seconds() // 60)
            if 0 <= index < 30:
                buckets[index].add(up)
        strip = [
            "mixed"
            if len(bucket) == 2
            else "ok"
            if True in bucket
            else "down"
            if bucket
            else "none"
            for bucket in buckets
        ]
        return {
            "state": self._state,
            "down_since": _iso(self._down_since),
            "last_check": _iso(self._last_check),
            "interval_s": INTERVAL_SECONDS,
            "strip": strip,
            "strip_end": _iso(start + timedelta(minutes=30)),
        }

    def record(self, *, up: bool, probe: bool = False) -> None:
        now = datetime.now(UTC)
        was_up = self._state == "up"
        self._state = "up" if up else "down"
        if up:
            self._down_since = None
        elif self._down_since is None:
            self._down_since = now
        self._last_check = now
        self._samples.append((now, up))
        if probe:
            self.probe_up = up
            if not up:
                self.probe_failure_generation += 1
        self._events.publish({"type": "telegram", **self.state()})
        if up and not was_up and self.on_up is not None:
            self.on_up()

    def report_failure(self) -> None:
        self.record(up=False)

    async def probe_once(self) -> None:
        try:
            up = await self._ops.probe()
        except Exception:
            up = False
        self.record(up=up, probe=True)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="chat-log-link")

    async def _run(self) -> None:
        while True:
            deadline = time.monotonic() + INTERVAL_SECONDS
            await self.probe_once()
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


class _StorageError(Exception):
    """Local persistence failed; the Telegram failure budget is unaffected."""


class OutboxDrainer:
    def __init__(self, outbox: Outbox, ops: TelegramWebchatOps, link: TelegramLinkMonitor) -> None:
        self._outbox = outbox
        self._ops = ops
        self._link = link
        self._wake = asyncio.Event()
        self._drain_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._failure_id: str | None = None
        self._up_failures = 0
        self._probe_generation = link.probe_failure_generation
        self._active: OutboxItem | None = None
        self._dirty = False
        self._retry_at = 0.0

    def wake(self) -> None:
        self._wake.set()

    def defer_retry(self, seconds: float) -> None:
        self._retry_at = max(self._retry_at, time.monotonic() + seconds)

    async def _save_active(self) -> None:
        item = self._active
        assert item is not None
        try:
            await self._outbox.mark_progress(
                item.id,
                sent_chunks=item.sent_chunks,
                attempts=item.attempts,
                last_error=item.last_error,
            )
        except Exception as exc:
            raise _StorageError from exc
        self._dirty = False

    async def _send_active(self) -> tuple[bool, bool]:  # noqa: C901 -- chunk and terminal saves
        """Return (terminal, rate limited), retaining acknowledgments before disk I/O."""
        item = self._active
        assert item is not None
        if self._dirty:
            await self._save_active()
        if item.state == "pending":

            async def on_chunk(sent_chunks: int) -> None:
                assert self._active is not None
                self._active = replace(self._active, sent_chunks=sent_chunks)
                self._dirty = True
                saving = asyncio.create_task(self._save_active())
                try:
                    await asyncio.shield(saving)
                except asyncio.CancelledError:
                    while not saving.done():
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await asyncio.shield(saving)
                    if not saving.cancelled():
                        saving.exception()
                    raise

            progress = await self._ops.send_text(
                item.chat_id,
                item.topic_id,
                item.text,
                item.sent_chunks,
                item.buttons_session,
                on_chunk=on_chunk,
            )
            limited = isinstance(progress.exception, TelegramRetryAfter)
            if isinstance(progress.exception, TelegramRetryAfter):
                self._retry_at = time.monotonic() + progress.exception.retry_after
            state: OutboxState = (
                "sent"
                if progress.delivered
                else ("failed" if progress.failure == "permanent" else "pending")
            )
            self._active = replace(
                item,
                sent_chunks=progress.sent_chunks,
                attempts=item.attempts + (not progress.delivered and not limited),
                state=state,
                last_error=progress.error or None,
            )
            self._dirty = True
            await self._save_active()
            if limited:
                return False, True
            item = self._active
        try:
            if item.state == "sent":
                await self._outbox.mark_sent(item.id)
                self._link.record(up=True)
                return True, False
            if item.state == "failed":
                await self._outbox.mark_failed(item.id, item.last_error or "send_failed")
                return True, False
        except Exception as exc:
            raise _StorageError from exc
        return False, False

    async def drain(self) -> None:  # noqa: C901, PLR0911, PLR0912 -- ordered delivery/storage recovery
        async with self._drain_lock:
            while True:
                async with self._outbox.delivery_lock:
                    if self._link.state()["state"] == "down" or time.monotonic() < self._retry_at:
                        return
                    generation = self._link.probe_failure_generation
                    if generation != self._probe_generation:
                        self._up_failures = 0
                    self._probe_generation = generation
                    attempts = self._active.attempts if self._active is not None else 0
                    try:
                        items = await self._outbox.pending()
                    except Exception as exc:
                        logger.warning("Outbox read failed: %s", type(exc).__name__)
                        return
                    try:
                        if not items:
                            self._active = None
                            return
                        item = items[0]
                        if self._active is None or self._active.id != item.id:
                            self._active = item
                            self._dirty = False
                        if self._failure_id != item.id:
                            self._failure_id = item.id
                            self._up_failures = 0
                        attempts = self._active.attempts
                        terminal, limited = await self._send_active()
                        if terminal:
                            self._active = None
                            continue
                        if limited:
                            return
                    except _StorageError:
                        logger.warning("Outbox storage failed; retaining delivery progress")
                        return
                    except TelegramRetryAfter as exc:
                        self._retry_at = time.monotonic() + exc.retry_after
                        return
                    except Exception as exc:
                        logger.warning("Outbox item operation failed: %s", type(exc).__name__)
                        if self._active is None:
                            return
                        # Keep acknowledged chunks and attempts even when storage is unavailable.
                        self._active = replace(
                            self._active,
                            attempts=max(self._active.attempts, attempts + 1),
                            last_error=type(exc).__name__,
                        )
                        self._dirty = True
                        with contextlib.suppress(Exception):
                            await self._save_active()
                if await self._record_failure(generation):
                    continue
                return

    async def _record_failure(self, generation: int) -> bool:
        """Return whether a failed head was retired and delivery can advance."""
        self._link.report_failure()
        # Only a fresh probe after this failure can justify exhausting retries.
        await self._link.probe_once()
        if generation != self._link.probe_failure_generation:
            self._up_failures = 0
        self._up_failures = self._up_failures + 1 if self._link.probe_up else 0
        self._probe_generation = self._link.probe_failure_generation
        self._wake.clear()
        if self._up_failures < 5:
            return False
        assert self._active is not None
        self._active = replace(self._active, state="failed", last_error="send_failed")
        # A failed terminal save is retried without sending the item again.
        try:
            await self._outbox.mark_failed(
                self._active.id,
                "send_failed",
                attempts=self._active.attempts,
                sent_chunks=self._active.sent_chunks,
            )
        except Exception as exc:
            logger.warning("Outbox terminal save failed: %s", type(exc).__name__)
            return False
        self._active = None
        return True

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="chat-log-outbox")

    async def _run(self) -> None:
        next_prune = 0.0
        while True:
            self._wake.clear()
            try:
                if time.monotonic() >= next_prune:
                    next_prune = time.monotonic() + PRUNE_INTERVAL_SECONDS
                    try:
                        await self._outbox.prune()
                    except Exception as exc:
                        logger.warning("Outbox prune failed: %s", type(exc).__name__)
                await self.drain()
            except Exception as exc:
                logger.warning("Outbox worker failed: %s", type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(
                    self._retry_at - time.monotonic()
                    if time.monotonic() < self._retry_at
                    else INTERVAL_SECONDS
                ):
                    await self._wake.wait()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
