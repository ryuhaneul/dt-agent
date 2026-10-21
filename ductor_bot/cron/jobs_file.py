"""Locked read-modify-write of ``cron_jobs.json``, shared by the cron manager and the tools.

Every writer takes the file's lock, reads the file again inside it, changes only what it owns
and saves atomically, so the tools (job definitions) and the manager (run status) cannot
overwrite each other's fields. The lock is held for the short synchronous section only.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from ductor_bot.infra.atomic_io import atomic_text_save
from ductor_bot.infra.file_lock import exclusive_file_lock

T = TypeVar("T")

STATE_FIELDS = (
    "last_run_at",
    "last_run_status",
    "last_delivery_status",
    "last_delivery_error",
    "failed_transports",
    "last_result_text",
    "delivery_retry_attempts",
    "next_delivery_retry_at",
)
"""What the manager writes after a run; the tools leave these as the file has them."""


def read_jobs_data(path: Path) -> dict[str, Any]:
    """The file as ``{"jobs": [...], ...}``; a missing file is empty, a bare list is wrapped.

    Raises ``ValueError`` for a file that cannot be read as cron jobs (it is not overwritten).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"jobs": []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"Corrupt {path.name} -- cannot parse"
        raise ValueError(msg) from exc
    if isinstance(data, list):
        data = {"jobs": data}
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        msg = f"Corrupt {path.name} -- cannot parse"
        raise ValueError(msg)  # noqa: TRY004
    return data


def write_jobs_data(path: Path, data: dict[str, Any]) -> None:
    """Replace the file atomically (temp file + rename), keeping its permissions."""
    try:
        mode: int | None = path.stat().st_mode & 0o777
    except OSError:
        mode = None
    atomic_text_save(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    if mode is not None:
        with contextlib.suppress(OSError):
            path.chmod(mode)


def update_jobs_file(path: Path, mutate: Callable[[dict[str, Any]], T]) -> T:
    """Lock, read again, apply *mutate* to the data, save atomically, unlock."""
    with exclusive_file_lock(path):
        data = read_jobs_data(path)
        result = mutate(data)
        write_jobs_data(path, data)
        return result


def save_definitions(
    path: Path,
    edited: dict[str, Any],
    *,
    upsert: Sequence[tuple[str, str]] = (),
    remove: Sequence[str] = (),
) -> None:
    """Write the tool's changes to some jobs and keep everything else as the file has it.

    *upsert* lists ``(id, previous_id)`` of jobs the tool added or edited (``previous_id`` differs
    from ``id`` for a renamed job); their definitions come from *edited*, their run status from
    the file's current job. *remove* lists ids the tool removed.
    """
    edited_by_id = {j.get("id"): j for j in edited.get("jobs", [])}

    def mutate(data: dict[str, Any]) -> None:
        jobs: list[dict[str, Any]] = data["jobs"]
        for job_id, previous_id in upsert:
            merged = dict(edited_by_id[job_id])
            current = next((j for j in jobs if j.get("id") == previous_id), None)
            if current is None:
                jobs.append(merged)
                continue
            for field in STATE_FIELDS:
                if field in current:
                    merged[field] = current[field]
                else:
                    merged.pop(field, None)
            jobs[jobs.index(current)] = merged
        gone = set(remove)
        jobs[:] = [j for j in jobs if j.get("id") not in gone]

    update_jobs_file(path, mutate)
