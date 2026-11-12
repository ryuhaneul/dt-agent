"""Durable append-only conversation history, ordered by sequence number."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from ductor_bot.chat_log.events import EventHub
from ductor_bot.chat_log.ids import filename_tid, history_filename, new_id, timestamp, to_tid
from ductor_bot.infra.atomic_io import atomic_text_save, complete_write
from ductor_bot.infra.file_lock import exclusive_file_lock
from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Entry:
    role: Literal["user", "bot", "event"]
    source: Literal["telegram", "web", "api", "system", "import"]
    text: str
    kind: str = "message"
    title: str | None = None
    status: str | None = None
    turn_id: str | None = None
    imported: bool = False
    import_ref: dict[str, Any] | None = None
    ts: str | None = field(default_factory=timestamp)
    id: str = field(default_factory=new_id)
    seq: int = 0
    v: int = 1


class HistoryStore:
    def __init__(self, directory: Path, events: EventHub | None = None) -> None:
        self.directory = directory
        self._events = events
        self._locks: dict[str, asyncio.Lock] = {}
        self._sequences: dict[str, tuple[int, int]] = {}

    def _lock(self, tid: str) -> asyncio.Lock:
        return self._locks.setdefault(tid, asyncio.Lock())

    def _path(self, tid: str) -> Path:
        return self.directory / history_filename(tid)

    def _sequence_path(self, tid: str) -> Path:
        return self._path(tid).with_suffix(".seq")

    def _maximum_sequence(self, tid: str, entries: list[Entry]) -> int:
        maximum = max((item.seq for item in entries if item.seq > 0), default=0)
        with contextlib.suppress(OSError, ValueError):
            maximum = max(maximum, int(self._sequence_path(tid).read_text(encoding="utf-8")))
        return maximum

    @staticmethod
    def _read(path: Path) -> list[Entry]:
        if not path.exists():
            return []
        entries = []
        with path.open("rb") as stream:
            for line_no, line in enumerate(stream, 1):
                try:
                    entries.append(Entry(**json.loads(line.decode("utf-8"))))
                except (ValueError, TypeError):
                    logger.warning("Skipping corrupt history line %s:%d", path.name, line_no)
        return sorted(entries, key=lambda entry: entry.seq)

    def _append(self, tid: str, entry: Entry) -> Entry:
        path = self._path(tid)
        with exclusive_file_lock(path):
            size = path.stat().st_size if path.exists() else 0
            cached = self._sequences.get(tid)
            if cached is None or cached[1] != size:
                maximum = self._maximum_sequence(tid, self._read(path))
            else:
                maximum = cached[0]
            saved = entry if entry.imported else replace(entry, seq=maximum + 1)
            if entry.imported and entry.seq >= 0:
                raise ValueError("Imported history requires a negative sequence")
            content = (json.dumps(asdict(saved), ensure_ascii=False) + "\n").encode("utf-8")
            with path.open("ab+") as stream:
                # Separate an incomplete final line left by an interrupted append.
                if size:
                    stream.seek(-1, os.SEEK_END)
                    if stream.read(1) != b"\n":
                        stream.write(b"\n")
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            self._sequences[tid] = (max(maximum, saved.seq), path.stat().st_size)
            return saved

    async def append(self, key: SessionKey | str, entry: Entry) -> Entry:
        tid = to_tid(key) if isinstance(key, SessionKey) else key
        async with self._lock(tid):
            saved = await complete_write(self._append, tid, entry)
        if self._events is not None:
            self._events.publish({"type": "entry", "tid": tid, "entry": asdict(saved)})
        return saved

    async def read(self, tid: str, before: int | None = None, limit: int = 100) -> list[Entry]:
        if not 1 <= limit <= 200:
            raise ValueError("History limit must be between 1 and 200")
        async with self._lock(tid):
            entries = await asyncio.to_thread(self._read, self._path(tid))
        return [entry for entry in entries if before is None or entry.seq < before][-limit:]

    async def count(self, tid: str) -> int:
        async with self._lock(tid):
            return len(await asyncio.to_thread(self._read, self._path(tid)))

    async def last(self, tid: str) -> Entry | None:
        entries = await self.read(tid, limit=1)
        return entries[0] if entries else None

    def tids(self) -> list[str]:
        tids = []
        for path in sorted(self.directory.glob("*.jsonl")):
            try:
                tids.append(filename_tid(path.name))
            except ValueError:
                logger.warning("Skipping unrecognized history filename: %s", path.name)
        return tids

    def _delete(self, tid: str) -> int:
        path = self._path(tid)
        with exclusive_file_lock(path):
            count = len(self._read(path))
            path.unlink(missing_ok=True)
            self._sequence_path(tid).unlink(missing_ok=True)
            self._sequences.pop(tid, None)
            return count

    async def delete(self, tid: str) -> int:
        async with self._lock(tid):
            return await complete_write(self._delete, tid)

    async def search(self, q: str, limit: int = 50) -> list[dict[str, Any]]:
        hits = []
        for tid in self.tids():
            async with self._lock(tid):
                entries = await asyncio.to_thread(self._read, self._path(tid))
            for entry in entries:
                index = entry.text.casefold().find(q.casefold())
                if index >= 0:
                    hits.append(
                        {
                            "tid": tid,
                            "seq": entry.seq,
                            "ts": entry.ts,
                            "role": entry.role,
                            "snippet": entry.text[max(0, index - 40) : index + 160],
                        }
                    )
        hits.sort(key=lambda hit: (hit["ts"] or "", hit["seq"]), reverse=True)
        return hits[: min(limit, 50)]

    async def has_import_ref(self, ref: dict[str, Any]) -> bool:
        for tid in self.tids():
            async with self._lock(tid):
                entries = await asyncio.to_thread(self._read, self._path(tid))
            if any(entry.import_ref == ref for entry in entries):
                return True
        return False

    def _remove_imported(self, tid: str) -> int:
        path = self._path(tid)
        with exclusive_file_lock(path):
            entries = self._read(path)
            remaining = [entry for entry in entries if not entry.imported]
            if tid == "unknown":
                path.unlink(missing_ok=True)
                removed = len(entries)
            else:
                atomic_text_save(
                    path,
                    "".join(
                        json.dumps(asdict(entry), ensure_ascii=False) + "\n" for entry in remaining
                    ),
                )
                removed = len(entries) - len(remaining)
            self._sequences.pop(tid, None)
            return removed

    async def remove_imported(self) -> int:
        removed = 0
        for tid in self.tids():
            async with self._lock(tid):
                removed += await complete_write(self._remove_imported, tid)
        return removed
