"""Web turns sharing Telegram session identities and locks."""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from ductor_bot.chat_log.history import Entry
from ductor_bot.chat_log.ids import from_tid, history_filename, new_id, to_tid
from ductor_bot.chat_log.recorder import ChatLogTurn, chat_log_source, chat_log_turn
from ductor_bot.i18n.loader import TranslationStore
from ductor_bot.infra.input_context import received_at

if TYPE_CHECKING:
    from ductor_bot.chat_log.recorder import TurnStatus
    from ductor_bot.chat_log.runtime import ChatLogRuntime
    from ductor_bot.cli.stream_events import ToolUseEvent
    from ductor_bot.messenger.telegram.app import TelegramBot
    from ductor_bot.orchestrator.flows import StreamingCallbacks
    from ductor_bot.orchestrator.registry import OrchestratorResult
    from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)


def user_mirror_fields(text: str, strings: TranslationStore) -> tuple[str, str]:
    """Return rollback-safe mirror text and its literal-delivery origin."""
    mirror_text = strings.chat("chat_log.mirror_user", text=text)
    # Older drainers parse file/button tags regardless of the item's origin.
    mirror_text = html.escape(mirror_text).replace("[", "&#91;").replace("]", "&#93;")
    return mirror_text, "web_user_text"


@dataclass
class _QueuedTurn:
    tid: str
    text: str
    identity: ChatLogTurn
    phase: str = "queued"
    prepared: asyncio.Event = field(default_factory=asyncio.Event)
    cancellation: asyncio.Task[None] | None = None


class WebTurns:
    def __init__(self, bot: TelegramBot, runtime: ChatLogRuntime) -> None:
        self.bot = bot
        self.runtime = runtime
        self.blocked: set[str] = set()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._started: set[str] = set()
        self._settled: set[str] = set()
        self._turns: dict[str, _QueuedTurn] = {}
        self._queues: dict[str, list[str]] = {}
        self._fifo_locks: dict[str, asyncio.Lock] = {}
        self._strings = TranslationStore(bot.config.language)

    def busy(self, tid: str) -> bool:
        return bool(self.snapshot(tid))

    def active_turn_id(self, tid: str) -> str | None:
        queued = self.snapshot(tid)
        running = next((turn for turn in queued if turn["phase"] == "started"), None)
        return (running or queued[0])["turn_id"] if queued else None

    def snapshot(self, tid: str) -> list[dict[str, str]]:
        return [
            {
                "turn_id": turn_id,
                "phase": state.phase,
                "text": state.text,
                "user_entry_id": state.identity["user_entry_id"],
            }
            for turn_id in self._queues.get(tid, [])
            if (state := self._turns.get(turn_id)) is not None
            and state.phase != "cancelled"
            and (task := self._tasks.get(turn_id)) is not None
            and not task.done()
        ]

    def _publish(self, tid: str, turn_id: str, phase: str) -> None:
        if tid not in self.blocked:
            self.runtime.events.publish(
                {"type": "turn", "tid": tid, "turn_id": turn_id, "phase": phase}
            )

    def submit(self, tid: str, text: str, *, session_name: str | None = None) -> str:
        turn: ChatLogTurn = {
            "turn_id": new_id(),
            "user_entry_id": new_id(),
            "reply_entry_id": new_id(),
        }
        queue = self._queues.setdefault(tid, [])
        self._fifo_locks.setdefault(tid, asyncio.Lock())
        turn_id = turn["turn_id"]
        state = _QueuedTurn(tid, text, turn)
        self._turns[turn_id] = state
        work = self._run(tid, text, turn, session_name=session_name)
        task = asyncio.create_task(work, name="webchat-turn")
        self._tasks[turn_id] = task
        queue.append(turn_id)
        task.add_done_callback(lambda completed: self._finished(turn_id, completed))
        return turn_id

    def _finished(self, turn_id: str, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("Web turn failed: reason=%s", type(error).__name__)
        state = self._turns.get(turn_id)
        self._tasks.pop(turn_id, None)
        self._started.discard(turn_id)
        self._settled.discard(turn_id)
        if state is not None:
            queue = self._queues.get(state.tid, [])
            if turn_id in queue:
                queue.remove(turn_id)
            if not queue:
                self._queues.pop(state.tid, None)
                self._fifo_locks.pop(state.tid, None)
            if state.phase != "cancelled":
                self._turns.pop(turn_id, None)

    async def _recorded_phase(self, tid: str, turn_id: str) -> str | None:
        before = None
        while True:
            entries, more = await self.runtime.history.read_page(tid, before=before, limit=200)
            for entry in entries:
                if entry.turn_id == turn_id:
                    return "cancelled" if entry.status == "cancelled" else "turn_started"
            if not more or not entries:
                return None
            before = entries[0].seq

    async def cancel(
        self, tid: str, turn_id: str
    ) -> Literal["cancelled", "turn_started", "unknown_turn"]:
        state = self._turns.get(turn_id)
        if state is None:
            phase = await self._recorded_phase(tid, turn_id)
            return (
                "cancelled" if phase == "cancelled" else "turn_started" if phase else "unknown_turn"
            )
        if state.tid != tid:
            return "unknown_turn"
        task = self._tasks.get(turn_id)
        if state.phase != "cancelled":
            if state.phase != "queued" or task is None or task.done():
                return "turn_started"
            # Claim cancellation without yielding, mutually exclusive with started below.
            state.phase = "cancelled"
        if state.cancellation is None or (
            state.cancellation.done()
            and (state.cancellation.cancelled() or state.cancellation.exception() is not None)
        ):
            state.cancellation = asyncio.create_task(
                self._finish_cancel(state), name="webchat-cancel"
            )
            state.cancellation.add_done_callback(self._cancel_finished)
        # A disconnected request must not interrupt the history/outbox write sequence.
        await asyncio.shield(state.cancellation)
        return "cancelled"

    @staticmethod
    def _cancel_finished(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("Web turn cancellation failed: reason=%s", type(error).__name__)

    async def _finish_cancel(self, state: _QueuedTurn) -> None:
        await state.prepared.wait()
        turn = state.identity
        task = self._tasks.get(turn["turn_id"])
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        key = from_tid(state.tid)
        async with self.runtime.outbox.delivery_lock:
            if state.tid in self.blocked:
                self._turns.pop(turn["turn_id"], None)
                return
            if await self.runtime.outbox.mirror_state(turn["user_entry_id"]) is None:
                await self.runtime.outbox.enqueue(
                    kind="user_mirror",
                    chat_id=key.chat_id,
                    topic_id=key.topic_id,
                    text=self._strings.chat("chat_log.mirror_user", text=state.text),
                    history_id=turn["user_entry_id"],
                )
            if await self.runtime.history.get_entry(state.tid, turn["user_entry_id"]) is None:
                await self.runtime.history.append(
                    key,
                    Entry(
                        role="user",
                        source="web",
                        text=state.text,
                        id=turn["user_entry_id"],
                        turn_id=turn["turn_id"],
                        status="cancelled",
                    ),
                )
            await self.runtime.outbox.enqueue(
                kind="user_mirror",
                chat_id=key.chat_id,
                topic_id=key.topic_id,
                text=self._strings.chat("notice.queue_cancelled"),
                history_id=turn["user_entry_id"],
            )
        self.runtime.drainer.wake()
        self._publish(state.tid, turn["turn_id"], "cancelled")
        self._turns.pop(turn["turn_id"], None)

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

    async def _reply(  # noqa: PLR0913 -- turn identity, status and named destination
        self,
        key: SessionKey,
        tid: str,
        text: str,
        turn: ChatLogTurn,
        status: str,
        *,
        session_name: str | None = None,
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
        routing: dict[str, Any] = (
            {"buttons_session": session_name} if session_name is not None else {}
        )
        async with self.runtime.outbox.delivery_lock:
            if tid in self.blocked:
                return
            await self.runtime.outbox.enqueue(
                kind="reply_mirror",
                chat_id=key.chat_id,
                topic_id=key.topic_id,
                text=text,
                history_id=turn["reply_entry_id"],
                **routing,
            )
        self.runtime.drainer.wake()

    async def _named_response(
        self,
        key: SessionKey,
        text: str,
        session_name: str,
        callbacks: StreamingCallbacks,
    ) -> OrchestratorResult:
        """Keep a named callback in its foreground session and record its web turn."""
        from ductor_bot.messenger.telegram.buttons import extract_button_rows
        from ductor_bot.orchestrator.core import _MessageDispatch
        from ductor_bot.orchestrator.flows import named_session_streaming

        dispatch = _MessageDispatch(
            key=key, text=text, cmd=text.strip().lower(), transport_override="api"
        )
        result = None
        status: TurnStatus = "error"
        await self.runtime.recorder.begin(dispatch)
        try:
            result = await named_session_streaming(
                self.bot._orch, key, session_name, text, cbs=callbacks, transport_override="api"
            )
            status = "ok"
        except asyncio.CancelledError:
            status = "aborted"
            raise
        else:
            return result
        finally:
            await self.runtime.recorder.finish(dispatch, result, status=status)
            if result is not None and result._chat_log_entry_id is not None:
                _, rows = extract_button_rows(result.text, session_name=session_name)
                if rows is not None:
                    await self.runtime.history.record_buttons(
                        to_tid(key), result._chat_log_entry_id, rows
                    )

    async def _run(  # noqa: C901, PLR0912, PLR0915 -- turn lifecycle, FIFO admission and named target
        self,
        tid: str,
        text: str,
        turn: ChatLogTurn,
        *,
        session_name: str | None = None,
    ) -> None:
        key = from_tid(tid)
        state = self._turns[turn["turn_id"]]
        fifo = self._fifo_locks[tid]
        owns_fifo = False
        partial = ""
        status = "ok"
        try:
            if tid in self.blocked:
                return
            async with self.runtime.outbox.delivery_lock:
                if tid in self.blocked:
                    return
                mirror_text, origin = user_mirror_fields(text, self._strings)
                await self.runtime.outbox.enqueue(
                    kind="user_mirror",
                    chat_id=key.chat_id,
                    topic_id=key.topic_id,
                    text=mirror_text,
                    origin=origin,
                    history_id=turn["user_entry_id"],
                )
            self.runtime.drainer.wake()
            state.prepared.set()
            if state.phase == "cancelled":
                return
            self._publish(tid, turn["turn_id"], "queued")
            await fifo.acquire()
            owns_fifo = True
            pool = self.bot._orch._lock_pool
            assert pool is not None
            lock = pool.get(key.lock_key)
            async with lock:
                if tid in self.blocked or state.phase == "cancelled":
                    return
                # No await between cancellation check and the started transition.
                state.phase = "started"
                self._started.add(turn["turn_id"])
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
                        if session_name is None:
                            result = await self.bot._orch.handle_message_streaming(
                                key,
                                text,
                                include_partial_messages=True,
                                on_text_delta=delta,
                                on_tool_activity=tool,
                                on_system_status=system_status,
                            )
                        else:
                            from ductor_bot.orchestrator.flows import StreamingCallbacks

                            result = await self._named_response(
                                key,
                                text,
                                session_name,
                                StreamingCallbacks(
                                    include_partial_messages=True,
                                    on_text_delta=delta,
                                    on_tool_activity=tool,
                                    on_system_status=system_status,
                                ),
                            )
                    self._settled.add(turn["turn_id"])
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
                await self._reply(key, tid, partial, turn, status, session_name=session_name)
        except asyncio.CancelledError:
            status = "aborted"
            if state.phase == "started":
                await self._reply(key, tid, partial, turn, status, session_name=session_name)
            raise
        except Exception as exc:
            status = "error"
            logger.warning(
                "Web turn failed: session=%s reason=%s", key.storage_key, type(exc).__name__
            )
        finally:
            state.prepared.set()
            if owns_fifo:
                fifo.release()
            self._started.discard(turn["turn_id"])
            if state.phase != "cancelled":
                self._publish(tid, turn["turn_id"], "done" if status == "ok" else status)

    async def cancel_pending(self, chat_id: int) -> None:
        """Cancel all queued web turns for a chat without touching its active turn."""
        pending = [
            (state.tid, turn_id)
            for turn_id, state in self._turns.items()
            if from_tid(state.tid).chat_id == chat_id and state.phase == "queued"
        ]
        for tid, turn_id in pending:
            await self.cancel(tid, turn_id)

    async def abort(self, tid: str, *, deleting: bool = False) -> int:
        active = next(
            (turn_id for turn_id in self._queues.get(tid, []) if turn_id in self._started), None
        )
        task = self._tasks.get(active) if active else None
        if deleting:
            tasks = [
                self._tasks[turn_id]
                for turn_id in self._queues.get(tid, [])
                if turn_id in self._tasks and turn_id != active
            ]
            for queued in tasks:
                queued.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        key = from_tid(tid)
        killed = await self.bot._orch._process_registry.kill_by_chat_topic(
            key.chat_id, key.topic_id
        )
        if (
            task is not None
            and not task.done()
            and (deleting or (killed == 0 and active not in self._settled))
        ):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return killed

    async def stop(self) -> None:
        tasks = list(self._tasks.items())
        for turn_id, task in tasks:
            state = self._turns.get(turn_id)
            if turn_id not in self._settled and (state is None or state.cancellation is None):
                task.cancel()
        await asyncio.gather(*(task for _turn_id, task in tasks), return_exceptions=True)
        cancellations = [
            state.cancellation for state in self._turns.values() if state.cancellation is not None
        ]
        await asyncio.gather(*cancellations, return_exceptions=True)
