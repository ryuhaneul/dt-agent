"""Which handlers are delivering the reply of a finished turn.

A shutdown waits for these handlers (a reply that was computed should reach the user) but not
for turns that are still running; those are cut off and recorded as interrupted.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator


class ReplyPhase:
    """Counts handlers between "the turn ended" and "the reply is sent"."""

    def __init__(self) -> None:
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def active(self) -> int:
        return self._active

    @contextlib.contextmanager
    def sending(self) -> Iterator[None]:
        """Mark the code inside as reply delivery."""
        self._active += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._active -= 1
            if self._active == 0:
                self._idle.set()

    async def wait_idle(self, seconds: float) -> bool:
        """Wait up to *seconds* until no handler is delivering a reply. False on timeout."""
        if self._active == 0:
            return True
        if seconds <= 0:
            return False
        try:
            await asyncio.wait_for(self._idle.wait(), seconds)
        except TimeoutError:
            return False
        return True
