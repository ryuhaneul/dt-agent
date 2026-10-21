"""Cron job management: JSON-based persistence.

Jobs are stored in a JSON file. The CronObserver watches the file
for changes and schedules jobs in-process.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ductor_bot.cron.jobs_file import STATE_FIELDS, update_jobs_file
from ductor_bot.infra.file_lock import LockTimeoutError
from ductor_bot.infra.json_store import load_json

logger = logging.getLogger(__name__)

# Where a job's result goes: "separate" = a separate run, the result as a message (default);
# "inject" = the separate run's result is injected into the target chat/topic's session;
# "in_session" = no separate run, the job's instruction is injected into that session.
DELIVERY_MODES = ("separate", "inject", "in_session")


@dataclass
class CronJob:
    """A scheduled job definition."""

    id: str
    title: str
    description: str
    schedule: str
    task_folder: str
    agent_instruction: str
    enabled: bool = True
    timezone: str = ""
    created_at: str = ""
    last_run_at: str | None = None
    last_run_status: str | None = None

    # Delivery tracking (#160): execution success and delivery success are
    # separate. On delivery failure the full result text is kept for resend.
    last_delivery_status: str | None = None  # "ok" | "failed" | "skipped" | None
    last_delivery_error: str = ""
    failed_transports: list[str] = field(default_factory=list)
    last_result_text: str | None = None
    delivery_retry_attempts: int = 0
    next_delivery_retry_at: str | None = None

    # Per-task execution overrides
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    cli_parameters: list[str] = field(default_factory=list)

    # Quiet hours (None = use global config defaults)
    quiet_start: int | None = None
    quiet_end: int | None = None

    # Optional dependency for sequential execution
    dependency: str | None = None

    # Routing: deliver results to the chat/topic where the job was created
    chat_id: int = 0
    topic_id: int | None = None
    transport: str = "tg"

    # Mute delivery on the success path; errors are still delivered (#133)
    silent_on_success: bool = False

    # How the result reaches the chat (see DELIVERY_MODES); anything else reads as "separate".
    delivery_mode: str = "separate"

    def delivery_mode_problem(self) -> str:
        """Why the stored mode cannot be used ("" when it can)."""
        if self.delivery_mode not in DELIVERY_MODES:
            return f"unknown delivery_mode {self.delivery_mode!r}"
        if self.delivery_mode != "separate" and not self.chat_id:
            return f"delivery_mode {self.delivery_mode!r} needs a chat (a broadcast job has none)"
        return ""

    def effective_delivery_mode(self) -> str:
        """The mode that runs: an unusable stored mode falls back to "separate"."""
        return "separate" if self.delivery_mode_problem() else self.delivery_mode

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "schedule": self.schedule,
            "task_folder": self.task_folder,
            "agent_instruction": self.agent_instruction,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_run_at": self.last_run_at,
            "last_run_status": self.last_run_status,
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "cli_parameters": self.cli_parameters,
            "quiet_start": self.quiet_start,
            "quiet_end": self.quiet_end,
            "dependency": self.dependency,
            "chat_id": self.chat_id,
            "topic_id": self.topic_id,
            "transport": self.transport,
            "silent_on_success": self.silent_on_success,
        }
        if self.timezone:
            result["timezone"] = self.timezone
        if self.delivery_mode != "separate":
            result["delivery_mode"] = self.delivery_mode
        if self.last_delivery_status is not None:
            result["last_delivery_status"] = self.last_delivery_status
        if self.last_delivery_error:
            result["last_delivery_error"] = self.last_delivery_error
        if self.failed_transports:
            result["failed_transports"] = self.failed_transports
        if self.last_result_text is not None:
            result["last_result_text"] = self.last_result_text
        if self.delivery_retry_attempts:
            result["delivery_retry_attempts"] = self.delivery_retry_attempts
        if self.next_delivery_retry_at is not None:
            result["next_delivery_retry_at"] = self.next_delivery_retry_at
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CronJob:
        return cls(
            id=data["id"],
            title=data["title"],
            description=data.get("description", ""),
            schedule=data["schedule"],
            task_folder=data["task_folder"],
            agent_instruction=data["agent_instruction"],
            enabled=data.get("enabled", True),
            timezone=data.get("timezone", ""),
            created_at=data.get("created_at", ""),
            last_run_at=data.get("last_run_at"),
            last_run_status=data.get("last_run_status"),
            last_delivery_status=data.get("last_delivery_status"),
            last_delivery_error=data.get("last_delivery_error", ""),
            failed_transports=data.get("failed_transports", []),
            last_result_text=data.get("last_result_text"),
            delivery_retry_attempts=data.get("delivery_retry_attempts", 0),
            next_delivery_retry_at=data.get("next_delivery_retry_at"),
            provider=data.get("provider"),
            model=data.get("model"),
            reasoning_effort=data.get("reasoning_effort"),
            cli_parameters=data.get("cli_parameters", []),
            quiet_start=data.get("quiet_start"),
            quiet_end=data.get("quiet_end"),
            dependency=data.get("dependency"),
            chat_id=data.get("chat_id", 0),
            topic_id=data.get("topic_id"),
            transport=data.get("transport", "tg"),
            silent_on_success=data.get("silent_on_success", False),
            delivery_mode=str(data.get("delivery_mode") or "separate"),
        )


class CronManager:
    """Manages cron jobs: JSON persistence.

    The CronObserver watches the JSON file for changes and handles
    scheduling. This class is responsible for data only.

    Every write is a locked read-modify-write of the file (see ``cron.jobs_file``): the file is
    read again under the lock and only the fields this manager owns are changed, so a job
    definition saved by a tool in the meantime is kept. ``self._jobs`` is the file as of the
    last write or load.
    """

    def __init__(self, *, jobs_path: Path) -> None:
        self._jobs_path = jobs_path
        self._jobs: list[CronJob] = self._load()
        # Definitions that another writer changed and this manager has not loaded and scheduled.
        self._external_change = False
        # Run-status writes that found the lock taken; applied by the next write.
        self._deferred: list[Callable[[list[dict[str, Any]]], object]] = []

    # -- CRUD --

    def add_job(self, job: CronJob) -> None:
        """Add a new job. Raises ValueError if ID already exists."""

        def mutate(jobs: list[dict[str, Any]]) -> None:
            if any(j.get("id") == job.id for j in jobs):
                msg = f"Job '{job.id}' already exists"
                raise ValueError(msg)
            jobs.append(job.to_dict())

        self._write(mutate)
        self._jobs = [job if j.id == job.id else j for j in self._jobs]  # keep the caller's object
        logger.info("Cron job added: %s (%s)", job.id, job.schedule)

    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID. Returns False if not found."""

        def mutate(jobs: list[dict[str, Any]]) -> bool:
            before = len(jobs)
            jobs[:] = [j for j in jobs if j.get("id") != job_id]
            return len(jobs) != before

        if not self._write(mutate):
            return False
        logger.info("Cron job removed: %s", job_id)
        return True

    def list_jobs(self) -> list[CronJob]:
        """Return all jobs."""
        return list(self._jobs)

    def get_job(self, job_id: str) -> CronJob | None:
        """Return a job by ID, or None."""
        return next((j for j in self._jobs if j.id == job_id), None)

    def set_enabled(self, job_id: str, *, enabled: bool) -> bool:
        """Set ``enabled`` for one job. Returns True if state changed."""

        def mutate(jobs: list[dict[str, Any]]) -> bool:
            raw = _raw_job(jobs, job_id)
            if raw is None or raw.get("enabled", True) == enabled:
                return False
            raw["enabled"] = enabled
            return True

        if not self._write(mutate):
            return False
        logger.info("Cron job %s: enabled=%s", job_id, enabled)
        return True

    def set_all_enabled(self, *, enabled: bool) -> int:
        """Set ``enabled`` for all jobs. Returns number of changed jobs."""

        def mutate(jobs: list[dict[str, Any]]) -> int:
            changed = 0
            for raw in jobs:
                if raw.get("enabled", True) != enabled:
                    raw["enabled"] = enabled
                    changed += 1
            return changed

        changed = int(self._write(mutate))
        if changed:
            logger.info("Cron jobs bulk update: enabled=%s changed=%d", enabled, changed)
        return changed

    def update_run_status(  # noqa: PLR0913
        self,
        job_id: str,
        *,
        status: str,
        delivery_status: str | None = None,
        delivery_error: str = "",
        failed_transports: list[str] | None = None,
        result_text: str | None = None,
    ) -> None:
        """Update last run and delivery tracking for a job (#160).

        *result_text* is only passed on delivery failure so the original
        output can be resent without re-running the job.
        """
        ran_at = datetime.now(UTC).isoformat()

        def mutate(jobs: list[dict[str, Any]]) -> bool:
            raw = _raw_job(jobs, job_id)
            if raw is None:
                return False
            job = CronJob.from_dict(raw)
            job.last_run_at = ran_at
            job.last_run_status = status
            job.last_delivery_status = delivery_status
            job.last_delivery_error = delivery_error
            job.failed_transports = list(failed_transports or [])
            job.last_result_text = result_text
            job.delivery_retry_attempts = 0
            job.next_delivery_retry_at = None
            _store_state(raw, job)
            return True

        if self._write_state(mutate):
            logger.info("Cron job %s status=%s recorded", job_id, status)

    def update_delivery_retry(  # noqa: PLR0913
        self,
        job_id: str,
        *,
        delivered: bool,
        delivery_error: str = "",
        failed_transports: list[str] | None = None,
        next_attempt_at: str | None = None,
        expected_text: str | None = None,
    ) -> None:
        """Persist one delivery retry without changing the job execution status.

        When *expected_text* is given, a successful retry only clears the
        preserved result if it still matches (compared with what the file holds now) — a newer
        failed result that landed while the retry was in flight is kept for the next sweep.
        """

        def mutate(jobs: list[dict[str, Any]]) -> bool:
            raw = _raw_job(jobs, job_id)
            if raw is None:
                return False
            job = CronJob.from_dict(raw)
            if delivered:
                if expected_text is not None and job.last_result_text != expected_text:
                    return False
                job.last_delivery_status = "ok"
                job.last_delivery_error = ""
                job.failed_transports = []
                job.last_result_text = None
                job.delivery_retry_attempts = 0
                job.next_delivery_retry_at = None
            else:
                job.last_delivery_status = "failed"
                job.last_delivery_error = delivery_error
                job.failed_transports = list(failed_transports or [])
                job.delivery_retry_attempts += 1
                job.next_delivery_retry_at = next_attempt_at
            _store_state(raw, job)
            return True

        self._write_state(mutate)

    def reload(self) -> None:
        """Re-read jobs from disk (called by CronObserver on file change)."""
        self._jobs = self._load()
        self._external_change = False

    def take_external_change(self) -> bool:
        """Whether a write merged in job definitions this manager had not loaded yet.

        The observer must then reload and reschedule instead of treating the write as its own.
        """
        changed, self._external_change = self._external_change, False
        return changed

    # -- Persistence --

    def _load(self) -> list[CronJob]:
        """Load jobs from JSON file."""
        data = load_json(self._jobs_path)
        if data is None:
            return []
        try:
            # Support both {"jobs": [...]} and bare [...] formats
            raw = data.get("jobs", []) if isinstance(data, dict) else data
            jobs = self._parse(raw)
        except (KeyError, TypeError):
            logger.warning("Corrupt cron jobs file: %s", self._jobs_path)
            return []
        for j in jobs:
            logger.debug("Job loaded id=%s title=%s enabled=%s", j.id, j.title, j.enabled)
            if problem := j.delivery_mode_problem():
                logger.warning("Cron job %s: %s; it runs as 'separate'", j.id, problem)
        return jobs

    @staticmethod
    def _parse(raw_jobs: list[dict[str, Any]]) -> list[CronJob]:
        return [CronJob.from_dict(j) for j in raw_jobs]

    def _adopt(self, parsed: list[CronJob]) -> None:
        """Make *parsed* the cache, keeping the job objects callers already hold up to date."""
        known = {job.id: job for job in self._jobs}
        adopted: list[CronJob] = []
        for new in parsed:
            old = known.get(new.id)
            if old is None:
                adopted.append(new)
                continue
            for item in fields(CronJob):
                setattr(old, item.name, getattr(new, item.name))
            adopted.append(old)
        self._jobs = adopted

    def _write(self, mutate: Callable[[list[dict[str, Any]]], Any]) -> Any:
        """Locked read-modify-write; the cache becomes the file as saved.

        Raises ``LockTimeoutError`` when the lock stays taken (5 s) and ``ValueError`` for a file
        that cannot be read (it is not overwritten).
        """

        def run(data: dict[str, Any]) -> Any:
            jobs: list[dict[str, Any]] = data["jobs"]
            if _definitions(jobs) != _definitions([j.to_dict() for j in self._jobs]):
                self._external_change = True  # the file holds definitions we have not loaded
            for patch in self._deferred:
                patch(jobs)
            result = mutate(jobs)
            self._adopt(self._parse(jobs))
            return result

        result = update_jobs_file(self._jobs_path, run)
        self._deferred.clear()
        return result

    def _write_state(self, mutate: Callable[[list[dict[str, Any]]], bool]) -> bool:
        """A run-status write: if the lock stays taken it is kept for the next write."""
        try:
            return bool(self._write(mutate))
        except LockTimeoutError:
            logger.warning("cron_jobs.json is locked; the status is written with the next save")
            self._deferred.append(mutate)
            raw = [j.to_dict() for j in self._jobs]
            mutate(raw)  # reads of the cache already show it
            self._adopt(self._parse(raw))
            return False


def _raw_job(jobs: list[dict[str, Any]], job_id: str) -> dict[str, Any] | None:
    return next((j for j in jobs if j.get("id") == job_id), None)


def _store_state(raw: dict[str, Any], job: CronJob) -> None:
    """Copy the run-status fields of *job* into the file's job; its other fields stay."""
    state = job.to_dict()
    for name in STATE_FIELDS:
        if name in state:
            raw[name] = state[name]
        else:
            raw.pop(name, None)


def _definitions(jobs: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """The jobs without their run status, in one normal form (None: not readable)."""
    try:
        return [
            {k: v for k, v in CronJob.from_dict(j).to_dict().items() if k not in STATE_FIELDS}
            for j in jobs
        ]
    except (KeyError, TypeError, AttributeError):
        return None
