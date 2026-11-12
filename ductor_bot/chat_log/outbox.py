"""Ordered disk outbox with durable per-item transmission progress."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from ductor_bot.chat_log.events import EventHub
from ductor_bot.chat_log.ids import new_id, timestamp, to_tid
from ductor_bot.infra.atomic_io import complete_write
from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)

OutboxState = Literal["pending", "sent", "failed", "dropped"]
OutboxKind = Literal["user_mirror", "reply_mirror", "bus"]


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _save(path: Path, item: OutboxItem) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(asdict(item), stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    # The item is now visible to other readers. Do not report a failed enqueue
    # after this point: callers would retry an item that is already queued.
    try:
        _fsync_directory(path.parent)
    except OSError as exc:
        logger.warning("Outbox directory sync failed after save: %s", type(exc).__name__)
    try:
        temporary.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Outbox temporary cleanup failed after save: %s", type(exc).__name__)


@dataclass(frozen=True, slots=True)
class OutboxItem:
    kind: OutboxKind
    chat_id: int
    topic_id: int | None
    tid: str
    text: str
    origin: str | None = None
    title: str | None = None
    history_id: str | None = None
    buttons_session: str | None = None
    sent_chunks: int = 0
    attempts: int = 0
    state: OutboxState = "pending"
    last_error: str | None = None
    created: str = field(default_factory=timestamp)
    updated: str = field(default_factory=timestamp)
    id: str = field(default_factory=new_id)
    v: int = 1

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "origin": self.origin,
            "title": self.title,
            "tid": self.tid,
            "preview": self.text[:80],
            "created": self.created,
            "state": self.state,
            "last_error": self.last_error,
        }


class Outbox:
    def __init__(self, directory: Path, events: EventHub | None = None) -> None:
        self.directory = directory
        self._events = events
        self._lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self._last_ns = 0

    @property
    def _pending_dir(self) -> Path:
        return self.directory / "pending"

    @property
    def _done_dir(self) -> Path:
        return self.directory / "done"

    @staticmethod
    def _load(path: Path) -> OutboxItem:
        item = OutboxItem(**json.loads(path.read_text(encoding="utf-8")))
        if (
            type(item.v) is not int
            or item.v != 1
            or item.kind not in {"bus", "user_mirror", "reply_mirror"}
            or item.state not in {"pending", "sent", "failed", "dropped"}
            or type(item.chat_id) is not int
            or (item.topic_id is not None and type(item.topic_id) is not int)
            or type(item.sent_chunks) is not int
            or item.sent_chunks < 0
            or type(item.attempts) is not int
            or item.attempts < 0
            or not isinstance(item.id, str)
            or re.fullmatch(r"[0-9a-f]{12}", item.id) is None
            or not path.name.endswith(f"-{item.id}.json")
            or not isinstance(item.tid, str)
            or not isinstance(item.text, str)
            or any(
                value is not None and not isinstance(value, str)
                for value in (
                    item.origin,
                    item.title,
                    item.history_id,
                    item.buttons_session,
                    item.last_error,
                )
            )
        ):
            raise ValueError("Unsupported outbox item")
        for value in (item.created, item.updated):
            if datetime.fromisoformat(value).tzinfo is None:
                raise ValueError("Outbox timestamp requires a timezone")
        return item

    def _readable(self, path: Path) -> OutboxItem | None:
        try:
            item = self._load(path)
        except (ValueError, TypeError):
            corrupt = self.directory / "corrupt"
            try:
                corrupt.mkdir(parents=True, exist_ok=True)
                path.replace(corrupt / path.name)
                _fsync_directory(corrupt)
                _fsync_directory(path.parent)
            except OSError as exc:
                logger.warning(
                    "Skipping unreadable outbox item %s; quarantine failed: %s",
                    path.name,
                    type(exc).__name__,
                )
            else:
                logger.warning("Skipping unreadable outbox item %s; quarantined", path.name)
            return None
        return item

    def _enqueue(self, item: OutboxItem) -> None:
        existing = sorted(self._pending_dir.glob("*.json")) + sorted(self._done_dir.glob("*.json"))
        previous = max(
            (
                int(path.name.split("-", 1)[0])
                for path in existing
                if path.name.split("-", 1)[0].isdecimal()
            ),
            default=0,
        )
        self._last_ns = max(time.time_ns(), self._last_ns + 1, previous + 1)
        _save(self._pending_dir / f"{self._last_ns:020d}-{item.id}.json", item)

    def _pending(self, *, skip_unreadable: bool = False) -> list[OutboxItem]:
        items = []
        for path in sorted(self._pending_dir.glob("*.json")):
            try:
                item = self._readable(path)
                if item is None:
                    continue
                if item.state == "pending":
                    items.append(item)
                else:
                    # Complete a terminal transition interrupted between save and rename.
                    self._move_done(path)
            except OSError as exc:
                if not skip_unreadable:
                    raise
                logger.warning("Outbox query skipped %s: %s", path.name, type(exc).__name__)
        return items

    def _move_done(self, path: Path) -> None:
        self._done_dir.mkdir(parents=True, exist_ok=True)
        path.replace(self._done_dir / path.name)
        _fsync_directory(self._done_dir)
        _fsync_directory(self._pending_dir)

    def _publish(self, item: OutboxItem, pending: int) -> None:
        if self._events is not None:
            self._events.publish({"type": "outbox", "pending": pending, "item": item.view()})

    async def enqueue(  # noqa: PLR0913 - the persisted item fields form the enqueue contract
        self,
        *,
        kind: OutboxKind,
        chat_id: int,
        topic_id: int | None = None,
        text: str,
        origin: str | None = None,
        title: str | None = None,
        history_id: str | None = None,
        buttons_session: str | None = None,
        sent_chunks: int = 0,
    ) -> OutboxItem:
        if sent_chunks < 0:
            raise ValueError("Chunk progress must be nonnegative")
        item = OutboxItem(
            kind=kind,
            chat_id=chat_id,
            topic_id=topic_id,
            tid=to_tid(SessionKey.telegram(chat_id, topic_id)),
            text=text,
            origin=origin,
            title=title,
            history_id=history_id,
            buttons_session=buttons_session,
            sent_chunks=sent_chunks,
        )
        async with self._lock:
            await complete_write(self._enqueue, item)
            try:
                count = len(await complete_write(self._pending, skip_unreadable=True))
                self._publish(item, count)
            except Exception as exc:
                logger.warning("Outbox notification failed after enqueue: %s", type(exc).__name__)
        return item

    async def pending(self, *, skip_unreadable: bool = False) -> list[OutboxItem]:
        async with self._lock:
            return await complete_write(self._pending, skip_unreadable=skip_unreadable)

    async def has_pending(self) -> bool:
        try:
            return bool(await self.pending())
        except OSError as exc:
            # An unreadable head must still block immediate delivery from overtaking it.
            logger.warning("Outbox presence check failed: %s", type(exc).__name__)
            return True

    def _update(self, item_id: str, **changes: Any) -> OutboxItem:
        if re.fullmatch(r"[0-9a-f]{12}", item_id) is None:
            raise ValueError("Invalid outbox identifier")
        paths = list(self._pending_dir.glob(f"*-{item_id}.json"))
        if len(paths) != 1:
            raise KeyError(item_id)
        path = paths[0]
        current = self._load(path)
        if changes.get("sent_chunks", current.sent_chunks) < current.sent_chunks:
            raise ValueError("Chunk progress cannot move backwards")
        item = replace(current, updated=timestamp(), **changes)
        _save(path, item)
        if item.state != "pending":
            self._move_done(path)
        return item

    async def _change(self, item_id: str, **changes: Any) -> OutboxItem:
        async with self._lock:
            item = await complete_write(self._update, item_id, **changes)
            count = len(await complete_write(self._pending))
            self._publish(item, count)
            return item

    async def mark_progress(
        self,
        item_id: str,
        *,
        sent_chunks: int,
        attempts: int,
        last_error: str | None = None,
    ) -> OutboxItem:
        return await self._change(
            item_id, sent_chunks=sent_chunks, attempts=attempts, last_error=last_error
        )

    async def mark_sent(self, item_id: str) -> OutboxItem:
        return await self._change(item_id, state="sent", last_error=None)

    async def mark_failed(
        self,
        item_id: str,
        last_error: str = "send_failed",
        *,
        attempts: int | None = None,
        sent_chunks: int | None = None,
    ) -> OutboxItem:
        changes: dict[str, Any] = {"state": "failed", "last_error": last_error}
        if attempts is not None:
            changes["attempts"] = attempts
        if sent_chunks is not None:
            changes["sent_chunks"] = sent_chunks
        return await self._change(item_id, **changes)

    async def drop_tid(self, tid: str) -> int:
        async with self._lock:
            items = await complete_write(self._pending)
            dropped = 0
            for item in items:
                if item.tid == tid:
                    saved = await complete_write(self._update, item.id, state="dropped")
                    dropped += 1
                    self._publish(saved, len(items) - dropped)
            return dropped

    def _prune(self, days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        removed = 0
        for directory in (self._done_dir, self.directory / "corrupt"):
            removed_here = 0
            for path in directory.glob("*.json"):
                try:
                    if path.is_symlink():
                        continue
                    if directory == self._done_dir:
                        item = self._readable(path)
                        expired = item is not None and datetime.fromisoformat(item.updated) < cutoff
                    else:
                        expired = datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff
                    if expired:
                        path.unlink()
                        removed_here += 1
                except OSError as exc:
                    logger.warning("Outbox prune skipped %s: %s", path.name, type(exc).__name__)
            removed += removed_here
            if removed_here:
                try:
                    _fsync_directory(directory)
                except OSError as exc:
                    logger.warning("Outbox prune sync failed: %s", type(exc).__name__)
        return removed

    async def prune(self, days: int = 7) -> int:
        async with self._lock:
            return await complete_write(self._prune, days)

    async def views(self) -> list[dict[str, Any]]:
        async with self._lock:
            pending = await complete_write(self._pending, skip_unreadable=True)
            done = await complete_write(self._recent_done)
        return [item.view() for item in pending + done if item.state in {"pending", "failed"}]

    def _recent_done(self) -> list[OutboxItem]:
        cutoff = datetime.now(UTC) - timedelta(days=7)
        items = []
        for path in sorted(self._done_dir.glob("*.json")):
            try:
                item = self._readable(path)
                if item is not None and datetime.fromisoformat(item.updated) >= cutoff:
                    items.append(item)
            except OSError as exc:
                logger.warning("Outbox query skipped %s: %s", path.name, type(exc).__name__)
        return items

    async def mirror_state(self, history_id: str) -> Literal["pending", "sent", "failed"] | None:
        async with self._lock:
            items = await complete_write(self._pending, skip_unreadable=True)
            items += await complete_write(self._recent_done)
        matching = [item for item in items if item.history_id == history_id]
        if not matching:
            return None
        if any(item.state == "pending" for item in matching):
            return "pending"
        return "sent" if all(item.state == "sent" for item in matching) else "failed"
