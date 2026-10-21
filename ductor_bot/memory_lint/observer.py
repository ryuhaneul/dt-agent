"""MemoryLintObserver: runs the workspace ``memory_lint.py check`` on a schedule.

``mode: structure`` (default): a clean wiki costs nothing, no agent turn is started. Only when
the lint finds problems does the observer hand a prompt (with the report) to the
transport-specific report handler, which runs one agent turn and delivers its answer.

``mode: full``: every period the observer hands over a prompt with the structural result, the
user-editable instructions (``tools/memory_tools/LINT_FULL.md``) and the monthly stale-check
flag; the agent turn does the semantic work. ``mode: off`` does nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from cronsim import CronSim, CronSimError

from ductor_bot.config import resolve_user_timezone
from ductor_bot.infra.atomic_io import atomic_text_save
from ductor_bot.infra.base_observer import BaseObserver
from ductor_bot.memory_lint.instructions import DEFAULT_FULL_INSTRUCTIONS

if TYPE_CHECKING:
    from ductor_bot.config import AgentConfig, MemoryLintConfig
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 600.0  # seconds between "is it due?" checks
_LINT_TIMEOUT = 60.0
_TOOL = ("tools", "memory_tools", "memory_lint.py")
_FULL_INSTRUCTIONS = ("tools", "memory_tools", "LINT_FULL.md")

# Called with the full prompt for the report turn. Returns once the turn was handled.
ReportHandler = Callable[[str], Awaitable[None]]

_REREAD_RULE = (
    "In this run, actually read the pages you judge again and decide from what they say now. "
    "Do not reuse conclusions of earlier runs as evidence."
)
_SCOPE_RULE = (
    "Only suggest things that belong to maintaining the memory wiki. Do not suggest work "
    "outside it, such as restoring test or working files."
)

REPORT_PROMPT = (
    "[Weekly memory lint]\n"
    "The scheduled check of memory_system/ found {count} problem(s). Report from "
    "tools/memory_tools/memory_lint.py:\n\n"
    "{report}\n\n"
    "Fix what is safe to fix yourself (frontmatter, links, missing index lines, MAINMEMORY size "
    "by moving details into topic files), then re-run "
    "`python3 tools/memory_tools/memory_lint.py check`. Never delete content without asking. "
    "Finish with a short message for the user: what you fixed and what needs their decision. "
    + _SCOPE_RULE
)

FULL_PROMPT = (
    "[Memory lint - full check]\n"
    "This is the scheduled full check of memory_system/. Your final answer is delivered to the "
    "user as the report.\n\n"
    "## Structural check result (tools/memory_tools/memory_lint.py check)\n"
    "{structure}\n\n"
    "## Stale check flag\n"
    "{stale}\n\n"
    "## Rules of this run\n" + _REREAD_RULE + "\n" + _SCOPE_RULE + "\n\n"
    "## Instructions\n"
    "{instructions}"
)
_STALE_PERFORM = "Perform the stale check in this run (first run this month)."
_STALE_SKIP = (
    "The stale check was already attempted in the first run of this month: skip it and state "
    "in the report that it was skipped in this run because it was already attempted this month."
)
_NO_PROBLEMS = "no structural problems"
_CHECK_FAILED = "structural check failed to run"


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    """Kill *proc* if it is still running and wait for it (also while being cancelled)."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    # A second cancellation must not abandon the reap: finish it in a shielded task.
    reaper = asyncio.ensure_future(proc.wait())
    try:
        await asyncio.shield(reaper)
    except asyncio.CancelledError:
        await reaper
        raise


class MemoryLintObserver(BaseObserver):
    """Periodic memory lint; reports to the user only when problems exist."""

    def __init__(self, config: AgentConfig, paths: DuctorPaths) -> None:
        super().__init__()
        self._config = config
        self._paths = paths
        self._handler: ReportHandler | None = None
        self._invalid_schedules: set[str] = set()

    @property
    def _cfg(self) -> MemoryLintConfig:
        return self._config.memory_lint

    def set_report_handler(self, handler: ReportHandler) -> None:
        self._handler = handler

    # -- state (last run time survives restarts) -------------------------------

    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._paths.memory_lint_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, state: dict[str, Any]) -> None:
        try:
            atomic_text_save(self._paths.memory_lint_state_path, json.dumps(state, indent=2) + "\n")
        except OSError:
            logger.warning("Could not persist memory lint state", exc_info=True)

    # -- loop ------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(_CHECK_INTERVAL)
                if not self._running:
                    break  # type: ignore[unreachable]  # state can change across the await
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Memory lint tick failed (continuing)")
        except asyncio.CancelledError:
            logger.debug("Memory lint loop cancelled")

    async def tick(self, *, now: float | None = None) -> bool:
        """Run the lint when due. Returns True when a report turn was started."""
        cfg = self._cfg
        if cfg.mode == "off" or cfg.report == "off" or self._handler is None:
            return False
        current = time.time() if now is None else now
        state = self._load_state()
        last = state.get("last_run")
        if not isinstance(last, (int, float)):
            # First sight of this home: start the clock instead of linting immediately.
            state["last_run"] = current
            self._save_state(state)
            return False
        if not self._is_due(cfg, float(last), current):
            return False
        # Record the attempt first: at most one report per period, even if the turn fails.
        state["last_run"] = current
        stale_due = False
        if cfg.mode == "full":
            month = self._month(current)
            stale_due = state.get("last_stale_month") != month
            if stale_due:
                state["last_stale_month"] = month
        self._save_state(state)
        report = await self.run_lint()
        if cfg.mode == "full":
            logger.info("Memory lint full check; starting report turn (stale check: %s)", stale_due)
            await self._handler(self._full_prompt(report, stale_due))
            return True
        if report is None or report.get("ok"):
            if report is None:
                logger.debug("Memory lint: nothing to report")
            else:
                logger.info("Memory lint structure check: 0 problems")
            return False
        from ductor_bot.memory_lint.render import render_report

        prompt = REPORT_PROMPT.format(
            count=report.get("problem_count", 0), report=render_report(report)
        )
        logger.info(
            "Memory lint found %s problem(s); starting report turn", report["problem_count"]
        )
        await self._handler(prompt)
        return True

    def _is_due(self, cfg: MemoryLintConfig, last: float, current: float) -> bool:
        if cfg.schedule:
            return self._slot_passed(cfg.schedule, last, current)
        return current - last >= cfg.every_hours * 3600

    def _slot_passed(self, schedule: str, last: float, current: float) -> bool:
        """True when a scheduled time lies after *last* and not after *current*.

        Times are wall-clock times in the agent's timezone, like cron jobs. Several missed
        slots count once. An invalid expression is logged once per value and never runs.
        """
        tz = resolve_user_timezone(self._config.user_timezone)
        try:
            after = datetime.fromtimestamp(last, tz).replace(tzinfo=None)
            slot = next(CronSim(schedule, after)).replace(tzinfo=tz)
        except (CronSimError, StopIteration):
            if schedule not in self._invalid_schedules:
                self._invalid_schedules.add(schedule)
                logger.warning("Memory lint: invalid schedule %r; the lint will not run", schedule)
            return False
        return slot.timestamp() <= current

    def _month(self, current: float) -> str:
        """Calendar month (YYYY-MM) of *current* in the agent's configured timezone."""
        tz = resolve_user_timezone(self._config.user_timezone)
        return datetime.fromtimestamp(current, tz).strftime("%Y-%m")

    def _load_full_instructions(self) -> str:
        """User-editable instructions; the built-in text when the file is missing/empty/unreadable."""
        path = self._paths.workspace.joinpath(*_FULL_INSTRUCTIONS)
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            logger.warning("Memory lint: cannot read %s; using the built-in instructions", path)
            return DEFAULT_FULL_INSTRUCTIONS.strip()
        if not text:
            logger.warning("Memory lint: %s is empty; using the built-in instructions", path)
            return DEFAULT_FULL_INSTRUCTIONS.strip()
        return text

    def _full_prompt(self, report: dict[str, Any] | None, stale_due: bool) -> str:
        from ductor_bot.memory_lint.render import render_report

        if report is None:
            structure = _CHECK_FAILED
        elif report.get("ok"):
            structure = _NO_PROBLEMS
        else:
            structure = f"{report.get('problem_count', 0)} problem(s):\n{render_report(report)}"
        return FULL_PROMPT.format(
            structure=structure,
            stale=_STALE_PERFORM if stale_due else _STALE_SKIP,
            instructions=self._load_full_instructions(),
        )

    async def run_lint(self) -> dict[str, Any] | None:
        """Execute the workspace lint tool; ``None`` when it is missing or fails."""
        tool = self._paths.workspace.joinpath(*_TOOL)
        if not tool.is_file():
            logger.warning("Memory lint tool missing: %s", tool)
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(tool),
                "check",
                "--json",
                cwd=str(self._paths.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            logger.warning("Memory lint run failed", exc_info=True)
            return None
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_LINT_TIMEOUT)
        except BaseException as exc:
            # Timeout, stop or cancellation: never leave the child running or unreaped.
            await _kill_and_reap(proc)
            if not isinstance(exc, (TimeoutError, OSError)):
                raise
            logger.warning("Memory lint run failed or timed out", exc_info=True)
            return None
        if proc.returncode not in (0, 1):
            logger.warning("Memory lint exited %s: %s", proc.returncode, stderr[:200])
            return None
        try:
            data = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Memory lint produced unreadable output")
            return None
        return data if isinstance(data, dict) else None
