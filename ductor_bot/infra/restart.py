"""Restart sentinel and request helpers for graceful hot-reload."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ductor_bot.infra.atomic_io import atomic_bytes_save

logger = logging.getLogger(__name__)

EXIT_RESTART = 42
"""Exit code that tells the supervisor to restart immediately."""


_restart_hooks: dict[Path, Callable[[], None]] = {}


def register_restart_hook(home: Path, hook: Callable[[], None]) -> None:
    """Run *hook* when a restart is requested for *home* (replaces an earlier hook)."""
    _restart_hooks[home] = hook


def unregister_restart_hook(home: Path) -> None:
    """Drop the restart hook of *home*."""
    _restart_hooks.pop(home, None)


def _restart_requested(path: Path) -> None:
    """Tell the agent owning *path* (a file directly in its home) that it is about to stop."""
    hook = _restart_hooks.get(path.parent)
    if hook is not None:
        hook()


def write_restart_sentinel(
    chat_id: int,
    message: str = "Restart completed.",
    *,
    sentinel_path: Path,
) -> None:
    """Write a sentinel file so the bot can notify the user after restart."""
    _restart_requested(sentinel_path)
    data = {
        "chat_id": chat_id,
        "message": message,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    atomic_bytes_save(sentinel_path, json.dumps(data).encode())
    logger.info("Restart sentinel written for chat=%d", chat_id)


def consume_restart_sentinel(*, sentinel_path: Path) -> dict[str, Any] | None:
    """Read and delete the sentinel file. Returns None if absent."""
    if not sentinel_path.exists():
        return None
    try:
        data: dict[str, Any] = json.loads(sentinel_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to read restart sentinel")
        sentinel_path.unlink(missing_ok=True)
        return None
    else:
        sentinel_path.unlink(missing_ok=True)
        logger.info("Restart sentinel consumed for chat=%s", data.get("chat_id"))
        return data


def write_restart_marker(*, marker_path: Path) -> None:
    """Write a marker file that tells the running bot to shut down with EXIT_RESTART."""
    _restart_requested(marker_path)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text("1", encoding="utf-8")
    logger.info("Restart marker written")


def consume_restart_marker(*, marker_path: Path, at_startup: bool = False) -> bool:
    """Check and delete the restart marker. Returns True if it existed.

    A marker found while starting up asks for nothing to stop: only a running agent
    (``at_startup=False``) treats it as the start of its shutdown.
    """
    if not marker_path.exists():
        return False
    if not at_startup:
        _restart_requested(marker_path)
    marker_path.unlink(missing_ok=True)
    return True
