"""Record user-visible conversation and bus delivery content."""

from __future__ import annotations

from collections.abc import Sequence
from contextvars import ContextVar
from typing import TYPE_CHECKING, Literal, TypedDict

from ductor_bot.bus.cron_sanitize import sanitize_cron_result_text
from ductor_bot.bus.envelope import DeliveryMode, Envelope, Origin
from ductor_bot.chat_log.events import EventHub
from ductor_bot.chat_log.history import Entry, HistoryStore
from ductor_bot.chat_log.ids import new_id, to_tid
from ductor_bot.session.key import SessionKey

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import _MessageDispatch
    from ductor_bot.orchestrator.registry import OrchestratorResult

ChatLogSource = Literal["telegram", "web", "api"]
TurnStatus = Literal["ok", "aborted", "error"]


class ChatLogTurn(TypedDict):
    turn_id: str
    user_entry_id: str
    reply_entry_id: str


chat_log_source: ContextVar[ChatLogSource | None] = ContextVar("chat_log_source", default=None)
chat_log_turn: ContextVar[ChatLogTurn | None] = ContextVar("chat_log_turn", default=None)

_BUS_KINDS = {
    Origin.TASK_RESULT: "task_result",
    Origin.TASK_QUESTION: "task_question",
    Origin.HEARTBEAT: "heartbeat",
    Origin.INTERAGENT: "interagent",
    Origin.CRON: "cron",
    Origin.WEBHOOK_WAKE: "webhook",
    Origin.WEBHOOK_CRON: "webhook",
    Origin.BACKGROUND: "background",
}


class HistoryRecorder:
    def __init__(
        self,
        history: HistoryStore,
        events: EventHub,
        allowed_user_ids: Sequence[int] = (),
    ) -> None:
        self._history = history
        self._events = events
        self._allowed_user_ids = tuple(allowed_user_ids)
        self.blocked: set[str] = set()

    @staticmethod
    def _source(dispatch: _MessageDispatch) -> ChatLogSource:
        return chat_log_source.get() or (
            "api" if dispatch.transport_override == "api" else "telegram"
        )

    def _busy(self, dispatch: _MessageDispatch, *, busy: bool) -> None:
        self._events.publish(
            {
                "type": "busy",
                "tid": to_tid(dispatch.key),
                "busy": busy,
                "source": self._source(dispatch),
            }
        )

    async def begin(self, dispatch: _MessageDispatch) -> None:
        if dispatch.key.transport != "tg":
            return
        self._busy(dispatch, busy=True)
        # Silent internal prompts still produce user-visible replies and busy events.
        if not dispatch.counts_as_activity:
            return
        source = self._source(dispatch)
        turn = chat_log_turn.get() if source == "web" else None
        await self._history.append(
            dispatch.key,
            Entry(
                role="user",
                source=source,
                text=dispatch.text,
                id=turn["user_entry_id"] if turn else new_id(),
                turn_id=turn["turn_id"] if turn else None,
            ),
        )

    async def finish(
        self,
        dispatch: _MessageDispatch,
        result: OrchestratorResult | None,
        *,
        status: TurnStatus = "ok",
    ) -> None:
        if dispatch.key.transport != "tg":
            return
        try:
            if result is not None and result.text:
                source = self._source(dispatch)
                turn = chat_log_turn.get() if source == "web" else None
                await self._history.append(
                    dispatch.key,
                    Entry(
                        role="bot",
                        source=source,
                        text=result.text,
                        id=turn["reply_entry_id"] if turn else new_id(),
                        turn_id=turn["turn_id"] if turn else None,
                        status=status if source == "web" else None,
                    ),
                )
        finally:
            self._busy(dispatch, busy=False)

    async def on_delivered(self, envelope: Envelope) -> None:
        kind = _BUS_KINDS.get(envelope.origin)
        if envelope.transport != "tg" or kind is None:
            return
        key = SessionKey.telegram(envelope.chat_id, envelope.topic_id)
        if envelope.delivery == DeliveryMode.BROADCAST and envelope.chat_id == 0:
            if not self._allowed_user_ids:
                return
            key = SessionKey.telegram(self._allowed_user_ids[0])
        title = envelope.metadata.get("name") or envelope.metadata.get("title")
        if envelope.origin == Origin.TASK_QUESTION:
            title = envelope.metadata.get("task_id")
        entry_id = envelope.metadata.get("chat_log_entry_id") or new_id()
        status = (
            "error"
            if envelope.origin == Origin.INTERAGENT and envelope.is_error
            else envelope.status
        )
        text = envelope.result_text
        if envelope.origin == Origin.TASK_RESULT and not envelope.needs_injection:
            text = ""
        elif envelope.origin == Origin.TASK_QUESTION:
            text = envelope.prompt
        elif envelope.origin == Origin.CRON:
            text = sanitize_cron_result_text(text)
        if to_tid(key) in self.blocked:
            return
        await self._history.append(
            key,
            Entry(
                role="event",
                source="system",
                kind=kind,
                text=text,
                status=status,
                title=title,
                id=entry_id,
            ),
        )
        if (
            envelope.origin == Origin.TASK_QUESTION
            and envelope.result_text
            and to_tid(key) not in self.blocked
        ):
            await self._history.append(
                key,
                Entry(
                    role="bot",
                    source="system",
                    kind=kind,
                    text=envelope.result_text,
                    status=status,
                    title=title,
                    id=envelope.metadata.get("chat_log_reply_entry_id") or new_id(),
                ),
            )
