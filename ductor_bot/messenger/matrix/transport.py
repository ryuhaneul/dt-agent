"""Matrix delivery adapter for the MessageBus.

Translates :class:`Envelope` instances into Matrix messages, mirroring
the structure of ``bus/telegram_transport.py``.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from ductor_bot.bus.cron_sanitize import sanitize_cron_result_text
from ductor_bot.bus.envelope import IA_CHUNK_SCHEMES, Envelope, Origin
from ductor_bot.bus.notice_text import (
    background_text,
    cron_text,
    interagent_failed_text,
    task_question_note,
    task_result_note,
    webhook_cron_text,
)
from ductor_bot.i18n import t
from ductor_bot.messenger.matrix.sender import MatrixSendOpts
from ductor_bot.messenger.matrix.sender import send_rich as matrix_send_rich
from ductor_bot.messenger.matrix.sender import send_rich_checked as matrix_send_rich_checked
from ductor_bot.messenger.matrix.sender import (
    send_rich_checked_progress as matrix_send_rich_checked_progress,
)
from ductor_bot.messenger.matrix.typing import MatrixTypingContext

if TYPE_CHECKING:
    from ductor_bot.messenger.matrix.bot import MatrixBot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transport adapter
# ---------------------------------------------------------------------------


class MatrixTransport:
    """Implements the ``TransportAdapter`` protocol for Matrix delivery."""

    def __init__(self, bot: MatrixBot) -> None:
        self._bot = bot

    # -- Protocol methods ---------------------------------------------------

    @property
    def transport_name(self) -> str:
        return "mx"

    @asynccontextmanager
    async def injection_typing(self, envelope: Envelope) -> AsyncIterator[None]:
        """Show typing when the envelope's Matrix room is known."""
        room_id = self._resolve_room(envelope)
        if room_id:
            async with MatrixTypingContext(self._bot.client, room_id):
                yield
        else:
            yield

    async def deliver(self, envelope: Envelope) -> None:
        """Deliver a unicast envelope to the target room."""
        handler = _HANDLERS.get(envelope.origin)
        if handler is not None:
            await handler(self, envelope)
        else:
            logger.warning("No handler for origin=%s", envelope.origin.value)

    async def deliver_broadcast(self, envelope: Envelope) -> None:
        """Deliver an envelope to all allowed rooms."""
        handler = _BROADCAST_HANDLERS.get(envelope.origin)
        if handler is not None:
            await handler(self, envelope)
        else:
            logger.warning("No broadcast handler for origin=%s", envelope.origin.value)

    # -- Internal helpers ---------------------------------------------------

    def _resolve_room(self, env: Envelope) -> str | None:
        """Resolve envelope chat_id back to Matrix room_id."""
        return self._bot.id_map.int_to_room(env.chat_id)

    def _opts(self, env: Envelope) -> MatrixSendOpts:
        orch = self._bot.orchestrator
        roots = self._bot.file_roots(orch.paths) if orch else None
        return MatrixSendOpts(allowed_roots=roots)

    # -- Origin handlers (unicast) -----------------------------------------

    async def _deliver_background(self, env: Envelope) -> None:
        room_id = self._resolve_room(env)
        if not room_id:
            return
        text = background_text(env)
        await matrix_send_rich(self._bot.client, room_id, text, self._opts(env))

    async def _deliver_heartbeat(self, env: Envelope) -> None:
        room_id = self._resolve_room(env)
        if room_id and env.result_text:
            await matrix_send_rich(self._bot.client, room_id, env.result_text, self._opts(env))

    async def _deliver_interagent(self, env: Envelope) -> None:
        env.chunk_scheme = IA_CHUNK_SCHEMES["mx"]
        if env.topic_id is not None:
            env.delivered = False
            env.delivery_error = "topic unsupported"
            return
        room_id = self._resolve_room(env)
        if not room_id:
            env.delivered = False
            env.delivery_error = "matrix room unavailable"
            return
        if env.is_error:
            text = interagent_failed_text(env)
            env.delivered_text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
            (
                env.delivered,
                _event_id,
                env.delivered_chunks,
            ) = await matrix_send_rich_checked_progress(
                self._bot.client, room_id, text, start_chunk=env.start_chunk
            )
            if not env.delivered:
                env.delivery_error = "matrix send failed"
            return

        await self._deliver_provider_notice(env, room_id)
        if env.result_text.strip():
            report_sent, _event_id, env.delivered_chunks = await matrix_send_rich_checked_progress(
                self._bot.client,
                room_id,
                env.result_text,
                start_chunk=env.start_chunk,
            )
            env.delivered = report_sent
        else:
            env.delivered = False
        if not env.delivered:
            env.delivery_error = "matrix send failed"

    async def _deliver_provider_notice(self, env: Envelope, room_id: str) -> None:
        notice = env.metadata.get("provider_switch_notice", "")
        if notice:
            try:
                notice_sent, _event_id = await matrix_send_rich_checked(
                    self._bot.client,
                    room_id,
                    t("notice.provider_switch", notice=notice),
                )
            except Exception as exc:
                logger.warning("Provider switch notice failed: %s", type(exc).__name__)
            else:
                if not notice_sent:
                    logger.warning("Provider switch notice was not delivered")

    async def _deliver_task_result(self, env: Envelope) -> None:
        room_id = self._resolve_room(env)
        if not room_id:
            return
        note = task_result_note(env)
        if note:
            await matrix_send_rich(self._bot.client, room_id, note)
        if env.needs_injection and env.result_text:
            await matrix_send_rich(self._bot.client, room_id, env.result_text)

    async def _deliver_task_question(self, env: Envelope) -> None:
        room_id = self._resolve_room(env)
        if not room_id:
            return
        note = task_question_note(env)
        await matrix_send_rich(self._bot.client, room_id, note)
        if env.result_text:
            await matrix_send_rich(self._bot.client, room_id, env.result_text)

    async def _deliver_webhook_wake(self, env: Envelope) -> None:
        room_id = self._resolve_room(env)
        if room_id and env.result_text:
            await matrix_send_rich(self._bot.client, room_id, env.result_text)

    async def _deliver_cron(self, env: Envelope) -> None:
        """Deliver cron result to a specific room (unicast).

        Falls back to broadcast when the target room cannot be resolved.
        """
        room_id = self._resolve_room(env)
        if not room_id:
            logger.warning(
                "Cron unicast: cannot resolve chat_id=%d, falling back to broadcast",
                env.chat_id,
            )
            await self._broadcast_cron(env)
            return
        clean_result = sanitize_cron_result_text(env.result_text)
        if env.result_text and not clean_result and env.status == "success":
            return
        await matrix_send_rich(self._bot.client, room_id, cron_text(env, clean_result))

    # -- Origin handlers (broadcast) ----------------------------------------

    async def _broadcast_cron(self, env: Envelope) -> None:
        clean_result = sanitize_cron_result_text(env.result_text)
        if env.result_text and not clean_result and env.status == "success":
            return
        await self._broadcast(cron_text(env, clean_result))

    async def _broadcast_interagent(self, env: Envelope) -> None:
        """Send the injected report to the same rooms as a cron broadcast."""
        from ductor_bot.messenger.matrix.bot import resolve_broadcast_rooms

        # A single offset cannot represent multiple recipient deliveries.
        env.start_chunk = 0
        env.delivered_chunks = 0
        env.chunk_scheme = ""
        rooms = resolve_broadcast_rooms(self._bot.config, self._bot._last_active_room)
        if not rooms or not env.result_text.strip():
            env.delivered = False
            env.delivery_error = "matrix broadcast has no recipients or report"
            return
        all_sent = True
        opts = self._opts(env)
        for room_id in rooms:
            try:
                sent, _event_id, _chunks = await matrix_send_rich_checked_progress(
                    self._bot.client,
                    room_id,
                    env.result_text,
                    opts,
                    start_chunk=0,
                )
            except Exception as exc:
                logger.warning("Matrix IA broadcast recipient failed: %s", type(exc).__name__)
                sent = False
            all_sent = all_sent and sent
        env.delivered = all_sent
        if not all_sent:
            env.delivery_error = "matrix broadcast send failed"

    async def _broadcast_webhook_cron(self, env: Envelope) -> None:
        await self._broadcast(webhook_cron_text(env))

    async def _broadcast(self, text: str) -> None:
        """Send to all allowed rooms (falls back to last active room)."""
        from ductor_bot.messenger.matrix.bot import resolve_broadcast_rooms

        rooms = resolve_broadcast_rooms(self._bot.config, self._bot._last_active_room)
        if not rooms:
            logger.warning("_broadcast: no rooms available, message lost: %s", text[:80])
            return
        for room_id in rooms:
            await matrix_send_rich(self._bot.client, room_id, text)


# ---------------------------------------------------------------------------
# Handler dispatch tables
# ---------------------------------------------------------------------------

_Handler = Callable[[MatrixTransport, Envelope], Awaitable[None]]

_HANDLERS: dict[Origin, _Handler] = {
    Origin.BACKGROUND: MatrixTransport._deliver_background,
    Origin.CRON: MatrixTransport._deliver_cron,
    Origin.HEARTBEAT: MatrixTransport._deliver_heartbeat,
    Origin.INTERAGENT: MatrixTransport._deliver_interagent,
    Origin.TASK_RESULT: MatrixTransport._deliver_task_result,
    Origin.TASK_QUESTION: MatrixTransport._deliver_task_question,
    Origin.WEBHOOK_WAKE: MatrixTransport._deliver_webhook_wake,
}

_BROADCAST_HANDLERS: dict[Origin, _Handler] = {
    Origin.INTERAGENT: MatrixTransport._broadcast_interagent,
    Origin.CRON: MatrixTransport._broadcast_cron,
    Origin.WEBHOOK_CRON: MatrixTransport._broadcast_webhook_cron,
}
