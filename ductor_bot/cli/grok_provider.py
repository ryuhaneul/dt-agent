"""Async wrapper around the xAI Grok Build CLI (`grok`).

Grok Build CLI surface (headless) closely mirrors Claude Code:

* ``-p / --single`` one-shot prompt
* ``--output-format json | streaming-json | plain``
* ``--resume`` / ``--continue`` session continuity
* ``--permission-mode`` (includes ``bypassPermissions``)
* ``--always-approve`` auto-approve tools
* ``--system-prompt-override`` / ``--rules``
* ``--model`` / ``--reasoning-effort`` / ``--max-turns``
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from functools import partial
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING

from ductor_bot.cli.base import (
    BaseCLI,
    CLIConfig,
    add_cli_opt,
    format_cli_cmd,
    write_private_temp_file,
)
from ductor_bot.cli.executor import SubprocessSpec, run_oneshot_subprocess, run_streaming_subprocess
from ductor_bot.cli.grok_events import parse_grok_json, parse_grok_stream_line
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    StreamEvent,
)
from ductor_bot.cli.types import CLIResponse

if TYPE_CHECKING:
    from ductor_bot.cli.timeout_controller import TimeoutController

logger = logging.getLogger(__name__)


class GrokCLI(BaseCLI):
    """Async wrapper around the Grok Build CLI."""

    def __init__(self, config: CLIConfig) -> None:
        self._config = config
        self._working_dir = Path(config.working_dir).resolve()
        self._cli = self._find_cli()
        self._temp_prompt_files: list[Path] = []
        logger.info("Grok Build CLI wrapper: cwd=%s, model=%s", self._working_dir, config.model)

    @staticmethod
    def _find_cli() -> str:
        path = which("grok")
        if not path:
            msg = (
                "grok CLI not found on PATH. "
                "Install via: curl -fsSL https://x.ai/cli/install.sh | bash"
            )
            raise FileNotFoundError(msg)
        return path

    def _build_command(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        *,
        output_format: str = "json",
    ) -> list[str]:
        cfg = self._config
        cmd = [self._cli, "--output-format", output_format]

        # Prefer explicit permission mode; auto-approve tools when bypassing.
        add_cli_opt(cmd, "--permission-mode", cfg.permission_mode)
        if cfg.permission_mode == "bypassPermissions":
            cmd.append("--always-approve")

        add_cli_opt(cmd, "--model", cfg.model)
        if cfg.reasoning_effort and cfg.reasoning_effort != "default":
            # Grok accepts both --reasoning-effort and --effort.
            cmd += ["--reasoning-effort", cfg.reasoning_effort]
        add_cli_opt(cmd, "--system-prompt-override", cfg.system_prompt)
        add_cli_opt(cmd, "--rules", cfg.append_system_prompt)
        add_cli_opt(cmd, "--max-turns", str(cfg.max_turns) if cfg.max_turns is not None else None)

        # Built-in tool filter (headless): comma-separated tool IDs.
        # Distinct from permission rules (--allow/--deny with Bash(...) globs).
        if cfg.allowed_tools:
            cmd += ["--tools", ",".join(cfg.allowed_tools)]
        if cfg.disallowed_tools:
            cmd += ["--disallowed-tools", ",".join(cfg.disallowed_tools)]

        if resume_session:
            cmd += ["--resume", resume_session]
        elif continue_session:
            cmd.append("--continue")

        if cfg.cli_parameters:
            cmd.extend(cfg.cli_parameters)

        # The prompt always goes through a 0600 --prompt-file, never argv (visible in ps).
        prompt_path = self._write_prompt_file(prompt)
        cmd += ["--prompt-file", str(prompt_path)]

        return cmd

    def _write_prompt_file(self, prompt: str) -> Path:
        path = Path(write_private_temp_file(prompt, prefix="ductor-grok-prompt-", suffix=".txt"))
        self._temp_prompt_files.append(path)
        return path

    def _cleanup_prompt_files(self) -> None:
        for path in self._temp_prompt_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.debug("Failed to remove temp prompt file %s", path)
        self._temp_prompt_files.clear()

    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> CLIResponse:
        """Send a prompt and return the final result."""
        try:
            cmd = self._build_command(
                prompt,
                resume_session,
                continue_session,
                output_format="json",
            )
            exec_cmd, use_cwd = cmd, str(self._working_dir)
            _log_cmd(exec_cmd)
            return await run_oneshot_subprocess(
                config=self._config,
                spec=SubprocessSpec(
                    exec_cmd,
                    use_cwd,
                    prompt,
                    timeout_seconds,
                    timeout_controller,
                ),
                parse_output=partial(
                    _parse_response, private_output=self._config.private_task_question
                ),
                provider_label="Grok Build",
            )
        finally:
            self._cleanup_prompt_files()

    async def send_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Send a prompt and yield stream events as they arrive."""
        try:
            cmd = self._build_command(
                prompt,
                resume_session,
                continue_session,
                output_format="streaming-json",
            )
            exec_cmd, use_cwd = cmd, str(self._working_dir)
            _log_cmd(exec_cmd, streaming=True)

            accumulated: list[str] = []
            saw_result = False

            async for event in run_streaming_subprocess(
                config=self._config,
                spec=SubprocessSpec(
                    exec_cmd,
                    use_cwd,
                    prompt,
                    timeout_seconds,
                    timeout_controller,
                ),
                line_handler=_grok_line_handler,
                provider_label="Grok Build",
            ):
                if isinstance(event, AssistantTextDelta) and event.text:
                    accumulated.append(event.text)
                out = event
                if isinstance(event, ResultEvent):
                    saw_result = True
                    # Grok end events often omit the full text; fill from deltas.
                    if not event.result and accumulated:
                        out = event.model_copy(update={"result": "".join(accumulated)})
                yield out

            if not saw_result and accumulated:
                yield ResultEvent(type="result", result="".join(accumulated), is_error=False)
        finally:
            self._cleanup_prompt_files()


async def _grok_line_handler(line: str) -> AsyncGenerator[StreamEvent, None]:
    """Parse a single Grok streaming-json line into stream events."""
    for event in parse_grok_stream_line(line):
        yield event


def _log_cmd(cmd: list[str], *, streaming: bool = False) -> None:
    """Log the Grok CLI command with truncated long values (no redaction)."""
    kind = "stream cmd" if streaming else "cmd"
    logger.info("Grok %s: %s", kind, format_cli_cmd(cmd, redact=False, opt_prefix="-"))


def _parse_response(
    stdout: bytes, stderr: bytes, returncode: int | None, *, private_output: bool = False
) -> CLIResponse:
    """Parse Grok oneshot JSON into a CLIResponse."""
    stderr_text = stderr.decode(errors="replace")[:2000] if stderr else ""
    if stderr_text:
        logger.warning(
            "Grok stderr: %s",
            f"private exit={returncode} bytes={len(stderr)}"
            if private_output
            else stderr_text[:500],
        )

    raw = stdout.decode().strip()
    if not raw:
        logger.error("Grok returned empty output (exit=%s)", returncode)
        return CLIResponse(
            result=stderr_text.strip(),
            is_error=True,
            returncode=returncode,
            stderr=stderr_text,
        )

    # Prefer last JSON object if the CLI printed trailing noise.
    text, session_id, num_turns, is_error = _parse_best_json(raw)
    if returncode not in (None, 0):
        is_error = True

    response = CLIResponse(
        session_id=session_id,
        result=text,
        is_error=is_error,
        returncode=returncode,
        stderr=stderr_text,
        num_turns=num_turns,
    )

    if response.is_error:
        logger.error(
            "Grok error: %s",
            f"private exit={returncode} chars={len(response.result or stderr_text)}"
            if private_output
            else (response.result or stderr_text)[:200],
        )
    else:
        logger.info(
            "Grok done session=%s turns=%s",
            (response.session_id or "?")[:8],
            response.num_turns,
        )
    return response


def _parse_best_json(
    raw: str,
) -> tuple[str, str | None, int | None, bool]:
    """Parse raw stdout, tolerating multi-line pretty JSON or trailing junk.

    ``parse_grok_json`` never raises — it degrades unparseable input to plain
    text. So probe validity with ``json.loads`` first and, when the whole blob
    is not JSON (NDJSON leaked into json mode, trailing noise), hand the last
    valid JSON line to the parser instead of surfacing raw JSON to the user.
    """
    try:
        json.loads(raw.strip())
    except json.JSONDecodeError:
        for line in reversed(raw.splitlines()):
            candidate = line.strip()
            if not candidate:
                continue
            try:
                json.loads(candidate)
            except json.JSONDecodeError:
                continue
            return parse_grok_json(candidate)
    return parse_grok_json(raw)
