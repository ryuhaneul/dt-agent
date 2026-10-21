"""Shared helpers for cron tool scripts."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from cronsim import CronSim, CronSimError

from ductor_bot.cron.jobs_file import save_definitions
from ductor_bot.infra.file_lock import LockTimeoutError

from ductor_bot._home_defaults.workspace.tools._tool_shared import (
    parse_tool_args,  # noqa: F401 -- re-exported for the tool scripts
    available_ids,
    find_by_id,
    load_collection_or_default,
    load_collection_strict,
    sanitize_name,
)

DUCTOR_HOME = Path(os.environ.get("DUCTOR_HOME", "~/.ductor")).expanduser()
CONFIG_PATH = DUCTOR_HOME / "config" / "config.json"
JOBS_PATH = DUCTOR_HOME / "cron_jobs.json"
CRON_TASKS_DIR = DUCTOR_HOME / "workspace" / "cron_tasks"

# Where a job's result goes (CronJob.delivery_mode); the first one is the default.
DELIVERY_MODES = ("separate", "inject", "in_session")

# Provider rule files — only create for authenticated providers.
_RULE_FILENAMES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")


_MODE_MEANING = {
    "separate": "a separate run; the result arrives as a plain message (the topic's session "
    "does not see it)",
    "inject": "a separate run; its result is injected into the topic's session, which replies "
    "(one extra model turn of that session per run)",
    "in_session": "no separate run: the instruction runs as a turn of the topic's session, with "
    "that session's own model (one model turn of that session per run)",
}


def describe_job(job: dict[str, Any], timezone: str) -> list[dict[str, str]]:
    """Per-setting value and one-line meaning, for the agent to tell the user."""
    mode = str(job.get("delivery_mode", "separate"))
    chat = job.get("chat_id", 0)
    topic = job.get("topic_id")
    if chat:
        target = f"chat {chat}" + (f", topic {topic}" if topic is not None else ", main thread")
        target += f" ({job.get('transport', 'tg')})"
    else:
        target = "broadcast to all users"
    model_parts = [
        f"{label}={job[key]}"
        for key, label in (("provider", "provider"), ("model", "model"), ("reasoning_effort", "effort"))
        if job.get(key)
    ]
    quiet = (
        f"{job['quiet_start']}:00-{job['quiet_end']}:00"
        if job.get("quiet_start") is not None and job.get("quiet_end") is not None
        else "none (this job has no quiet hours; the heartbeat quiet hours do not apply to cron)"
    )
    return [
        {
            "setting": "schedule",
            "value": f"{job.get('schedule')} ({timezone or 'UTC, no user_timezone set'})",
            "meaning": "when the job runs; the hours are read in this timezone",
        },
        {
            "setting": "target",
            "value": target,
            "meaning": "where the result goes (the chat/topic the job was created in)",
        },
        {
            "setting": "delivery_mode",
            "value": mode,
            "meaning": _MODE_MEANING.get(mode, "unknown mode (runs as separate)"),
        },
        {
            "setting": "model",
            "value": ", ".join(model_parts) or "global config defaults",
            "meaning": (
                "ignored in in_session mode: the topic session's own model is used"
                if mode == "in_session"
                else "which CLI/model runs the job"
            ),
        },
        {
            "setting": "quiet_hours",
            "value": quiet,
            "meaning": "the job does not run during these hours",
        },
        {
            "setting": "silent_on_success",
            "value": str(bool(job.get("silent_on_success", False))).lower(),
            "meaning": (
                "no result message when a run succeeds (errors still arrive); has no effect in "
                "inject and in_session mode"
            ),
        },
        {
            "setting": "dependency",
            "value": str(job.get("dependency") or "none"),
            "meaning": "jobs with the same dependency run one at a time",
        },
    ]


def read_user_timezone() -> str:
    """Read user_timezone from config.json. Returns empty string if not set."""
    if not CONFIG_PATH.exists():
        return ""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return str(data.get("user_timezone", "")).strip()
    except (json.JSONDecodeError, OSError):
        return ""


def detect_rule_filenames() -> list[str]:
    """Determine which rule files to create based on parent cron_tasks/ contents.

    Checks which provider rule files (CLAUDE.md, AGENTS.md, GEMINI.md) exist
    in the ``cron_tasks/`` root.  These are deployed by the RulesSelector during
    workspace init based on CLI authentication status.

    Falls back to ``["CLAUDE.md"]`` when no rule files are found.
    """
    found = [name for name in _RULE_FILENAMES if (CRON_TASKS_DIR / name).is_file()]
    return found or ["CLAUDE.md"]


def render_cron_task_claude_md(name: str) -> str:
    """Render fixed CLAUDE.md/AGENTS.md content for a cron task folder."""
    return f"""\
# Your Mission

You are an **automated agent**. You run on a schedule with NO human interaction.
Complete your task autonomously and update memory when done.

## Workflow

1. **Read** `{name}_MEMORY.md` first -- it contains context from previous runs.
2. **Read** the whole `TASK_DESCRIPTION.md`!
3. **Follow the assignment** in `TASK_DESCRIPTION.md`!
4. Perform the task conscientiously!
5. **Update** `{name}_MEMORY.md` with the current date/time and what you did.

## Rules

- Stay focused on this task only. Do not deviate.
- Do not modify files outside this task folder unless explicitly instructed by the user or the assigned TASK_DESCRIPTION.md.
- Check whether all scripts are already present in the script folder! (IF they are needed!)
- Use `.venv` for Python dependencies: `source .venv/bin/activate`

## Important

You provide the final answer after the task is completed in a pleasant, concise,
and well-formatted manner.
"""


def load_jobs_or_default(jobs_path: Path) -> dict[str, Any]:
    """Load cron jobs JSON or return an empty payload if missing/corrupt."""
    return load_collection_or_default(jobs_path, "jobs")


def load_jobs_strict(jobs_path: Path) -> dict[str, Any]:
    """Load cron jobs JSON and raise on malformed structure."""
    return load_collection_strict(jobs_path, "jobs")


def find_job_by_id_or_task_folder(jobs: list[dict[str, Any]], job_id: str) -> dict[str, Any] | None:
    """Find a job by exact id, then by task_folder."""
    exact = find_by_id(jobs, job_id)
    if exact:
        return exact
    return next((j for j in jobs if j.get("task_folder") == job_id), None)


def available_job_ids(jobs: list[dict[str, Any]]) -> list[str]:
    """Return all job IDs for diagnostics."""
    return available_ids(jobs)


def safe_task_dir(task_folder: str) -> Path:
    """Resolve and validate a cron task folder path under CRON_TASKS_DIR."""
    folder_path = (CRON_TASKS_DIR / task_folder).resolve()
    if not folder_path.is_relative_to(CRON_TASKS_DIR.resolve()):
        msg = f"Path traversal blocked: {task_folder!r} resolves outside cron_tasks"
        raise ValueError(msg)
    return folder_path


def save_jobs(
    jobs_path: Path,
    data: dict[str, Any],
    *,
    upsert: Sequence[tuple[str, str]] = (),
    remove: Sequence[str] = (),
) -> None:
    """Save the tool's changes to cron jobs under the file lock, keeping the rest as the file has it.

    *upsert* lists ``(id, previous_id)`` of the jobs the tool added or edited and *remove* the ids
    it removed; their run status stays as the file holds it now. Exits with an error when the
    file is locked by another writer for too long or cannot be read.
    """
    try:
        save_definitions(jobs_path, data, upsert=upsert, remove=remove)
    except LockTimeoutError:
        print(json.dumps({"error": "cron_jobs.json is locked by another writer; try again"}))
        sys.exit(1)
    except (ValueError, TypeError):
        print(json.dumps({"error": "Corrupt cron_jobs.json -- cannot parse"}))
        sys.exit(1)


def schedule_problem(schedule: str) -> str:
    """Why *schedule* is not a cron expression the scheduler accepts ("" when it is)."""
    try:
        next(CronSim(schedule, datetime(2000, 1, 1)))
    except CronSimError as exc:
        return f"Invalid cron expression {schedule!r}: {exc}"
    except StopIteration:
        return f"Cron expression {schedule!r} never matches"
    return ""
