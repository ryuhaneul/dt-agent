"""An exclusive lock on a fixed companion file, for read-modify-write of a data file.

The data file itself is replaced on every save (temp file + rename), so a lock on it would be
lost with the old inode. The lock is taken on ``<name>.lock`` instead, which is never replaced.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from pathlib import Path

try:
    import fcntl
except ImportError:  # no flock on this platform: writers are not serialized by it
    fcntl = None  # type: ignore[assignment]

LOCK_TIMEOUT_SECONDS = 5.0


class LockTimeoutError(OSError):
    """The lock stayed with another writer for the whole wait."""


@contextlib.contextmanager
def exclusive_file_lock(target: Path, *, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold the exclusive lock for *target* (``<target>.lock``) while the block runs."""
    lock_path = target.with_name(target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        msg = f"{lock_path.name} is locked by another writer"
                        raise LockTimeoutError(msg) from None
                    time.sleep(0.02)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock
