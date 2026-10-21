"""Slack delivery adapter for the MessageBus."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
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
from ductor_bot.messenger.slack.sender import SlackSendOpts
from ductor_bot.messenger.slack.sender import send_rich as slack_send_rich
from ductor_bot.messenger.slack.sender import send_rich_checked as slack_send_rich_checked
from ductor_bot.messenger.slack.sender import (
    send_rich_checked_progress as slack_send_rich_checked_progress,
)

if TYPE_CHECKING:
    from ductor_bot.messenger.slack.bot import SlackBot

logger = logging.getLogger(__name__)


class SlackTransport:
    """Implements the transport adapter protocol for Slack delivery."""

    def __init__(self, bot: SlackBot) -> None:
        self._bot = bot

    @property
    def transport_name(self) -> str:
        return "sl"

    async def deliver(self, envelope: Envelope) -> None:
        handler = _HANDLERS.get(envelope.origin)
        if handler is None:
            logger.warning("No handler for origin=%s", envelope.origin.value)
            return
        await handler(self, envelope)

    async def deliver_broadcast(self, envelope: Envelope) -> None:
        handler = _BROADCAST_HANDLERS.get(envelope.origin)
        if handler is None:
            logger.warning("No broadcast handler for origin=%s", envelope.origin.value)
            return
        await handler(self, envelope)

    def _resolve_target(self, env: Envelope) -> tuple[str | None, str | None]:
        channel_id = self._bot.id_map.int_to_channel(env.chat_id)
        if channel_id is None:
            return None, None
        if env.topic_id is None:
            return channel_id, None
        thread = self._bot.id_map.int_to_thread(env.topic_id)
        if thread is None:
            return channel_id, None
        thread_channel, thread_ts = thread
        if thread_channel != channel_id:
            logger.warning(
                "Slack topic mapping mismatch for chat_id=%s topic_id=%s", env.chat_id, env.topic_id
            )
            return channel_id, None
        return channel_id, thread_ts

    def _opts(self, env: Envelope) -> SlackSendOpts:
        channel_id, thread_ts = self._resolve_target(env)
        del channel_id
        orch = self._bot.orchestrator
        roots = self._bot.file_roots(orch.paths) if orch else None
        return SlackSendOpts(allowed_roots=roots, thread_ts=thread_ts)

    async def _deliver_background(self, env: Envelope) -> None:
        channel_id, _thread_ts = self._resolve_target(env)
        if not channel_id:
            return
        text = background_text(env)
        await slack_send_rich(self._bot.client, channel_id, text, self._opts(env))

    async def _deliver_heartbeat(self, env: Envelope) -> None:
        channel_id, _thread_ts = self._resolve_target(env)
        if channel_id and env.result_text:
            await slack_send_rich(self._bot.client, channel_id, env.result_text, self._opts(env))

    async def _deliver_interagent(self, env: Envelope) -> None:
        env.chunk_scheme = IA_CHUNK_SCHEMES["sl"]
        channel_id, thread_ts = self._resolve_target(env)
        if env.topic_id is not None and thread_ts is None:
            env.delivered = False
            env.delivery_error = "topic unresolved"
            return
        if not channel_id:
            env.delivered = False
            env.delivery_error = "slack channel unavailable"
            return
        if env.is_error:
            text = interagent_failed_text(env)
            env.delivered_text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
            env.delivered, _last_ts, env.delivered_chunks = await slack_send_rich_checked_progress(
                self._bot.client,
                channel_id,
                text,
                self._opts(env),
                start_chunk=env.start_chunk,
            )
            if not env.delivered:
                env.delivery_error = "slack send failed"
            return

        await self._deliver_provider_notice(env, channel_id)
        if env.result_text.strip():
            report_sent, _last_ts, env.delivered_chunks = await slack_send_rich_checked_progress(
                self._bot.client,
                channel_id,
                env.result_text,
                self._opts(env),
                start_chunk=env.start_chunk,
            )
            env.delivered = report_sent
        else:
            env.delivered = False
        if not env.delivered:
            env.delivery_error = "slack send failed"

    async def _deliver_provider_notice(self, env: Envelope, channel_id: str) -> None:
        notice = env.metadata.get("provider_switch_notice", "")
        if notice:
            try:
                notice_sent, _last_ts = await slack_send_rich_checked(
                    self._bot.client,
                    channel_id,
                    t("notice.provider_switch", notice=notice),
                    self._opts(env),
                )
            except Exception as exc:
                logger.warning("Provider switch notice failed: %s", type(exc).__name__)
            else:
                if not notice_sent:
                    logger.warning("Provider switch notice was not delivered")

    async def _deliver_task_result(self, env: Envelope) -> None:
        channel_id, _thread_ts = self._resolve_target(env)
        if not channel_id:
            return
        note = task_result_note(env)
        if note:
            await slack_send_rich(self._bot.client, channel_id, note, self._opts(env))
        if env.needs_injection and env.result_text:
            await slack_send_rich(self._bot.client, channel_id, env.result_text, self._opts(env))

    async def _deliver_task_question(self, env: Envelope) -> None:
        channel_id, _thread_ts = self._resolve_target(env)
        if not channel_id:
            return
        note = task_question_note(env)
        await slack_send_rich(self._bot.client, channel_id, note, self._opts(env))
        if env.result_text:
            await slack_send_rich(self._bot.client, channel_id, env.result_text, self._opts(env))

    async def _deliver_webhook_wake(self, env: Envelope) -> None:
        channel_id, _thread_ts = self._resolve_target(env)
        if channel_id and env.result_text:
            await slack_send_rich(self._bot.client, channel_id, env.result_text, self._opts(env))

    async def _deliver_cron(self, env: Envelope) -> None:
        channel_id, _thread_ts = self._resolve_target(env)
        if not channel_id:
            logger.warning(
                "Slack cron unicast: cannot resolve chat_id=%d, falling back to broadcast",
                env.chat_id,
            )
            await self._broadcast_cron(env)
            return
        clean_result = sanitize_cron_result_text(env.result_text)
        if env.result_text and not clean_result and env.status == "success":
            return
        text = cron_text(env, clean_result)
        await slack_send_rich(self._bot.client, channel_id, text, self._opts(env))

    async def _broadcast_cron(self, env: Envelope) -> None:
        clean_result = sanitize_cron_result_text(env.result_text)
        if env.result_text and not clean_result and env.status == "success":
            return
        await self._bot.broadcast(cron_text(env, clean_result))

    async def _broadcast_interagent(self, env: Envelope) -> None:
        """Send the injected report to the same channels as a cron broadcast."""
        # A single offset cannot represent multiple recipient deliveries.
        env.start_chunk = 0
        env.delivered_chunks = 0
        env.chunk_scheme = ""
        channels = self._bot._broadcast_channels()
        if not channels or not env.result_text.strip():
            logger.warning("Slack IA broadcast has no channels or report")
            env.delivered = False
            env.delivery_error = "slack broadcast has no recipients or report"
            return
        all_sent = True
        opts = SlackSendOpts(allowed_roots=self._opts(env).allowed_roots)
        for channel_id in channels:
            try:
                sent, _last_ts, _chunks = await slack_send_rich_checked_progress(
                    self._bot.client,
                    channel_id,
                    env.result_text,
                    opts,
                    start_chunk=0,
                )
            except Exception as exc:
                logger.warning("Slack IA broadcast recipient failed: %s", type(exc).__name__)
                sent = False
            all_sent = all_sent and sent
        env.delivered = all_sent
        if not all_sent:
            env.delivery_error = "slack broadcast send failed"

    async def _broadcast_webhook_cron(self, env: Envelope) -> None:
        await self._bot.broadcast(webhook_cron_text(env))


_Handler = Callable[[SlackTransport, Envelope], Awaitable[None]]

_HANDLERS: dict[Origin, _Handler] = {
    Origin.BACKGROUND: SlackTransport._deliver_background,
    Origin.CRON: SlackTransport._deliver_cron,
    Origin.HEARTBEAT: SlackTransport._deliver_heartbeat,
    Origin.INTERAGENT: SlackTransport._deliver_interagent,
    Origin.TASK_RESULT: SlackTransport._deliver_task_result,
    Origin.TASK_QUESTION: SlackTransport._deliver_task_question,
    Origin.WEBHOOK_WAKE: SlackTransport._deliver_webhook_wake,
}

_BROADCAST_HANDLERS: dict[Origin, _Handler] = {
    Origin.INTERAGENT: SlackTransport._broadcast_interagent,
    Origin.CRON: SlackTransport._broadcast_cron,
    Origin.WEBHOOK_CRON: SlackTransport._broadcast_webhook_cron,
}
