"""Telegram delivery adapter for the MessageBus.

Translates :class:`Envelope` instances into Telegram messages using
the formatting logic that was previously spread across
``bot/result_delivery.py``.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.bus.cron_sanitize import sanitize_cron_result_text
from ductor_bot.bus.envelope import IA_CHUNK_SCHEMES, Envelope, Origin
from ductor_bot.bus.failure_reason import display_reason
from ductor_bot.i18n import t
from ductor_bot.messenger.telegram.sender import (
    SendRichOpts,
    send_rich,
    send_rich_checked_progress,
)
from ductor_bot.messenger.telegram.typing import TypingContext
from ductor_bot.session.named import is_interagent_session_name
from ductor_bot.text.response_format import (
    SEP,
    fmt,
    show_run_result,
    show_run_status,
    show_task_error,
)

if TYPE_CHECKING:
    from ductor_bot.messenger.telegram.app import TelegramBot

logger = logging.getLogger(__name__)


def _target_label(chat_id: int, topic_id: int | None) -> str:
    """Where a delivery was meant to go, for the fallback notices."""
    if topic_id:
        return t("notice.target_topic", chat=chat_id, topic=topic_id)
    return t("notice.target", chat=chat_id)


def _cron_body(status: str, result: str) -> str:
    """The result of a cron or webhook run for the chat; its status when it has no text."""
    return show_run_result(status, result) if result else f"_{show_run_status(status)}_"


# ---------------------------------------------------------------------------
# Transport adapter
# ---------------------------------------------------------------------------


class TelegramTransport:
    """Implements the ``TransportAdapter`` protocol for Telegram delivery."""

    def __init__(self, bot: TelegramBot) -> None:
        self._bot = bot

    # -- Protocol methods ---------------------------------------------------

    @property
    def transport_name(self) -> str:
        return "tg"

    def injection_typing(self, envelope: Envelope) -> TypingContext:
        """Show typing in the conversation receiving an injected result."""
        return TypingContext(
            self._bot.bot_instance,
            envelope.chat_id,
            thread_id=envelope.topic_id,
        )

    async def deliver(self, envelope: Envelope) -> None:
        """Deliver a unicast envelope to the target chat_id."""
        handler = _HANDLERS.get(envelope.origin)
        if handler is not None:
            await handler(self, envelope)
        else:
            logger.warning("No handler for origin=%s", envelope.origin.value)

    async def deliver_broadcast(self, envelope: Envelope) -> None:
        """Deliver an envelope to all allowed users."""
        handler = _BROADCAST_HANDLERS.get(envelope.origin)
        if handler is not None:
            await handler(self, envelope)
        else:
            logger.warning("No broadcast handler for origin=%s", envelope.origin.value)

    # -- Internal helpers ---------------------------------------------------

    def _roots(self) -> list[Path] | None:
        return self._bot.file_roots(self._bot._orch.paths)

    def _opts(self, envelope: Envelope) -> SendRichOpts:
        return SendRichOpts(
            reply_to_message_id=envelope.reply_to_message_id,
            allowed_roots=self._roots(),
            thread_id=envelope.topic_id or envelope.thread_id,
        )

    # -- Origin handlers (unicast) -----------------------------------------

    async def _deliver_background(self, env: Envelope) -> None:
        """Deliver background session / stateless task result."""
        elapsed = t("age.s", n=f"{env.elapsed_seconds:.0f}")

        if env.session_name:
            # Update named session registry. Inter-agent sessions were already stored by the
            # background observer while the session was still protected by its gate.
            if not is_interagent_session_name(env.session_name):
                self._bot._orch.named_sessions.update_after_response(
                    env.chat_id, env.session_name, env.session_id
                )
            text = self._format_named_session(env, elapsed)
            from ductor_bot.messenger.telegram.buttons import extract_buttons_for_session

            cleaned, markup = extract_buttons_for_session(text, env.session_name)
            opts = self._opts(env)
            opts.reply_markup = markup
            await send_rich(self._bot.bot_instance, env.chat_id, cleaned, opts)
        else:
            text = self._format_stateless(env, elapsed)
            await send_rich(self._bot.bot_instance, env.chat_id, text, self._opts(env))

    @staticmethod
    def _format_named_session(env: Envelope, elapsed: str) -> str:
        name = env.session_name
        if env.status == "aborted":
            return fmt(t("notice.session_cancelled", name=name), SEP, f"_{env.prompt_preview}_")
        if env.is_error:
            body = (
                show_run_result(env.status, env.result_text[:2000])
                if env.result_text
                else t("notice.no_output")
            )
            return fmt(t("notice.session_failed", name=name, elapsed=elapsed), SEP, body)
        return fmt(
            t("notice.session_complete", name=name, elapsed=elapsed),
            SEP,
            env.result_text or t("notice.no_output"),
        )

    @staticmethod
    def _format_stateless(env: Envelope, elapsed: str) -> str:
        task_id = env.metadata.get("task_id", "?")
        if env.status == "aborted":
            return fmt(
                t("notice.bg_cancelled"),
                SEP,
                t("notice.bg_cancelled_body", task_id=task_id, prompt=env.prompt_preview),
            )
        if env.is_error:
            output = (
                show_run_result(env.status, env.result_text[:2000])
                if env.result_text
                else t("notice.no_output")
            )
            return fmt(
                t("notice.bg_failed", elapsed=elapsed),
                SEP,
                t(
                    "notice.bg_failed_body",
                    task_id=task_id,
                    status=show_run_status(env.status),
                    prompt=env.prompt_preview,
                )
                + f"\n\n{output}",
            )
        return fmt(
            t("notice.bg_complete", elapsed=elapsed),
            SEP,
            env.result_text or t("notice.no_output"),
        )

    async def _deliver_heartbeat(self, env: Envelope) -> None:
        """Deliver heartbeat to chat/topic. Falls back to main user on failure."""
        from aiogram.exceptions import TelegramAPIError

        opts = SendRichOpts(allowed_roots=self._roots(), thread_id=env.topic_id)
        try:
            await send_rich(self._bot.bot_instance, env.chat_id, env.result_text, opts)
            logger.info("Heartbeat delivered")
        except TelegramAPIError:
            logger.warning(
                "Heartbeat delivery failed for chat %s topic %s, falling back to main user",
                env.chat_id,
                env.topic_id,
            )
            fallback_text = t(
                "notice.heartbeat_failed",
                target=_target_label(env.chat_id, env.topic_id),
                text=env.result_text,
            )
            fallback_id = self._bot._config.allowed_user_ids[0]
            await send_rich(
                self._bot.bot_instance,
                fallback_id,
                fallback_text,
                SendRichOpts(allowed_roots=self._roots()),
            )

    async def _deliver_interagent(self, env: Envelope) -> None:
        """Deliver inter-agent result (error notification or injected response)."""
        env.chunk_scheme = IA_CHUNK_SCHEMES["tg"]
        opts = self._opts(env)
        opts.ia_task_id = env.metadata.get("ia_task_id", "")

        if env.is_error:
            session_info = t("notice.ia_session", name=env.session_name) if env.session_name else ""
            error_text = t(
                "notice.ia_failed",
                agent=env.metadata.get("recipient", "?"),
                session=session_info,
                error=(
                    display_reason(str(env.metadata["error"]))
                    if "error" in env.metadata
                    else t("notice.unknown")
                ),
                request=env.prompt_preview,
            )
            env.delivered_text_sha256 = hashlib.sha256(error_text.encode("utf-8")).hexdigest()
            env.delivered, env.delivered_chunks = await send_rich_checked_progress(
                self._bot.bot_instance,
                env.chat_id,
                error_text,
                opts,
                start_chunk=env.start_chunk,
            )
            if not env.delivered:
                env.delivery_error = "telegram send failed"
            return

        # Provider switch notice (before result)
        notice = env.metadata.get("provider_switch_notice", "")
        if notice:
            try:
                notice_sent = bool(
                    await send_rich(
                        self._bot.bot_instance,
                        env.chat_id,
                        t("notice.provider_switch", notice=notice),
                        opts,
                    )
                )
            except Exception as exc:
                logger.warning("Provider switch notice failed: %s", type(exc).__name__)
            else:
                if not notice_sent:
                    logger.warning("Provider switch notice was not delivered")

        # Result text (filled by bus injection)
        if env.result_text.strip():
            env.delivered, env.delivered_chunks = await send_rich_checked_progress(
                self._bot.bot_instance,
                env.chat_id,
                env.result_text,
                opts,
                start_chunk=env.start_chunk,
            )
        else:
            env.delivered = False
        if not env.delivered:
            env.delivery_error = "telegram send failed"

    async def _deliver_task_result(self, env: Envelope) -> None:
        """Deliver task result notification + injected response."""
        opts = self._opts(env)
        name = env.metadata.get("name", env.metadata.get("task_id", "?"))

        # 1. Notification (skip "waiting" — question already shown)
        note = ""
        if env.status == "done":
            duration = t("age.s", n=f"{env.elapsed_seconds:.0f}")
            target = f"{env.provider}/{env.model}" if env.provider else ""
            detail = f"{duration}, {target}" if target else duration
            note = t("notice.task_done", name=name, detail=detail)
        elif env.status == "cancelled":
            note = t("notice.task_cancelled", name=name)
        elif env.status == "failed":
            reason = (
                show_task_error(str(env.metadata["error"]))
                if "error" in env.metadata
                else t("notice.unknown")
            )
            note = t("notice.task_failed", name=name, reason=reason)

        if note:
            await send_rich(self._bot.bot_instance, env.chat_id, note, opts)

        # 2. Injected response (filled by bus injection for done/failed)
        if env.needs_injection and env.result_text:
            await send_rich(self._bot.bot_instance, env.chat_id, env.result_text, opts)

    async def _deliver_task_question(self, env: Envelope) -> None:
        """Deliver task question notification + injected agent response."""
        opts = self._opts(env)
        task_id = env.metadata.get("task_id", "?")

        # 1. Notification
        note = t("notice.task_question", task_id=task_id, question=env.prompt)
        await send_rich(self._bot.bot_instance, env.chat_id, note, opts)

        # 2. Agent response (filled by bus injection)
        if env.result_text:
            await send_rich(self._bot.bot_instance, env.chat_id, env.result_text, opts)

    async def _deliver_webhook_wake(self, env: Envelope) -> None:
        """Deliver webhook wake result."""
        if env.result_text:
            await send_rich(self._bot.bot_instance, env.chat_id, env.result_text, self._opts(env))

    async def _deliver_cron(self, env: Envelope) -> None:
        """Deliver cron result to a specific chat/topic (unicast).

        Falls back to the main agent when delivery fails.
        """
        from aiogram.exceptions import TelegramAPIError

        title = env.metadata.get("title", "?")
        clean_result = sanitize_cron_result_text(env.result_text)
        if env.result_text and not clean_result and env.status == "success":
            logger.debug(
                "Cron result only had transport confirmations; skipping unicast task=%s",
                title,
            )
            return
        shown = _cron_body(env.status, clean_result)
        text = f"{t('notice.task_header', title=title)}\n\n{shown}"
        opts = SendRichOpts(
            allowed_roots=self._roots(),
            thread_id=env.topic_id,
        )
        try:
            delivered = await send_rich(self._bot.bot_instance, env.chat_id, text, opts)
        except TelegramAPIError as exc:
            logger.warning(
                "Cron '%s' delivery failed for chat %d, falling back to main agent",
                title,
                env.chat_id,
            )
            env.delivered = False
            env.delivery_error = type(exc).__name__
            fallback_text = t(
                "notice.cron_delivery_failed",
                title=title,
                target=_target_label(env.chat_id, env.topic_id),
                text=shown,
            )
            fallback_id = self._bot._config.allowed_user_ids[0]
            await send_rich(
                self._bot.bot_instance,
                fallback_id,
                fallback_text,
                SendRichOpts(allowed_roots=self._roots()),
            )
        else:
            if not delivered:
                # send_rich swallowed a network error (#160) — record the miss
                # so the cron observer can persist the result for resend.
                env.delivered = False
                env.delivery_error = "TelegramNetworkError"

    # -- Origin handlers (broadcast) ----------------------------------------

    async def _broadcast_cron(self, env: Envelope) -> None:
        title = env.metadata.get("title", "?")
        clean_result = sanitize_cron_result_text(env.result_text)
        if env.result_text and not clean_result and env.status == "success":
            logger.debug(
                "Cron result only had transport confirmations; skipping broadcast task=%s", title
            )
            return
        text = f"{t('notice.task_header', title=title)}\n\n{_cron_body(env.status, clean_result)}"
        await self._bot.broadcast(text, SendRichOpts(allowed_roots=self._roots()))

    async def _broadcast_interagent(self, env: Envelope) -> None:
        """Send the injected report to the same users as a cron broadcast."""
        # A single offset cannot represent multiple recipient deliveries.
        env.start_chunk = 0
        env.delivered_chunks = 0
        env.chunk_scheme = ""
        recipients = self._bot._config.allowed_user_ids
        if not recipients or not env.result_text.strip():
            env.delivered = False
            env.delivery_error = "telegram broadcast has no recipients or report"
            return
        opts = SendRichOpts(
            allowed_roots=self._roots(),
            ia_task_id=env.metadata.get("ia_task_id", ""),
        )
        all_sent = True
        for chat_id in recipients:
            try:
                sent, _chunks = await send_rich_checked_progress(
                    self._bot.bot_instance,
                    chat_id,
                    env.result_text,
                    opts,
                    start_chunk=0,
                )
            except Exception as exc:
                logger.warning("Telegram IA broadcast recipient failed: %s", type(exc).__name__)
                sent = False
            all_sent = all_sent and sent
        env.delivered = all_sent
        if not all_sent:
            env.delivery_error = "telegram broadcast send failed"

    async def _broadcast_webhook_cron(self, env: Envelope) -> None:
        title = env.metadata.get("hook_title", "?")
        header = t("notice.webhook_header", title=title)
        text = f"{header}\n\n{_cron_body(env.status, env.result_text)}"
        await self._bot.broadcast(text, SendRichOpts(allowed_roots=self._roots()))


# ---------------------------------------------------------------------------
# Handler dispatch tables
# ---------------------------------------------------------------------------

_Handler = Callable[[TelegramTransport, Envelope], Awaitable[None]]

_HANDLERS: dict[Origin, _Handler] = {
    Origin.BACKGROUND: TelegramTransport._deliver_background,
    Origin.CRON: TelegramTransport._deliver_cron,
    Origin.HEARTBEAT: TelegramTransport._deliver_heartbeat,
    Origin.INTERAGENT: TelegramTransport._deliver_interagent,
    Origin.TASK_RESULT: TelegramTransport._deliver_task_result,
    Origin.TASK_QUESTION: TelegramTransport._deliver_task_question,
    Origin.WEBHOOK_WAKE: TelegramTransport._deliver_webhook_wake,
}

_BROADCAST_HANDLERS: dict[Origin, _Handler] = {
    Origin.INTERAGENT: TelegramTransport._broadcast_interagent,
    Origin.CRON: TelegramTransport._broadcast_cron,
    Origin.WEBHOOK_CRON: TelegramTransport._broadcast_webhook_cron,
}
