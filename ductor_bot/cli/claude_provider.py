"""Async wrapper around the Claude Code CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ductor_bot.cli.base import (
    BaseCLI,
    CLIConfig,
    add_cli_opt,
    format_cli_cmd,
)
from ductor_bot.cli.claude_accounts import SlotRegistry, detect_rate_limit
from ductor_bot.cli.claude_usage import UsageStore, extract_usage_events, usage_file_path
from ductor_bot.cli.executor import (
    PostHandler,
    SubprocessResult,
    SubprocessSpec,
    run_oneshot_subprocess,
    run_streaming_subprocess,
)
from ductor_bot.cli.gemini_utils import create_system_prompt_file
from ductor_bot.cli.slot_rotation import (
    UNPARSABLE_RESET_COOLDOWN_SECONDS,
    AttemptOutcome,
    RotationResult,
    build_fallback_prefix,
    build_unparsable_prefix,
    hot_slots,
    make_registry,
    parse_reset_from_text,
    record_cooldown,
    run_with_rotation,
    slot_extra_env,
)
from ductor_bot.cli.stream_events import (
    ResultEvent,
    StreamEvent,
    parse_stream_line,
)
from ductor_bot.cli.types import CLIResponse
from ductor_bot.config import resolve_user_timezone

if TYPE_CHECKING:
    from ductor_bot.cli.timeout_controller import TimeoutController

logger = logging.getLogger(__name__)

# System prompts go to the CLI as 0600 temp files and the user prompt over stdin,
# so neither shows up in the process list (``ps``) or hits the argv size cap.
_SYSTEM_PREFIX = "ductor_system_"
_APPEND_PREFIX = "ductor_append_"


@dataclass(slots=True)
class _PromptFiles:
    """Temp files carrying the configured system prompts for one CLI run."""

    system: str | None = None
    append: str | None = None


@dataclass(slots=True)
class _StreamPassOutcome:
    """Buffered result of one streaming pass (used to decide fallback)."""

    events: list[StreamEvent] = field(default_factory=list)
    final_result: ResultEvent | None = None
    stderr_text: str = ""
    returncode: int | None = None


class ClaudeCodeCLI(BaseCLI):
    """Async wrapper around the Claude Code CLI."""

    def __init__(self, config: CLIConfig) -> None:
        self._config = config
        self._working_dir = Path(config.working_dir).resolve()
        self._cli = self._find_cli()
        self._registry: SlotRegistry | None = None
        self._usage_store = UsageStore(usage_file_path(Path(config.ductor_home) / "workspace"))
        if config.claude_accounts:
            # This project is container-only: share cooldown state across all agents
            self._registry = make_registry(
                working_dir=str(Path(config.ductor_home) / "workspace"),
            )
        logger.info("CLI wrapper: cwd=%s, model=%s", self._working_dir, config.model)

    @staticmethod
    def _find_cli() -> str:
        path = which("claude")
        if not path:
            msg = (
                "claude CLI not found on PATH. "
                "Install via: npm install -g @anthropic-ai/claude-code"
            )
            raise FileNotFoundError(msg)
        return path

    def _build_command(
        self,
        resume_session: str | None = None,
        continue_session: bool = False,
        *,
        prompt_files: _PromptFiles | None = None,
    ) -> list[str]:
        """Build the CLI argv. The user prompt is never part of it (stdin only)."""
        cfg = self._config
        files = prompt_files or _PromptFiles()
        cmd = [self._cli, "-p", "--output-format", "json"]

        add_cli_opt(cmd, "--permission-mode", cfg.permission_mode)
        add_cli_opt(cmd, "--model", cfg.model)
        if cfg.reasoning_effort and cfg.reasoning_effort != "default":
            cmd += ["--effort", cfg.reasoning_effort]
        add_cli_opt(cmd, "--system-prompt-file", files.system)
        add_cli_opt(cmd, "--append-system-prompt-file", files.append)
        add_cli_opt(
            cmd,
            "--max-turns",
            str(cfg.max_turns) if cfg.max_turns is not None else None,
        )
        add_cli_opt(
            cmd,
            "--max-budget-usd",
            str(cfg.max_budget_usd) if cfg.max_budget_usd is not None else None,
        )

        if cfg.allowed_tools:
            cmd += ["--allowedTools", *cfg.allowed_tools]
        if cfg.disallowed_tools:
            cmd += ["--disallowedTools", *cfg.disallowed_tools]

        if resume_session:
            cmd += ["--resume", resume_session]
        elif continue_session:
            cmd.append("--continue")

        # Add extra CLI parameters before the separator
        if cfg.cli_parameters:
            cmd.extend(cfg.cli_parameters)

        if "--verbose" not in cmd:
            cmd.insert(1, "--verbose")
        return cmd

    # ------------------------------------------------------------------
    # Slot selection helpers
    # ------------------------------------------------------------------

    def _slot_extra_env(self, slot_name: str) -> dict[str, str]:
        """Return the env overrides that pin the CLI to *slot_name*."""
        return slot_extra_env(
            slot_name,
            working_dir=str(Path(self._config.ductor_home) / "workspace"),
        )

    def _default_timezone(self) -> ZoneInfo:
        return resolve_user_timezone(self._config.user_timezone)

    # ------------------------------------------------------------------
    # send() — non-streaming
    # ------------------------------------------------------------------

    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> CLIResponse:
        """Send a prompt and return the final result (with optional fallback)."""

        async def _attempt(slot: str | None) -> tuple[AttemptOutcome, CLIResponse]:
            response = await self._send_once(
                prompt,
                resume_session=resume_session,
                continue_session=continue_session,
                timeout_seconds=timeout_seconds,
                timeout_controller=timeout_controller,
                slot_name=slot,
            )
            outcome = AttemptOutcome(
                returncode=response.returncode,
                is_error=response.is_error,
                stderr=response.stderr,
                result_text=response.result or "",
            )
            return outcome, response

        rotation = await run_with_rotation(
            accounts=list(self._config.claude_accounts),
            registry=self._registry,
            default_tz=self._default_timezone(),
            attempt=_attempt,
        )
        return self._apply_send_prefix(rotation)

    def _apply_send_prefix(self, rotation: RotationResult[CLIResponse]) -> CLIResponse:
        """Turn a :class:`RotationResult` into the final CLIResponse."""
        assert rotation.payload is not None  # accounts empty path still attempts once
        payload = rotation.payload

        if rotation.unparsable_stderr is not None:
            prefix = build_unparsable_prefix(rotation.unparsable_stderr)
            return payload.model_copy(update={"result": prefix + (payload.result or "")})

        # Fallback prefix applies only when an earlier slot was rate-limited
        # *and* a later slot actually succeeded (all_rate_limited=False).
        if (
            rotation.first_rl_slot is not None
            and rotation.first_rl_reset is not None
            and rotation.slot is not None
            and not rotation.all_rate_limited
        ):
            prefix = build_fallback_prefix(
                rotation.first_rl_slot,
                rotation.slot,
                rotation.first_rl_reset,
                self._default_timezone(),
                reset_estimated=rotation.first_rl_reset_estimated,
            )
            return payload.model_copy(update={"result": prefix + (payload.result or "")})

        return payload

    async def _send_once(  # noqa: PLR0913
        self,
        prompt: str,
        *,
        resume_session: str | None,
        continue_session: bool,
        timeout_seconds: float | None,
        timeout_controller: TimeoutController | None,
        slot_name: str | None,
    ) -> CLIResponse:
        """Execute one non-streaming pass using *slot_name* (or CLI default)."""
        prompt_files = self._write_prompt_files()
        usage_output = b""

        def parse_output(stdout: bytes, stderr: bytes, returncode: int | None) -> CLIResponse:
            nonlocal usage_output
            usage_output = stdout
            return _parse_response(
                stdout, stderr, returncode, private_output=self._config.private_task_question
            )

        try:
            cmd = self._build_command(resume_session, continue_session, prompt_files=prompt_files)
            extra_env = self._slot_extra_env(slot_name or "claude")
            exec_cmd, use_cwd = cmd, str(self._working_dir)
            _log_cmd(exec_cmd)
            captured_at = int(time.time())
            response = await run_oneshot_subprocess(
                config=self._config,
                spec=SubprocessSpec(
                    exec_cmd,
                    use_cwd,
                    prompt,
                    timeout_seconds,
                    timeout_controller,
                    stdin_text=prompt,
                    extra_env=extra_env or None,
                ),
                parse_output=parse_output,
                provider_label="CLI",
            )
            await self._capture_usage(usage_output, slot_name, captured_at=captured_at)
            return response
        finally:
            await _cleanup_prompt_files(prompt_files)

    # ------------------------------------------------------------------
    # send_streaming()
    # ------------------------------------------------------------------

    def _build_command_streaming(
        self,
        resume_session: str | None = None,
        continue_session: bool = False,
        *,
        prompt_files: _PromptFiles | None = None,
    ) -> list[str]:
        """Build CLI command with --output-format stream-json."""
        cmd = self._build_command(resume_session, continue_session, prompt_files=prompt_files)
        try:
            idx = cmd.index("json")
            cmd[idx] = "stream-json"
        except ValueError:
            pass
        if "--verbose" not in cmd:
            cmd.insert(1, "--verbose")
        return cmd

    async def send_streaming(  # noqa: C901, PLR0912
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Send a prompt and yield stream events live as they arrive.

        Multi-account setups stream events live (not buffered) from each
        attempt. If a slot returns a rate-limit ``ResultEvent``, we record
        the cooldown and retry on the next hot slot — subsequent stream
        events replace the prior slot's output in the edit-mode UI. The
        final emitted ``ResultEvent`` carries the fallback prefix so the
        user sees ``[Claude: A → B ...]`` when a switch happened.
        """
        accounts = list(self._config.claude_accounts)

        if not accounts:
            async for event in self._send_streaming_once(
                prompt,
                resume_session=resume_session,
                continue_session=continue_session,
                timeout_seconds=timeout_seconds,
                timeout_controller=timeout_controller,
                slot_name=None,
            ):
                yield event
            return

        hot = await hot_slots(self._registry, accounts)

        if not hot:
            # Every configured slot is cooling down — attempt the top
            # priority once so the user still sees a response. No fallback
            # prefix (it would misleadingly imply a successful switch).
            logger.info("Claude: all slots cooling down; attempting top slot once")
            top = accounts[0]
            async for event in self._stream_single_attempt_with_cooldown(
                prompt,
                resume_session=resume_session,
                continue_session=continue_session,
                timeout_seconds=timeout_seconds,
                timeout_controller=timeout_controller,
                slot_name=top,
                record_on_rate_limit=True,
            ):
                yield event
            return

        first_rl_slot: str | None = None
        first_rl_reset = None
        first_rl_reset_estimated = False

        for idx, slot in enumerate(hot):
            is_last = idx == len(hot) - 1
            capture = _StreamPassOutcome()

            async for event in self._stream_attempt(
                prompt,
                resume_session=resume_session,
                continue_session=continue_session,
                timeout_seconds=timeout_seconds,
                timeout_controller=timeout_controller,
                slot_name=slot,
                capture=capture,
            ):
                if isinstance(event, ResultEvent):
                    # Defer final result emission until we decide on retry.
                    continue
                yield event

            final = capture.final_result
            returncode = capture.returncode
            if returncode is None and final is not None:
                returncode = final.returncode
            attempt = AttemptOutcome(
                returncode=returncode,
                is_error=bool(final.is_error) if final is not None else False,
                stderr=capture.stderr_text,
                result_text=final.result if final is not None else "",
            )

            is_rl = detect_rate_limit(
                returncode=attempt.returncode,
                is_error=attempt.is_error,
                stderr=attempt.stderr,
                result=attempt.result_text,
            )

            if not is_rl:
                if final is None:
                    return
                if first_rl_slot is not None and first_rl_reset is not None:
                    prefix = build_fallback_prefix(
                        first_rl_slot,
                        slot,
                        first_rl_reset,
                        self._default_timezone(),
                        reset_estimated=first_rl_reset_estimated,
                    )
                    yield final.model_copy(
                        update={"result": prefix + (final.result or "")},
                    )
                else:
                    yield final
                return

            reset_dt = parse_reset_from_text(
                attempt.stderr + "\n" + attempt.result_text,
                self._default_timezone(),
            )
            reset_estimated = reset_dt is None
            if reset_dt is None:
                logger.warning(
                    "Claude slot=%s reset time unparsable — cooling down for 1 hour",
                    slot,
                )
                reset_dt = datetime.fromtimestamp(
                    time.time() + UNPARSABLE_RESET_COOLDOWN_SECONDS, tz=self._default_timezone()
                )

            await record_cooldown(self._registry, slot, reset_dt.timestamp())
            if first_rl_slot is None:
                first_rl_slot = slot
                first_rl_reset = reset_dt
                first_rl_reset_estimated = reset_estimated
            logger.info(
                "Claude slot=%s rate-limited (reset %s); %s",
                slot,
                reset_dt.isoformat(timespec="minutes"),
                "trying next slot" if not is_last else "no more hot slots",
            )

            if is_last:
                # Every hot slot was rate-limited — surface the final 429
                # unchanged; a "switched" prefix would be misleading.
                logger.info(
                    "Claude: all %d hot slots rate-limited; surfacing last 429",
                    len(hot),
                )
                if final is not None:
                    yield final
                return

    async def _stream_attempt(  # noqa: PLR0913
        self,
        prompt: str,
        *,
        resume_session: str | None,
        continue_session: bool,
        timeout_seconds: float | None,
        timeout_controller: TimeoutController | None,
        slot_name: str,
        capture: _StreamPassOutcome,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Run one streaming pass live, capturing stderr/result for rotation decisions.

        Events are yielded live as they arrive from the subprocess. The
        first ``ResultEvent`` is additionally stashed in *capture* so the
        caller can inspect it for rate-limit detection without buffering
        the whole stream.
        """
        prompt_files = self._write_prompt_files()
        usage_events: list[tuple[object, int]] = []
        try:
            cmd = self._build_command_streaming(
                resume_session, continue_session, prompt_files=prompt_files
            )
            extra_env = self._slot_extra_env(slot_name)
            exec_cmd, use_cwd = cmd, str(self._working_dir)
            _log_cmd(exec_cmd, streaming=True)

            async for event in run_streaming_subprocess(
                config=self._config,
                spec=SubprocessSpec(
                    exec_cmd,
                    use_cwd,
                    prompt,
                    timeout_seconds,
                    timeout_controller,
                    stdin_text=prompt,
                    extra_env=extra_env or None,
                ),
                line_handler=partial(self._usage_line_handler, usage_events=usage_events),
                provider_label="CLI",
                post_handler=_make_capturing_post_handler(capture),
            ):
                if isinstance(event, ResultEvent) and capture.final_result is None:
                    capture.final_result = event
                yield event
        finally:
            try:
                await self._save_usage_events(usage_events, slot_name)
            finally:
                await _cleanup_prompt_files(prompt_files)

    async def _stream_single_attempt_with_cooldown(  # noqa: PLR0913
        self,
        prompt: str,
        *,
        resume_session: str | None,
        continue_session: bool,
        timeout_seconds: float | None,
        timeout_controller: TimeoutController | None,
        slot_name: str,
        record_on_rate_limit: bool,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream a single slot live; update cooldown if result is rate-limited.

        Used for the "all slots cooling down" fallback path where we still
        want to attempt the top slot for user feedback and record any new
        cooldown we observe, but we do NOT retry on other slots.
        """
        capture = _StreamPassOutcome()
        async for event in self._stream_attempt(
            prompt,
            resume_session=resume_session,
            continue_session=continue_session,
            timeout_seconds=timeout_seconds,
            timeout_controller=timeout_controller,
            slot_name=slot_name,
            capture=capture,
        ):
            yield event

        if not record_on_rate_limit:
            return

        final = capture.final_result
        returncode = capture.returncode
        if returncode is None and final is not None:
            returncode = final.returncode
        if detect_rate_limit(
            returncode=returncode,
            is_error=bool(final.is_error) if final is not None else False,
            stderr=capture.stderr_text,
            result=final.result if final is not None else "",
        ):
            reset_dt = parse_reset_from_text(
                capture.stderr_text + "\n" + (final.result if final else ""),
                self._default_timezone(),
            )
            if reset_dt is None:
                logger.warning(
                    "Claude slot=%s reset time unparsable — cooling down for 1 hour", slot_name
                )
                reset_dt = datetime.fromtimestamp(
                    time.time() + UNPARSABLE_RESET_COOLDOWN_SECONDS, tz=self._default_timezone()
                )
            await record_cooldown(self._registry, slot_name, reset_dt.timestamp())

    async def _send_streaming_once(  # noqa: PLR0913
        self,
        prompt: str,
        *,
        resume_session: str | None,
        continue_session: bool,
        timeout_seconds: float | None,
        timeout_controller: TimeoutController | None,
        slot_name: str | None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Run one streaming pass, yielding events as they are parsed."""
        prompt_files = self._write_prompt_files()
        usage_events: list[tuple[object, int]] = []
        try:
            cmd = self._build_command_streaming(
                resume_session, continue_session, prompt_files=prompt_files
            )
            extra_env = self._slot_extra_env(slot_name or "claude")
            exec_cmd, use_cwd = cmd, str(self._working_dir)
            _log_cmd(exec_cmd, streaming=True)

            async for event in run_streaming_subprocess(
                config=self._config,
                spec=SubprocessSpec(
                    exec_cmd,
                    use_cwd,
                    prompt,
                    timeout_seconds,
                    timeout_controller,
                    stdin_text=prompt,
                    extra_env=extra_env or None,
                ),
                line_handler=partial(self._usage_line_handler, usage_events=usage_events),
                provider_label="CLI",
            ):
                yield event
        finally:
            try:
                await self._save_usage_events(usage_events, slot_name)
            finally:
                await _cleanup_prompt_files(prompt_files)

    async def _capture_usage(
        self, output: str | bytes, slot_name: str | None, *, captured_at: int
    ) -> None:
        events = [(event, captured_at) for event in extract_usage_events(output)]
        await self._save_usage_events(events, slot_name)

    async def _save_usage_events(
        self, events: list[tuple[object, int]], slot_name: str | None
    ) -> None:
        for event, captured_at in events:
            try:
                await asyncio.to_thread(
                    self._usage_store.record, slot_name or "claude", event, captured_at=captured_at
                )
            except Exception as exc:
                logger.warning("Claude usage save failed: %s", type(exc).__name__)

    async def _usage_line_handler(
        self, line: str, *, usage_events: list[tuple[object, int]]
    ) -> AsyncGenerator[StreamEvent, None]:
        if '"rate_limit_event"' in line:
            captured_at = int(time.time())
            usage_events.extend((event, captured_at) for event in extract_usage_events(line))
        async for event in _claude_line_handler(line):
            yield event

    def _write_prompt_files(self) -> _PromptFiles:
        """Write the configured system prompts to private temp files (always)."""
        cfg = self._config
        files = _PromptFiles()
        try:
            if cfg.system_prompt:
                files.system = create_system_prompt_file(cfg.system_prompt, prefix=_SYSTEM_PREFIX)
            if cfg.append_system_prompt:
                files.append = create_system_prompt_file(
                    cfg.append_system_prompt, prefix=_APPEND_PREFIX
                )
        except BaseException:
            _unlink_prompt_files(files)
            raise
        return files


def _unlink_prompt_files(files: _PromptFiles) -> None:
    for path in (files.system, files.append):
        if path:
            with contextlib.suppress(OSError):
                Path(path).unlink(missing_ok=True)


async def _cleanup_prompt_files(files: _PromptFiles) -> None:
    """Delete both temp files in one worker call (best effort, cancel-safe)."""
    await asyncio.to_thread(_unlink_prompt_files, files)


def _make_capturing_post_handler(
    outcome: _StreamPassOutcome,
) -> PostHandler:
    """Build an executor post-handler that captures full stderr into *outcome*.

    Mirrors the executor's default post handler (yielding a synthetic
    ``ResultEvent`` on non-zero exit), but additionally stashes the full
    stderr and returncode in *outcome* so the fallback layer can parse the
    reset time accurately.
    """

    async def _post(result: SubprocessResult) -> AsyncGenerator[StreamEvent, None]:
        outcome.stderr_text = (
            result.stderr_bytes.decode(errors="replace") if result.stderr_bytes else ""
        )
        outcome.returncode = result.process.returncode
        # Claude CLI emits a rich final `result` event on rate limits
        # (is_error=true, result="You've hit your limit ..."); only synthesize
        # a placeholder when the stream did NOT already produce one, so we
        # don't clobber the real haystack that detect_rate_limit() needs.
        if result.process.returncode != 0 and outcome.final_result is None:
            yield ResultEvent(
                type="result",
                result=outcome.stderr_text[:500],
                is_error=True,
                returncode=result.process.returncode,
            )

    return _post


async def _claude_line_handler(line: str) -> AsyncGenerator[StreamEvent, None]:
    """Parse a single Claude stream-json line into stream events."""
    for event in parse_stream_line(line):
        yield event


def _log_cmd(cmd: list[str], *, streaming: bool = False) -> None:
    """Log the Claude CLI command with redacted, truncated long values."""
    kind = "stream cmd" if streaming else "cmd"
    logger.info("CLI %s: %s", kind, format_cli_cmd(cmd))


def _parse_response(
    stdout: bytes, stderr: bytes, returncode: int | None, *, private_output: bool = False
) -> CLIResponse:
    """Parse CLI subprocess output into a CLIResponse."""
    stderr_text = stderr.decode(errors="replace")[:2000] if stderr else ""
    if stderr_text:
        logger.warning(
            "CLI stderr: %s",
            f"private exit={returncode} bytes={len(stderr)}"
            if private_output
            else stderr_text[:500],
        )

    raw = stdout.decode().strip()
    if not raw:
        logger.error("CLI returned empty output (exit=%s)", returncode)
        return CLIResponse(
            result=stderr_text.strip(),
            is_error=True,
            returncode=returncode,
            stderr=stderr_text,
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.exception(
            "Failed to parse CLI JSON: %s",
            f"private exit={returncode} bytes={len(stdout)}" if private_output else raw[:500],
            exc_info=not private_output,
        )
        return CLIResponse(result=raw, is_error=True, returncode=returncode, stderr=stderr_text)

    # When --verbose is combined with --output-format json, Claude CLI emits a
    # JSON array of events (system init, assistant messages, rate-limit, result)
    # instead of a single dict. Extract the final 'result' entry so downstream
    # code still receives a dict.
    if isinstance(data, list):
        result_entry = next(
            (
                evt
                for evt in reversed(data)
                if isinstance(evt, dict) and evt.get("type") == "result"
            ),
            None,
        )
        if result_entry is None:
            logger.error(
                "CLI returned array without a 'result' event: %s",
                f"private exit={returncode} bytes={len(stdout)}" if private_output else raw[:500],
            )
            return CLIResponse(result=raw, is_error=True, returncode=returncode, stderr=stderr_text)
        data = result_entry

    response = CLIResponse(
        session_id=data.get("session_id"),
        result=data.get("result", ""),
        is_error=data.get("is_error", False),
        returncode=returncode,
        stderr=stderr_text,
        duration_ms=data.get("duration_ms"),
        duration_api_ms=data.get("duration_api_ms"),
        num_turns=data.get("num_turns"),
    )

    if response.is_error:
        logger.error(
            "CLI error: %s",
            f"private exit={returncode} chars={len(response.result)}"
            if private_output
            else response.result[:200],
        )
    else:
        logger.info(
            "CLI done session=%s turns=%s duration_ms=%.0f",
            (response.session_id or "?")[:8],
            response.num_turns,
            response.duration_ms or 0,
        )

    return response
