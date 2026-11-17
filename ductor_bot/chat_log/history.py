"""Durable append-only conversation history, ordered by sequence number."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, BinaryIO, Literal

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


@dataclass(frozen=True, slots=True)
class HistorySummary:
    count: int = 0
    imported_count: int = 0
    last: Entry | None = None


@dataclass(slots=True)
class _HistoryIndex:
    key: tuple[int, int, int, int]
    rows: list[tuple[int, int, int]]
    imported_count: int
    last: Entry | None


def _file_key(stream: BinaryIO) -> tuple[int, int, int, int]:
    info = os.fstat(stream.fileno())
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size


def _parse_entry(line: bytes, path: Path, line_no: int) -> Entry | None:
    try:
        return Entry(**json.loads(line.decode("utf-8")))
    except (ValueError, TypeError):
        logger.warning("Skipping corrupt history line %s:%d", path.name, line_no)
        return None


class HistoryStore:
    def __init__(self, directory: Path, events: EventHub | None = None) -> None:
        self.directory = directory
        self._events = events
        self._locks: dict[str, asyncio.Lock] = {}
        self._sequences: dict[str, tuple[int, int]] = {}
        self._indexes: dict[str, _HistoryIndex] = {}

    def _lock(self, tid: str) -> asyncio.Lock:
        return self._locks.setdefault(tid, asyncio.Lock())

    def _path(self, tid: str) -> Path:
        return self.directory / history_filename(tid)

    def _metadata_path(self, tid: str) -> Path:
        self._path(tid)  # Validate the same topic identifiers as the history file.
        return self.directory / f"{tid}.meta.jsonl"

    @staticmethod
    def _read_metadata(path: Path) -> dict[str, dict[str, Any]]:
        if not path.exists():
            return {}
        result = {}
        for line_no, line in enumerate(path.read_bytes().splitlines(), 1):
            try:
                item = json.loads(line)
            except (ValueError, TypeError):
                item = None
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not isinstance(item.get("buttons"), list)
            ):
                logger.warning("Skipping corrupt history metadata %s:%d", path.name, line_no)
                continue
            result[item["id"]] = {"buttons": item["buttons"]}
        return result

    def _append_metadata(
        self, tid: str, entry_id: str, buttons: Sequence[Sequence[Mapping[str, object]]]
    ) -> None:
        path = self._metadata_path(tid)
        item = {"id": entry_id, "buttons": [[dict(button) for button in row] for row in buttons]}
        content = (json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8")
        with exclusive_file_lock(path), path.open("ab+") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell():
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    stream.write(b"\n")
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    async def metadata(self, tid: str) -> dict[str, dict[str, Any]]:
        async with self._lock(tid):
            return await asyncio.to_thread(self._read_metadata, self._metadata_path(tid))

    def metadata_tids(self) -> list[str]:
        tids = []
        for path in sorted(self.directory.glob("*.meta.jsonl")):
            tid = path.name.removesuffix(".meta.jsonl")
            try:
                self._path(tid)
            except ValueError:
                logger.warning("Skipping unrecognized metadata filename: %s", path.name)
            else:
                tids.append(tid)
        return tids

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
                entry = _parse_entry(line, path, line_no)
                if entry is not None:
                    entries.append(entry)
        return sorted(entries, key=lambda entry: entry.seq)

    def _index(self, tid: str, stream: BinaryIO) -> _HistoryIndex:
        key = _file_key(stream)
        cached = self._indexes.get(tid)
        if cached is not None and cached.key == key:
            return cached
        rows = []
        imported_count = 0
        last = None
        offset = 0
        path = self._path(tid)
        for line_no, line in enumerate(stream, 1):
            entry = _parse_entry(line, path, line_no)
            if entry is not None:
                rows.append((entry.seq, offset, len(line)))
                imported_count += bool(entry.imported)
                if last is None or entry.seq >= last.seq:
                    last = entry
            offset += len(line)
        rows.sort(key=lambda row: row[0])
        index = _HistoryIndex(key, rows, imported_count, last)
        self._indexes[tid] = index
        return index

    def _query(
        self, tid: str, before: int | None = None, limit: int = 0
    ) -> tuple[list[Entry], bool, HistorySummary]:
        path = self._path(tid)
        # Missing history is an empty snapshot; existing files still require the lock.
        if not path.exists():
            self._indexes.pop(tid, None)
            return [], False, HistorySummary()
        with exclusive_file_lock(path):
            try:
                stream = path.open("rb")
            except FileNotFoundError:
                self._indexes.pop(tid, None)
                return [], False, HistorySummary()
            with stream:
                index = self._index(tid, stream)
                summary = HistorySummary(len(index.rows), index.imported_count, index.last)
                if not limit:
                    return [], False, summary
                end = (
                    len(index.rows)
                    if before is None
                    else bisect_left(index.rows, before, key=lambda row: row[0])
                )
                selected = index.rows[max(0, end - limit) : end]
                entries = []
                for _, offset, length in selected:
                    stream.seek(offset)
                    entry = _parse_entry(stream.read(length), path, 0)
                    if entry is not None:
                        entries.append(entry)
                more = bool(selected and index.rows[0][0] < selected[0][0])
                return entries, more, summary

    def _append(
        self,
        tid: str,
        entry: Entry,
        buttons: Sequence[Sequence[Mapping[str, object]]] | None = None,
    ) -> Entry:
        path = self._path(tid)
        with exclusive_file_lock(path):
            if buttons is not None:
                self._append_metadata(tid, entry.id, buttons)
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
                index = self._indexes.get(tid)
                if index is not None and index.key != _file_key(stream):
                    self._indexes.pop(tid, None)
                    index = None
                offset = size
                # Separate an incomplete final line left by an interrupted append.
                if size:
                    stream.seek(-1, os.SEEK_END)
                    if stream.read(1) != b"\n":
                        stream.write(b"\n")
                        offset += 1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                if index is not None:
                    position = bisect_right(index.rows, saved.seq, key=lambda row: row[0])
                    index.rows.insert(position, (saved.seq, offset, len(content)))
                    index.imported_count += bool(saved.imported)
                    if index.last is None or saved.seq >= index.last.seq:
                        index.last = saved
                    index.key = _file_key(stream)
            self._sequences[tid] = (max(maximum, saved.seq), path.stat().st_size)
            return saved

    async def append(
        self,
        key: SessionKey | str,
        entry: Entry,
        *,
        buttons: Sequence[Sequence[Mapping[str, object]]] | None = None,
    ) -> Entry:
        tid = to_tid(key) if isinstance(key, SessionKey) else key
        async with self._lock(tid):
            if buttons is None:
                saved = await complete_write(self._append, tid, entry)
            else:
                saved = await complete_write(self._append, tid, entry, buttons)
        if self._events is not None:
            rendered = asdict(saved)
            if buttons is not None:
                rendered["buttons"] = buttons
            self._events.publish({"type": "entry", "tid": tid, "entry": rendered})
        return saved

    async def read_page(
        self, tid: str, before: int | None = None, limit: int = 100
    ) -> tuple[list[Entry], bool]:
        if not 1 <= limit <= 200:
            raise ValueError("History limit must be between 1 and 200")
        async with self._lock(tid):
            entries, more, _ = await asyncio.to_thread(self._query, tid, before, limit)
        return entries, more

    async def read(self, tid: str, before: int | None = None, limit: int = 100) -> list[Entry]:
        entries, _ = await self.read_page(tid, before=before, limit=limit)
        return entries

    async def summary(self, tid: str) -> HistorySummary:
        async with self._lock(tid):
            _, _, summary = await asyncio.to_thread(self._query, tid)
        return summary

    async def count(self, tid: str) -> int:
        return (await self.summary(tid)).count

    async def last(self, tid: str) -> Entry | None:
        return (await self.summary(tid)).last

    def tids(self) -> list[str]:
        tids = []
        for path in sorted(self.directory.glob("*.jsonl")):
            if path.name.endswith(".meta.jsonl"):
                continue
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
            self._metadata_path(tid).unlink(missing_ok=True)
            self._sequences.pop(tid, None)
            self._indexes.pop(tid, None)
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
