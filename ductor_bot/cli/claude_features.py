"""Claude CLI feature detection (``--system-prompt-snapshot``), cached per process."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

SNAPSHOT_FLAG = "--system-prompt-snapshot"
_HELP_TIMEOUT_SECONDS = 5.0

_supported: bool | None = None
_probing: asyncio.Future[bool] | None = None


def _probe() -> bool:
    """Ask ``claude --help`` (no model call); any failure counts as unsupported."""
    cli = shutil.which("claude")
    if not cli:
        return False
    try:
        done = subprocess.run(
            [cli, "--help"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_HELP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and SNAPSHOT_FLAG in f"{done.stdout}\n{done.stderr}"


async def _probe_once() -> bool:
    """Probe in a thread, remember the result and warn (once) when unsupported."""
    global _supported, _probing  # noqa: PLW0603
    try:
        _supported = await asyncio.to_thread(_probe)
    finally:
        _probing = None
    if not _supported:
        logger.warning(
            "claude CLI does not support %s (or `claude --help` failed): the flag is "
            "omitted and the system prompt keeps its start-of-session assembly",
            SNAPSHOT_FLAG,
        )
    return _supported


async def snapshot_flag_supported() -> bool:
    """Whether the installed Claude CLI knows the flag (probed once; warns once if not).

    Concurrent first calls share the one probe in progress.
    """
    global _probing  # noqa: PLW0603
    if _supported is not None:
        return _supported
    if _probing is None:
        _probing = asyncio.ensure_future(_probe_once())
    return await asyncio.shield(_probing)


async def snapshot_refresh_active(mode: str, provider: str) -> bool:
    """Whether this turn refreshes the Claude system prompt every turn.

    True only for a Claude turn with mode ``"off"`` (snapshot off) on a CLI that has the flag.
    """
    return provider == "claude" and mode == "off" and await snapshot_flag_supported()
