"""Persistent count of repeated asynchronous delegation failures.

This is a prompt-level guard: changing the wording of a request creates a new
series. It does not enforce a hard limit on re-delegation. Counts remain in
memory if saving fails; unsaved updates are lost if the process restarts (an
ERROR log records the failed save).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ductor_bot.infra.atomic_io import atomic_text_save

if TYPE_CHECKING:
    from ductor_bot.multiagent.bus import AsyncInterAgentResult

logger = logging.getLogger(__name__)

_TTL_SECONDS = 24 * 60 * 60


def failure_key(target: str, result: AsyncInterAgentResult) -> str:
    """Identify the same request across recipients, but not across result origins."""
    normalized_message = " ".join(result.original_message.split()).lower()
    source = "\0".join(
        (
            target,
            str(result.chat_id),
            str(result.topic_id),
            result.result_origin,
            normalized_message,
        )
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class FailureLedger:
    """Serialize failure-count updates to the main agent's private ledger."""

    def __init__(self, main_home: Path) -> None:
        self.path = main_home / "interagent" / "failures.json"
        self._lock = asyncio.Lock()
        self._records: dict[str, dict[str, Any]] | None = None
        self._dirty = False

    async def record_failure(self, target: str, result: AsyncInterAgentResult) -> int:
        """Count a failed task once and return its request series' 24-hour total."""
        async with self._lock:
            now = time.time()
            records = self._get_records(now)
            key = failure_key(target, result)
            entry = records.get(key)
            if entry is not None and result.task_id in entry["task_ids"]:
                self._persist()
                return cast("int", entry["count"])
            if entry is None:
                entry = {"count": 0, "first_at": now, "last_at": now, "task_ids": []}
                records[key] = entry
            entry["count"] += 1
            entry["last_at"] = now
            entry["task_ids"].append(result.task_id)
            self._dirty = True
            self._persist()
            return cast("int", entry["count"])

    async def clear_success(self, target: str, result: AsyncInterAgentResult) -> None:
        """Reset this request series when a successful result arrives."""
        async with self._lock:
            records = self._get_records(time.time())
            if records.pop(failure_key(target, result), None) is not None:
                self._dirty = True
            self._persist()

    def _get_records(self, now: float) -> dict[str, dict[str, Any]]:
        if self._records is None:
            self._records, self._dirty = self._read(now)
        else:
            cutoff = now - _TTL_SECONDS
            expired = [key for key, entry in self._records.items() if entry["last_at"] < cutoff]
            for key in expired:
                del self._records[key]
            if expired:
                self._dirty = True
        return self._records

    def _persist(self) -> None:
        if self._dirty and self._records is not None and self._save(self._records):
            self._dirty = False

    def _read(self, now: float) -> tuple[dict[str, dict[str, Any]], bool]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}, False
        except (OSError, UnicodeError):
            logger.warning("Cannot read async failure ledger", exc_info=True)
            return {}, True
        try:
            loaded = json.loads(raw)
        except (ValueError, UnicodeError):
            logger.warning("Invalid async failure ledger; starting a new count")
            return {}, True
        if not isinstance(loaded, dict):
            logger.warning("Invalid async failure ledger; starting a new count")
            return {}, True
        cutoff = now - _TTL_SECONDS
        records: dict[str, dict[str, Any]] = {}
        for key, entry in loaded.items():
            if (
                isinstance(key, str)
                and isinstance(entry, dict)
                and isinstance(entry.get("count"), int)
                and entry["count"] > 0
                and isinstance(entry.get("first_at"), (int, float))
                and isinstance(entry.get("last_at"), (int, float))
                and isinstance(entry.get("task_ids"), list)
                and all(isinstance(task_id, str) for task_id in entry["task_ids"])
                and entry["last_at"] >= cutoff
            ):
                records[key] = entry  # noqa: PERF403 - validate persisted records before reuse
        return records, len(records) != len(loaded)

    def _save(self, records: dict[str, dict[str, Any]]) -> bool:
        previous_umask = os.umask(0o077)
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.path.parent.chmod(0o700)
            atomic_text_save(self.path, json.dumps(records, separators=(",", ":")))
            self.path.chmod(0o600)
            return True  # noqa: TRY300 - keep permission check in the protected save
        except OSError as exc:
            logger.error(  # noqa: TRY400 - avoid logging exception text
                "Cannot save async failure ledger %s (%s)", self.path, type(exc).__name__
            )
            return False
        finally:
            os.umask(previous_umask)
