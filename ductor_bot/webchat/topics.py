"""Topic listing and Telegram-backed management."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramBadRequest

from ductor_bot.chat_log.ids import from_tid, timestamp, to_tid
from ductor_bot.i18n.loader import load_web_strings
from ductor_bot.infra.atomic_io import complete_write
from ductor_bot.infra.json_store import atomic_json_save
from ductor_bot.session.key import SessionKey
from ductor_bot.webchat.settings import resolve_default_chat_id

if TYPE_CHECKING:
    from pathlib import Path

    from ductor_bot.chat_log.runtime import ChatLogRuntime
    from ductor_bot.messenger.telegram.app import TelegramBot
    from ductor_bot.webchat.turns import WebTurns


logger = logging.getLogger(__name__)


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
        self._registry_lock = runtime.topic_registry_lock
        self._gates: dict[str, asyncio.Lock] = {}
        self._path = bot._orch.paths.webchat_dir / "topics.json"
        self._strings = load_web_strings(bot.config.language)

    def gate(self, tid: str) -> asyncio.Lock:
        return self._gates.setdefault(tid, asyncio.Lock())

    def _read(self) -> list[dict[str, Any]]:
        return self._read_registry(self._path)

    @staticmethod
    def _read_registry(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("topics_registry_invalid")
        return data

    @staticmethod
    def _read_deleted(path: Path) -> set[tuple[int, int]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, ValueError, TypeError):
            data = None
        if not isinstance(data, list) or any(
            not isinstance(row, list)
            or len(row) != 2
            or any(type(value) is not int for value in row)
            for row in data
        ):
            logger.warning("Deleted topic registry is corrupt or unreadable; treating as empty")
            return set()
        return {(row[0], row[1]) for row in data}

    @staticmethod
    async def restore_names(bot: TelegramBot) -> None:
        """Restore registry-only topics before their first foreground session."""
        directory = bot._orch.paths.webchat_dir
        path = directory / "topics.json"
        if not path.exists():
            return
        entries = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
        if not isinstance(entries, list):
            raise TypeError("topics_registry_invalid")
        deleted = await asyncio.to_thread(
            WebTopics._read_deleted, directory / "deleted_topics.json"
        )
        for item in entries:
            if not isinstance(item, dict):
                continue
            chat_id, topic_id, name = item.get("chat_id"), item.get("topic_id"), item.get("name")
            if (
                type(chat_id) is not int
                or type(topic_id) is not int
                or not isinstance(name, str)
                or not name.strip()
            ):
                continue
            if (chat_id, topic_id) not in deleted and bot._topic_names.get(
                chat_id, topic_id
            ) is None:
                bot._topic_names.set(chat_id, topic_id, name)

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
            summary = await self.runtime.history.summary(tid)
            session = sessions.get(tid)
            saved = registered.get(tid, {})
            unknown = tid == "unknown"
            key = None if unknown else from_tid(tid)
            imported_count = summary.imported_count
            active = _date(session.last_active) if session else None
            imported = (
                imported_count == summary.count
                and active is not None
                and active < datetime.now(UTC) - timedelta(days=30)
            )
            group = "unknown" if unknown else "imported" if imported else "telegram"
            cached = (
                self.bot._topic_names.get(key.chat_id, key.topic_id)
                if key and key.topic_id is not None
                else None
            )
            custom_name = (session.topic_name if session else None) or saved.get("name") or cached
            name_key = (
                "topic.unknown_name"
                if unknown
                else "topic.general"
                if not custom_name and key and key.topic_id is None
                else None
            )
            name = (
                self._strings["topic.unknown_name"]
                if unknown
                else custom_name
                or (
                    self._strings["topic.general"]
                    if key and key.topic_id is None
                    else f"Topic #{key.topic_id if key else 0}"
                )
            )
            last_ts = (
                summary.last.ts
                if summary.last is not None
                else session.last_active
                if session
                else saved.get("created_at")
            )
            preview = (
                self._strings["topic.imported_preview"].format(n=summary.count)
                if group == "imported"
                else summary.last.text[:120]
                if summary.last is not None
                else ""
            )
            topics.append(
                {
                    "tid": tid,
                    "chat_id": key.chat_id if key else 0,
                    "topic_id": key.topic_id if key else None,
                    "name": name,
                    "name_key": name_key,
                    "group": group,
                    "last_ts": last_ts,
                    "preview": preview,
                    "count": summary.count,
                    "imported_count": imported_count,
                    "created_at": session.created_at if session else saved.get("created_at"),
                    "turn_id": self.turns.active_turn_id(tid),
                    "turns": self.turns.snapshot(tid),
                    "busy": False
                    if key is None
                    else self.turns.busy(tid) or self.bot._orch.is_topic_busy(key),
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

    @staticmethod
    async def record_name(  # noqa: PLR0913 -- shared persistence fields and ingress guard
        bot: TelegramBot,
        runtime: ChatLogRuntime,
        key: SessionKey,
        name: str,
        *,
        topic: dict[str, Any] | None = None,
        existing_only: bool = False,
    ) -> None:
        """Persist an observed Telegram title using the web topic registry."""
        assert key.topic_id is not None
        tid = to_tid(key)
        path = bot._orch.paths.webchat_dir / "topics.json"
        async with runtime.topic_registry_lock:
            deleted = await asyncio.to_thread(
                WebTopics._read_deleted, path.with_name("deleted_topics.json")
            )
            if tid in runtime.recorder.blocked or key.lock_key in deleted:
                return
            bot._topic_names.set(key.chat_id, key.topic_id, name)
            try:
                entries = await asyncio.to_thread(WebTopics._read_registry, path)
                saved = next(
                    (
                        item
                        for item in entries
                        if (item["chat_id"], item["topic_id"]) == key.lock_key
                    ),
                    None,
                )
                entries = [
                    item for item in entries if (item["chat_id"], item["topic_id"]) != key.lock_key
                ]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("Topic title recording skipped: %s", type(exc).__name__)
                return
            if (
                existing_only
                and saved is None
                and await bot._orch._sessions.get_active(key) is None
                and tid not in runtime.history.tids()
            ):
                return
            entries.append(
                {
                    "chat_id": key.chat_id,
                    "topic_id": key.topic_id,
                    "name": name,
                    "created_at": topic["created_at"]
                    if topic is not None
                    else saved.get("created_at")
                    if saved
                    else timestamp(),
                }
            )
            await bot._orch._sessions.set_topic_name(key, name)
            await complete_write(atomic_json_save, path, entries)
        runtime.events.publish({"type": "topics"})

    async def create(self, name: str) -> dict[str, Any]:
        chat_id = resolve_default_chat_id(self.bot.config)
        topic_id = await self.runtime.ops.create_topic(chat_id, name)
        await self.record_name(self.bot, self.runtime, SessionKey.telegram(chat_id, topic_id), name)
        topic = await self.get(f"{chat_id}.{topic_id}")
        assert topic is not None
        return topic

    async def rename(self, topic: dict[str, Any], name: str) -> dict[str, Any]:
        key = from_tid(topic["tid"])
        assert key.topic_id is not None
        await self.runtime.ops.rename_topic(key.chat_id, key.topic_id, name)
        await self.record_name(self.bot, self.runtime, key, name, topic=topic)
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
                async with self.bot._orch._autocompact_global_lock:
                    removed = await self.bot._orch._sessions.delete_session(key)
                async with self._registry_lock:
                    deleted_path = self._path.with_name("deleted_topics.json")
                    deleted_topics = await asyncio.to_thread(self._read_deleted, deleted_path)
                    # Keep confirmed web deletions across restarts without automatic expiry.
                    deleted_topics.add((key.chat_id, key.topic_id))
                    await complete_write(
                        atomic_json_save,
                        deleted_path,
                        [list(pair) for pair in sorted(deleted_topics)],
                    )
                    entries = [
                        item
                        for item in await asyncio.to_thread(self._read)
                        if (item["chat_id"], item["topic_id"]) != key.lock_key
                    ]
                    await complete_write(atomic_json_save, self._path, entries)
                await self.turns.discard_topic(tid)
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
