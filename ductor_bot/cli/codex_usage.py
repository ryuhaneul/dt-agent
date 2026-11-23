"""On-demand, process-shared Codex account limit queries over stdio JSON-RPC."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from shutil import which
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ductor_bot.cli.auth import AuthStatus, check_codex_auth
from ductor_bot.infra.platform import CREATION_FLAGS
from ductor_bot.infra.process_tree import force_kill_process_tree

logger = logging.getLogger(__name__)
QUERY_TIMEOUT = 15.0
UsageError = Literal["login_required", "timeout", "invalid_response"]


class CodexWindow(BaseModel):
    """Only the window values needed for display, without account identifiers."""

    model_config = ConfigDict(strict=True, populate_by_name=True)
    used_percent: int = Field(alias="usedPercent", ge=0)
    window_minutes: int | None = Field(alias="windowDurationMins")
    resets_at: int | None = Field(alias="resetsAt")


@dataclass(frozen=True, slots=True)
class CodexUsage:
    """A transient query result; failures and identifiers are never persisted."""

    windows: tuple[CodexWindow, ...] = ()
    captured_at: int | None = None
    error: UsageError | None = None


_in_flight: asyncio.Task[CodexUsage] | None = None
_completed_at: float | None = None
_last_successful: CodexUsage | None = None
_turn_refreshes: set[asyncio.Task[None]] = set()
_usage_listeners: set[Callable[[], None]] = set()
_logged_refresh_errors: set[str] = set()


def parse_codex_usage(result: object) -> CodexUsage:
    """Read only primary/secondary from the required camelCase rateLimits bucket."""
    if not isinstance(result, dict) or not isinstance(result.get("rateLimits"), dict):
        raise TypeError("Missing rate limits")
    limits = result["rateLimits"]
    windows = tuple(
        CodexWindow.model_validate(limits[name], by_name=False)
        for name in ("primary", "secondary")
        if limits.get(name) is not None
    )
    return CodexUsage(windows=windows, captured_at=int(time.time()))


def last_codex_usage() -> CodexUsage | None:
    """Read the last successful observation without starting or awaiting a query."""
    return _last_successful


def subscribe_codex_usage(listener: Callable[[], None]) -> Callable[[], None]:
    """Notify the listener after a new successful observation is stored."""
    _usage_listeners.add(listener)
    return lambda: _usage_listeners.discard(listener)


def schedule_codex_usage_refresh() -> None:
    """Refresh after a turn without making its response wait for the query."""
    task = asyncio.create_task(_refresh_after_turn(), name="codex-usage-refresh")
    _turn_refreshes.add(task)
    task.add_done_callback(_turn_refreshes.discard)


async def _refresh_after_turn() -> None:
    try:
        reason: str | None = (await query_codex_usage(reuse_completed=False)).error
    except Exception as exc:
        reason = type(exc).__name__
    if reason is not None:
        level = logging.DEBUG if reason in _logged_refresh_errors else logging.WARNING
        _logged_refresh_errors.add(reason)
        logger.log(level, "Codex usage refresh failed: %s", reason)


async def query_codex_usage(*, reuse_completed: bool = True) -> CodexUsage:
    """Share pending queries and optionally reuse results for ten seconds."""
    global _in_flight  # noqa: PLW0603
    if _in_flight is None or (
        _in_flight.done()
        and (
            not reuse_completed
            or _in_flight.cancelled()
            or _completed_at is None
            or time.monotonic() - _completed_at >= 10
        )
    ):
        _in_flight = asyncio.create_task(_query_and_cache())
    return await asyncio.shield(_in_flight)


async def _query_and_cache() -> CodexUsage:
    global _completed_at, _last_successful  # noqa: PLW0603
    result = await _query()
    changed = result.error is None and result != _last_successful
    if result.error is None:
        _last_successful = result
    if changed:
        for listener in tuple(_usage_listeners):
            try:
                listener()
            except Exception as exc:
                logger.debug("Codex usage listener failed: %s", type(exc).__name__)
    _completed_at = time.monotonic()
    return result


async def _send(process: asyncio.subprocess.Process, message: dict[str, object]) -> None:
    if process.stdin is None:
        raise ValueError("Missing input pipe")
    process.stdin.write((json.dumps(message) + "\n").encode())
    await process.stdin.drain()


async def _read_result(process: asyncio.subprocess.Process, request_id: int) -> object:
    if process.stdout is None:
        raise ValueError("Missing output pipe")
    while line := await process.stdout.readline():
        message = json.loads(line)
        if not isinstance(message, dict):
            raise TypeError("Invalid RPC message")
        if message.get("id") != request_id:
            continue
        if "error" in message or not isinstance(message.get("result"), dict):
            raise ValueError("Invalid RPC response")
        return message["result"]
    raise ValueError("Missing RPC response")


async def _query() -> CodexUsage:
    process: asyncio.subprocess.Process | None = None
    try:
        codex_path = which("codex")
        if not codex_path or check_codex_auth().status != AuthStatus.AUTHENTICATED:
            return CodexUsage(error="login_required")
        async with asyncio.timeout(QUERY_TIMEOUT):
            process = await asyncio.create_subprocess_exec(
                codex_path,
                "app-server",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=CREATION_FLAGS,
            )
            await _send(
                process,
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "ductor", "version": "1.0"}},
                },
            )
            await _read_result(process, 1)
            await _send(process, {"method": "initialized"})
            await _send(
                process,
                {
                    "id": 2,
                    "method": "account/rateLimits/read",
                    "params": {"excludeResetCreditDetails": True},
                },
            )
            result = parse_codex_usage(await _read_result(process, 2))
            if process.returncode not in (None, 0):
                return CodexUsage(error="invalid_response")
            return result
    except TimeoutError:
        logger.debug("Codex usage query timed out")
        return CodexUsage(error="timeout")
    except Exception as exc:
        logger.debug("Codex usage query unavailable: %s", type(exc).__name__)
        return CodexUsage(error="invalid_response")
    finally:
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if process.returncode is None:
                await asyncio.to_thread(force_kill_process_tree, process.pid)
            try:
                async with asyncio.timeout(5):
                    await process.wait()
            except TimeoutError:
                logger.warning("Codex usage process wait timed out")
