"""Receipt metadata carried separately from displayed and stored prompt text."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import wraps
from typing import Any, ParamSpec, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

P = ParamSpec("P")
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class InputReceipt:
    received_at: float
    waiting_since: float | None = None
    waited_seconds: float | None = None
    sent_at: float | None = None  # when the sender sent it, if the transport says so


_receipt: ContextVar[InputReceipt | None] = ContextVar("input_receipt", default=None)


@contextmanager
def received_at(
    timestamp: float | None = None, *, clear: bool = False, waited_seconds: float | None = None
) -> Iterator[None]:
    """Start a distinct input scope; children explicitly handling it inherit it."""
    token = _receipt.set(
        None
        if clear
        else InputReceipt(
            time.time() if timestamp is None else timestamp, waited_seconds=waited_seconds
        )
    )
    try:
        yield
    finally:
        _receipt.reset(token)


def received_input(func: Callable[P, Awaitable[T]]) -> Callable[P, Coroutine[Any, Any, T]]:
    """Capture arrival before a transport's first await (including media resolution)."""

    @wraps(func)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        with received_at():
            return await func(*args, **kwargs)

    return wrapped


def inherit_received_input(func: Callable[P, Awaitable[T]]) -> Callable[P, Coroutine[Any, Any, T]]:
    """Keep a callback's arrival when it is forwarded through message middleware."""
    capture = received_input(func)

    @wraps(func)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        return await (func(*args, **kwargs) if _receipt.get() else capture(*args, **kwargs))

    return wrapped


def current_input_timestamp() -> float | None:
    """Return receipt metadata for adapters without formatting it into the body."""
    receipt = _receipt.get()
    return receipt.received_at if receipt is not None else None


def set_input_sent_at(sent_at: float | None) -> None:
    """Record the transport's send time of the current input (user messages only)."""
    receipt = _receipt.get()
    if receipt is not None and sent_at is not None:
        _receipt.set(replace(receipt, sent_at=sent_at))


def mark_input_waiting() -> None:
    receipt = _receipt.get()
    if receipt is not None and receipt.waiting_since is None:
        _receipt.set(replace(receipt, waiting_since=time.monotonic()))


def add_input_wait(seconds: float) -> None:
    """Add a measured wait (for example in an inter-agent session queue) to this input's Waited."""
    receipt = _receipt.get()
    if receipt is not None and seconds >= 0.001:
        _receipt.set(replace(receipt, waited_seconds=(receipt.waited_seconds or 0.0) + seconds))


# A message that reached the bot this much later than it was sent also shows its send time.
SENT_TIME_THRESHOLD_SECONDS = 10.0


def current_input_context(
    timezone: str, *, include_wait: bool = True, include_sent: bool = True
) -> str | None:
    receipt = _receipt.get()
    if receipt is None:
        return None
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    stamp = datetime.fromtimestamp(receipt.received_at, UTC).astimezone(zone)
    line = f"[Received: {stamp.strftime('%Y-%m-%d %H:%M:%S %Z')}]"
    if (
        include_sent
        and receipt.sent_at is not None
        and receipt.received_at - receipt.sent_at >= SENT_TIME_THRESHOLD_SECONDS
    ):
        sent = datetime.fromtimestamp(receipt.sent_at, UTC).astimezone(zone)
        line += f" [Sent: {sent.strftime('%Y-%m-%d %H:%M:%S %Z')}]"
    if include_wait and receipt.waited_seconds is not None:
        seconds = max(0, int(receipt.waited_seconds))
        line += f" [Waited: {seconds}s]"
    return line


async def acquire_input_lock(lock: asyncio.Lock) -> None:
    """Measure acquisition itself, including FIFO waiters after an unlock."""
    started = time.monotonic()
    try:
        await lock.acquire()
    finally:
        receipt = _receipt.get()
        if receipt is not None:
            elapsed = max(0.0, time.monotonic() - started)
            waited = receipt.waited_seconds
            # Ignore sub-millisecond acquisition overhead on an uncontended lock.
            if elapsed >= 0.001:
                waited = (waited or 0.0) + elapsed
            _receipt.set(replace(receipt, waiting_since=None, waited_seconds=waited))


@asynccontextmanager
async def input_lock(lock: asyncio.Lock) -> AsyncIterator[None]:
    """Keep the existing lock and release contract; freeze wait before preparation."""
    await acquire_input_lock(lock)
    try:
        yield
    finally:
        lock.release()
