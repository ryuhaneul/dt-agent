"""One deadline shared by every step of a shutdown, so the steps cannot add up past the grace."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

DEFAULT_TOTAL_SECONDS = 25.0
"""Total time of a shutdown: five seconds under the 30 second container stop grace."""

CANCEL_FLOOR_SECONDS = 1.0
"""A cancelled step gets this long to clean up, even when the budget is used up."""


class ShutdownBudget:
    """A monotonic deadline; each step waits at most ``cap(limit)``."""

    def __init__(
        self,
        total: float = DEFAULT_TOTAL_SECONDS,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._deadline = clock() + max(0.0, total)

    def remaining(self) -> float:
        """Seconds left (never negative)."""
        return max(0.0, self._deadline - self._clock())

    def cap(self, limit: float) -> float:
        """The wait a step with its own *limit* may use: ``min(limit, remaining())``."""
        return max(0.0, min(limit, self.remaining()))

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0


async def wait_capped(work: Awaitable[object], seconds: float, *, label: str) -> bool:
    """Run *work* for at most *seconds*; a step that is not done by then is cancelled.

    The cancelled step may take ``CANCEL_FLOOR_SECONDS`` to clean up, and is then left
    behind. Errors of the step are logged (type only), not raised. Returns whether the
    step finished by itself in time.
    """
    task = asyncio.ensure_future(work)
    try:
        await asyncio.wait({task}, timeout=max(0.0, seconds))
    except asyncio.CancelledError:
        task.cancel()
        raise
    finished = task.done()
    if not finished:
        task.cancel()
        await asyncio.wait({task}, timeout=CANCEL_FLOOR_SECONDS)
        logger.warning("Shutdown step '%s' did not finish within its time; cancelled", label)
    if task.done() and not task.cancelled() and (exc := task.exception()) is not None:
        logger.warning("Shutdown step '%s' failed (%s)", label, type(exc).__name__)
        return False
    return finished
