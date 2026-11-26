"""Durable append-only conversation history, ordered by sequence number."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import tempfile
from bisect import bisect_left, bisect_right
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from itertools import pairwise
from pathlib import Path
from typing import Any, BinaryIO, Literal

from ductor_bot.chat_log.events import EventHub
from ductor_bot.chat_log.ids import filename_tid, history_filename, new_id, timestamp, to_tid
from ductor_bot.infra.atomic_io import atomic_text_save, complete_write
from ductor_bot.infra.file_lock import exclusive_file_lock
from ductor_bot.messenger.telegram.buttons import extract_button_rows
from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)
_METADATA_COMPACT_BYTES = 256 * 1024


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
    duplicate_seqs: list[int]
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


class SelectorConflictError(Exception):
    """A callback belongs to a superseded or completed selector screen."""


class InvalidSelectorButtonError(Exception):
    """Callback data is absent from the current screen."""


class HistoryStore:
    def __init__(
        self,
        directory: Path,
        events: EventHub | None = None,
        *,
        attachment_index_path: Path | None = None,
    ) -> None:
        self.directory = directory
        self.attachment_index_path = attachment_index_path or directory.parent / "attachments.jsonl"
        self._events = events
        self._locks: dict[str, asyncio.Lock] = {}
        self._sequences: dict[str, tuple[int, int]] = {}
        self._indexes: dict[str, _HistoryIndex] = {}
        self._metadata_compacted_sizes: dict[str, int] = {}
        self._selector_locks: dict[tuple[str, str], asyncio.Lock] = {}

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
                or not (
                    isinstance(item.get("buttons"), list)
                    or (item.keys() == {"id", "activity"} and isinstance(item["activity"], list))
                )
            ):
                logger.warning("Skipping corrupt history metadata %s:%d", path.name, line_no)
                continue
            metadata = {
                key: item[key]
                for key in (
                    "buttons",
                    "telegram_message_id",
                    "telegram_html_text",
                    "answered",
                    "text",
                    "selector_revision",
                    "mirror_revision",
                    "display_markdown",
                )
                if key in item
            }
            if isinstance(item.get("activity"), list):
                metadata["activity"] = item["activity"]
            result[item["id"]] = metadata
        return result

    def _append_metadata(
        self, tid: str, entry_id: str, buttons: Sequence[Sequence[Mapping[str, object]]]
    ) -> None:
        self._update_metadata(
            tid, entry_id, {"buttons": [[dict(button) for button in row] for row in buttons]}
        )

    @staticmethod
    def _write_metadata(path: Path, entry_id: str, metadata: Mapping[str, object]) -> None:
        content = (json.dumps({"id": entry_id, **metadata}, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        with path.open("ab+") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell():
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    stream.write(b"\n")
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _save_compacted_metadata(path: Path, content: str) -> None:
        """Publish a durable replacement without weakening the appended record's fsync."""
        with contextlib.ExitStack() as cleanup:
            stream = tempfile.NamedTemporaryFile(  # noqa: SIM115 -- register unlink before closing and replacing
                mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
            )
            temporary = Path(stream.name)
            cleanup.callback(temporary.unlink, missing_ok=True)
            with stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def _update_metadata(  # noqa: PLR0913 -- atomic claims and keyboard revision check
        self,
        tid: str,
        entry_id: str,
        updates: Mapping[str, object],
        *,
        claim: bool = False,
        only_if_missing: bool = False,
        binding_revision: int | None = None,
    ) -> dict[str, Any] | None:
        path = self._metadata_path(tid)
        path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(path):
            records = self._read_metadata(path)
            current = records.get(entry_id)
            if only_if_missing and current is not None and "buttons" in current:
                current.pop("activity", None)
                return current
            if claim and (
                current is None or "buttons" not in current or current.get("answered") is not None
            ):
                return None
            if (
                binding_revision is not None
                and (current or {}).get("selector_revision", 0) != binding_revision
            ):
                updates = {
                    key: value
                    for key, value in updates.items()
                    if key not in {"buttons", "telegram_html_text"}
                }
            metadata = {**(current or {}), **updates}
            if "activity" not in updates:
                metadata = {"buttons": [], **metadata}
            if "text" in updates:
                metadata["selector_revision"] = (current or {}).get("selector_revision", 0) + 1
            self._write_metadata(path, entry_id, metadata)
            try:
                limit = max(_METADATA_COMPACT_BYTES, 2 * self._metadata_compacted_sizes.get(tid, 0))
                if path.stat().st_size >= limit:
                    records[entry_id] = metadata
                    self._save_compacted_metadata(
                        path,
                        "".join(
                            json.dumps({"id": ident, **record}, ensure_ascii=False) + "\n"
                            for ident, record in records.items()
                        ),
                    )
                    self._metadata_compacted_sizes[tid] = path.stat().st_size
            except OSError as exc:
                logger.warning("History metadata compaction failed: %s", type(exc).__name__)
            # Keep the metadata returned to existing callers and event publishers unchanged.
            metadata.pop("activity", None)
            return metadata

    def _publish_metadata(self, tid: str, entry_id: str, metadata: dict[str, Any]) -> None:
        if self._events is not None:
            self._events.publish(
                {"type": "entry_meta", "tid": tid, "entry_id": entry_id, "meta": metadata}
            )

    def _record_activity(self, tid: str, entry_id: str, activity: list[dict[str, Any]]) -> None:
        path = self._path(tid)
        # Keep the existence check and sidecar write together across prune and delete.
        with exclusive_file_lock(path):
            if any(
                entry.id == entry_id and entry.role == "bot" and not entry.imported
                for entry in reversed(self._read(path))
            ):
                self._update_metadata(tid, entry_id, {"activity": activity})

    async def record_activity(
        self, tid: str, entry_id: str, activity: list[dict[str, Any]]
    ) -> None:
        """Save activity in the sidecar for an existing, non-imported reply."""
        async with self._lock(tid):
            await complete_write(self._record_activity, tid, entry_id, activity)

    async def record_display_markdown(self, tid: str, entry_id: str, value: str) -> None:
        async with self._lock(tid):
            metadata = await complete_write(
                self._update_metadata, tid, entry_id, {"display_markdown": value}
            )
        assert metadata is not None
        self._publish_metadata(tid, entry_id, metadata)

    async def bind_telegram_message(  # noqa: PLR0913 -- keyboard identity, snapshot and observed revision
        self,
        tid: str,
        entry_id: str,
        message_id: int,
        *,
        buttons: Sequence[Sequence[Mapping[str, object]]] | None = None,
        html_text: str | None = None,
        keyboard_revision: int = 0,
    ) -> dict[str, Any]:
        """Bind the actual keyboard message, preserving its callback destinations."""
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("A positive Telegram message id is required")
        updates: dict[str, object] = {
            "telegram_message_id": message_id,
            "telegram_html_text": html_text,
        }
        if buttons is not None:
            updates["buttons"] = [[dict(button) for button in row] for row in buttons]
        async with self._lock(tid):
            metadata = await complete_write(
                self._update_metadata, tid, entry_id, updates, binding_revision=keyboard_revision
            )
        assert metadata is not None
        self._publish_metadata(tid, entry_id, metadata)
        return metadata

    def file_observer(self, key: SessionKey, entry_id: str) -> Callable[[Path], Awaitable[None]]:
        async def record(path: Path) -> None:
            await self.record_file(to_tid(key), entry_id, path, provenance="delivered")

        return record

    async def record_file(
        self, tid: str, entry_id: str, path: Path, *, provenance: Literal["received", "delivered"]
    ) -> None:
        """Bind only paths supplied by actual receive/send operations, outside history."""
        from ductor_bot.webchat.attachments import AttachmentIndex

        absolute = await asyncio.to_thread(path.absolute)
        if ".." in absolute.parts:
            return
        stored = absolute.as_posix()
        info = await asyncio.to_thread(absolute.stat)
        item = {
            "id": hashlib.sha256((provenance + absolute.as_posix()).encode()).hexdigest()[:32],
            "name": path.name,
            "size": info.st_size,
            "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "path": stored,
            "inline_allowed": True,
            "provenance": provenance,
        }
        if provenance == "received":
            workspace = self.attachment_index_path.parent.parent / "workspace"
            relative = absolute.relative_to(workspace)
            if not relative.is_relative_to("telegram_files"):
                raise ValueError("invalid_received_file_root")
            item["path"] = relative.as_posix()
        index = AttachmentIndex(self.attachment_index_path)
        async with self._lock(tid):
            items = await index.get(tid, entry_id)
            if any(old["id"] == item["id"] for old in items):
                return
            await asyncio.to_thread(index.append, tid, entry_id, [*items, item])
        if self._events is not None:
            self._events.publish({"type": "attachments_changed", "tid": tid, "entry_id": entry_id})

    async def record_buttons(
        self,
        tid: str,
        entry_id: str,
        buttons: Sequence[Sequence[Mapping[str, object]]],
        *,
        only_if_missing: bool = False,
    ) -> dict[str, Any]:
        """Register validated legacy rows or preserve a named response destination."""
        async with self._lock(tid):
            metadata = await complete_write(
                self._update_metadata,
                tid,
                entry_id,
                {"buttons": [[dict(button) for button in row] for row in buttons]},
                only_if_missing=only_if_missing,
            )
        assert metadata is not None
        self._publish_metadata(tid, entry_id, metadata)
        return metadata

    def selector_gate(self, tid: str, entry_id: str) -> asyncio.Lock:
        return self._selector_locks.setdefault((tid, entry_id), asyncio.Lock())

    async def record_mirror_revision(self, tid: str, entry_id: str, revision: int) -> None:
        async with self._lock(tid):
            await complete_write(
                self._update_metadata, tid, entry_id, {"mirror_revision": revision}
            )

    @contextlib.asynccontextmanager
    async def selector_step(
        self, tid: str, entry_id: str, data: str, revision: int
    ) -> AsyncIterator[dict[str, Any]]:
        """Serialize both transports before validating a screen and applying its action."""
        async with self.selector_gate(tid, entry_id):
            meta = (await self.metadata(tid)).get(entry_id, {})
            if meta.get("selector_revision", 0) != revision or meta.get("answered") is not None:
                raise SelectorConflictError
            if not any(
                button.get("callback_data") == data
                for row in meta.get("buttons", [])
                for button in row
            ):
                raise InvalidSelectorButtonError
            yield meta

    async def record_selector_response(
        self, tid: str, entry_id: str, text: str, buttons: Sequence[Sequence[Mapping[str, object]]]
    ) -> dict[str, Any]:
        """Persist the latest screen without changing the original Entry or outbox."""
        from ductor_bot.messenger.telegram.formatting import markdown_to_telegram_html

        async with self._lock(tid):
            metadata = await complete_write(
                self._update_metadata,
                tid,
                entry_id,
                {
                    "text": text,
                    "buttons": [[dict(button) for button in row] for row in buttons],
                    "telegram_html_text": markdown_to_telegram_html(text),
                },
            )
        assert metadata is not None
        self._publish_metadata(tid, entry_id, metadata)
        return metadata

    async def get_entry(self, tid: str, entry_id: str) -> Entry | None:
        """Find a response on any history page without rewriting it."""
        async with self._lock(tid):
            entries = await asyncio.to_thread(self._read, self._path(tid))
        return next((entry for entry in entries if entry.id == entry_id), None)

    async def mark_answered(self, tid: str, entry_id: str, label: str) -> bool:
        """Atomically claim an existing button response once across processes."""
        if not label:
            raise ValueError("An answered label is required")
        async with self._lock(tid):
            metadata = await complete_write(
                self._update_metadata, tid, entry_id, {"answered": label}, claim=True
            )
        if metadata is None:
            return False
        self._publish_metadata(tid, entry_id, metadata)
        return True

    async def is_answered(self, tid: str, entry_id: str) -> bool:
        """Return the durable answer state, including after a restart."""
        return (await self.metadata(tid)).get(entry_id, {}).get("answered") is not None

    async def entry_for_telegram_message(self, tid: str, message_id: int) -> str | None:
        """Find a recorded keyboard's entry in the same chat/topic."""
        for entry_id, metadata in (await self.metadata(tid)).items():
            if metadata.get("telegram_message_id") == message_id:
                return entry_id
        return None

    async def metadata(
        self, tid: str, *, include_activity: bool = False
    ) -> dict[str, dict[str, Any]]:
        """Read selector and mirror metadata, with activity available to message history."""
        async with self._lock(tid):
            records = await asyncio.to_thread(self._read_metadata, self._metadata_path(tid))
        if include_activity:
            return records
        return {
            entry_id: {key: value for key, value in metadata.items() if key != "activity"}
            for entry_id, metadata in records.items()
            if "buttons" in metadata
        }

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
        duplicate_seqs = [seq for prev, seq in pairwise(row[0] for row in rows) if prev == seq]
        index = _HistoryIndex(key, rows, duplicate_seqs, imported_count, last)
        self._indexes[tid] = index
        return index

    @contextlib.contextmanager
    def _indexed_history(
        self, tid: str, path: Path
    ) -> Iterator[tuple[BinaryIO, _HistoryIndex] | None]:
        # Missing history is an empty snapshot; existing files still require the lock.
        if not path.exists():
            self._indexes.pop(tid, None)
            yield None
            return
        with exclusive_file_lock(path):
            try:
                stream = path.open("rb")
            except FileNotFoundError:
                self._indexes.pop(tid, None)
                yield None
                return
            with stream:
                yield stream, self._index(tid, stream)

    def _query(
        self, tid: str, before: int | None = None, limit: int = 0
    ) -> tuple[list[Entry], bool, HistorySummary]:
        path = self._path(tid)
        with self._indexed_history(tid, path) as history:
            if history is None:
                return [], False, HistorySummary()
            stream, index = history
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

    def _count_after(self, tid: str, cursor: int | None) -> int:
        with self._indexed_history(tid, self._path(tid)) as history:
            if history is None:
                return 0
            _, index = history
            if cursor is None:
                return len(index.rows)
            first = bisect_right(index.rows, cursor, key=lambda row: row[0])
            # Each duplicate contributes one extra row with the same sequence.
            duplicates = len(index.duplicate_seqs) - bisect_right(index.duplicate_seqs, cursor)
            return len(index.rows) - first - duplicates

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
                    if position and index.rows[position - 1][0] == saved.seq:
                        duplicate_position = bisect_right(index.duplicate_seqs, saved.seq)
                        index.duplicate_seqs.insert(duplicate_position, saved.seq)
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
        if entry.role != "user" and not entry.imported:
            text, extracted = extract_button_rows(entry.text)
            entry = replace(entry, text=text)
            if buttons is None:
                buttons = extracted
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

    async def count_after(self, tid: str, cursor: int | None) -> int:
        """Count all rows without a cursor, or distinct sequences strictly after it."""
        async with self._lock(tid):
            return await asyncio.to_thread(self._count_after, tid, cursor)

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
            self._metadata_compacted_sizes.pop(tid, None)
            self._indexes.pop(tid, None)
            return count

    async def delete(self, tid: str) -> int:
        async with self._lock(tid):
            removed = await complete_write(self._delete, tid)
            # Queued waiters still use the gate during release/acquire handoff.
            for key, lock in list(self._selector_locks.items()):
                if key[0] == tid and not lock.locked() and not getattr(lock, "_waiters", None):
                    del self._selector_locks[key]
            return removed

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
