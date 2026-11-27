"""Shared helpers for cron tool scripts."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ductor_bot.cron import definitions
from ductor_bot.cron.definitions import (
    describe_job,
    sanitize_name,
    schedule_problem,
)
from ductor_bot.cron.jobs_file import read_jobs_data, save_definitions
from ductor_bot.infra.file_lock import LockTimeoutError, exclusive_file_lock

from ductor_bot._home_defaults.workspace.tools._tool_shared import (
    parse_autocompact_option,  # noqa: F401 -- shared CLI option parser
    normalize_autocompact_records,
    parse_tool_args,  # noqa: F401 -- re-exported for the tool scripts
    available_ids,
    find_by_id,
    load_collection_or_default,
    load_collection_strict,
)
from ductor_bot.home import subprocess_settings_home
from ductor_bot.multiagent.registry import AgentRegistry
from ductor_bot.workspace.paths import resolve_paths

DUCTOR_HOME = Path(os.environ.get("DUCTOR_HOME", "~/.ductor")).expanduser()
SUB_AGENT_NAME = (
    os.environ["DUCTOR_AGENT_NAME"] if os.environ.get("DUCTOR_AGENT_ROLE") == "sub" else None
)
MAIN_HOME = subprocess_settings_home(DUCTOR_HOME)
CONFIG_PATH = resolve_paths(MAIN_HOME).config_path
JOBS_PATH = DUCTOR_HOME / "cron_jobs.json"
CRON_TASKS_DIR = DUCTOR_HOME / "workspace" / "cron_tasks"

DELIVERY_MODES = definitions.DELIVERY_MODES
_RULE_FILENAMES = definitions.RULE_FILENAMES


def read_user_timezone() -> str:
    """Read the main timezone with the sub-agent's override, if set."""
    if SUB_AGENT_NAME is not None:
        for sub in AgentRegistry(MAIN_HOME / "agents.json").load():
            if sub.name == SUB_AGENT_NAME and sub.user_timezone is not None:
                return sub.user_timezone.strip()

    if not CONFIG_PATH.exists():
        return ""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return str(data.get("user_timezone", "")).strip()
    except (json.JSONDecodeError, OSError):
        return ""


def detect_rule_filenames() -> list[str]:
    return definitions.detect_rule_filenames(CRON_TASKS_DIR)


def render_cron_task_claude_md(name: str) -> str:
    return definitions.render_cron_task_claude_md(name, DUCTOR_HOME)


def load_jobs_or_default(jobs_path: Path) -> dict[str, Any]:
    """Load cron jobs JSON or return an empty payload if missing/corrupt."""
    return normalize_autocompact_records(load_collection_or_default(jobs_path, "jobs"), "jobs")


def load_jobs_strict(jobs_path: Path) -> dict[str, Any]:
    """Load cron jobs JSON and raise on malformed structure."""
    return normalize_autocompact_records(load_collection_strict(jobs_path, "jobs"), "jobs")


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
    return definitions.safe_task_dir(task_folder, CRON_TASKS_DIR)


@contextmanager
def locked_jobs_file(jobs_path: Path) -> Iterator[dict[str, Any]]:
    """Read the registry under the lock and retain it through the caller's writes."""
    try:
        with exclusive_file_lock(jobs_path):
            try:
                data = read_jobs_data(jobs_path)
            except (ValueError, TypeError):
                print(json.dumps({"error": "Corrupt cron_jobs.json -- cannot parse"}))
                sys.exit(1)
            yield data
    except LockTimeoutError:
        print(json.dumps({"error": "cron_jobs.json is locked by another writer; try again"}))
        sys.exit(1)


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
