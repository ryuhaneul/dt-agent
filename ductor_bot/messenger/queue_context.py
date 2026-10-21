"""Task-local wait metadata, kept separate from user text until CLI dispatch."""

from __future__ import annotations

import asyncio
import re
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ductor_bot.infra.input_context import acquire_input_lock, current_input_context

_START = "[Queued user message]"
_END = "[/Queued user message]"
QUEUE_NOTICE = (
    "This message waited while the previous task was in progress. "
    "It may not be a reaction to the immediately preceding reply; "
    "determine which task it relates to."
)


@dataclass(frozen=True, slots=True)
class QueueWait:
    sent_at: datetime | None
    waited_seconds: float
    position: int
    owner: asyncio.Task[object] | None


_context: ContextVar[QueueWait | None] = ContextVar("queued_user_message", default=None)
_waiters: WeakKeyDictionary[asyncio.Lock, int] = WeakKeyDictionary()


def message_time(value: object, *, milliseconds: bool = False) -> datetime | None:
    """Normalize transport timestamps without inventing a missing send time."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    if not isinstance(value, (int, float, str)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(float(value) / (1000 if milliseconds else 1), UTC)
    except (ValueError, OverflowError, OSError):
        return None


class QueuedMessageLock:
    """Use the existing FIFO lock; annotate actual acquisition waits and known queues."""

    def __init__(
        self,
        lock: asyncio.Lock,
        sent_at: datetime | None,
        *,
        annotate: bool = True,
    ) -> None:
        self._lock, self._sent_at, self._annotate = lock, sent_at, annotate
        self._waiting = lock.locked() or bool(_waiters.get(lock))
        # Count every pending user acquisition: external bus waiters can keep an
        # unlocked lock contended before any user waiter is visible here.
        self._position = _waiters.get(lock, 0) + 1
        _waiters[lock] = self._position
        self._registered = True
        self._token: Token[QueueWait | None] | None = None

    def discard(self) -> None:
        """Release pending bookkeeping if cancelled before lock acquisition."""
        if self._registered:
            remaining = _waiters[self._lock] - 1
            if remaining:
                _waiters[self._lock] = remaining
            else:
                del _waiters[self._lock]
            self._registered = False
            self._waiting = False

    async def __aenter__(self) -> None:
        started = time.monotonic()
        try:
            await acquire_input_lock(self._lock)
        except BaseException:
            self.discard()
            raise
        waited = max(0.0, time.monotonic() - started)
        info = None
        if (self._waiting or waited >= 0.001) and self._annotate:
            info = QueueWait(
                self._sent_at,
                waited,
                self._position,
                asyncio.current_task(),
            )
        self.discard()
        self._token = _context.set(info)

    async def __aexit__(self, *_exc: object) -> None:
        if self._token is not None:
            _context.reset(self._token)
        self._lock.release()


def current_queue_context(timezone: str) -> str | None:
    """Only the receiving task may add this metadata to a foreground request."""
    info = _context.get()
    if info is None or info.owner is not asyncio.current_task():
        return None
    try:
        zone = ZoneInfo(timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    sent = (
        info.sent_at.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S %Z")
        if info.sent_at
        else "unknown"
    )
    seconds = int(info.waited_seconds)
    waited = f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"
    # The block states the send time itself ("Sent at"); the receipt line must not repeat it.
    received = current_input_context(timezone, include_wait=False, include_sent=False)
    receipt_line = f"{received}\n" if received else ""
    return (
        f"{_START}\n{receipt_line}Sent at: {sent}\nWaited: {waited}\n"
        f"Queue position at arrival: {info.position}\n{QUEUE_NOTICE}\n{_END}"
    )


def without_queue_context(text: str) -> str:
    """Remove framework wait blocks from CLI command previews, never the argv."""
    text = re.sub(
        r"^\[Received: [^\n]+?\](?: \[Sent: [^\n]+?\])?(?: \[Waited: \d+s\])?\n", "", text
    )
    return re.sub(
        re.escape(_START) + r"\n.*?" + re.escape(_END) + r"\n\n", "", text, flags=re.DOTALL
    )
