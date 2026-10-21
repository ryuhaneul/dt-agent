"""Telegram bot middleware: auth filtering and sequential processing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
    TelegramObject,
)

from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.i18n import t
from ductor_bot.infra.input_context import inherit_received_input, set_input_sent_at
from ductor_bot.log_context import set_log_context
from ductor_bot.messenger.queue_context import QueuedMessageLock, message_time
from ductor_bot.messenger.rejection_log import RejectionLog
from ductor_bot.messenger.telegram.abort import (
    is_abort_all_message,
    is_abort_message,
    is_interrupt_message,
)
from ductor_bot.messenger.telegram.dedup import DedupeCache, build_dedup_key
from ductor_bot.messenger.telegram.media import should_drop_in_group
from ductor_bot.messenger.telegram.topic import (
    TopicNameCache,
    get_session_key,
    get_thread_id,
)

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

AbortHandler = Callable[[int, "Message"], Awaitable[bool]]
"""Async callback: (chat_id, message) -> handled?"""

AbortAllHandler = Callable[[int, "Message"], Awaitable[bool]]
"""Async callback for /stop_all: (chat_id, message) -> handled?"""

QuickCommandHandler = Callable[[int, "Message"], Awaitable[bool]]
"""Async callback for read-only commands that bypass the per-chat lock."""

QUICK_COMMANDS: frozenset[str] = frozenset(
    {
        "/status",
        "/memory",
        "/cron",
        "/diagnose",
        "/model",
        "/accounts",
        "/usage",
        "/effort",
        "/showfiles",
        "/sessions",
        "/tasks",
        "/where",
        "/leave",
    }
)

MARK_STARTED_KEY = "mark_processing_started"
"""``data`` key holding the callable that records "processing of this update has started"."""

KEEP_PENDING_KEY = "keep_update_pending"
"""``data`` key set when an update was left unprocessed because the bot is stopping."""

MQ_PREFIX = "mq:"
"""Callback data prefix for message queue cancel buttons."""


def is_quick_command(text: str, bot_username: str | None = None) -> bool:
    """Return True if *text* is a command that can bypass the lock.

    Matches bare commands (``/status``), bot-mentioned commands
    (``/status@my_bot``), and commands with arguments (``/model sonnet``).
    Commands addressed to a different bot (``/status@other_bot``) are rejected.
    """
    cmd_part = text.strip().lower().split(None, 1)[0] if text.strip() else ""
    if "@" in cmd_part:
        cmd, mention = cmd_part.split("@", 1)
        if bot_username and mention != bot_username.lower():
            return False
        return cmd in QUICK_COMMANDS
    return cmd_part in QUICK_COMMANDS


RejectedCallback = Callable[[int, str, str], None]
"""Sync callback for rejected group messages: (chat_id, chat_type, title)."""


class JournalMiddleware(BaseMiddleware):
    """Outermost message middleware: tells the update journal when processing started.

    Inner middleware calls ``data[MARK_STARTED_KEY]()`` at the real start (the per-session
    lock was acquired). Whatever ends without that (rejected, dropped, answered before the
    lock, failed) counts as started when it ends. A cancelled update that never started is
    not marked, so a restart can still replay it.
    """

    def __init__(self, mark_started: Callable[[int], None]) -> None:
        self._mark_started = mark_started

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update = data.get("event_update")
        if update is None:
            return await handler(event, data)
        update_id = update.update_id
        marked = False
        save_failed = False

        def mark() -> None:
            """Move the update to handled. If the save fails nothing may start for it."""
            nonlocal marked, save_failed
            if marked:
                return
            try:
                self._mark_started(update_id)
            except OSError:
                save_failed = True
                logger.warning("Update %d stays pending: the journal could not be saved", update_id)
                raise
            marked = True

        def mark_at_end() -> None:
            # Whatever ended without taking the lock is over; a save that fails here leaves it
            # pending (replayed at the next start). After a failed start nothing is marked, and
            # an update left alone because the bot is stopping stays pending.
            if not save_failed and not data.get(KEEP_PENDING_KEY):
                with contextlib.suppress(OSError):
                    mark()

        data[MARK_STARTED_KEY] = mark
        try:
            result = await handler(event, data)
        except asyncio.CancelledError:
            raise
        except Exception:
            mark_at_end()
            raise
        mark_at_end()
        return result


class AuthMiddleware(BaseMiddleware):
    """Outer middleware: drop unauthorized messages with rate-limited metadata logs.

    In private chats only ``allowed_user_ids`` is checked.
    In group/supergroup chats **both** the group (``allowed_group_ids``)
    and the sender (``allowed_user_ids``) must be allowlisted.
    """

    def __init__(
        self,
        allowed_user_ids: set[int],
        *,
        allowed_group_ids: set[int] | None = None,
        on_rejected: RejectedCallback | None = None,
    ) -> None:
        self._allowed_users = allowed_user_ids
        self._allowed_groups = allowed_group_ids if allowed_group_ids is not None else set()
        self._on_rejected = on_rejected
        self._rejections = RejectionLog(logger)
        self._bot_id: int | None = None

    def set_bot_id(self, bot_id: int | None) -> None:
        """Known after startup: the bot's own messages are not logged as rejections."""
        self._bot_id = bot_id

    @inherit_received_input
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, (Message, CallbackQuery)):
            user = event.from_user
        else:
            return await handler(event, data)

        chat = self._event_chat(event)

        chat_type = chat.type if chat else None
        message = event if isinstance(event, Message) else event.message

        def log_rejection() -> None:
            self._log_rejection(chat_type, chat, user, message)

        if not user:
            log_rejection()
            return None

        if chat_type in ("group", "supergroup"):
            group_id = chat.id if chat else None
            if group_id not in self._allowed_groups:
                log_rejection()
                if self._on_rejected and chat:
                    self._on_rejected(chat.id, chat_type, chat.title or "")
                return None
            if user.id not in self._allowed_users:
                log_rejection()
                return None
        elif user.id not in self._allowed_users:
            log_rejection()
            return None

        self._note_sent_time(event)
        return await handler(event, data)

    @staticmethod
    def _event_chat(event: Message | CallbackQuery) -> Any:
        # Resolve chat: Message.chat directly, CallbackQuery via .message.chat.
        if isinstance(event, Message):
            return event.chat
        if event.message is not None:
            return getattr(event.message, "chat", None)
        return None

    def _log_rejection(self, chat_type: str | None, chat: Any, user: Any, message: Any) -> None:
        if user is not None and self._bot_id is not None and user.id == self._bot_id:
            return  # the bot's own message or service message (topic created/renamed)
        topic_id = get_thread_id(message) if isinstance(message, Message) else None
        self._rejections.log(
            chat_type or "unknown",
            chat.id if chat else None,
            user.id if user else None,
            topic_id,
        )

    @staticmethod
    def _note_sent_time(event: TelegramObject) -> None:
        """Carry the send time of a real user message (text or media) into the receipt.

        A callback's message is the bot's own older message and has no send time of its own.
        """
        if not isinstance(event, Message):
            return
        sent = message_time(getattr(event, "date", None))
        set_input_sent_at(sent.timestamp() if sent is not None else None)


@dataclass(slots=True)
class _QueueEntry:
    """A message waiting behind the per-chat lock."""

    entry_id: int
    chat_id: int
    message_id: int
    text_preview: str
    topic_id: int | None = None
    cancelled: bool = False
    indicator_msg_id: int | None = field(default=None, repr=False)
    mark_started: Callable[[], None] | None = field(default=None, repr=False)


class SequentialMiddleware(BaseMiddleware):
    """Outer middleware: dedup + per-chat lock ensures sequential processing.

    Tracks pending messages per chat so they can be individually cancelled
    (via inline keyboard) or bulk-discarded on ``/stop``.
    """

    def __init__(
        self,
        lock_pool: LockPool | None = None,
        topic_names: TopicNameCache | None = None,
        *,
        group_mention_only: bool = False,
        shutdown_check: Callable[[], bool] | None = None,
    ) -> None:
        self._lock_pool = lock_pool if lock_pool is not None else LockPool()
        self._topic_names = topic_names
        self._dedup = DedupeCache()
        self._interrupt_handler: AbortHandler | None = None
        self._abort_handler: AbortHandler | None = None
        self._abort_all_handler: AbortAllHandler | None = None
        self._quick_command_handler: QuickCommandHandler | None = None
        self._message_received_handler: Callable[[Message], None] | None = None
        self._pending: dict[int, list[_QueueEntry]] = {}
        self._entry_counter = 0
        self._bot: Bot | None = None
        self._bot_id: int | None = None
        self._bot_username: str | None = None
        self._group_mention_only = group_mention_only
        # Once this is true no more update is started: it stays pending for the next start.
        self._shutdown_begun: Callable[[], bool] = shutdown_check or (lambda: False)

    @property
    def lock_pool(self) -> LockPool:
        """The underlying lock pool (shared with the message bus)."""
        return self._lock_pool

    def set_bot(self, bot: Bot) -> None:
        """Inject the Bot instance used to send/edit queue indicator messages."""
        self._bot = bot

    def set_bot_username(self, bot_username: str | None) -> None:
        """Set the bot username for command mention filtering."""
        self._bot_username = bot_username

    def set_bot_id(self, bot_id: int | None) -> None:
        """Set the bot user id for reply-to-self detection in group filtering."""
        self._bot_id = bot_id

    def set_interrupt_handler(self, handler: AbortHandler) -> None:
        """Register a callback invoked for the '/interrupt' command *before* the lock."""
        self._interrupt_handler = handler

    def set_abort_handler(self, handler: AbortHandler) -> None:
        """Register a callback invoked for the '/stop' command *before* the lock."""
        self._abort_handler = handler

    def set_abort_all_handler(self, handler: AbortAllHandler) -> None:
        """Register a callback invoked for the '/stop_all' command *before* the lock."""
        self._abort_all_handler = handler

    def set_quick_command_handler(self, handler: QuickCommandHandler) -> None:
        """Register a callback for read-only commands dispatched *before* the lock."""
        self._quick_command_handler = handler

    def set_message_received_handler(self, handler: Callable[[Message], None]) -> None:
        """Register nonblocking topic naming after dedup, before the session lock."""
        self._message_received_handler = handler

    def get_lock(self, lock_key: tuple[int, int | None] | int) -> asyncio.Lock:
        """Return the per-session lock, creating it if needed.

        Accepts either a ``(chat_id, topic_id)`` tuple (from
        ``SessionKey.lock_key``) or a plain ``chat_id`` integer for
        backward compatibility.

        Used by webhook wake dispatch to queue behind active conversations.
        """
        return self._lock_pool.get(lock_key)

    # -- Queue inspection & manipulation ---------------------------------------

    def has_pending(self, chat_id: int) -> bool:
        """Return True if *chat_id* has messages waiting in the queue."""
        return bool(self._pending.get(chat_id))

    def is_busy(self, chat_id: int) -> bool:
        """Return True if *chat_id* has any lock held or pending messages.

        Checks all topic-scoped locks for the given chat.
        """
        return self._lock_pool.any_locked_for_chat(chat_id) or self.has_pending(chat_id)

    async def cancel_entry(self, chat_id: int, entry_id: int) -> bool:
        """Cancel a single queued message and edit its indicator.

        Returns True if the entry was found and cancelled.
        """
        entries = self._pending.get(chat_id, [])
        for entry in entries:
            if entry.entry_id == entry_id and not entry.cancelled:
                self._cancel(entry)
                await self._edit_indicator(chat_id, entry, t("notice.queue_cancelled"))
                logger.info("Queue entry cancelled chat=%d entry=%d", chat_id, entry_id)
                return True
        return False

    async def drain_pending(self, chat_id: int) -> int:
        """Cancel ALL pending messages for *chat_id* and edit their indicators.

        Returns the number of entries discarded.
        """
        return await self._drain(chat_id, lambda _entry: True)

    async def drain_topic(self, chat_id: int, topic_id: int | None) -> int:
        """Cancel the pending messages of one topic of *chat_id* (``None``: no topic).

        Returns the number of entries discarded.
        """
        return await self._drain(chat_id, lambda entry: entry.topic_id == topic_id)

    async def _drain(self, chat_id: int, wanted: Callable[[_QueueEntry], bool]) -> int:
        """Cancel the wanted waiting messages, all of them before anything is awaited.

        Editing the indicators sends to the network; if the running turn ended meanwhile, the
        freed lock would start a waiting message that was not cancelled yet. A message that
        already started is no longer in the list and is not counted.
        """
        targets = [e for e in self._pending.get(chat_id, []) if not e.cancelled and wanted(e)]
        for entry in targets:
            self._cancel(entry)
        for entry in targets:
            await self._edit_indicator(chat_id, entry, t("notice.queue_discarded"))
        logger.info("Queue drained chat=%d discarded=%d", chat_id, len(targets))
        return len(targets)

    @staticmethod
    def _cancel(entry: _QueueEntry) -> None:
        """Cancel a waiting message on the user's request.

        The update counts as dealt with, so a restart does not bring it back. (A shutdown
        cancels the waiting task itself and does not come through here.)
        """
        entry.cancelled = True
        if entry.mark_started is not None:
            # a failed save leaves it pending (a restart may replay it); the cancel itself goes on
            with contextlib.suppress(OSError):
                entry.mark_started()

    # -- Middleware entry point ------------------------------------------------

    async def _check_abort(self, chat_id: int, text: str, event: Message) -> bool:
        """Check for the interrupt, stop-all and stop commands. Returns True if handled."""
        if self._interrupt_handler and is_interrupt_message(text):
            logger.debug("Interrupt trigger detected text=%s", text[:40])
            handled = await self._interrupt_handler(chat_id, event)
            if handled:
                return True

        # Check "/stop_all" BEFORE "/stop" (both are matched by their command name)
        if self._abort_all_handler and is_abort_all_message(text):
            logger.debug("Abort-all trigger detected text=%s", text[:40])
            await self.drain_pending(chat_id)  # before the kill frees the locks
            handled = await self._abort_all_handler(chat_id, event)
            if handled:
                return True

        if self._abort_handler and is_abort_message(text):
            logger.debug("Abort trigger detected text=%s", text[:40])
            # The handler discards this topic's queue itself, before it kills the running turn
            # (the kill frees the lock, which would start the next waiting message).
            handled = await self._abort_handler(chat_id, event)
            if handled:
                return True

        return False

    def _is_unaddressed_group(self, event: Message) -> bool:
        """True if this group message is not addressed to us and must be dropped.

        Centralised so the queue indicator (``_send_indicator``) is never
        rendered for messages the bot would silently discard later.
        """
        drop = should_drop_in_group(
            event,
            bot_id=self._bot_id,
            bot_username=self._bot_username,
            group_mention_only=self._group_mention_only,
        )
        if drop:
            logger.debug(
                "Unaddressed group message dropped chat=%d msg_id=%d",
                event.chat.id,
                event.message_id,
            )
        return drop

    def _leave_pending_if_stopping(self, data: dict[str, Any]) -> bool:
        """Once the stop has begun nothing starts: the update stays pending for the next start."""
        if not self._shutdown_begun():
            return False
        data[KEEP_PENDING_KEY] = True
        return True

    @inherit_received_input
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.chat:
            return await handler(event, data)
        if self._is_unaddressed_group(event) or self._leave_pending_if_stopping(data):
            return None

        topic_label: str | None = None
        if event.is_topic_message and event.message_thread_id and self._topic_names:
            topic_label = self._topic_names.resolve(event.chat.id, event.message_thread_id)

        set_log_context(
            operation="msg",
            chat_id=event.chat.id if hasattr(event, "chat") else None,
            topic=topic_label,
        )

        chat_id = event.chat.id
        text = (event.text or "").strip()

        if text and await self._check_abort(chat_id, text, event):
            return None

        if self._quick_command_handler and text and is_quick_command(text, self._bot_username):
            logger.debug("Quick command bypass cmd=%s", text)
            handled = await self._quick_command_handler(chat_id, event)
            if handled:
                return None

        callback_id = data.get("command_callback_id")
        dedup_key = (
            f"cb:{chat_id}:{callback_id}"
            if callback_id
            else build_dedup_key(chat_id, event.message_id)
        )
        if self._dedup.check(dedup_key):
            logger.debug("Message deduplicated msg_id=%d", event.message_id)
            return None

        if self._message_received_handler and not data.get("command_callback_id"):
            self._message_received_handler(event)
            data["topic_naming_started"] = True
        session_key = get_session_key(event)
        return await self._run_under_lock(handler, event, data, chat_id, session_key)

    async def run_callback(
        self,
        handler: Callable[[], Awaitable[None]],
        event: Message,
        display_label: str,
    ) -> None:
        """Queue a button choice without running message ingress filters or dedup."""
        if self._shutdown_begun():
            return

        async def dispatch(_event: TelegramObject, _data: dict[str, Any]) -> None:
            await handler()

        await self._run_under_lock(
            dispatch,
            event,
            {"callback_label": display_label},
            event.chat.id,
            get_session_key(event),
        )

    # -- Internal helpers ------------------------------------------------------

    async def _run_under_lock(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
        chat_id: int,
        session_key: Any,
    ) -> Any:
        """Acquire the per-session lock; queue with indicator if it was busy."""
        callback_label: str | None = data.get("callback_label")
        lock = self.get_lock(session_key.lock_key)
        entry: _QueueEntry | None = None
        queued_lock = QueuedMessageLock(
            lock,
            None if callback_label is not None else message_time(getattr(event, "date", None)),
            annotate=callback_label is not None
            or (
                not (event.text or "").lstrip().startswith("/")
                and not data.get("command_callback_id")
            ),
        )
        try:
            if lock.locked():
                entry = self._create_entry(chat_id, event)
                if callback_label is not None:
                    entry.text_preview = callback_label[:40]
                entry.mark_started = data.get(MARK_STARTED_KEY)
                self._pending.setdefault(chat_id, []).append(entry)
                await self._send_indicator(chat_id, entry, event)

            async with queued_lock:
                stopping = (
                    self._shutdown_begun()
                    if callback_label is not None
                    else self._leave_pending_if_stopping(data)
                )
                if stopping:
                    # The lock came free because the stop ended the turn before this one.
                    if entry is not None:
                        self._remove_entry(chat_id, entry)
                        await self._delete_indicator(chat_id, entry)
                    return None
                mark = data.get(MARK_STARTED_KEY)
                if mark is not None:
                    mark()  # the lock is ours: this update is being processed now
                if entry is not None:
                    self._remove_entry(chat_id, entry)
                    if entry.cancelled:
                        await self._delete_indicator(chat_id, entry)
                        return None
                    await self._delete_indicator(chat_id, entry)
                return await handler(event, data)
        finally:
            queued_lock.discard()
            if entry is not None:
                pending = entry in self._pending.get(chat_id, [])
                self._remove_entry(chat_id, entry)
                if callback_label is not None and pending:
                    await self._delete_indicator(chat_id, entry)

    def _create_entry(self, chat_id: int, event: Message) -> _QueueEntry:
        self._entry_counter += 1
        return _QueueEntry(
            entry_id=self._entry_counter,
            chat_id=chat_id,
            message_id=event.message_id,
            text_preview=(event.text or "")[:40],
            topic_id=get_thread_id(event),
        )

    def _remove_entry(self, chat_id: int, entry: _QueueEntry) -> None:
        entries = self._pending.get(chat_id)
        if entries is None:
            return
        with contextlib.suppress(ValueError):
            entries.remove(entry)
        if not entries:
            del self._pending[chat_id]

    async def _send_indicator(self, chat_id: int, entry: _QueueEntry, event: Message) -> None:
        if not self._bot:
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=t("notice.queue_cancel_button"),
                        callback_data=f"{MQ_PREFIX}{entry.entry_id}",
                    )
                ]
            ]
        )
        try:
            sent = await self._bot.send_message(
                chat_id,
                t("notice.queue_waiting"),
                parse_mode=ParseMode.HTML,
                reply_parameters=ReplyParameters(
                    message_id=event.message_id,
                    allow_sending_without_reply=True,
                ),
                reply_markup=keyboard,
                message_thread_id=get_thread_id(event),
            )
            entry.indicator_msg_id = sent.message_id
        except Exception:
            logger.debug("Failed to send queue indicator", exc_info=True)

    async def _edit_indicator(self, chat_id: int, entry: _QueueEntry, html: str) -> None:
        if not self._bot or not entry.indicator_msg_id:
            return
        try:
            await self._bot.edit_message_text(
                text=html,
                chat_id=chat_id,
                message_id=entry.indicator_msg_id,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        except Exception:
            logger.debug("Failed to edit queue indicator", exc_info=True)

    async def _delete_indicator(self, chat_id: int, entry: _QueueEntry) -> None:
        if not self._bot or not entry.indicator_msg_id:
            return
        try:
            await self._bot.delete_message(chat_id, entry.indicator_msg_id)
        except Exception:
            logger.debug("Failed to delete queue indicator", exc_info=True)
