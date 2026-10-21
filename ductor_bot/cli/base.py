"""Base types and abstract interface for CLI backends."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli._log_redact import redact_cmd_for_log
from ductor_bot.cli.stream_events import StreamEvent
from ductor_bot.cli.types import CLIResponse

if TYPE_CHECKING:
    from ductor_bot.cli.process_registry import ProcessRegistry
    from ductor_bot.cli.timeout_controller import TimeoutController

_IS_WINDOWS = sys.platform == "win32"


def _win_feed_stdin(process: asyncio.subprocess.Process, data: str) -> None:
    """Write prompt to stdin and close on Windows; no-op on POSIX."""
    if _IS_WINDOWS and process.stdin is not None:
        process.stdin.write(data.encode())
        process.stdin.close()


async def _feed_stdin_and_close(
    process: asyncio.subprocess.Process,
    data: str,
) -> None:
    """Write prompt to stdin and close the writer gracefully."""
    writer = getattr(process, "stdin", None)
    if writer is None:
        return

    with contextlib.suppress(BrokenPipeError, ConnectionResetError, RuntimeError, ValueError):
        writer.write(data.encode())
        drain_result = writer.drain()
        if inspect.isawaitable(drain_result):
            await drain_result

    writer.close()
    wait_closed = getattr(writer, "wait_closed", None)
    if wait_closed is None:
        return
    with contextlib.suppress(
        BrokenPipeError,
        ConnectionResetError,
        RuntimeError,
        OSError,
        ValueError,
    ):
        closed_result = wait_closed()
        if inspect.isawaitable(closed_result):
            await closed_result


def add_cli_opt(cmd: list[str], flag: str, value: str | None) -> None:
    """Append a ``flag value`` pair to *cmd* when *value* is truthy."""
    if value:
        cmd += [flag, value]


def format_cli_cmd(cmd: list[str], *, redact: bool = True, opt_prefix: str | None = "--") -> str:
    """Render *cmd* as a log-safe string: long values truncated, optionally redacted.

    Args:
        cmd: The command argv.
        redact: Redact env-assignment values via :func:`redact_cmd_for_log`.
        opt_prefix: Only truncate a long value when the preceding arg starts
            with this prefix; ``None`` truncates every long value.
    """
    from ductor_bot.messenger.queue_context import without_queue_context

    display = [without_queue_context(arg) for arg in (redact_cmd_for_log(cmd) if redact else cmd)]
    safe = [
        (c[:80] + "...")
        if len(c) > 80 and (opt_prefix is None or (i > 0 and display[i - 1].startswith(opt_prefix)))
        else c
        for i, c in enumerate(display)
    ]
    return " ".join(safe)


@dataclass(slots=True)
class CLIConfig:
    """Configuration for any CLI wrapper."""

    provider: str = "claude"
    working_dir: str | Path = "."
    ductor_home: str | Path = "/ductor"
    model: str | None = None
    system_prompt: str | None = None
    append_system_prompt: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    permission_mode: str = "bypassPermissions"
    # Reasoning effort: used by Codex (-c model_reasoning_effort) and Claude (--effort).
    reasoning_effort: str = "medium"
    # Codex-specific fields (ignored by Claude provider):
    sandbox_mode: str = "read-only"
    images: list[str] = field(default_factory=list)
    instructions: str | None = None
    # Process tracking (shared across providers):
    process_registry: ProcessRegistry | None = None
    chat_id: int = 0
    topic_id: int | None = None
    # Requesting thread for inter-agent turns (see AgentRequest.origin_*).
    origin_chat_id: int = 0
    origin_topic_id: int | None = None
    origin_transport: str = ""
    interagent_turn: bool = False
    private_task_question: bool = False
    delegation_id: str = ""
    session_kind: str = "background"
    session_name: str = ""
    process_label: str = "main"
    session_id: str = ""
    # Gemini-specific auth fallback:
    gemini_api_key: str | None = None
    # Extra CLI parameters (provider-specific):
    cli_parameters: list[str] = field(default_factory=list)
    # Transport identification (for routing results back):
    transport: str = "tg"
    result_origin: str = ""
    broadcast_ref: str = ""
    # Multi-agent identification:
    agent_name: str = "main"
    interagent_port: int = 8799
    # Claude-specific multi-account fallback (priority order, empty = disabled):
    claude_accounts: list[str] = field(default_factory=list)
    # Default timezone for parsing provider reset-time strings (e.g. "resets 3pm"):
    user_timezone: str = ""
    # External transcription hooks (#66) — empty strings keep built-in strategies.
    transcribe_command: str = ""
    video_transcribe_command: str = ""


def write_private_temp_file(
    content: str, *, prefix: str, suffix: str, directory: str | None = None
) -> str:
    """Write *content* to a new 0600 temp file and return its path (caller removes it).

    If creating, writing or closing fails, the partially written file is removed before the
    original error propagates, so no half-written private prompt is left behind.
    """
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w",
        encoding="utf-8",
        prefix=prefix,
        suffix=suffix,
        delete=False,
        dir=directory,
    )
    path = handle.name
    try:
        with handle:
            handle.write(content)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(path).unlink(missing_ok=True)
        raise
    return path


async def _cleanup_file(path: str | None) -> None:
    """Delete a temporary file from an async context (best effort)."""
    if not path:
        return
    with contextlib.suppress(OSError):
        await asyncio.to_thread(Path(path).unlink, missing_ok=True)


class BaseCLI(ABC):
    """Abstract interface for CLI backends (Claude, Codex, etc.)."""

    @abstractmethod
    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> CLIResponse: ...

    @abstractmethod
    def send_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> AsyncGenerator[StreamEvent, None]: ...
