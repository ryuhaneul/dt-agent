"""Topic listing and Telegram-backed management."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramBadRequest

from ductor_bot.chat_log.ids import from_tid, history_filename, timestamp, to_tid
from ductor_bot.i18n.loader import load_web_strings
from ductor_bot.infra.atomic_io import complete_write
from ductor_bot.infra.json_store import atomic_json_save
from ductor_bot.webchat.settings import resolve_default_chat_id

if TYPE_CHECKING:
    from ductor_bot.chat_log.runtime import ChatLogRuntime
    from ductor_bot.messenger.telegram.app import TelegramBot
    from ductor_bot.webchat.turns import WebTurns


class TopicBusyError(Exception):
    """The current turn did not release its session lock before deletion."""


class TopicDeleteError(Exception):
    """Telegram did not confirm deletion."""


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        date = datetime.fromisoformat(value)
    except ValueError:
        return None
    return date.replace(tzinfo=UTC) if date.tzinfo is None else date


class WebTopics:
    def __init__(self, bot: TelegramBot, runtime: ChatLogRuntime, turns: WebTurns) -> None:
        self.bot = bot
        self.runtime = runtime
        self.turns = turns
        self._registry_lock = asyncio.Lock()
        self._gates: dict[str, asyncio.Lock] = {}
        self._path = bot._orch.paths.webchat_dir / "topics.json"
        self._strings = load_web_strings(bot.config.language)

    def gate(self, tid: str) -> asyncio.Lock:
        return self._gates.setdefault(tid, asyncio.Lock())

    def _read(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        data = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("topics_registry_invalid")
        return data

    async def list(self) -> list[dict[str, Any]]:
        sessions = {
            to_tid(item.session_key): item
            for item in await self.bot._orch._sessions.list_all()
            if item.transport == "tg"
        }
        registered = {
            to_tid(from_tid(f"{item['chat_id']}.{item['topic_id']}")): item
            for item in await asyncio.to_thread(self._read)
        }
        topics: list[dict[str, Any]] = []
        for tid in set(sessions) | set(registered) | set(self.runtime.history.tids()):
            history = self.runtime.history
            async with history._lock(tid):
                entries = await asyncio.to_thread(
                    history._read, history.directory / history_filename(tid)
                )
            session = sessions.get(tid)
            saved = registered.get(tid, {})
            unknown = tid == "unknown"
            key = None if unknown else from_tid(tid)
            imported_count = sum(entry.imported for entry in entries)
            active = _date(session.last_active) if session else None
            imported = (
                imported_count == len(entries)
                and active is not None
                and active < datetime.now(UTC) - timedelta(days=30)
            )
            group = "unknown" if unknown else "imported" if imported else "telegram"
            cached = (
                self.bot._topic_names.get(key.chat_id, key.topic_id)
                if key and key.topic_id is not None
                else None
            )
            name = (
                self._strings["topic.unknown_name"]
                if unknown
                else (session.topic_name if session else None)
                or saved.get("name")
                or cached
                or (
                    self._strings["topic.general"]
                    if key and key.topic_id is None
                    else f"Topic #{key.topic_id if key else 0}"
                )
            )
            last_ts = (
                entries[-1].ts
                if entries
                else session.last_active
                if session
                else saved.get("created_at")
            )
            preview = (
                self._strings["topic.imported_preview"].format(n=len(entries))
                if group == "imported"
                else entries[-1].text[:120]
                if entries
                else ""
            )
            topics.append(
                {
                    "tid": tid,
                    "chat_id": key.chat_id if key else 0,
                    "topic_id": key.topic_id if key else None,
                    "name": name,
                    "group": group,
                    "last_ts": last_ts,
                    "preview": preview,
                    "count": len(entries),
                    "imported_count": imported_count,
                    "created_at": session.created_at if session else saved.get("created_at"),
                    "turn_id": self.turns.active_turn_id(tid),
                    "busy": False
                    if key is None
                    else self.turns.busy(tid)
                    or self.bot._orch.is_chat_busy(key.chat_id, key.topic_id),
                    "renamable": bool(key and key.topic_id is not None),
                    "deletable": bool(key and key.topic_id is not None),
                    "read_only": unknown,
                }
            )
        topics.sort(
            key=lambda item: _date(item["last_ts"]) or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return topics

    async def lookup(self, tid: str) -> dict[str, Any] | None:
        """Validate a target without waiting for an in-flight history append."""
        if tid == "unknown":
            return {"tid": tid} if tid in self.runtime.history.tids() else None
        try:
            key = from_tid(tid)
        except ValueError:
            return None
        session = await self.bot._orch._sessions.get_active(key)
        saved = next(
            (
                item
                for item in await asyncio.to_thread(self._read)
                if (item["chat_id"], item["topic_id"]) == key.lock_key
            ),
            None,
        )
        if to_tid(key) != tid or (
            session is None and saved is None and tid not in self.runtime.history.tids()
        ):
            return None
        cached = (
            self.bot._topic_names.get(key.chat_id, key.topic_id)
            if key.topic_id is not None
            else None
        )
        name = (
            (session.topic_name if session else None)
            or (saved["name"] if saved else None)
            or cached
            or (
                self._strings["topic.general"] if key.topic_id is None else f"Topic #{key.topic_id}"
            )
        )
        return {
            "tid": tid,
            "name": name,
            "deletable": key.topic_id is not None,
            "created_at": session.created_at
            if session
            else saved.get("created_at")
            if saved
            else None,
        }

    async def get(self, tid: str) -> dict[str, Any] | None:
        return next((topic for topic in await self.list() if topic["tid"] == tid), None)

    async def create(self, name: str) -> dict[str, Any]:
        chat_id = resolve_default_chat_id(self.bot.config)
        topic_id = await self.runtime.ops.create_topic(chat_id, name)
        async with self._registry_lock:
            entries = await asyncio.to_thread(self._read)
            entries.append(
                {"chat_id": chat_id, "topic_id": topic_id, "name": name, "created_at": timestamp()}
            )
            await complete_write(atomic_json_save, self._path, entries)
        self.bot._topic_names.set(chat_id, topic_id, name)
        self.runtime.events.publish({"type": "topics"})
        topic = await self.get(f"{chat_id}.{topic_id}")
        assert topic is not None
        return topic

    async def rename(self, topic: dict[str, Any], name: str) -> dict[str, Any]:
        key = from_tid(topic["tid"])
        assert key.topic_id is not None
        await self.runtime.ops.rename_topic(key.chat_id, key.topic_id, name)
        self.bot._topic_names.set(key.chat_id, key.topic_id, name)
        await self.bot._orch._sessions.set_topic_name(key, name)
        async with self._registry_lock:
            entries = await asyncio.to_thread(self._read)
            entries = [
                item for item in entries if (item["chat_id"], item["topic_id"]) != key.lock_key
            ]
            entries.append(
                {
                    "chat_id": key.chat_id,
                    "topic_id": key.topic_id,
                    "name": name,
                    "created_at": topic["created_at"],
                }
            )
            await complete_write(atomic_json_save, self._path, entries)
        self.runtime.events.publish({"type": "topics"})
        updated = await self.get(topic["tid"])
        assert updated is not None
        return updated

    async def delete(self, tid: str) -> dict[str, Any]:
        key = from_tid(tid)
        assert key.topic_id is not None
        self.runtime.recorder.blocked.add(tid)
        self.turns.blocked.add(tid)
        deleted = False
        try:
            await self.turns.abort(tid, deleting=True)
            pool = self.bot._orch._lock_pool
            assert pool is not None
            lock = pool.get(key.lock_key)
            try:
                async with asyncio.timeout(30):
                    await lock.acquire()
            except TimeoutError:
                raise TopicBusyError from None
            try:
                try:
                    deleted = await self.runtime.ops.delete_topic(key.chat_id, key.topic_id)
                except TelegramBadRequest as exc:
                    message = exc.message.removeprefix("Bad Request: ").casefold()
                    if message not in {"topic_id_invalid", "message thread not found"}:
                        raise
                    deleted = True
                if not deleted:
                    raise TopicDeleteError
                removed = await self.bot._orch._sessions.delete_session(key)
                async with self._registry_lock:
                    entries = [
                        item
                        for item in await asyncio.to_thread(self._read)
                        if (item["chat_id"], item["topic_id"]) != key.lock_key
                    ]
                    await complete_write(atomic_json_save, self._path, entries)
                count = await self.runtime.history.delete(tid)
                dropped = await self.runtime.outbox.drop_tid(tid)
            finally:
                lock.release()
        finally:
            if not deleted:
                self.runtime.recorder.blocked.discard(tid)
                self.turns.blocked.discard(tid)
        self.runtime.events.publish({"type": "topics"})
        return {
            "telegram_deleted": deleted,
            "session_removed": removed,
            "history_entries": count,
            "outbox_dropped": dropped,
        }
