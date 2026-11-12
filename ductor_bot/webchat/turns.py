"""Web turns sharing Telegram session identities and locks."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ductor_bot.chat_log.history import Entry
from ductor_bot.chat_log.ids import from_tid, history_filename, new_id
from ductor_bot.chat_log.recorder import ChatLogTurn, chat_log_source, chat_log_turn
from ductor_bot.i18n.loader import TranslationStore
from ductor_bot.infra.input_context import received_at

if TYPE_CHECKING:
    from ductor_bot.chat_log.runtime import ChatLogRuntime
    from ductor_bot.cli.stream_events import ToolUseEvent
    from ductor_bot.messenger.telegram.app import TelegramBot
    from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)


class WebTurns:
    def __init__(self, bot: TelegramBot, runtime: ChatLogRuntime) -> None:
        self.bot = bot
        self.runtime = runtime
        self.blocked: set[str] = set()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._started: set[str] = set()
        self._settled: set[str] = set()
        self._turn_ids: dict[str, str] = {}
        self._strings = TranslationStore(bot.config.language)

    def busy(self, tid: str) -> bool:
        task = self._tasks.get(tid)
        return task is not None and not task.done()

    def active_turn_id(self, tid: str) -> str | None:
        return self._turn_ids.get(tid) if self.busy(tid) else None

    def _publish(self, tid: str, turn_id: str, phase: str) -> None:
        if tid not in self.blocked:
            self.runtime.events.publish(
                {"type": "turn", "tid": tid, "turn_id": turn_id, "phase": phase}
            )

    def submit(self, tid: str, text: str) -> str:
        turn: ChatLogTurn = {
            "turn_id": new_id(),
            "user_entry_id": new_id(),
            "reply_entry_id": new_id(),
        }
        task = asyncio.create_task(self._run(tid, text, turn), name="webchat-turn")
        self._started.discard(tid)
        self._settled.discard(tid)
        self._tasks[tid] = task
        self._turn_ids[tid] = turn["turn_id"]
        task.add_done_callback(lambda completed: self._finished(tid, completed))
        return turn["turn_id"]

    def _finished(self, tid: str, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning(
                "Web turn failed: session=%s reason=%s",
                from_tid(tid).storage_key,
                type(error).__name__,
            )
        if self._tasks.get(tid) is task:
            self._tasks.pop(tid, None)
            self._turn_ids.pop(tid, None)
            self._started.discard(tid)
            self._settled.discard(tid)

    async def _status(self, tid: str, reply_id: str, *, aborted: bool = False) -> str | None:
        try:
            history = self.runtime.history
            async with history._lock(tid):
                entries = await asyncio.to_thread(
                    history._read, history.directory / history_filename(tid)
                )
            for entry in reversed(entries):
                if entry.id == reply_id:
                    return entry.status or "ok"
        except Exception as exc:
            reason = type(exc).__name__
            logger.warning(
                "Web turn status unavailable: session=%s reason=%s",
                from_tid(tid).storage_key,
                reason,
            )
            return "ok"
        if not aborted:
            logger.warning(
                "Web turn status unavailable: session=%s reason=reply_missing",
                from_tid(tid).storage_key,
            )
        return None

    async def _reply(
        self, key: SessionKey, tid: str, text: str, turn: ChatLogTurn, status: str
    ) -> None:
        if tid in self.blocked or status == "error":
            return
        if status == "aborted":
            if await self._status(tid, turn["reply_entry_id"], aborted=True) is None:
                await self.runtime.history.append(
                    key,
                    Entry(
                        role="bot",
                        source="web",
                        text=text,
                        id=turn["reply_entry_id"],
                        turn_id=turn["turn_id"],
                        status="aborted",
                    ),
                )
            if text:
                text += "\n\n" + self._strings.chat("chat_log.mirror_aborted")
        if not text:
            return
        async with self.runtime.outbox.delivery_lock:
            if tid in self.blocked:
                return
            await self.runtime.outbox.enqueue(
                kind="reply_mirror",
                chat_id=key.chat_id,
                topic_id=key.topic_id,
                text=text,
                history_id=turn["reply_entry_id"],
            )
        self.runtime.drainer.wake()

    async def _run(self, tid: str, text: str, turn: ChatLogTurn) -> None:  # noqa: C901, PLR0915
        key = from_tid(tid)
        partial = ""
        status = "ok"
        try:
            if tid in self.blocked:
                return
            async with self.runtime.outbox.delivery_lock:
                if tid in self.blocked:
                    return
                await self.runtime.outbox.enqueue(
                    kind="user_mirror",
                    chat_id=key.chat_id,
                    topic_id=key.topic_id,
                    text=self._strings.chat("chat_log.mirror_user", text=text),
                    history_id=turn["user_entry_id"],
                )
            self.runtime.drainer.wake()
            self._publish(tid, turn["turn_id"], "queued")
            pool = self.bot._orch._lock_pool
            assert pool is not None
            lock = pool.get(key.lock_key)
            async with lock:
                if tid in self.blocked:
                    return
                self._started.add(tid)
                self._publish(tid, turn["turn_id"], "started")

                async def delta(value: str) -> None:
                    nonlocal partial
                    partial += value
                    if tid not in self.blocked:
                        self.runtime.events.publish(
                            {"type": "delta", "tid": tid, "turn_id": turn["turn_id"], "text": value}
                        )

                async def tool(event: ToolUseEvent) -> None:
                    if tid not in self.blocked:
                        self.runtime.events.publish(
                            {
                                "type": "tool",
                                "tid": tid,
                                "turn_id": turn["turn_id"],
                                "name": event.tool_name,
                            }
                        )

                async def system_status(_value: str | None) -> None:
                    pass

                source_token = chat_log_source.set("web")
                turn_token = chat_log_turn.set(turn)
                try:
                    with received_at():
                        result = await self.bot._orch.handle_message_streaming(
                            key,
                            text,
                            include_partial_messages=True,
                            on_text_delta=delta,
                            on_tool_activity=tool,
                            on_system_status=system_status,
                        )
                    self._settled.add(tid)
                    registry = self.bot._orch._process_registry
                    aborted = registry.was_aborted(key.chat_id) or registry.was_aborted_topic(
                        key.chat_id, key.topic_id
                    )
                    status = (
                        await self._status(tid, turn["reply_entry_id"], aborted=aborted) or "ok"
                    )
                    if status != "error" and aborted:
                        status = "aborted"
                    partial = result.text or partial
                finally:
                    chat_log_turn.reset(turn_token)
                    chat_log_source.reset(source_token)
                await self._reply(key, tid, partial, turn, status)
        except asyncio.CancelledError:
            status = "aborted"
            await self._reply(key, tid, partial, turn, status)
            raise
        except Exception as exc:
            status = "error"
            logger.warning(
                "Web turn failed: session=%s reason=%s", key.storage_key, type(exc).__name__
            )
        finally:
            self._started.discard(tid)
            self._publish(tid, turn["turn_id"], "done" if status == "ok" else status)

    async def abort(self, tid: str, *, deleting: bool = False) -> int:
        task = self._tasks.get(tid)
        if task is not None and tid not in self._started:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        key = from_tid(tid)
        killed = await self.bot._orch._process_registry.kill_by_chat_topic(
            key.chat_id, key.topic_id
        )
        if (
            task is not None
            and not task.done()
            and (deleting or (killed == 0 and tid not in self._settled))
        ):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return killed

    async def stop(self) -> None:
        tasks = list(self._tasks.items())
        for tid, task in tasks:
            if tid not in self._settled:
                task.cancel()
        await asyncio.gather(*(task for _tid, task in tasks), return_exceptions=True)
