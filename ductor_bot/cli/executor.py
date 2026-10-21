"""Shared subprocess execution for CLI providers.

Centralises the duplicated subprocess lifecycle (creation, stdin feeding,
process-registry tracking, stderr draining, streaming read-loop with timeout,
and cleanup) that was repeated across ``claude_provider`` and ``codex_provider``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from asyncio import CancelledError, ensure_future, shield
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from ductor_bot.cli.base import (
    _IS_WINDOWS,
    CLIConfig,
    _feed_stdin_and_close,
    _win_feed_stdin,
)
from ductor_bot.cli.stream_events import ResultEvent, StreamEvent
from ductor_bot.cli.timeout_controller import TimeoutController
from ductor_bot.cli.types import CLIResponse, task_id_from_label
from ductor_bot.infra.platform import CREATION_FLAGS as _CREATION_FLAGS
from ductor_bot.infra.process_tree import force_kill_process_tree

if TYPE_CHECKING:
    from ductor_bot.cli.process_registry import TrackedProcess

logger = logging.getLogger(__name__)


_CleanupResult = TypeVar("_CleanupResult")


async def await_process_cleanup(cleanup: Awaitable[_CleanupResult]) -> _CleanupResult:
    """Finish owned process cleanup before propagating another cancellation."""
    waiter = ensure_future(cleanup)
    cancelled = False
    while not waiter.done():
        try:
            await shield(waiter)
        except CancelledError:
            cancelled = True
    result = waiter.result()
    if cancelled:
        raise CancelledError
    return result


def _origin_env(config: CLIConfig) -> dict[str, str | None]:
    """Return the requesting thread's routing vars for an inter-agent turn."""
    chat_id = config.origin_chat_id
    return {
        "DUCTOR_ORIGIN_CHAT_ID": str(chat_id) if chat_id else None,
        "DUCTOR_ORIGIN_TOPIC_ID": (
            str(config.origin_topic_id) if chat_id and config.origin_topic_id else None
        ),
        "DUCTOR_ORIGIN_TRANSPORT": (
            (config.origin_transport or config.transport) if chat_id else None
        ),
    }


def framework_identity_env(agent_name: str, interagent_port: int) -> dict[str, str]:
    """Identity and internal API address shared by interactive and one-shot turns."""
    return {
        "DUCTOR_INTERAGENT_HOST": "127.0.0.1",
        "DUCTOR_INTERAGENT_PORT": str(interagent_port),
        "DUCTOR_AGENT_NAME": agent_name,
        "DUCTOR_AGENT_ROLE": "main" if agent_name == "main" else "sub",
    }


def build_subprocess_env(
    config: CLIConfig,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the CLI environment from the trusted bot home and request context."""
    import os
    from pathlib import Path

    from ductor_bot.infra.env_secrets import (
        SECRETS_FILE_VARIABLE,
        load_env_secrets,
    )
    from ductor_bot.infra.secret_store import store_path

    env = os.environ.copy()
    ductor_home = Path(config.ductor_home).expanduser()
    main_home = ductor_home if config.agent_name == "main" else ductor_home.parent.parent

    # Working directory may be an untrusted project_roots path. Only the bot
    # home's .env supplies provider keys, with existing process vars winning.
    value: str | None
    for key, value in load_env_secrets(ductor_home / ".env").items():
        env.setdefault(key, value)
    # The shared secrets store is the main home's; the path is only a default for a run whose
    # process environment and ``.env`` do not name one. The values stay in the file.
    env[SECRETS_FILE_VARIABLE] = str(store_path(ductor_home))
    if extra_env:
        env.update(extra_env)

    # Framework routing and identity always win over inherited, .env, and
    # provider-specific extra variables.
    env.update(framework_identity_env(config.agent_name, config.interagent_port))
    env["DUCTOR_HOME"] = str(ductor_home)
    env["DUCTOR_TRANSPORT"] = config.transport
    env["DUCTOR_SHARED_MEMORY_PATH"] = str(main_home / "SHAREDMEMORY.md")

    optional = {
        "DUCTOR_CHAT_ID": str(config.chat_id) if config.chat_id else None,
        "DUCTOR_TOPIC_ID": str(config.topic_id) if config.topic_id else None,
        **_origin_env(config),
        "DUCTOR_IA_TURN": "1" if config.interagent_turn else None,
        "DUCTOR_DELEGATION_ID": config.delegation_id or None,
        "DUCTOR_SESSION_ID": config.session_id or None,
        "DUCTOR_SESSION_KIND": config.session_kind,
        "DUCTOR_SESSION_NAME": config.session_name or None,
        "DUCTOR_RESULT_ORIGIN": config.result_origin or None,
        "DUCTOR_BROADCAST_REF": config.broadcast_ref or None,
        "DUCTOR_TASK_ID": task_id_from_label(config.process_label) or None,
        "DUCTOR_TRANSCRIBE_COMMAND": config.transcribe_command or None,
        "DUCTOR_VIDEO_TRANSCRIBE_COMMAND": config.video_transcribe_command or None,
    }
    for key, value in optional.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


@dataclass(slots=True)
class SubprocessSpec:
    """What to run: command, working directory, prompt, timeout, and stdin."""

    exec_cmd: list[str]
    use_cwd: str | None
    prompt: str
    timeout_seconds: float | None = None
    timeout_controller: TimeoutController | None = None
    stdin_text: str | None = None
    extra_env: dict[str, str] | None = None


def effective_limit(
    timeout_seconds: float | None, timeout_controller: TimeoutController | None
) -> float | None:
    """The longest a request lets its process run (a controller's extensions included)."""
    if timeout_controller:
        return timeout_controller.max_total_seconds
    return timeout_seconds


def _effective_limit(spec: SubprocessSpec) -> float | None:
    return effective_limit(spec.timeout_seconds, spec.timeout_controller)


def _flag_stale(event: StreamEvent, tracked: TrackedProcess | None) -> StreamEvent:
    """Carry "the stale cleanup killed it" into the final event of the run."""
    if isinstance(event, ResultEvent) and tracked is not None and tracked.stale_killed:
        event.stale_killed = True
    return event


@dataclass(slots=True)
class SubprocessResult:
    """Outcome of a completed streaming subprocess."""

    process: asyncio.subprocess.Process
    stderr_bytes: bytes


# ---------------------------------------------------------------------------
# Streaming subprocess
# ---------------------------------------------------------------------------

LineHandler = Callable[[str], AsyncGenerator[StreamEvent, None]]
"""Async generator that receives a decoded stdout line and yields events."""

PostHandler = Callable[[SubprocessResult], AsyncGenerator[StreamEvent, None]]
"""Async generator that receives the subprocess result after stream ends."""


async def _default_post_handler(result: SubprocessResult) -> AsyncGenerator[StreamEvent, None]:
    """Yield an error ``ResultEvent`` when the process exited non-zero."""
    if result.process.returncode != 0:
        stderr_text = (
            result.stderr_bytes.decode(errors="replace")[:2000] if result.stderr_bytes else ""
        )
        yield ResultEvent(
            type="result",
            result=stderr_text[:500],
            is_error=True,
            returncode=result.process.returncode,
        )


async def run_streaming_subprocess(
    config: CLIConfig,
    spec: SubprocessSpec,
    line_handler: LineHandler,
    *,
    provider_label: str = "CLI",
    post_handler: PostHandler | None = None,
) -> AsyncGenerator[StreamEvent, None]:
    """Spawn a subprocess and stream stdout lines through *line_handler*.

    Lifecycle:
    1. Create subprocess with stdout/stderr pipes
    2. Feed stdin when requested (or on Windows legacy prompt pipe)
    3. Register in process registry
    4. Drain stderr in background task
    5. Stream stdout lines through *line_handler* with timeout
    6. On timeout: kill, yield error, return
    7. Cleanup: cancel drain, unregister tracked process
    8. Post-loop: delegate to *post_handler* (default: yield error on non-zero exit)
    """
    subprocess_env = build_subprocess_env(config, spec.extra_env) if spec.use_cwd else None
    process = await asyncio.create_subprocess_exec(
        *spec.exec_cmd,
        stdin=_stdin_pipe(spec),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=spec.use_cwd,
        env=subprocess_env,
        limit=4 * 1024 * 1024,
        creationflags=_CREATION_FLAGS,
    )
    if process.stdout is None or process.stderr is None:
        msg = "Subprocess created without stdout/stderr pipes"
        raise RuntimeError(msg)
    # Feed stdin concurrently with the stdout read loop: a prompt larger than
    # the OS pipe buffer (~64 KiB) would otherwise deadlock against a child
    # that starts emitting stdout before draining stdin.
    stdin_feed = asyncio.create_task(_feed_streaming_stdin(process, spec))
    logger.info("%s subprocess starting pid=%s", provider_label, process.pid)

    reg = config.process_registry
    tracked = (
        reg.register(
            config.chat_id,
            process,
            config.process_label,
            topic_id=config.topic_id,
            limit_seconds=_effective_limit(spec),
        )
        if reg
        else None
    )
    stderr_drain = asyncio.create_task(process.stderr.read())

    try:
        async for event in _stream_with_timeout(process, spec, line_handler):
            yield _flag_stale(event, tracked)
        stderr_bytes = await stderr_drain
    except TimeoutError:
        force_kill_process_tree(process.pid)
        await process.wait()
        timeout_s = spec.timeout_seconds or 0
        logger.warning("%s stream timed out after %.0fs", provider_label, timeout_s)
        yield ResultEvent(
            type="result",
            result=f"__TIMEOUT__{int(timeout_s)}",
            is_error=True,
        )
        return
    finally:
        if not stdin_feed.done():
            stdin_feed.cancel()
        with contextlib.suppress(BaseException):
            await stdin_feed
        await _cancel_drain(stderr_drain)
        if tracked and reg:
            reg.unregister(tracked)

    await process.wait()

    handler = post_handler or _default_post_handler
    async for event in handler(SubprocessResult(process=process, stderr_bytes=stderr_bytes)):
        yield _flag_stale(event, tracked)


# ---------------------------------------------------------------------------
# Streaming timeout strategies
# ---------------------------------------------------------------------------


async def _stream_with_timeout(
    process: asyncio.subprocess.Process,
    spec: SubprocessSpec,
    line_handler: LineHandler,
) -> AsyncGenerator[StreamEvent, None]:
    """Read stdout lines with either a plain timeout or a managed controller.

    When ``spec.timeout_controller`` is set, the controller manages deadline
    extensions triggered by output activity and fires warning callbacks.
    Otherwise a plain ``asyncio.timeout`` is used (backward-compatible).
    """
    if spec.timeout_controller:
        async for event in _stream_with_controller(process, spec.timeout_controller, line_handler):
            yield event
    else:
        async with asyncio.timeout(spec.timeout_seconds):
            while True:
                line_bytes = await process.stdout.readline()  # type: ignore[union-attr]
                if not line_bytes:
                    break
                line = line_bytes.decode(errors="replace").rstrip()
                logger.debug("Stream line: %s", line[:120])
                async for event in line_handler(line):
                    yield event


async def _stream_with_controller(
    process: asyncio.subprocess.Process,
    tc: TimeoutController,
    line_handler: LineHandler,
) -> AsyncGenerator[StreamEvent, None]:
    """Streaming read loop managed by a :class:`TimeoutController`.

    Uses ``asyncio.timeout`` with a retry-on-extend pattern:  when the timeout
    fires but the controller grants an extension (recent activity + budget),
    a new timeout context is entered to continue reading.
    """
    tc.begin()
    warning_task = tc.start_warning_loop()

    try:
        timeout_secs = tc.timeout_seconds
        while True:
            try:
                async with asyncio.timeout(timeout_secs):
                    while True:
                        line_bytes = await process.stdout.readline()  # type: ignore[union-attr]
                        if not line_bytes:
                            return  # EOF
                        tc.record_activity()
                        line = line_bytes.decode(errors="replace").rstrip()
                        logger.debug("Stream line: %s", line[:120])
                        async for event in line_handler(line):
                            yield event
            except TimeoutError:
                if tc.try_extend():
                    timeout_secs = tc.activity_extension_seconds
                    continue
                raise
    finally:
        if warning_task and not warning_task.done():
            warning_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await warning_task


# ---------------------------------------------------------------------------
# Non-streaming subprocess
# ---------------------------------------------------------------------------


async def run_oneshot_subprocess(
    config: CLIConfig,
    spec: SubprocessSpec,
    parse_output: Callable[[bytes, bytes, int | None], CLIResponse],
    *,
    provider_label: str = "CLI",
) -> CLIResponse:
    """Run a subprocess, wait for completion, return parsed output.

    Lifecycle:
    1. Create subprocess with pipes
    2. Communicate (optional stdin + wait)
    3. Register/unregister in process registry
    4. Handle timeout
    5. Parse output via *parse_output* callback
    """
    oneshot_env = build_subprocess_env(config, spec.extra_env) if spec.use_cwd else None
    process = await asyncio.create_subprocess_exec(
        *spec.exec_cmd,
        stdin=_stdin_pipe(spec),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=spec.use_cwd,
        env=oneshot_env,
        creationflags=_CREATION_FLAGS,
    )
    logger.info("%s subprocess starting pid=%s", provider_label, process.pid)

    reg = config.process_registry
    tracked = (
        reg.register(
            config.chat_id,
            process,
            config.process_label,
            topic_id=config.topic_id,
            limit_seconds=_effective_limit(spec),
        )
        if reg
        else None
    )
    try:
        stdin_data = _stdin_bytes(spec)
        if spec.timeout_controller:
            communicate_coro = process.communicate(input=stdin_data)
            stdout, stderr = await spec.timeout_controller.run_with_timeout(communicate_coro)
        else:
            async with asyncio.timeout(spec.timeout_seconds):
                stdout, stderr = await process.communicate(input=stdin_data)
    except CancelledError:
        if process.returncode is None:
            force_kill_process_tree(process.pid)
        with contextlib.suppress(CancelledError):
            await await_process_cleanup(process.wait())
        raise
    except TimeoutError:
        if process.returncode is None:
            force_kill_process_tree(process.pid)
        await await_process_cleanup(process.wait())
        logger.warning("%s timed out after %.0fs", provider_label, spec.timeout_seconds)
        return CLIResponse(result="", is_error=True, timed_out=True)
    finally:
        if tracked and reg:
            reg.unregister(tracked)

    response = parse_output(stdout, stderr, process.returncode)
    if tracked and tracked.stale_killed:
        response.stale_killed = True
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stdin_pipe(spec: SubprocessSpec) -> int | None:
    """Return a stdin pipe when the provider supplies stdin or Windows needs it."""
    return asyncio.subprocess.PIPE if spec.stdin_text is not None or _IS_WINDOWS else None


def _stdin_bytes(spec: SubprocessSpec) -> bytes | None:
    """Return bytes to feed to communicate(), preserving Windows legacy behavior."""
    if spec.stdin_text is not None:
        return spec.stdin_text.encode()
    return spec.prompt.encode() if _IS_WINDOWS else None


async def _feed_streaming_stdin(
    process: asyncio.subprocess.Process,
    spec: SubprocessSpec,
) -> None:
    """Feed stdin for streaming processes, using the old Windows fallback when needed."""
    if spec.stdin_text is not None:
        await _feed_stdin_and_close(process, spec.stdin_text)
        return
    _win_feed_stdin(process, spec.prompt)


async def _cancel_drain(drain: asyncio.Task[bytes]) -> None:
    """Cancel a stderr drain task and silently absorb any resulting exception."""
    if not drain.done():
        drain.cancel()
        with contextlib.suppress(BaseException):
            await drain
