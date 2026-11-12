"""Atomic file write primitives.

All persistent writes in the codebase should funnel through these helpers.
They use ``tempfile.mkstemp`` + ``os.replace`` for POSIX-atomic semantics:
a partial write can never leave a corrupt target file.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import ParamSpec, TypeVar


def atomic_text_save(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write *content* to *path* atomically via temp file + rename.

    Creates parent directories if they don't exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written == 0:
            raise OSError("Write returned zero bytes")
        remaining = remaining[written:]


def atomic_bytes_save(path: Path, data: bytes) -> None:
    """Write *data* to *path* atomically via temp file + rename.

    Creates parent directories if they don't exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    tmp = Path(tmp_str)
    try:
        _write_all(fd, data)
        os.close(fd)
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise


P = ParamSpec("P")
T = TypeVar("T")


async def complete_write(func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Keep the caller's storage lock until its worker finishes, even when cancelled."""
    worker = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(worker)
        if not worker.cancelled():
            worker.exception()
        raise
