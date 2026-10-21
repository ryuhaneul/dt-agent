"""Cross-platform process-tree termination helpers."""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import sys

from ductor_bot.infra.platform import CREATION_FLAGS as _CREATION_FLAGS

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

_PS_TIMEOUT_SECONDS = 5.0
_TASKKILL_TIMEOUT_SECONDS = 5.0


def list_process_descendants(pid: int) -> list[int]:
    """Return recursive child PIDs for *pid* on POSIX systems.

    On Windows this returns an empty list because recursive enumeration is
    delegated to ``taskkill /T`` in kill helpers.
    """
    if _IS_WINDOWS or pid <= 0:
        return []

    snapshot = _read_process_snapshot()
    if not snapshot:
        return []

    children: dict[int, list[int]] = {}
    for line in snapshot.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        with contextlib.suppress(ValueError):
            child_pid = int(parts[0])
            parent_pid = int(parts[1])
            children.setdefault(parent_pid, []).append(child_pid)

    descendants: list[int] = []
    seen: set[int] = set()
    stack = list(children.get(pid, []))
    while stack:
        child = stack.pop()
        if child in seen:
            continue
        seen.add(child)
        descendants.append(child)
        stack.extend(children.get(child, []))
    return descendants


def terminate_process_tree(pid: int) -> None:
    """Send a graceful termination signal to a process tree."""
    if pid <= 0:
        return

    if _IS_WINDOWS:
        _run_taskkill(pid, force=False)
        return

    targets = [pid, *list_process_descendants(pid)]
    _send_posix_signal(targets, signal.SIGTERM)


def force_kill_process_tree(pid: int) -> None:
    """Force-kill a process tree."""
    if pid <= 0:
        return

    if _IS_WINDOWS:
        _run_taskkill(pid, force=True)
        return

    # Kill descendants before root to avoid reparenting survivors.
    targets = [*list_process_descendants(pid), pid]
    _send_posix_signal(targets, signal.SIGKILL)


def interrupt_process(pid: int) -> None:
    """Send SIGINT to a single process (soft interrupt, e.g. ESC in CLI).

    Unlike :func:`terminate_process_tree` this targets only the lead process
    and uses SIGINT so the CLI can handle the interrupt gracefully (e.g.
    cancel the current tool execution without exiting).
    """
    if pid <= 0:
        return
    if _IS_WINDOWS:
        with contextlib.suppress(OSError, PermissionError):
            os.kill(pid, getattr(signal, "CTRL_C_EVENT", signal.SIGINT))
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGINT)


def _run_taskkill(pid: int, *, force: bool) -> None:
    cmd = ["taskkill"]
    if force:
        cmd.append("/F")
    cmd.extend(["/T", "/PID", str(pid)])
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=_TASKKILL_TIMEOUT_SECONDS,
            creationflags=_CREATION_FLAGS,
        )


def _send_posix_signal(targets: list[int], sig: signal.Signals) -> None:
    current_pid = os.getpid()
    for target in targets:
        if target <= 0 or target == current_pid:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(target, sig)


def _read_process_snapshot() -> str:
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            check=False,
            timeout=_PS_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            return result.stdout

    logger.debug("Failed to read process snapshot via ps")
    return ""
