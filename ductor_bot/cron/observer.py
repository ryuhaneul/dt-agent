"""In-process cron job scheduler: watches cron_jobs.json, schedules and executes jobs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypedDict

from cronsim import CronSim, CronSimError

from ductor_bot.cli.param_resolver import TaskOverrides
from ductor_bot.config import resolve_user_timezone
from ductor_bot.cron.manager import CronManager
from ductor_bot.infra.base_task_observer import BaseTaskObserver
from ductor_bot.infra.file_watcher import FileWatcher
from ductor_bot.infra.task_runner import execute_in_task_folder
from ductor_bot.log_context import set_log_context
from ductor_bot.utils.quiet_hours import check_quiet_hour

if TYPE_CHECKING:
    from ductor_bot.cli.codex_cache import CodexModelCache
    from ductor_bot.config import AgentConfig
    from ductor_bot.cron.manager import CronJob
    from ductor_bot.infra.task_runner import TaskResult
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill a preflight including grandchildren (POSIX process group; plain kill elsewhere).

    Safety invariant: killpg only ever runs for a verified pid > 1 and a
    pgid > 1. killpg(0) would signal our own group, killpg(1) becomes
    kill(-1) and terminates every process of this user — a mocked or
    corrupted pid must never be able to reach the syscall.
    """
    pid = proc.pid
    if sys.platform != "win32" and isinstance(pid, int) and pid > 1:
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError):
            pgid = 0
        if pgid > 1:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            else:
                return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


# Callback signature: (job_title, result_text, status, chat_id, topic_id, transport)
# New bus handlers also return failed transport names for targeted retries.
CronResultCallback = Callable[..., Awaitable[tuple[bool, str] | tuple[bool, str, list[str]]]]


@dataclass(slots=True)
class _ScheduledJob:
    """Scheduling payload for one cron entry."""

    id: str
    schedule: str
    instruction: str
    task_folder: str
    timezone: str


@dataclass(frozen=True, slots=True)
class _RunSnapshot:
    """What a run knows about its job at its start: kept for every notice the run sends.

    The definition may be deleted or replaced while the run is in progress; the destination
    and title of the run do not change with it.
    """

    job: CronJob | None
    title: str
    routing: tuple[int, int | None, str]


class _InjectionArgs(TypedDict, total=False):
    delivery_mode: str
    prompt: str


class CronObserver(BaseTaskObserver):
    """Watches cron_jobs.json and schedules jobs in-process.

    On start: reads all jobs, calculates next run times via cronsim,
    and schedules asyncio tasks. A background watcher polls the JSON
    file's mtime every 5 seconds; on change it reloads and reschedules.
    """

    def __init__(
        self,
        paths: DuctorPaths,
        manager: CronManager,
        *,
        config: AgentConfig,
        codex_cache: CodexModelCache,
        agent_name: str = "main",
    ) -> None:
        super().__init__(paths, config, codex_cache, agent_name)
        self._manager = manager
        self._on_result: CronResultCallback | None = None
        self._scheduled: dict[str, asyncio.Task[None]] = {}
        self._executing: set[str] = set()
        self._warned: set[str] = set()
        self._delivery_attempted: set[str] = set()
        self._reschedule_lock = asyncio.Lock()
        self._requested_reschedule_task: asyncio.Task[None] | None = None
        self._delivery_retry_task: asyncio.Task[None] | None = None
        self._running = False
        self._watcher = FileWatcher(
            paths.cron_jobs_path,
            self._on_file_change,
        )

    def set_result_handler(self, handler: CronResultCallback) -> None:
        """Set callback for job results (called after each execution)."""
        self._on_result = handler

    async def start(self) -> None:
        """Start the observer: schedule all jobs and begin watching."""
        self._running = True
        await self._schedule_all()
        await self._watcher.start()
        if self._config.cron_delivery_retry.enabled:
            self._delivery_retry_task = asyncio.create_task(self._delivery_retry_loop())
        logger.info("CronObserver started (%d jobs scheduled)", len(self._scheduled))

    async def stop(self) -> None:
        """Stop the observer: cancel all scheduled jobs and the watcher."""
        self._running = False
        await self._watcher.stop()
        retry_task = self._delivery_retry_task
        self._delivery_retry_task = None
        if retry_task is not None:
            retry_task.cancel()
            await asyncio.gather(retry_task, return_exceptions=True)
        request_task = self._requested_reschedule_task
        self._requested_reschedule_task = None
        if request_task is not None:
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
        tasks = list(self._scheduled.values())
        for task in tasks:
            task.cancel()
        self._scheduled.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("CronObserver stopped")

    def request_reschedule(self) -> None:
        """Queue a background reschedule request without blocking the caller."""
        if not self._running:
            return
        task = self._requested_reschedule_task
        if task is not None and not task.done():
            return
        self._requested_reschedule_task = asyncio.create_task(self._run_requested_reschedule())

    async def reschedule_now(self) -> None:
        """Reschedule all jobs immediately (used by interactive cron toggles)."""
        if not self._running:
            return
        await self._update_mtime()
        await self._reschedule_locked()

    async def _run_requested_reschedule(self) -> None:
        """Execute one queued reschedule request and log failures."""
        try:
            await self.reschedule_now()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background cron reschedule failed")
        finally:
            self._requested_reschedule_task = None

    # -- File watcher callback --

    async def _on_file_change(self) -> None:
        """Reload manager in a thread, then reschedule."""
        logger.info("File watcher detected cron_jobs.json change, rescheduling")
        await asyncio.to_thread(self._manager.reload)
        await self._reschedule_locked()

    # -- Scheduling --

    async def _schedule_all(self) -> None:
        """Schedule asyncio tasks for all enabled jobs."""
        await self._watcher.update_mtime()
        for job in self._manager.list_jobs():
            if job.enabled:
                self._schedule_job(
                    job.id,
                    job.schedule,
                    job.agent_instruction,
                    job.task_folder,
                    job.timezone,
                )

    async def _reschedule_all(self) -> None:
        """Cancel idle schedules, await their termination, then reschedule.

        Jobs that are currently executing are left running so that a
        ``cron_jobs.json`` change mid-execution does not cancel an active
        subprocess.  Awaiting cancellation of idle tasks prevents a race
        where the old task runs concurrently with its replacement.
        """
        cancelled: list[asyncio.Task[None]] = []
        for job_id, task in list(self._scheduled.items()):
            if job_id not in self._executing:
                task.cancel()
                cancelled.append(task)
                del self._scheduled[job_id]
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        await self._schedule_all()
        logger.info("Rescheduled %d jobs", len(self._scheduled))

    async def _reschedule_locked(self) -> None:
        """Serialize reschedules from watcher and interactive updates."""
        async with self._reschedule_lock:
            await self._reschedule_all()

    def _schedule_job(
        self,
        job_id: str,
        schedule: str,
        instruction: str,
        task_folder: str,
        job_timezone: str = "",
    ) -> None:
        """Calculate next run time and schedule an asyncio task.

        Uses the job's timezone (if set), then the global ``user_timezone``
        config, then the host OS timezone, and finally UTC as last resort.
        CronSim iterates in the resolved local timezone so that ``0 9 * * *``
        means 09:00 in the user's wall-clock time.
        """
        try:
            tz = resolve_user_timezone(job_timezone or self._config.user_timezone)
            now_local = datetime.now(tz)
            # CronSim works on time components; feed it the local time
            # so hour fields match the user's wall clock.
            now_naive = now_local.replace(tzinfo=None)
            it = CronSim(schedule, now_naive)
            next_naive: datetime = next(it)
            # Re-attach the timezone using fold=0 (prefer pre-DST interpretation
            # for ambiguous times).  For non-existent times (DST spring-forward
            # gap) the delay becomes negative; in that case advance to the next
            # cron slot so the job fires at the correct wall-clock time.
            next_aware = next_naive.replace(tzinfo=tz)  # fold=0 default
            delay = (next_aware - datetime.now(tz)).total_seconds()
            if delay < 0:
                next_naive = next(it)
                next_aware = next_naive.replace(tzinfo=tz)
                delay = (next_aware - datetime.now(tz)).total_seconds()
            delay = max(delay, 0)
            scheduled_job = _ScheduledJob(
                id=job_id,
                schedule=schedule,
                instruction=instruction,
                task_folder=task_folder,
                timezone=job_timezone,
            )
            existing = self._scheduled.get(job_id)
            if existing is not None and not existing.done() and job_id not in self._executing:
                existing.cancel()
                logger.debug("Cancelled prior idle task for %s", job_id)
            task = asyncio.create_task(
                self._run_at(delay, scheduled_job),
            )
            self._scheduled[job_id] = task
            logger.debug(
                "Scheduled %s: next run %s (%s), delay %.0fs",
                job_id,
                next_naive.isoformat(),
                tz.key,
                delay,
            )
        except (CronSimError, StopIteration):
            logger.warning("Invalid cron expression for job %s: %s", job_id, schedule)
        except Exception:
            logger.exception("Failed to schedule job %s", job_id)

    async def _run_at(self, delay: float, scheduled_job: _ScheduledJob) -> None:
        """Wait for delay, execute the job, then reschedule for next occurrence."""
        stage = "waiting"  # until the job is about to run; a cancel then only moves the schedule
        try:
            await asyncio.sleep(delay)
            # Clock-skew guard: verify wall clock reached the cron slot.
            # Protects against asyncio.sleep waking early after a clock jump
            # (WSL2/VM suspend/resume, NTP corrections, etc.).
            await self._ensure_cron_slot_reached(scheduled_job)
            stage = "executing"
            await self._execute_job(
                scheduled_job.id,
                scheduled_job.instruction,
                scheduled_job.task_folder,
            )
        except asyncio.CancelledError:
            if stage == "waiting":
                logger.debug("Cron job %s rescheduled while waiting for its slot", scheduled_job.id)
            else:
                logger.warning("Cron job %s cancelled during execution", scheduled_job.id)
            return
        except Exception:
            logger.exception("Cron job %s failed unexpectedly", scheduled_job.id)
        if self._running:
            job = self._manager.get_job(scheduled_job.id)
            if job and job.enabled:
                self._schedule_job(
                    scheduled_job.id,
                    scheduled_job.schedule,
                    scheduled_job.instruction,
                    scheduled_job.task_folder,
                    scheduled_job.timezone,
                )

    async def _ensure_cron_slot_reached(self, scheduled_job: _ScheduledJob) -> None:
        """Re-sleep if wall clock shows we woke before the cron slot.

        ``asyncio.sleep`` relies on the event-loop monotonic clock, which can
        jump forward (relative to wall time) when the host suspends and
        resumes (WSL2, VMs, hibernation) or after NTP corrections. When that
        happens the sleep completes before the intended cron slot and the job
        fires early.  This helper re-checks wall clock against the next cron
        match and sleeps the deficit.  Iterations are bounded to avoid an
        infinite loop on misconfigured expressions.
        """
        tz = resolve_user_timezone(
            scheduled_job.timezone or self._config.user_timezone,
        )
        for _ in range(5):
            now_aware = datetime.now(tz)
            try:
                it = CronSim(
                    scheduled_job.schedule,
                    now_aware.replace(tzinfo=None) - timedelta(minutes=1),
                )
                next_naive = next(it)
            except (CronSimError, StopIteration):
                return
            next_aware = next_naive.replace(tzinfo=tz)
            deficit = (next_aware - now_aware).total_seconds()
            if deficit <= 30:
                return
            logger.warning(
                "Cron job %s woke %.0fs early (wall clock), re-sleeping",
                scheduled_job.id,
                deficit,
            )
            await asyncio.sleep(deficit)

    # -- Execution --

    async def _deliver_result(  # noqa: PLR0913
        self,
        job_id: str,
        job_title: str,
        result_text: str,
        status: str,
        routing: tuple[int, int | None, str] = (0, None, "tg"),
        *,
        retry_transports: list[str] | None = None,
        delivery_mode: str = "separate",
        prompt: str = "",
    ) -> tuple[bool, str, list[str]]:
        """Send result to the external handler (e.g. Telegram).

        Uses *job_title* (computed at execution start) so delivery works even
        if the job was removed from the manager mid-execution.
        *routing* is ``(chat_id, topic_id, transport)``; ``(0, None, "tg")``
        means broadcast.

        A *prompt* (``delivery_mode`` inject / in_session) is injected into the target
        session instead; the handler then delivers the session's reply.

        Returns delivery status, error, and failed broadcast transports.
        """
        if job_id in self._executing:
            self._delivery_attempted.add(job_id)
        if not self._on_result:
            return False, "no result handler registered", []
        try:
            if prompt:
                ack = await self._on_result(
                    job_title,
                    result_text,
                    status,
                    *routing,
                    delivery_mode=delivery_mode,
                    prompt=prompt,
                    job_id=job_id,
                )
            elif retry_transports:
                ack = await self._on_result(
                    job_title, result_text, status, *routing, retry_transports=retry_transports
                )
            else:
                ack = await self._on_result(job_title, result_text, status, *routing)
            return ack[0], ack[1], list(ack[2]) if len(ack) == 3 else []
        except Exception as exc:
            logger.exception("Error in cron result handler for job %s", job_id)
            return False, type(exc).__name__, list(retry_transports or [])

    async def _delivery_retry_loop(self) -> None:
        """Periodically retry preserved delivery failures without rerunning the agent."""
        interval = self._config.cron_delivery_retry.interval_seconds
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self._retry_failed_deliveries()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Cron delivery retry sweep failed")

    def _delivery_retry_due(self, job: CronJob, now: datetime) -> bool:
        """Whether *job* has a preserved failed delivery that is due for a retry."""
        if job.id in self._executing:
            # A regular execution is in flight; it may replace the preserved
            # result at any moment — never race it with a retry delivery.
            return False
        if job.last_delivery_status != "failed" or not job.last_result_text:
            return False
        if job.delivery_retry_attempts >= self._config.cron_delivery_retry.max_attempts:
            return False
        if job.next_delivery_retry_at:
            try:
                if datetime.fromisoformat(job.next_delivery_retry_at) > now:
                    return False
            except (ValueError, TypeError):
                logger.warning(
                    "Invalid next_delivery_retry_at for cron job %s; retrying now",
                    job.id,
                )
        return True

    async def _retry_failed_deliveries(self) -> None:
        """Retry every due failed delivery once, preserving failures for later sweeps."""
        retry_config = self._config.cron_delivery_retry
        now = datetime.now(UTC)
        changed = False
        for job in self._manager.list_jobs():
            if not self._delivery_retry_due(job, now):
                continue

            retried_text = job.last_result_text or ""
            routing = (job.chat_id, job.topic_id, job.transport)
            delivered, error, failed_transports = await self._deliver_result(
                job.id,
                job.title,
                retried_text,
                job.last_run_status or "success",
                routing,
                retry_transports=job.failed_transports if not job.chat_id else None,
            )
            next_attempt = None
            if not delivered:
                next_attempt = (now + timedelta(seconds=retry_config.interval_seconds)).isoformat()
            self._manager.update_delivery_retry(
                job.id,
                delivered=delivered,
                delivery_error=error,
                failed_transports=failed_transports,
                next_attempt_at=next_attempt,
                expected_text=retried_text,
            )
            changed = True
            if delivered:
                logger.info("Cron delivery retry succeeded job=%s", job.title)
            else:
                logger.warning(
                    "Cron delivery retry failed job=%s attempt=%d/%d error=%s",
                    job.title,
                    job.delivery_retry_attempts,
                    retry_config.max_attempts,
                    error,
                )
        if changed:
            await self._refresh_mtime()

    async def _execute_job(
        self,
        job_id: str,
        instruction: str,
        task_folder: str,
    ) -> None:
        """Spawn a fresh CLI session in the cron_task folder."""
        self._executing.add(job_id)
        self._delivery_attempted.discard(job_id)
        snapshot = self._snapshot(job_id)
        try:
            await self._execute_job_inner(job_id, instruction, task_folder, snapshot)
        except Exception as exc:
            logger.exception("Cron job %s failed with an exception", job_id)
            await self._report_job_exception(job_id, exc, snapshot)
        finally:
            self._executing.discard(job_id)
            self._delivery_attempted.discard(job_id)

    def _snapshot(self, job_id: str) -> _RunSnapshot:
        job = self._manager.get_job(job_id)
        routing = (job.chat_id, job.topic_id, job.transport) if job else (0, None, "tg")
        return _RunSnapshot(job, job.title if job else job_id, routing)

    async def _report_job_exception(
        self, job_id: str, exc: Exception, snapshot: _RunSnapshot
    ) -> None:
        """Turn an exception of a run into one failed run result.

        The failure is recorded and delivered like any other failed run (a plain message,
        also in inject / in_session mode: no session turn is made for it); a failed
        delivery is preserved for the delivery retry. Only the exception type is
        reported to the chat. A run that already attempted its delivery is not notified
        a second time. The notice goes to the destination the run started with, even when the
        job was deleted or given another destination meanwhile.
        """
        if job_id in self._delivery_attempted:
            return
        job_title, routing = snapshot.title, snapshot.routing
        status = "error:exception"
        text = f"Cron job '{job_title}' failed with an error ({type(exc).__name__})."
        delivered, delivery_error, failed_transports = await self._deliver_result(
            job_id, job_title, text, status, routing
        )
        current = self._manager.get_job(job_id)
        if current is None or (current.chat_id, current.topic_id, current.transport) != routing:
            # Deleted or given another destination meanwhile: the failure belongs to the old
            # run only, and a preserved notice must never be resent to a new destination.
            logger.info("Cron job %s changed during the run; failed run not recorded", job_id)
            return
        try:
            self._manager.update_run_status(
                job_id,
                status=status,
                delivery_status="ok" if delivered else "failed",
                delivery_error=delivery_error,
                failed_transports=failed_transports,
                result_text=None if delivered else text,
            )
            await self._refresh_mtime()
        except Exception:
            logger.exception("Cron job %s: could not record the failed run", job_id)

    def _result_time(self) -> str:
        """Current time in the user's timezone, as shown in the header of an injected turn."""
        tz = resolve_user_timezone(self._config.user_timezone)
        return f"{datetime.now(tz).isoformat(timespec='seconds')} ({tz.key})"

    async def _execute_job_inner(
        self,
        job_id: str,
        instruction: str,
        task_folder: str,
        snapshot: _RunSnapshot | None = None,
    ) -> None:
        set_log_context(operation="cron")
        run = snapshot or self._snapshot(job_id)
        job, job_title, routing = run.job, run.title, run.routing

        if job and not job.enabled:
            logger.info("Cron job %s is disabled, skipping execution", job_title)
            return

        if self._is_quiet_hours(job, job_title):
            return

        if job and (problem := job.delivery_mode_problem()):
            logger.warning("Cron job %s: %s; running as 'separate'", job_title, problem)
        logger.info("Cron job starting job=%s", job_title)
        t0 = time.monotonic()

        if await self._run_preflight(job_title, task_folder):
            self._manager.update_run_status(
                job_id,
                status="success:preflight",
                delivery_status="skipped",
            )
            await self._refresh_mtime()
            return

        if job and job.effective_delivery_mode() == "in_session":
            await self._run_in_session(job, instruction, task_folder, routing)
            return

        overrides = TaskOverrides(
            provider=job.provider if job else None,
            model=job.model if job else None,
            reasoning_effort=job.reasoning_effort if job else None,
            cli_parameters=job.cli_parameters if job else [],
        )

        routing_env = {"DUCTOR_TRANSPORT": routing[2]}
        if routing[0]:
            routing_env["DUCTOR_CHAT_ID"] = str(routing[0])
            if routing[1] is not None:
                routing_env["DUCTOR_TOPIC_ID"] = str(routing[1])
        else:
            routing_env["DUCTOR_RESULT_ORIGIN"] = "cron-broadcast"
            routing_env["DUCTOR_BROADCAST_REF"] = job_id

        result = await execute_in_task_folder(
            self,
            cron_tasks_dir=self._paths.cron_tasks_dir,
            task_folder=task_folder,
            instruction=instruction,
            overrides=overrides,
            dependency=job.dependency if job else None,
            task_id=job_id,
            task_label="Cron job",
            timeout_seconds=self._config.cli_timeout,
            routing_env=routing_env,
        )

        if result.status == "error:folder_missing":
            logger.error("Cron task folder missing: %s", task_folder)
            self._manager.update_run_status(job_id, status="error:folder_missing")
            return

        if result.execution is None:
            logger.error("CLI not found for cron job %s", job_id)
            delivered, delivery_error, failed_transports = await self._deliver_result(
                job_id,
                job_title,
                result.result_text,
                result.status,
                routing,
                **await self._injection(job, job_title, result.status, result.result_text),
            )
            self._manager.update_run_status(
                job_id,
                status=result.status,
                delivery_status="ok" if delivered else "failed",
                delivery_error=delivery_error,
                failed_transports=failed_transports,
                result_text=None if delivered else result.result_text,
            )
            return

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "Cron job completed job=%s status=%s duration_ms=%.0f stdout=%d result=%d",
            job_title,
            result.status,
            elapsed_ms,
            len(result.execution.stdout),
            len(result.result_text),
        )

        # Deliver result BEFORE writing run-status to disk.  The file
        # write can trigger the file-watcher which reschedules (and
        # cancels) running tasks.  Delivering first guarantees the
        # Telegram message is sent even if the task is cancelled during
        # the subsequent file I/O.
        (
            delivery_status,
            delivered,
            delivery_error,
            failed_transports,
        ) = await self._deliver_run_result(job, job_id, job_title, result, routing)

        # #160: execution status and delivery status are tracked separately;
        # on delivery failure the full result text is persisted for resend.
        self._manager.update_run_status(
            job_id,
            status=result.status,
            delivery_status=delivery_status,
            delivery_error=delivery_error,
            failed_transports=failed_transports,
            result_text=None if delivered else result.result_text,
        )
        # Refresh our mtime baseline so the file-watcher doesn't treat the
        # run-status write as a user-initiated change and trigger a full
        # reschedule of all other jobs.
        await self._refresh_mtime()

    async def _deliver_run_result(
        self,
        job: CronJob | None,
        job_id: str,
        job_title: str,
        result: TaskResult,
        routing: tuple[int, int | None, str],
    ) -> tuple[str, bool, str, list[str]]:
        """Deliver a run's result, or skip it silently; returns the delivery tracking values.

        ``silent_on_success`` mutes a successful run, except in ``inject`` mode, where the
        session must hear about the result.
        """
        injection = await self._injection(job, job_title, result.status, result.result_text)
        if job and job.silent_on_success and result.status == "success" and not injection:
            logger.info("Cron job %s succeeded silently (silent_on_success)", job_title)
            return "skipped", True, "", []
        delivered, delivery_error, failed_transports = await self._deliver_result(
            job_id,
            job_title,
            result.result_text,
            result.status,
            routing,
            **injection,
        )
        if not delivered:
            logger.warning(
                "Cron job %s executed (%s) but delivery failed: %s — result preserved",
                job_title,
                result.status,
                delivery_error,
            )
        return ("ok" if delivered else "failed"), delivered, delivery_error, failed_transports

    async def _injection(
        self, job: CronJob | None, job_title: str, status: str, result_text: str
    ) -> _InjectionArgs:
        """Delivery arguments of ``delivery_mode: inject`` (empty: deliver the result as before).

        The prompt carries the title, status and result; a result too long for it is kept in
        full in ``LAST_RESULT.md`` of the task folder.
        """
        if job is None or job.effective_delivery_mode() != "inject":
            return {}
        from ductor_bot.bus.adapters import CRON_RESULT_PROMPT_CHARS, cron_result_prompt

        result_file = ""
        if len(result_text) > CRON_RESULT_PROMPT_CHARS:
            path = self._paths.cron_tasks_dir / job.task_folder / "LAST_RESULT.md"
            try:
                await asyncio.to_thread(path.write_text, result_text, encoding="utf-8")
                result_file = str(path)
            except OSError:
                logger.warning("Cron job %s: could not write the full result file", job.id)
                result_file = "(not saved)"
        prompt = cron_result_prompt(
            job_title,
            job.id,
            status,
            result_text,
            ran_at=self._result_time(),
            result_file=result_file,
        )
        return {"delivery_mode": "inject", "prompt": prompt}

    async def _run_in_session(
        self,
        job: CronJob,
        instruction: str,
        task_folder: str,
        routing: tuple[int, int | None, str],
    ) -> None:
        """``delivery_mode: in_session``: the job's instruction runs as a turn of the target session.

        No separate run happens; the session does the work and its reply is delivered. The
        job's provider/model/effort do not apply (the session's own are used).
        """
        from ductor_bot.bus.adapters import cron_instruction_prompt
        from ductor_bot.cron.dependency_queue import get_dependency_queue

        folder = self._paths.cron_tasks_dir / task_folder
        if not await asyncio.to_thread(folder.is_dir):
            logger.error("Cron task folder missing: %s", task_folder)
            self._manager.update_run_status(job.id, status="error:folder_missing")
            return
        if (job.provider or job.model or job.reasoning_effort) and job.id not in self._warned:
            self._warned.add(job.id)
            logger.warning(
                "Cron job %s: provider/model/reasoning_effort are ignored with "
                "delivery_mode 'in_session' (the session's own are used)",
                job.id,
            )
        description_file = folder / "TASK_DESCRIPTION.md"
        try:
            description = await asyncio.to_thread(description_file.read_text, encoding="utf-8")
        except OSError:
            description = "(TASK_DESCRIPTION.md could not be read)"
        prompt = cron_instruction_prompt(
            job.title,
            job.id,
            instruction,
            started_at=self._result_time(),
            task_folder=str(folder),
            description_file=str(description_file),
            description=description,
        )
        async with get_dependency_queue().acquire(job.id, "Cron job", job.dependency):
            delivered, error, _failed = await self._deliver_result(
                job.id,
                job.title,
                f"Cron job '{job.title}' could not run in this session.",
                "error:injection",
                routing,
                delivery_mode="in_session",
                prompt=prompt,
            )
        self._manager.update_run_status(
            job.id,
            status="success" if delivered else "error:injection",
            delivery_status="ok" if delivered else "failed",
            delivery_error=error,
        )
        await self._refresh_mtime()

    async def _run_preflight(self, job_title: str, task_folder: str) -> bool:
        """Return true when an enabled task-local preflight safely skips the agent."""
        preflight = self._config.cron_preflight
        if not preflight.enabled:
            return False
        folder = self._paths.cron_tasks_dir / task_folder
        script = folder / "scripts" / "preflight.py"
        if not script.is_file():
            return False

        env = os.environ.copy()
        env["DUCTOR_HOME"] = str(self._paths.ductor_home)
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script),
                cwd=str(folder),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=sys.platform != "win32",
            )
        except OSError:
            # Fail open: a preflight that cannot even spawn (EMFILE, ENOMEM,
            # permissions) must never suppress the scheduled agent run.
            logger.warning("Cron preflight spawn failed job=%s; running agent", job_title)
            return False
        try:
            async with asyncio.timeout(preflight.timeout_seconds):
                stdout, stderr = await proc.communicate()
        except TimeoutError:
            _kill_process_group(proc)
            try:
                async with asyncio.timeout(5):
                    await proc.communicate()
            except TimeoutError:
                # A grandchild kept the pipes open past SIGKILL; abandon the
                # reap rather than hanging this job's coroutine forever.
                logger.warning("Cron preflight unreapable after kill job=%s", job_title)
            logger.warning("Cron preflight timed out job=%s", job_title)
            return False

        output = stdout.decode(errors="replace")
        error = stderr.decode(errors="replace").strip()
        last_line = next(
            (line.strip() for line in reversed(output.splitlines()) if line.strip()),
            "",
        )
        if proc.returncode == 0 and last_line == preflight.skip_marker:
            logger.info("Cron preflight skipped agent job=%s task=%s", job_title, task_folder)
            return True
        if proc.returncode not in (0, 2) or error:
            logger.warning(
                "Cron preflight failed open job=%s returncode=%s stdout=%d stderr=%d",
                job_title,
                proc.returncode,
                len(stdout),
                len(stderr),
            )
        return False

    def _is_quiet_hours(self, job: CronJob | None, job_title: str) -> bool:
        """Return True when the job must be skipped due to quiet-hour settings."""
        job_start = job.quiet_start if job else None
        job_end = job.quiet_end if job else None

        # Cron jobs only respect quiet hours explicitly set on the job itself.
        # Do NOT fall back to heartbeat quiet hours.
        if job_start is None and job_end is None:
            return False

        is_quiet, now_hour, tz = check_quiet_hour(
            quiet_start=job_start,
            quiet_end=job_end,
            user_timezone=self._config.user_timezone,
            global_quiet_start=0,
            global_quiet_end=0,
        )
        if not is_quiet:
            return False

        logger.debug(
            "Cron job skipped: quiet hours (%d:00 %s) job=%s",
            now_hour,
            tz.key,
            job_title,
        )
        return True

    async def _refresh_mtime(self) -> None:
        """Take our own write as the baseline of the file watcher, unless it merged a change.

        A write of the manager also saves job definitions another writer (a tool) changed since
        the last load. The baseline is then left behind, so the watcher still sees the file as
        changed and reloads and reschedules.
        """
        if self._manager.take_external_change():
            logger.info(
                "cron_jobs.json holds definitions not loaded yet; leaving it to the watcher"
            )
            return
        await self._watcher.update_mtime()

    async def _update_mtime(self) -> None:
        """Cache the current mtime of the jobs file."""
        await self._refresh_mtime()
