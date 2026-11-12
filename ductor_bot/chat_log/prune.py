"""Age-based history, terminal delivery and web edit backup retention."""

from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ductor_bot.chat_log.history import HistoryStore
from ductor_bot.chat_log.outbox import Outbox
from ductor_bot.infra.atomic_io import atomic_bytes_save, atomic_text_save, complete_write
from ductor_bot.infra.file_lock import exclusive_file_lock

logger = logging.getLogger(__name__)
_BACKUP = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2})/"
    r"(?:PERSONA|BEHAVIOR|MAINMEMORY|SHAREDMEMORY)\.md\.([0-9]{6})(?:\.[1-9][0-9]*)?"
)


@dataclass
class PruneResult:
    entries: int = 0
    history_files: int = 0
    outbox_items: int = 0
    backups: int = 0
    bytes: int = 0
    failures: int = 0


def _older(value: object, cutoff: datetime) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed < cutoff


def _backup_created(path: Path) -> datetime | None:
    match = _BACKUP.fullmatch(f"{path.parent.name}/{path.name}")
    if match is None:
        return None
    try:
        created = datetime.strptime(f"{match[1]} {match[2]}", "%Y-%m-%d %H%M%S").replace(
            tzinfo=ZoneInfo("Asia/Seoul")
        )
    except ValueError:
        return None
    return created


class RetentionPruner:
    def __init__(self, history: HistoryStore, outbox: Outbox, backups: Path) -> None:
        self.history = history
        self.outbox = outbox
        self.backups = backups

    def _history_file(self, tid: str, cutoff: datetime, dry_run: bool) -> PruneResult:
        path = self.history._path(tid)
        result = PruneResult()
        # A preview reads a snapshot without creating companion lock files.
        guard = contextlib.nullcontext() if dry_run else exclusive_file_lock(path)
        with guard:
            if path.is_symlink():
                return result
            try:
                lines = path.read_bytes().splitlines(keepends=True)
            except FileNotFoundError:
                return result
            remaining = []
            for line in lines:
                try:
                    data = json.loads(line)
                except (ValueError, UnicodeError):
                    data = None
                if isinstance(data, dict) and _older(data.get("ts"), cutoff):
                    result.entries += 1
                    result.bytes += len(line)
                else:
                    remaining.append(line)
            if result.entries:
                result.history_files = 1
                if not dry_run:
                    maximum = self.history._maximum_sequence(tid, self.history._read(path))
                    if maximum:
                        atomic_text_save(self.history._sequence_path(tid), str(maximum) + "\n")
                    if remaining:
                        # Keep all surviving bytes, including malformed or undated lines.
                        atomic_bytes_save(path, b"".join(remaining))
                    else:
                        path.unlink()
                    self.history._sequences.pop(tid, None)
        return result

    def _done(self, cutoff: datetime, dry_run: bool) -> PruneResult:
        result = PruneResult()
        for name in ("done", "corrupt"):
            for path in (self.outbox.directory / name).glob("*.json"):
                try:
                    if path.is_symlink():
                        continue
                    if name == "corrupt":
                        info = path.stat()
                        if datetime.fromtimestamp(info.st_mtime, UTC) >= cutoff:
                            continue
                        size = info.st_size
                    else:
                        raw = path.read_bytes()
                        item = json.loads(raw)
                        if (
                            not isinstance(item, dict)
                            or item.get("state") not in ("sent", "dropped", "failed")
                            or not _older(item.get("updated"), cutoff)
                        ):
                            continue
                        size = len(raw)
                    if not dry_run:
                        path.unlink()
                except (FileNotFoundError, ValueError, UnicodeError):
                    continue
                except OSError as exc:
                    logger.warning("Queue retention skipped %s: %s", path.name, type(exc).__name__)
                    result.failures += 1
                    continue
                result.outbox_items += 1
                result.bytes += size
        return result

    def _backups(self, cutoff: datetime, dry_run: bool) -> PruneResult:
        result = PruneResult()
        unrecognized = False
        for directory in self.backups.iterdir() if self.backups.is_dir() else []:
            if directory.is_symlink() or not directory.is_dir():
                unrecognized = True
                continue
            removed_in_directory = False
            for path in directory.iterdir():
                created = _backup_created(path)
                if path.is_symlink() or not path.is_file() or created is None:
                    unrecognized = True
                    continue
                if created >= cutoff:
                    continue
                try:
                    size = path.stat().st_size
                    if not dry_run:
                        path.unlink()
                except FileNotFoundError:
                    continue
                removed_in_directory = True
                result.backups += 1
                result.bytes += size
            if not dry_run and removed_in_directory:
                # A concurrent edit may have created another backup in this directory.
                with contextlib.suppress(OSError):
                    directory.rmdir()
        if unrecognized:
            logger.warning("Skipping unrecognized backup paths")
        return result

    async def prune(
        self,
        *,
        history_days: int | None = None,
        backup_days: int | None = None,
        done_days: int | None = None,
        dry_run: bool = False,
    ) -> PruneResult:
        """Remove only records strictly older than the selected positive day limits."""
        now = datetime.now(UTC)
        result = PruneResult()
        if history_days:
            cutoff = now - timedelta(days=history_days)
            for tid in self.history.tids():
                try:
                    async with self.history._lock(tid):
                        removed = await complete_write(self._history_file, tid, cutoff, dry_run)
                        result.entries += removed.entries
                        result.history_files += removed.history_files
                        result.bytes += removed.bytes
                except OSError as exc:
                    logger.warning("History retention failed for %s: %s", tid, type(exc).__name__)
                    result.failures += 1
        if done_days:
            async with self.outbox._lock:
                removed = await complete_write(self._done, now - timedelta(days=done_days), dry_run)
                result.outbox_items = removed.outbox_items
                result.bytes += removed.bytes
                result.failures += removed.failures
        if backup_days:
            removed = await complete_write(
                self._backups, now - timedelta(days=backup_days), dry_run
            )
            result.backups = removed.backups
            result.bytes += removed.bytes
        return result
