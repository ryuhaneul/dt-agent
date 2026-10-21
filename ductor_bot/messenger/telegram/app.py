"""Telegram bot: aiogram 3.x frontend for the orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, ChatMemberUpdated, MessageEntity, ReplyParameters

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.commands import BOT_COMMANDS as _COMMAND_DEFS
from ductor_bot.commands import MULTIAGENT_SUB_COMMANDS as _MA_SUB_DEFS
from ductor_bot.commands import get_main_only_commands
from ductor_bot.config import AgentConfig
from ductor_bot.errors import ProductError
from ductor_bot.files.allowed_roots import resolve_allowed_roots
from ductor_bot.i18n import t
from ductor_bot.infra.input_context import inherit_received_input, input_lock
from ductor_bot.infra.restart import EXIT_RESTART, consume_restart_marker
from ductor_bot.infra.secret_store import store_path
from ductor_bot.infra.shutdown_budget import ShutdownBudget, wait_capped
from ductor_bot.infra.version import get_current_version
from ductor_bot.log_context import set_log_context
from ductor_bot.messenger.commands import classify_command
from ductor_bot.messenger.notifications import NotificationService
from ductor_bot.messenger.telegram.callbacks import (
    button_grid_to_markup,
    edit_selector_response,
    mark_button_choice,
    parse_ns_callback,
)
from ductor_bot.messenger.telegram.chat_tracker import ChatRecord, ChatTracker
from ductor_bot.messenger.telegram.file_browser import (
    file_browser_start,
    handle_file_browser_callback,
    is_file_browser_callback,
)
from ductor_bot.messenger.telegram.formatting import markdown_to_telegram_html
from ductor_bot.messenger.telegram.handlers import (
    build_reply_prompt,
    handle_abort,
    handle_abort_all,
    handle_command,
    handle_interrupt,
    handle_new_session,
    prepend_reply_to_media,
    strip_mention,
)
from ductor_bot.messenger.telegram.media import (
    has_media,
    is_command_for_others,
    is_message_addressed,
    resolve_media_text,
    should_drop_in_group,
)
from ductor_bot.messenger.telegram.message_dispatch import (
    NonStreamingDispatch,
    StreamingDispatch,
    run_non_streaming_message,
    run_streaming_message,
)
from ductor_bot.messenger.telegram.middleware import (
    MQ_PREFIX,
    AuthMiddleware,
    JournalMiddleware,
    SequentialMiddleware,
)
from ductor_bot.messenger.telegram.polling import PollingDispatcher
from ductor_bot.messenger.telegram.reply_phase import ReplyPhase
from ductor_bot.messenger.telegram.secret_input import SecretInput
from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich
from ductor_bot.messenger.telegram.sender import (
    send_files_from_text as _send_files_from_text,
)
from ductor_bot.messenger.telegram.topic import (
    TopicNameCache,
    get_session_key,
    get_thread_id,
)
from ductor_bot.messenger.telegram.typing import TypingContext as _TypingContext
from ductor_bot.messenger.telegram.update_journal import PENDING_TTL_SECONDS, UpdateJournal
from ductor_bot.messenger.telegram.welcome import (
    build_welcome_keyboard,
    build_welcome_text,
    get_welcome_button_label,
    is_welcome_callback,
    resolve_welcome_callback,
)
from ductor_bot.multiagent.bus import AsyncInterAgentResult
from ductor_bot.session.key import SessionKey
from ductor_bot.tasks.models import TaskResult
from ductor_bot.text.response_format import SEP, fmt
from ductor_bot.workspace.paths import DuctorPaths, resolve_paths

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, ForumTopicCreated, Message

    from ductor_bot.bus.envelope import DeliveryOutcome
    from ductor_bot.cli.codex_usage import CodexUsage
    from ductor_bot.messenger.telegram.topic_naming import DMTopicNamer
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.orchestrator.selectors.models import SelectorResponse

logger = logging.getLogger(__name__)


# Backward-compatible patch points used by tests.
TypingContext = _TypingContext
send_files_from_text = _send_files_from_text

_BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command=cmd, description=desc) for cmd, desc in _COMMAND_DEFS
]

_CMD_DESC: dict[str, str] = {**dict(_COMMAND_DEFS), **dict(_MA_SUB_DEFS)}


def _rebuild_commands() -> None:
    """Rebuild module-level command lists from current translations."""
    global _BOT_COMMANDS  # noqa: PLW0603
    from ductor_bot.commands import get_bot_commands, get_multiagent_sub_commands

    cmd_defs = get_bot_commands()
    ma_defs = get_multiagent_sub_commands()
    _BOT_COMMANDS = [BotCommand(command=cmd, description=desc) for cmd, desc in cmd_defs]
    _CMD_DESC.clear()
    _CMD_DESC.update({**dict(cmd_defs), **dict(ma_defs)})


def _help_line(command: str) -> str:
    """Return one command line for the help panel."""
    description = _CMD_DESC.get(command, "")
    return f"/{command} -- {description}" if description else f"/{command}"


def _build_help_text() -> str:
    return fmt(
        t("help.header"),
        SEP,
        f"{t('help.cat_daily')}\n{_help_line('new')}\n{_help_line('reset')}\n{_help_line('stop')}\n"
        f"{_help_line('interrupt')}\n{_help_line('stop_all')}\n"
        f"{_help_line('model')}\n{_help_line('effort')}\n{_help_line('status')}\n"
        f"{_help_line('usage')}\n{_help_line('memory')}",
        f"{t('help.cat_automation')}\n{_help_line('session')}\n{_help_line('tasks')}\n{_help_line('cron')}",
        f"{t('help.cat_multiagent')}\n{_help_line('agent_commands')}",
        f"{t('help.cat_browse')}\n{_help_line('where')}\n{_help_line('leave')}\n"
        f"{_help_line('showfiles')}\n{_help_line('info')}\n{_help_line('help')}",
        f"{t('help.cat_maintenance')}\n{_help_line('diagnose')}\n{_help_line('restart')}",
        SEP,
        t("help.footer"),
    )


_POLLING_STOP_SECONDS = 5.0
_POLLING_FORCE_SECONDS = 2.0
_REPLY_DRAIN_SECONDS = 8.0
_HANDLER_FINISH_SECONDS = 2.0
_NAMING_CLEANUP_SECONDS = 5.0


async def _wait_done(task: asyncio.Task[Any], seconds: float) -> None:
    """Wait for *task* to end for at most *seconds*; its outcome is not raised."""
    if not task.done():
        await asyncio.wait({task}, timeout=seconds)
    if task.done() and not task.cancelled():
        task.exception()  # mark it retrieved


async def _cancel_task(task: asyncio.Task[None] | None) -> None:
    """Cancel an asyncio task and suppress CancelledError."""
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TelegramNotificationService:
    """NotificationService implementation for Telegram."""

    transport_name = "tg"

    def __init__(self, bot: Bot, config: AgentConfig) -> None:
        self._bot = bot
        self._config = config

    async def notify_origin(
        self, transport: str, chat_id: int, text: str, *, topic_id: int | None = None
    ) -> bool:
        if transport != self.transport_name or not chat_id:
            logger.warning(
                "Cannot notify Telegram origin transport=%s chat_id=%s", transport, chat_id
            )
            return False
        try:
            sent = await send_rich(self._bot, chat_id, text, SendRichOpts(thread_id=topic_id))
        except TelegramAPIError:
            logger.warning(
                "Failed to notify Telegram origin chat_id=%s topic_id=%s",
                chat_id,
                topic_id,
                exc_info=True,
            )
            return False
        if not sent:
            logger.warning(
                "Failed to notify Telegram origin chat_id=%s topic_id=%s", chat_id, topic_id
            )
        return bool(sent)

    async def notify(self, chat_id: int, text: str) -> None:
        await send_rich(self._bot, chat_id, text, None)

    async def notify_all(self, text: str) -> None:
        for uid in self._config.allowed_user_ids:
            try:
                await send_rich(self._bot, uid, text, None)
            except TelegramAPIError:
                # An unreachable recipient (e.g. never pressed /start -> "chat
                # not found") must not abort the notification fan-out — or, at
                # startup, the whole boot.
                logger.warning("Notification to user %d failed, skipping", uid)


class TelegramBot:
    """Telegram frontend. All logic lives in the Orchestrator."""

    def __init__(  # noqa: PLR0915
        self,
        config: AgentConfig,
        *,
        agent_name: str = "main",
        bus: MessageBus | None = None,
        lock_pool: LockPool | None = None,
    ) -> None:
        self._config = config
        self._agent_name = agent_name
        self._orchestrator: Orchestrator | None = None
        self._abort_all_callback: Callable[[], Awaitable[int]] | None = None

        self._bot = Bot(
            token=config.telegram_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._notification_service: NotificationService = TelegramNotificationService(
            self._bot, config
        )
        self._bot_id: int | None = None
        self._bot_username: str | None = None

        self._journal = UpdateJournal(
            resolve_paths(ductor_home=config.ductor_home).telegram_update_state_path
        )
        home = resolve_paths(ductor_home=config.ductor_home).ductor_home
        allowed = set(config.allowed_user_ids)
        self._secret_input = SecretInput(
            self._bot,
            is_main=(agent_name == "main"),
            allowed_users=allowed,
            ductor_home=home,
            store_path=store_path(home),
        )
        self._dp = PollingDispatcher(journal=self._journal, secret_input=self._secret_input)
        self._journal_replayed = False
        self._polling_task: asyncio.Task[None] | None = None
        self._stop_requested = False
        self._reply_phase = ReplyPhase()
        self._router = Router(name="main")
        self._exit_code: int = 0
        self._restart_watcher: asyncio.Task[None] | None = None
        self._group_audit_task: asyncio.Task[None] | None = None

        allowed_groups = set(config.allowed_group_ids)
        allowed_channels = set(config.allowed_channel_ids)
        self._allowed_users = allowed
        self._allowed_groups = allowed_groups
        self._allowed_channels = allowed_channels
        self._chat_tracker: ChatTracker | None = None  # set in _on_startup
        self._topic_names = TopicNameCache()
        self._topic_namer: DMTopicNamer | None = None
        self._topic_naming_stopped = False
        self._lock_pool = lock_pool if lock_pool is not None else LockPool()
        self._bus = bus or MessageBus(lock_pool=self._lock_pool)

        from ductor_bot.messenger.telegram.transport import TelegramTransport

        self._bus.register_transport(TelegramTransport(self))
        self._button_choices: set[tuple[int, int, str]] = set()
        self._sequential = SequentialMiddleware(
            shutdown_check=self._shutdown_begun,
            lock_pool=self._lock_pool,
            topic_names=self._topic_names,
            group_mention_only=config.group_mention_only,
        )
        self._sequential.set_bot(self._bot)
        self._sequential.set_message_received_handler(self._start_topic_name)
        self._sequential.set_interrupt_handler(self._on_interrupt)
        self._sequential.set_abort_handler(self._on_abort)
        self._sequential.set_abort_all_handler(self._on_abort_all)
        self._sequential.set_quick_command_handler(self._on_quick_command)
        on_rejected = self._on_group_rejected
        auth = AuthMiddleware(allowed, allowed_group_ids=allowed_groups, on_rejected=on_rejected)
        self._auth = auth
        self._router.message.outer_middleware(JournalMiddleware(self._journal.mark_started))
        self._router.message.outer_middleware(auth)
        self._router.message.outer_middleware(self._sequential)
        self._router.callback_query.outer_middleware(auth)

        self._register_handlers()
        self._register_member_handlers()
        self._dp.include_router(self._router)
        self._dp.startup.register(self._on_startup)

    @property
    def _orch(self) -> Orchestrator:
        if self._orchestrator is None:
            msg = "Orchestrator not initialized -- call after startup"
            raise RuntimeError(msg)
        return self._orchestrator

    @property
    def orchestrator(self) -> Orchestrator | None:
        """Public read-only access to the orchestrator (None before startup)."""
        return self._orchestrator

    def set_abort_all_callback(self, callback: Callable[[], Awaitable[int]]) -> None:
        """Set a callback that kills processes on ALL agents (set by supervisor)."""
        self._abort_all_callback = callback

    @property
    def dispatcher(self) -> Dispatcher:
        """Public read-only access to the aiogram Dispatcher."""
        return self._dp

    @property
    def bot_instance(self) -> Bot:
        """Public read-only access to the aiogram Bot instance."""
        return self._bot

    @property
    def config(self) -> AgentConfig:
        """Public read-only access to the agent configuration."""
        return self._config

    @property
    def notification_service(self) -> NotificationService:
        """Transport-agnostic notification interface."""
        return self._notification_service

    def register_startup_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Register a callback to run after bot startup (used by supervisor)."""
        self._dp.startup.register(hook)

    @property
    def sequential(self) -> SequentialMiddleware:
        """Public read-only access to the sequential middleware."""
        return self._sequential

    @property
    def lock_pool(self) -> LockPool:
        """Shared lock pool (used by middleware, bus, and API server)."""
        return self._lock_pool

    def _is_addressed(self, message: Message) -> bool:
        """True if the message is addressed to this bot instance."""
        if message.chat.type not in ("group", "supergroup"):
            return True
        return is_message_addressed(message, self._bot_id, self._bot_username)

    def _is_for_others(self, message: Message) -> bool:
        """True if the message is a command explicitly for another bot."""
        if message.chat.type not in ("group", "supergroup"):
            return False
        return is_command_for_others(message, self._bot_username)

    def file_roots(self, paths: DuctorPaths) -> list[Path] | None:
        """Allowed root directories for ``<file:...>`` tag sends."""
        return resolve_allowed_roots(self._config.file_access, paths.workspace)

    async def broadcast(self, text: str, opts: SendRichOpts | None = None) -> None:
        """Send a message to all allowed users."""
        for uid in self._config.allowed_user_ids:
            await send_rich(self._bot, uid, text, opts)

    async def notify_startup(self, text: str) -> None:
        """Route startup-lifecycle notifications (#64).

        Fallback policy:
          * ``startup_targets`` empty (default) -> broadcast via ``notify_all``.
          * ``startup_targets`` non-empty but every entry disabled -> explicit
            silence (no fallback). This is how users opt out of lifecycle
            notifications.

        Per-target failures are swallowed at warning level so a single bad
        target does not mask the rest.
        """
        configured = self._config.notifications.startup_targets
        if not configured:
            await self._notification_service.notify_all(text)
            return
        targets = [t for t in configured if t.enabled and t.chat_id is not None]
        for target in targets:
            try:
                assert target.chat_id is not None
                await send_rich(
                    self._bot,
                    target.chat_id,
                    text,
                    SendRichOpts(thread_id=target.topic_id),
                )
            except Exception:
                logger.warning(
                    "notify_startup: delivery failed for chat_id=%s topic_id=%s",
                    target.chat_id,
                    target.topic_id,
                    exc_info=True,
                )

    async def _on_startup(self) -> None:
        from ductor_bot.messenger.telegram.startup import run_startup

        await run_startup(self)
        self._sequential.set_bot_username(self._bot_username)
        self._sequential.set_bot_id(self._bot_id)
        self._auth.set_bot_id(self._bot_id)

    def _register_handlers(self) -> None:
        r = self._router
        r.message(CommandStart(ignore_case=True))(self._on_start)
        r.message(Command("help", ignore_case=True))(self._on_help)
        r.message(Command("info", ignore_case=True))(self._on_info)
        r.message(Command("stop_all", ignore_case=True))(self._on_stop_all)
        r.message(Command("stop", ignore_case=True))(self._on_stop)
        r.message(Command("restart", ignore_case=True))(self._on_restart)
        r.message(Command("new", ignore_case=True))(self._on_new)
        r.message(Command("session", ignore_case=True))(self._on_session)
        r.message(Command("sessions", ignore_case=True))(self._on_sessions)
        r.message(Command("tasks", ignore_case=True))(self._on_tasks)
        r.message(Command("accounts", ignore_case=True))(self._on_accounts)
        r.message(Command("usage", ignore_case=True))(self._on_usage)
        r.message(Command("showfiles", ignore_case=True))(self._on_showfiles)
        r.message(Command("agent_commands", ignore_case=True))(self._on_agent_commands)
        base_cmds = [
            "status",
            "memory",
            "model",
            "effort",
            "cron",
            "diagnose",
            "reset",
        ]
        if self._agent_name == "main":
            base_cmds += ["agents", "agent_start", "agent_stop", "agent_restart"]
        for cmd in base_cmds:
            r.message(Command(cmd, ignore_case=True))(self._on_command)
        r.message(F.forum_topic_created)(self._on_forum_topic_created)
        r.message(F.forum_topic_edited)(self._on_forum_topic_edited)
        r.message()(self._on_message)
        r.callback_query()(self._on_callback_query)

    def _register_member_handlers(self) -> None:
        """Register my_chat_member handlers on the dispatcher (not router).

        ``ChatMemberUpdated`` events bypass message middleware, so they go
        directly on the dispatcher.
        """
        from aiogram.filters import ChatMemberUpdatedFilter
        from aiogram.filters.chat_member_updated import (
            IS_MEMBER,
            IS_NOT_MEMBER,
        )

        self._dp.my_chat_member.register(
            self._on_bot_added,
            ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER),
        )
        self._dp.my_chat_member.register(
            self._on_bot_removed,
            ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER),
        )

    def _on_auth_hot_reload(self, config: AgentConfig, hot: dict[str, object]) -> None:
        """Update auth sets and language in-place when config is hot-reloaded."""
        if "allowed_user_ids" in hot:
            self._allowed_users.clear()
            self._allowed_users.update(config.allowed_user_ids)
            logger.info("Auth hot-reloaded: allowed_user_ids (%d)", len(self._allowed_users))
        if "allowed_group_ids" in hot:
            self._allowed_groups.clear()
            self._allowed_groups.update(config.allowed_group_ids)
            logger.info("Auth hot-reloaded: allowed_group_ids (%d)", len(self._allowed_groups))
            self._group_audit_task = asyncio.create_task(self._fire_audit())
        if "allowed_channel_ids" in hot:
            self._allowed_channels.clear()
            self._allowed_channels.update(config.allowed_channel_ids)
            logger.info(
                "Auth hot-reloaded: allowed_channel_ids (%d)",
                len(self._allowed_channels),
            )
        if "language" in hot:
            _rebuild_commands()
            self._lang_sync_task = asyncio.create_task(self._sync_commands())
            logger.info("Language hot-reloaded: commands re-synced")

    # -- Chat tracker (my_chat_member + /where + /leave) ------------------------

    def _on_group_rejected(self, chat_id: int, chat_type: str, title: str) -> None:
        """Callback from AuthMiddleware when a group message is rejected."""
        if self._chat_tracker:
            self._chat_tracker.record_rejected(chat_id, chat_type, title)

    async def _on_bot_added(self, event: ChatMemberUpdated) -> None:
        """Bot was added to a group or channel."""
        chat = event.chat
        is_channel = chat.type == "channel"
        if is_channel:
            allowed = chat.id in self._allowed_channels
            reject_key = "telegram.channel_not_whitelisted"
            chat_kind = "channel"
        else:
            allowed = chat.id in self._allowed_groups
            reject_key = "telegram.group_rejected"
            chat_kind = "group"
        if self._chat_tracker:
            self._chat_tracker.record_join(
                chat.id,
                chat.type,
                chat.title or "",
                allowed=allowed,
            )
        if not allowed:
            with contextlib.suppress(TelegramAPIError):
                await self._bot.send_message(
                    chat.id,
                    t(reject_key),
                )
            with contextlib.suppress(TelegramAPIError):
                await self._bot.leave_chat(chat.id)
            if self._chat_tracker:
                self._chat_tracker.record_leave(chat.id, "auto_left")
            logger.info(
                "Auto-left unauthorized %s chat_id=%d title=%s",
                chat_kind,
                chat.id,
                chat.title,
            )
            return
        await self._send_join_notification(chat.id)

    async def _on_bot_removed(self, event: ChatMemberUpdated) -> None:
        """Bot was removed from a group."""
        chat = event.chat
        status = "kicked" if event.new_chat_member.status == "kicked" else "left"
        if self._chat_tracker:
            self._chat_tracker.record_leave(chat.id, status)
        logger.info("Bot removed from group chat_id=%d status=%s", chat.id, status)

    async def _send_join_notification(self, chat_id: int) -> None:
        """Send JOIN_NOTIFICATION.md content and try to pin it."""
        if not self._orchestrator:
            return
        path = self._orch.paths.join_notification_path
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return
        from ductor_bot.messenger.telegram.sender import _send_text_chunks

        msg, _delivered = await _send_text_chunks(self._bot, chat_id, text)
        if msg:
            with contextlib.suppress(TelegramAPIError):
                await self._bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)

    _GROUP_AUDIT_INTERVAL = 86400  # 24 hours

    async def _fire_audit(self) -> None:
        """Fire-and-forget wrapper for ``audit_groups``."""
        await self.audit_groups()

    async def _run_group_audit_loop(self) -> None:
        """Run ``audit_groups`` every 24 hours."""
        while True:
            await asyncio.sleep(self._GROUP_AUDIT_INTERVAL)
            try:
                left = await self.audit_groups()
                if left:
                    logger.info("Periodic group audit: left %d group(s)", left)
            except Exception:
                logger.debug("Periodic group audit error", exc_info=True)

    async def audit_groups(self) -> int:
        """Leave groups where the bot is still a member but no longer allowed.

        Checks tracked active groups against ``allowed_group_ids`` and calls
        ``leave_chat`` for any that lost authorization.  Returns the number
        of groups left.
        """
        if not self._chat_tracker:
            return 0
        left = 0
        for rec in self._chat_tracker.get_all():
            if rec.status != "active":
                continue
            if rec.chat_type == "channel":
                if rec.chat_id in self._allowed_channels:
                    continue
            elif rec.chat_id in self._allowed_groups:
                continue
            # Not allowed — try to leave.
            try:
                await self._bot.leave_chat(rec.chat_id)
            except TelegramAPIError:
                logger.debug("audit_groups: leave_chat failed for %d", rec.chat_id, exc_info=True)
            self._chat_tracker.record_leave(rec.chat_id, "auto_left")
            logger.info("Audit: auto-left group %d (%s)", rec.chat_id, rec.title)
            left += 1
        return left

    @staticmethod
    def _where_line(r: ChatRecord) -> str:
        """Format a single chat record for /where output."""
        title = r.title or "untitled"
        return f"`{r.chat_id}` — {title} ({r.chat_type})"

    def _format_where(self) -> str:
        """Build the /where response text."""
        if not self._chat_tracker:
            return fmt(t("telegram.where_header"), SEP, t("telegram.where_no_tracker"))
        records = self._chat_tracker.get_all()
        if not records:
            return fmt(t("telegram.where_header"), SEP, t("telegram.where_empty"))

        sections: list[str] = []
        active = [r for r in records if r.status == "active" and r.allowed]
        rejected = [r for r in records if not r.allowed or r.status == "rejected"]
        left = [r for r in records if r.status in ("left", "kicked", "auto_left")]

        if active:
            lines = [self._where_line(r) for r in active]
            sections.append("**Active**\n" + "\n".join(lines))
        if rejected:
            lines = []
            for r in rejected:
                extra = f" — {r.rejected_count}x rejected" if r.rejected_count else ""
                lines.append(f"{self._where_line(r)}{extra}")
            sections.append("**Rejected**\n" + "\n".join(lines))
        if left:
            lines = [f"{self._where_line(r)} [{r.status}]" for r in left]
            sections.append("**Left**\n" + "\n".join(lines))

        return fmt(t("telegram.where_header"), SEP, *sections)

    async def _handle_where(self, chat_id: int, message: Message) -> None:
        """Handle /where: show all tracked chats/groups."""
        await send_rich(
            self._bot,
            chat_id,
            self._format_where(),
            SendRichOpts(
                reply_to_message_id=message.message_id,
                thread_id=get_thread_id(message),
            ),
        )

    async def _handle_leave(self, chat_id: int, message: Message) -> None:
        """Handle /leave <group_id>: manually leave a group."""
        thread_id = get_thread_id(message)
        parts = (message.text or "").strip().split(None, 1)
        if len(parts) < 2:
            await send_rich(
                self._bot,
                chat_id,
                fmt(t("telegram.leave_usage_header"), SEP, t("telegram.leave_usage")),
                SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
            )
            return

        try:
            group_id = int(parts[1].strip())
        except ValueError:
            await send_rich(
                self._bot,
                chat_id,
                t("telegram.leave_invalid_id"),
                SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
            )
            return

        try:
            await self._bot.leave_chat(group_id)
        except TelegramAPIError as exc:
            await send_rich(
                self._bot,
                chat_id,
                t("telegram.leave_failed", error=exc),
                SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
            )
            return

        if self._chat_tracker:
            self._chat_tracker.record_leave(group_id, "left")

        await send_rich(
            self._bot,
            chat_id,
            t("telegram.left_group", group_id=group_id),
            SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
        )

    # -- Welcome & help ---------------------------------------------------------

    async def _show_welcome(self, message: Message) -> None:
        """Send the welcome screen with auth status and quick-start buttons."""
        from ductor_bot.cli.auth import check_all_auth

        chat_id = message.chat.id
        thread_id = get_thread_id(message)
        user_name = message.from_user.first_name if message.from_user else ""

        auth_results = await asyncio.to_thread(check_all_auth)
        text = build_welcome_text(user_name, auth_results, self._config)
        keyboard = build_welcome_keyboard()

        await send_rich(
            self._bot,
            chat_id,
            text,
            SendRichOpts(
                reply_to_message_id=message.message_id,
                reply_markup=keyboard,
                thread_id=thread_id,
            ),
        )

    async def _on_start(self, message: Message) -> None:
        """Handle /start: always show welcome screen."""
        if self._is_for_others(message):
            return
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        await self._show_welcome(message)
        await self._send_join_notification(message.chat.id)

    async def _on_help(self, message: Message) -> None:
        """Handle /help: show command reference."""
        if self._is_for_others(message):
            return
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        await send_rich(
            self._bot,
            message.chat.id,
            _build_help_text(),
            SendRichOpts(reply_to_message_id=message.message_id, thread_id=get_thread_id(message)),
        )

    async def _on_agent_commands(self, message: Message) -> None:
        """Handle /agent_commands: explain multi-agent system + list commands."""
        if self._is_for_others(message):
            return
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        chat_id = message.chat.id
        thread_id = get_thread_id(message)

        lines = [
            t("agents.telegram_explanation"),
            "",
            t("agents.commands_header"),
            t("agents.cmd_list", prefix="/"),
            t("agents.cmd_start", prefix="/"),
            t("agents.cmd_stop", prefix="/"),
            t("agents.cmd_restart", prefix="/"),
            "",
            t("agents.setup_header"),
            t("agents.setup_instruction"),
        ]
        text = fmt(t("agents.system_header"), SEP, "\n".join(lines))
        await send_rich(
            self._bot,
            chat_id,
            text,
            SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
        )

    async def _on_info(self, message: Message) -> None:
        """Handle /info: show project links and version."""
        if self._is_for_others(message):
            return
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        version = get_current_version()
        text = fmt(
            t("info.header"),
            t("info.version", version=version),
            SEP,
            t("info.telegram_description"),
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="GitHub", url="https://github.com/ryuhaneul/dt-agent"
                    ),
                    InlineKeyboardButton(
                        text="Changelog",
                        url="https://github.com/ryuhaneul/dt-agent/releases",
                    ),
                ],
            ],
        )
        await send_rich(
            self._bot,
            message.chat.id,
            text,
            SendRichOpts(
                reply_to_message_id=message.message_id,
                reply_markup=keyboard,
                thread_id=get_thread_id(message),
            ),
        )

    async def _on_showfiles(self, message: Message) -> None:
        """Handle /showfiles: interactive file browser for ~/.ductor."""
        text, keyboard = await file_browser_start(self._orch.paths)
        await send_rich(
            self._bot,
            message.chat.id,
            text,
            SendRichOpts(
                reply_to_message_id=message.message_id,
                reply_markup=keyboard,
                thread_id=get_thread_id(message),
            ),
        )

    # -- Interrupt, abort, commands, sessions ----------------------------------

    async def _on_interrupt(self, chat_id: int, message: Message) -> bool:
        return await handle_interrupt(
            self._orchestrator,
            self._bot,
            chat_id=chat_id,
            message=message,
        )

    async def _on_abort_all(self, chat_id: int, message: Message) -> bool:
        return await handle_abort_all(
            self._orchestrator,
            self._bot,
            chat_id=chat_id,
            message=message,
            abort_all_callback=self._abort_all_callback,
        )

    async def _on_abort(self, chat_id: int, message: Message) -> bool:
        return await handle_abort(
            self._orchestrator,
            self._bot,
            chat_id=chat_id,
            message=message,
            discard_queue=partial(self._sequential.drain_topic, chat_id, get_thread_id(message)),
        )

    async def _dispatch_direct_command(
        self,
        chat_id: int,
        message: Message,
        text_lower: str,
    ) -> bool | None:
        """Handle commands that don't need the orchestrator. Returns True/None."""
        if text_lower.startswith("/where"):
            await self._handle_where(chat_id, message)
            return True
        if text_lower.startswith("/leave"):
            await self._handle_leave(chat_id, message)
            return True
        if text_lower.startswith("/showfiles") and self._orchestrator is not None:
            await self._on_showfiles(message)
            return True
        if (
            text_lower
            and text_lower.split(None, 1)[0].split("@", 1)[0] == "/accounts"
            and self._orchestrator is not None
        ):
            await self._on_accounts(message)
            return True
        if (
            text_lower
            and text_lower.split(None, 1)[0].split("@", 1)[0] == "/usage"
            and self._orchestrator is not None
        ):
            await self._on_usage(message)
            return True
        return None

    async def _on_quick_command(self, chat_id: int, message: Message) -> bool:
        """Handle a read-only command without the sequential lock.

        ``/model`` is special: when the chat is busy it returns an immediate
        "agent is working" message; otherwise it acquires the lock for an
        atomic model switch.
        """
        if self._is_for_others(message) or (
            self._config.group_mention_only and not self._is_addressed(message)
        ):
            return False

        text_lower = (message.text or "").strip().lower()

        direct = await self._dispatch_direct_command(chat_id, message, text_lower)
        if direct is not None or self._orchestrator is None:
            return direct or False

        if not self._orchestrator.has_command(text_lower):
            return False

        if text_lower.startswith(("/sessions", "/tasks")):
            await handle_command(self._orchestrator, self._bot, message)
            return True

        if text_lower.startswith("/model"):
            await handle_command(self._orchestrator, self._bot, message)
            return True

        await handle_command(self._orchestrator, self._bot, message)
        return True

    async def _on_stop_all(self, message: Message) -> None:
        if self._is_for_others(message):
            return
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        await handle_abort_all(
            self._orchestrator,
            self._bot,
            chat_id=message.chat.id,
            message=message,
            abort_all_callback=self._abort_all_callback,
        )

    async def _on_stop(self, message: Message) -> None:
        if self._is_for_others(message):
            return
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        await self._on_abort(message.chat.id, message)

    async def _on_command(self, message: Message) -> None:
        if self._is_for_others(message):
            return
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        await handle_command(self._orch, self._bot, message)

    async def _on_new(self, message: Message) -> None:
        if self._is_for_others(message):
            return
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        await handle_new_session(self._orch, self._bot, message, topic_names=self._topic_names)

    def _get_topic_namer(self) -> DMTopicNamer:
        from ductor_bot.messenger.telegram.topic_naming import DMTopicNamer

        if self._topic_namer is None:
            self._topic_namer = DMTopicNamer(self._config, self._bot, self._orch, self._topic_names)
        return self._topic_namer

    async def _on_forum_topic_created(self, message: Message) -> None:
        """Cache the name when a forum topic is created."""
        from ductor_bot.messenger.telegram.topic import get_topic_name_from_message

        if message.chat.type == "private" and get_thread_id(message) is not None:
            self._get_topic_namer().created(
                message.chat.id,
                cast("int", get_thread_id(message)),
                implicit=cast("ForumTopicCreated", message.forum_topic_created).is_name_implicit
                is True,
            )
        name = get_topic_name_from_message(message)
        # Creation can wait behind the dialog lock while naming or user edits finish.
        if (
            name
            and message.message_thread_id is not None
            and self._topic_names.get(message.chat.id, message.message_thread_id) is None
        ):
            self._topic_names.set(message.chat.id, message.message_thread_id, name)
            logger.debug(
                "Topic name cached: %d/%d = %s",
                message.chat.id,
                message.message_thread_id,
                name,
            )

    async def _on_forum_topic_edited(self, message: Message) -> None:
        """Update the cache when a forum topic is renamed."""
        from ductor_bot.messenger.telegram.topic import get_topic_name_from_message

        if message.chat.type == "private" and get_thread_id(message) is not None:
            self._get_topic_namer().edited(message.chat.id, cast("int", get_thread_id(message)))
        name = get_topic_name_from_message(message)
        if name and message.message_thread_id is not None:
            self._topic_names.set(message.chat.id, message.message_thread_id, name)
            logger.debug(
                "Topic name updated: %d/%d = %s",
                message.chat.id,
                message.message_thread_id,
                name,
            )

    def _build_session_help(self) -> str:
        """Build the /session hub: explain the system + show commands."""
        providers = self._orch.available_providers
        lines: list[str] = [
            t("session_help.telegram_explanation"),
            "",
            t("session_help.usage_header"),
        ]

        if len(providers) == 1:
            p = next(iter(providers))
            if p == "claude":
                lines.append(t("session_help.claude_single"))
                lines.append(t("session_help.claude_model"))
            elif p == "codex":
                lines.append(t("session_help.codex_single"))
            elif p == "grok":
                lines.append(t("session_help.grok_single"))
                lines.append(t("session_help.grok_model"))
            else:
                lines.append(t("session_help.gemini_single"))
                lines.append(t("session_help.gemini_model"))
        else:
            lines.append(t("session_help.default_provider"))
            if "claude" in providers:
                lines.append(t("session_help.claude_multi"))
            if "codex" in providers:
                lines.append(t("session_help.codex_multi"))
            if "gemini" in providers:
                lines.append(t("session_help.gemini_multi"))
            if "grok" in providers:
                lines.append(t("session_help.grok_multi"))
            lines.append(t("session_help.explicit"))

        lines += [
            "",
            t("session_help.followup_header"),
            t("session_help.followup_line"),
            "",
            t("session_help.commands_header"),
            t("session_help.telegram_sessions_cmd"),
            t("session_help.telegram_stop_cmd"),
        ]

        return fmt(t("session_help.header"), SEP, "\n".join(lines))

    async def _on_session(self, message: Message) -> None:
        """Handle /session: submit a named background session."""
        import re

        text = (message.text or "").strip()
        parts = text.split(None, 1)
        chat_id = message.chat.id
        thread_id = get_thread_id(message)

        if len(parts) < 2 or not parts[1].strip():
            await send_rich(
                self._bot,
                chat_id,
                self._build_session_help(),
                SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
            )
            return

        prompt = parts[1].strip()

        # Parse optional @directive prefix:
        #   @provider [model] <prompt>    — e.g. @codex, @claude opus
        #   @model <prompt>               — e.g. @opus (infers provider)
        #   @session-name <prompt>        — follow-up to existing session
        provider_override: str | None = None
        model_override: str | None = None
        session_followup: str | None = None
        directive_match = re.match(r"@([a-zA-Z][a-zA-Z0-9_.-]*)\s+", prompt)
        if directive_match:
            key = directive_match.group(1).lower()
            rest = prompt[directive_match.end() :]

            resolved = self._orch.resolve_session_directive(key)
            if resolved:
                provider_override, model_override = resolved[0], resolved[1] or None
                prompt = rest
                # If key was a provider name, check for optional model after it
                if key in ("claude", "codex", "gemini", "grok"):
                    model_match = re.match(r"([a-zA-Z][a-zA-Z0-9_.-]*)\s+", prompt)
                    if model_match:
                        candidate = model_match.group(1).lower()
                        if self._orch.is_known_model(candidate):
                            model_override = candidate
                            prompt = prompt[model_match.end() :]
            elif self._orch.get_named_session(chat_id, key):
                session_followup = key
                prompt = rest

        try:
            if session_followup:
                task_id = self._orch.submit_named_followup_bg(
                    chat_id, session_followup, prompt, message.message_id, thread_id
                )
                await send_rich(
                    self._bot,
                    chat_id,
                    fmt(
                        t("session_notice.followup_sent", name=session_followup),
                        SEP,
                        t("session_notice.task_queued", task_id=task_id),
                    ),
                    SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
                )
            else:
                from ductor_bot.orchestrator.core import NamedSessionRequest

                ns_request = NamedSessionRequest(
                    message_id=message.message_id,
                    thread_id=thread_id,
                    provider_override=provider_override,
                    model_override=model_override,
                )
                task_id, session_name = self._orch.submit_named_session(
                    chat_id,
                    prompt,
                    ns_request,
                )
                ns = self._orch.get_named_session(chat_id, session_name)
                provider = ns.provider if ns else (provider_override or self._orch.config.provider)
                model = ns.model if ns else ""
                provider_label = {
                    "claude": "Claude",
                    "codex": "Codex",
                    "gemini": "Gemini",
                    "grok": "Grok Build",
                }.get(provider, provider)
                model_info = f" ({model})" if model else ""
                await send_rich(
                    self._bot,
                    chat_id,
                    fmt(
                        t("session_notice.started_title", name=session_name),
                        SEP,
                        t(
                            "session_notice.started_body",
                            provider=provider_label,
                            model_info=model_info,
                            name=session_name,
                        ),
                    ),
                    SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
                )
        except ValueError as exc:
            await send_rich(
                self._bot,
                chat_id,
                exc.shown() if isinstance(exc, ProductError) else str(exc),
                SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
            )

    async def _on_sessions(self, message: Message) -> None:
        """Handle /sessions: show session management UI."""
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        await handle_command(self._orch, self._bot, message)

    async def _usage_response(self, codex: CodexUsage | None) -> SelectorResponse:
        """Read the shared Claude snapshot and wrap the fixed-width usage body."""
        from ductor_bot.cli.claude_usage import UsageStore, usage_file_path
        from ductor_bot.orchestrator.selectors.models import SelectorResponse
        from ductor_bot.orchestrator.selectors.usage_view import render_usage, usage_buttons

        path = usage_file_path(Path(self._config.ductor_home) / "workspace")
        slots = await asyncio.to_thread(UsageStore(path).load)
        body = render_usage(self._config, slots, codex)
        return SelectorResponse(
            text=f"```\n{body}\n```",
            buttons=usage_buttons() if codex is not None else None,
        )

    async def _on_usage(self, message: Message) -> None:
        """Show saved usage immediately, then edit in the queried Codex limits."""
        if self._is_for_others(message) or (
            self._config.group_mention_only and not self._is_addressed(message)
        ):
            return
        try:
            initial = await self._usage_response(None)
        except Exception as exc:
            logger.warning("Usage display failed: %s", type(exc).__name__)
            await self._bot.send_message(
                chat_id=message.chat.id,
                text=t("usage.error"),
                reply_parameters=ReplyParameters(message_id=message.message_id),
                message_thread_id=get_thread_id(message),
            )
            return
        sent = await self._bot.send_message(
            chat_id=message.chat.id,
            text=markdown_to_telegram_html(initial.text),
            parse_mode=ParseMode.HTML,
            reply_parameters=ReplyParameters(message_id=message.message_id),
            message_thread_id=get_thread_id(message),
        )
        await self._handle_usage_refresh(message.chat.id, sent.message_id)

    async def _handle_usage_refresh(self, chat_id: int, message_id: int) -> None:
        """Refresh the same message without changing account or usage files."""
        from ductor_bot.cli.codex_usage import query_codex_usage
        from ductor_bot.orchestrator.selectors.models import SelectorResponse

        try:
            response = await self._usage_response(await query_codex_usage())
            await edit_selector_response(self._bot, chat_id, message_id, response)
        except TelegramRetryAfter:
            logger.debug("Usage message edit throttled")
        except Exception as exc:
            logger.warning("Usage display failed: %s", type(exc).__name__)
            try:
                await edit_selector_response(
                    self._bot, chat_id, message_id, SelectorResponse(text=t("usage.error"))
                )
            except TelegramRetryAfter:
                logger.debug("Usage message edit throttled")

    async def _on_accounts(self, message: Message) -> None:
        """Show account controls only in the main agent bot."""
        from ductor_bot.orchestrator.selectors.account_selector import account_selector_start
        from ductor_bot.orchestrator.selectors.models import SelectorResponse

        if self._is_for_others(message) or (
            self._config.group_mention_only and not self._is_addressed(message)
        ):
            return
        if self._agent_name != "main":
            resp = SelectorResponse(text=t("accounts.main_only"))
        else:
            resp = await account_selector_start(self._orch.paths.ductor_home, self._config)
        await send_rich(
            self._bot,
            message.chat.id,
            resp.text,
            SendRichOpts(
                reply_to_message_id=message.message_id,
                reply_markup=button_grid_to_markup(resp.buttons),
                thread_id=get_thread_id(message),
            ),
        )

    async def _on_tasks(self, message: Message) -> None:
        """Handle /tasks: show background task management UI."""
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        await handle_command(self._orch, self._bot, message)

    async def _on_restart(self, message: Message) -> None:
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        from ductor_bot.infra.restart import write_restart_sentinel

        chat_id = message.chat.id
        paths = self._orch.paths
        sentinel = paths.ductor_home / "restart-sentinel.json"
        await asyncio.to_thread(
            write_restart_sentinel,
            chat_id,
            t("startup.restart_default"),
            sentinel_path=sentinel,
        )
        text = fmt(t("startup.restart_header"), SEP, t("startup.restart_body"))
        await send_rich(
            self._bot,
            message.chat.id,
            text,
            SendRichOpts(reply_to_message_id=message.message_id, thread_id=get_thread_id(message)),
        )
        self._exit_code = EXIT_RESTART
        self._stop_requested = True
        await self._dp.stop_polling()

    # -- Callbacks -------------------------------------------------------------

    @inherit_received_input
    async def _on_callback_query(self, callback: CallbackQuery) -> None:
        """Handle inline keyboard button presses.

        Welcome quick-start (``w:`` prefix), model selector (``ms:`` prefix),
        and generic button callbacks are each routed to their own handler.

        All orchestrator interactions acquire the per-chat lock to prevent
        race conditions with concurrent webhook wake dispatch or model switches.
        """
        from aiogram.types import InaccessibleMessage

        if (callback.data or "").startswith("sec:"):
            await self._secret_input.handle_callback(callback)
            return

        await callback.answer()
        data = callback.data
        msg = callback.message
        if not data or msg is None or isinstance(msg, InaccessibleMessage):
            return

        chat_id = msg.chat.id
        key = get_session_key(msg)
        thread_id = get_thread_id(msg)
        set_log_context(operation="cb", chat_id=chat_id)
        logger.info("Callback data=%s", data[:40])

        # Resolve display label before data gets rewritten
        display_label: str = data
        if is_welcome_callback(data):
            display_label = get_welcome_button_label(data) or data
            resolved = resolve_welcome_callback(data)
            if not resolved:
                return
            data = resolved

        if await self._route_special_callback(key, msg.message_id, data, thread_id=thread_id):
            return

        command = data.split(None, 1)[0] if data.strip() else ""
        name = command.removeprefix("/").split("@", 1)[0].lower()
        if data.startswith("/") and classify_command(name) != "unknown":
            await self._mark_button_choice(chat_id, msg, display_label)
            command_message = msg.model_copy(
                update={
                    "text": data,
                    "from_user": callback.from_user,
                    "entities": [MessageEntity(type="bot_command", offset=0, length=len(command))],
                    "caption": None,
                    "caption_entities": None,
                    "reply_to_message": None,
                    "quote": None,
                    "photo": None,
                    "document": None,
                    "voice": None,
                    "video": None,
                    "audio": None,
                    "sticker": None,
                    "video_note": None,
                }
            )
            # Reuse typed-command filters, auth, mention gates and quick/abort handling.
            # Keep the bot message ID for replies, but dedupe by the callback ID.
            await self._router.propagate_event(
                "message",
                command_message,
                bot=self._bot,
                command_callback_id=callback.id,
            )
            return

        choice = (chat_id, msg.message_id, callback.data or "")
        if choice in self._button_choices or self._shutdown_begun():
            return
        self._button_choices.add(choice)
        try:
            await self._mark_button_choice(chat_id, msg, display_label)

            async def handle_choice() -> None:
                if self._config.streaming.enabled:
                    await self._handle_streaming(msg, key, data, thread_id=thread_id)
                else:
                    await self._handle_non_streaming(msg, key, data, thread_id=thread_id)

            await self._sequential.run_callback(handle_choice, msg, display_label)
        finally:
            self._button_choices.discard(choice)

    async def _route_special_callback(
        self,
        key: SessionKey,
        message_id: int,
        data: str,
        *,
        thread_id: int | None = None,
    ) -> bool:
        """Handle known callback namespaces. Returns True when handled."""
        from ductor_bot.orchestrator.selectors.account_selector import is_account_selector_callback

        if is_account_selector_callback(data):
            await self._handle_account_selector(key.chat_id, message_id, data)
            return True
        if await self._route_prefix_callback(key, message_id, data, thread_id=thread_id):
            return True

        from ductor_bot.orchestrator.selectors.model_selector import (
            is_model_selector_callback,
        )

        if is_model_selector_callback(data):
            await self._handle_model_selector(key, message_id, data)
            return True

        from ductor_bot.orchestrator.selectors.cron_selector import (
            is_cron_selector_callback,
        )

        if is_cron_selector_callback(data):
            await self._handle_cron_selector(key.chat_id, message_id, data)
            return True

        if is_file_browser_callback(data):
            await self._handle_file_browser(key, message_id, data, thread_id=thread_id)
            return True

        return False

    async def _route_prefix_callback(
        self,
        key: SessionKey,
        message_id: int,
        data: str,
        *,
        thread_id: int | None = None,
    ) -> bool:
        """Handle prefix-based callback namespaces. Returns True when handled."""
        chat_id = key.chat_id
        if data.startswith(MQ_PREFIX):
            await self._handle_queue_cancel(chat_id, data)
            return True
        if data.startswith("usg:"):
            await self._handle_usage_refresh(key.chat_id, message_id)
            return True

        from ductor_bot.orchestrator.selectors.session_selector import (
            is_session_selector_callback,
        )
        from ductor_bot.orchestrator.selectors.task_selector import (
            is_task_selector_callback,
        )

        if is_session_selector_callback(data):
            await self._handle_session_selector(chat_id, message_id, data)
            return True

        if is_task_selector_callback(data):
            await self._handle_task_selector(chat_id, message_id, data)
            return True

        if data.startswith("ns:"):
            await self._handle_ns_callback(key, data, thread_id=thread_id)
            return True

        return False

    async def _handle_model_selector(self, key: SessionKey, message_id: int, data: str) -> None:
        """Handle model selector wizard by editing the message in-place."""
        from ductor_bot.orchestrator.selectors.model_selector import (
            handle_model_callback,
        )

        async with input_lock(self._sequential.get_lock(key.lock_key)):
            resp = await handle_model_callback(self._orch, key, data)
        await edit_selector_response(self._bot, key.chat_id, message_id, resp)

    async def _handle_cron_selector(self, chat_id: int, message_id: int, data: str) -> None:
        """Handle cron selector wizard by editing the message in-place."""
        from ductor_bot.orchestrator.selectors.cron_selector import handle_cron_callback

        async with input_lock(self._sequential.get_lock(chat_id)):
            resp = await handle_cron_callback(self._orch, data)
        await edit_selector_response(self._bot, chat_id, message_id, resp)

    async def _handle_session_selector(self, chat_id: int, message_id: int, data: str) -> None:
        """Handle session selector wizard by editing the message in-place."""
        from ductor_bot.orchestrator.selectors.session_selector import (
            handle_session_callback,
        )

        async with input_lock(self._sequential.get_lock(chat_id)):
            resp = await handle_session_callback(self._orch, chat_id, data)
        await edit_selector_response(self._bot, chat_id, message_id, resp)

    async def _handle_account_selector(self, chat_id: int, message_id: int, data: str) -> None:
        """Check the main-agent boundary before applying any account action."""
        from ductor_bot.orchestrator.selectors.account_selector import handle_account_callback
        from ductor_bot.orchestrator.selectors.models import SelectorResponse

        if self._agent_name != "main":
            resp = SelectorResponse(text=t("accounts.main_only"))
        else:
            resp = await handle_account_callback(self._orch.paths.ductor_home, self._config, data)
        await edit_selector_response(self._bot, chat_id, message_id, resp)

    async def _handle_task_selector(self, chat_id: int, message_id: int, data: str) -> None:
        """Handle task selector wizard by editing the message in-place."""
        from ductor_bot.orchestrator.selectors.task_selector import handle_task_callback

        hub = self._orch.task_hub
        if hub is None:
            return
        resp = await handle_task_callback(hub, chat_id, data)
        await edit_selector_response(self._bot, chat_id, message_id, resp)

    async def _handle_ns_callback(
        self, key: SessionKey, data: str, *, thread_id: int | None = None
    ) -> None:
        """Handle ``ns:<session_name>:<label>`` button callbacks from session results."""
        parsed = parse_ns_callback(data)
        if parsed is None:
            return
        session_name, label = parsed

        async with input_lock(self._sequential.get_lock(key.lock_key)):
            if self._config.streaming.enabled:
                from ductor_bot.orchestrator.flows import named_session_streaming

                result = await named_session_streaming(self._orch, key, session_name, label)
            else:
                from ductor_bot.orchestrator.flows import named_session_flow

                result = await named_session_flow(self._orch, key, session_name, label)

            if result.text:
                await send_rich(
                    self._bot,
                    key.chat_id,
                    result.text,
                    SendRichOpts(
                        allowed_roots=self.file_roots(self._orch.paths),
                        thread_id=thread_id,
                    ),
                )

    async def _handle_file_browser(
        self,
        key: SessionKey,
        message_id: int,
        data: str,
        *,
        thread_id: int | None = None,
    ) -> None:
        """Handle file browser navigation or file request."""
        chat_id = key.chat_id
        text, keyboard, prompt = await handle_file_browser_callback(self._orch.paths, data)

        if prompt:
            # File request: remove the keyboard and send prompt to orchestrator
            with contextlib.suppress(TelegramBadRequest):
                await self._bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message_id, reply_markup=None
                )
            async with input_lock(self._sequential.get_lock(key.lock_key)):
                if self._config.streaming.enabled:
                    fake_msg = await self._bot.send_message(
                        chat_id,
                        prompt,
                        parse_mode=None,
                        message_thread_id=thread_id,
                    )
                    await self._handle_streaming(fake_msg, key, prompt, thread_id=thread_id)
                else:
                    await self._handle_non_streaming(None, key, prompt, thread_id=thread_id)
            return

        # Directory navigation: edit message in-place
        with contextlib.suppress(TelegramBadRequest):
            await self._bot.edit_message_text(
                text=markdown_to_telegram_html(text),
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )

    async def _handle_queue_cancel(self, chat_id: int, data: str) -> None:
        """Handle a ``mq:<entry_id>`` callback to cancel a queued message."""
        try:
            entry_id = int(data[len(MQ_PREFIX) :])
        except (ValueError, IndexError):
            return
        await self._sequential.cancel_entry(chat_id, entry_id)

    async def _mark_button_choice(self, chat_id: int, msg: Message, label: str) -> None:
        """Edit the bot message to append ``[USER ANSWER] label`` and remove the keyboard."""
        await mark_button_choice(self._bot, chat_id, msg, label)

    # -- Messages --------------------------------------------------------------

    def _start_topic_name(self, message: Message) -> None:
        if self._topic_naming_stopped:
            return
        thread_id = get_thread_id(message)
        if message.chat.type == "private" and thread_id is not None:
            created = getattr(message, "forum_topic_created", None)
            edited = getattr(message, "forum_topic_edited", None)
            # Observe ownership before a busy lock: service-handler delivery can wait.
            if created is not None:
                self._get_topic_namer().created(
                    message.chat.id, thread_id, implicit=created.is_name_implicit is True
                )
                return
            if edited is not None:
                self._get_topic_namer().edited(message.chat.id, thread_id)
                return
        text = message.text or getattr(message, "caption", None) or ""
        if (
            message.chat.type == "private"
            and get_thread_id(message) is not None
            and message.from_user
            and getattr(message.from_user, "is_bot", True) is False
            and (text.strip() or has_media(message))
            and not text.lstrip().startswith("/")
        ):
            self._get_topic_namer().on_user_message(get_session_key(message), text)

    async def _on_message(self, message: Message, topic_naming_started: bool = False) -> None:
        if not topic_naming_started:
            self._start_topic_name(message)
        text = await self._resolve_text(message)
        if text is None:
            return

        key = get_session_key(message)
        thread_id = get_thread_id(message)
        logger.debug("Message text=%s", text[:80])

        # #63: status_reaction (stage-based) wins over seen_reaction (one-shot).
        # Both enabled would fight over the same Telegram emoji slot.
        if self._config.scene.seen_reaction and not self._config.scene.status_reaction:
            await self._set_seen_reaction(message)

        if self._config.streaming.enabled:
            await self._handle_streaming(message, key, text, thread_id=thread_id)
        else:
            await self._handle_non_streaming(message, key, text, thread_id=thread_id)

    async def _set_seen_reaction(self, message: Message) -> None:
        """Set a seen reaction on the user message. Graceful degradation on failure."""
        try:
            from aiogram.types import ReactionTypeEmoji

            await self._bot.set_message_reaction(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji="\U0001f440")],
            )
        except Exception:
            logger.debug("Failed to set seen reaction", exc_info=True)

    async def _resolve_text(self, message: Message) -> str | None:
        """Extract processable text from *message* (plain text or media prompt)."""
        if should_drop_in_group(
            message,
            bot_id=self._bot_id,
            bot_username=self._bot_username,
            group_mention_only=self._config.group_mention_only,
        ):
            return None

        if has_media(message):
            paths = self._orch.paths
            media_prompt = await resolve_media_text(
                self._bot, message, paths.telegram_files_dir, paths.workspace
            )
            if media_prompt is None:
                return None
            return prepend_reply_to_media(message, media_prompt)
        if not message.text:
            return None
        text = strip_mention(message.text, self._bot_username)
        return build_reply_prompt(message, text)

    async def _handle_streaming(
        self,
        message: Message,
        key: SessionKey,
        text: str,
        *,
        thread_id: int | None = None,
    ) -> str:
        """Streaming flow: coalescer -> stream editor -> Telegram."""
        return await run_streaming_message(
            StreamingDispatch(
                bot=self._bot,
                orchestrator=self._orch,
                message=message,
                key=key,
                text=text,
                streaming_cfg=self._config.streaming,
                allowed_roots=self.file_roots(self._orch.paths),
                thread_id=thread_id,
                scene_config=self._config.scene,
                reply_phase=self._reply_phase,
            ),
        )

    async def _handle_non_streaming(
        self,
        reply_to: Message | None,
        key: SessionKey,
        text: str,
        *,
        thread_id: int | None = None,
    ) -> str:
        """Non-streaming flow: one-shot orchestrator call -> Telegram delivery.

        ``reply_to`` doubles as the user's trigger message (passed as
        ``message`` so the reaction tracker anchors consistently with the
        streaming path — MED #10).
        """
        return await run_non_streaming_message(
            NonStreamingDispatch(
                bot=self._bot,
                orchestrator=self._orch,
                key=key,
                text=text,
                allowed_roots=self.file_roots(self._orch.paths),
                message=reply_to,
                reply_to=reply_to,
                thread_id=thread_id,
                scene_config=self._config.scene,
                reply_phase=self._reply_phase,
            ),
        )

    # -- Background handlers ---------------------------------------------------

    async def on_async_interagent_result(self, result: AsyncInterAgentResult) -> DeliveryOutcome:
        """Handle async inter-agent result via the message bus."""
        from ductor_bot.bus.adapters import (
            build_interagent_injection_prompt,
            from_interagent_result,
        )
        from ductor_bot.bus.envelope import DeliveryOutcome

        if result.result_origin == "cron-broadcast" and not result.chat_id:
            if result.topic_id is not None:
                return DeliveryOutcome(
                    ok=False, injected=False, delivered=False, error="topic without chat_id"
                )
            prompt = build_interagent_injection_prompt(
                result,
                agent_name=self._agent_name,
                transport_label="Telegram broadcast",
            )
            return await self._bus.submit(
                from_interagent_result(result, 0, injection_prompt=prompt, transport="tg")
            )

        if result.transport and result.transport != "tg":
            logger.debug(
                "Skipping async interagent result for transport=%s in Telegram handler",
                result.transport,
            )
            return DeliveryOutcome(
                ok=False, injected=False, delivered=False, error="transport mismatch"
            )

        if not result.chat_id and result.topic_id is not None:
            logger.warning("IA result has topic_id without chat_id: task=%s", result.task_id)
            return DeliveryOutcome(
                ok=False, injected=False, delivered=False, error="topic without chat_id"
            )
        chat_id = result.chat_id
        if not chat_id:
            logger.info(
                "ia-async result has no origin chat → default DM task=%s sender=%s",
                result.task_id,
                result.sender,
            )
            chat_id = self._config.allowed_user_ids[0] if self._config.allowed_user_ids else 0
        if not chat_id:
            logger.warning("No chat_id available for async interagent result delivery")
            return DeliveryOutcome(ok=False, injected=False, delivered=False, error="no chat_id")
        set_log_context(operation="ia-async", chat_id=chat_id)

        injection_prompt = build_interagent_injection_prompt(
            result,
            agent_name=self._agent_name,
            transport_label="Telegram chat",
        )
        if injection_prompt:
            logger.info(
                "ia-async inject: task=%s from=%s prompt_len=%d "
                "chat_id=%s topic_id=%s transport=%s",
                result.task_id,
                result.recipient or result.sender,
                len(injection_prompt),
                result.chat_id,
                result.topic_id,
                result.transport,
            )

        return await self._bus.submit(
            from_interagent_result(
                result,
                chat_id,
                injection_prompt=injection_prompt,
                transport="tg",
            )
        )

    async def on_task_result(self, result: TaskResult) -> None:
        """Handle background task result via the message bus."""
        from ductor_bot.bus.adapters import from_task_result

        chat_id = result.chat_id
        if not chat_id:
            chat_id = self._config.allowed_user_ids[0] if self._config.allowed_user_ids else 0
        if not chat_id:
            logger.warning("No chat_id for task result delivery (task=%s)", result.task_id)
            return
        set_log_context(operation="task", chat_id=chat_id)
        await self._bus.submit(from_task_result(result))

    async def notify_task_interrupted(self, result: TaskResult) -> bool:
        """Report a task a restart cut off to its chat and topic, named for this transport."""
        from ductor_bot.bus.adapters import from_task_result

        chat_id = result.chat_id or (
            self._config.allowed_user_ids[0] if self._config.allowed_user_ids else 0
        )
        if not chat_id:
            return False
        env = from_task_result(result)
        env.chat_id = chat_id
        env.transport = "tg"
        return (await self._bus.submit(env)).delivered

    async def on_task_question(  # noqa: PLR0913, PLR0917
        self,
        task_id: str,
        question: str,
        prompt_preview: str,
        chat_id: int,
        thread_id: int | None = None,
        transport: str = "",
    ) -> None:
        """Deliver a background task question via the message bus."""
        from ductor_bot.bus.adapters import from_task_question

        if not chat_id:
            chat_id = self._config.allowed_user_ids[0] if self._config.allowed_user_ids else 0
        if not chat_id:
            logger.warning("No chat_id for task question delivery (task=%s)", task_id)
            return
        set_log_context(operation="task", chat_id=chat_id)
        await self._bus.submit(
            from_task_question(task_id, question, prompt_preview, chat_id, topic_id=thread_id)
        )

    async def _handle_webhook_wake(self, chat_id: int, prompt: str) -> str | None:
        """Process webhook wake prompt via the message bus."""
        from ductor_bot.bus.envelope import LockMode

        set_log_context(operation="wh", chat_id=chat_id)
        key = SessionKey(chat_id=chat_id)
        lock = self._lock_pool.get(key.lock_key)
        async with lock:
            result = await self._orch.handle_message(key, prompt)

        # Deliver result — lock already released, skip bus lock
        from ductor_bot.bus.adapters import from_webhook_wake

        env = from_webhook_wake(chat_id, prompt)
        env.result_text = result.text
        env.lock_mode = LockMode.NONE  # Lock already held above
        await self._bus.submit(env)
        return result.text

    async def _sync_commands(self) -> None:
        from aiogram.types import (
            BotCommandScopeAllGroupChats,
            BotCommandScopeAllPrivateChats,
        )

        desired = _BOT_COMMANDS + (
            [BotCommand(command=cmd, description=desc) for cmd, desc in get_main_only_commands()]
            if self._agent_name == "main"
            else []
        )

        # Clear legacy scoped commands (previous versions set per-scope lists).
        # Telegram keeps scoped commands independently — they must be deleted
        # explicitly or they shadow the default-scope list.
        for scope in (BotCommandScopeAllPrivateChats(), BotCommandScopeAllGroupChats()):
            try:
                scoped = await self._bot.get_my_commands(scope=scope)
                if scoped:
                    await self._bot.delete_my_commands(scope=scope)
                    logger.info("Cleared legacy %s commands", type(scope).__name__)
            except TelegramAPIError:
                pass  # scope not set — nothing to clear

        # Set default-scope commands (shown everywhere).
        # Compare as ordered list so reordering triggers an update.
        current = await self._bot.get_my_commands()
        current_tuples = [(c.command, c.description) for c in current]
        desired_tuples = [(c.command, c.description) for c in desired]
        if current_tuples != desired_tuples:
            await self._bot.set_my_commands(desired)
            logger.info("Updated %d bot commands", len(desired))

    async def _watch_restart_marker(self) -> None:
        """Poll for restart-requested marker file."""
        paths = self._orch.paths
        marker = paths.ductor_home / "restart-requested"
        try:
            while True:
                await asyncio.sleep(2.0)
                if await asyncio.to_thread(consume_restart_marker, marker_path=marker):
                    logger.info("Restart marker detected, stopping polling")
                    self._exit_code = EXIT_RESTART
                    self._stop_requested = True
                    await self._dp.stop_polling()
        except asyncio.CancelledError:
            logger.debug("Restart watcher cancelled")

    def _shutdown_begun(self) -> bool:
        """Whether the stop has begun: from then on no update may start a new turn."""
        if self._stop_requested:
            return True
        tracker = getattr(self._orchestrator, "inflight_tracker", None)
        return getattr(tracker, "shutting_down", False) is True

    def request_stop(self) -> bool:
        """Ask polling to end on its own (``run()`` then returns); does not wait.

        Also valid before polling started: ``run()`` then ends right after its startup.
        """
        self._stop_requested = True
        self._stop_requested = True
        self._dp.signal_stop()
        return True

    async def _replay_update_journal(self, **kwargs: Any) -> None:
        """Feed the updates that were received but not started before the last stop (once)."""
        if self._journal_replayed:
            return
        self._journal_replayed = True
        try:
            expired = self._journal.expire_older_than(PENDING_TTL_SECONDS)
        except OSError:
            # They stay pending and are not replayed; the notice and the move wait for the next start.
            logger.warning("Old waiting messages could not be set aside; skipped for this start")
            expired = {}
        workflow = {key: value for key, value in kwargs.items() if key != "bot"}
        replayed = self._dp.replay_journal(
            self._bot, stale_before=time.time() - PENDING_TTL_SECONDS, **workflow
        )
        if replayed:
            logger.info("Replaying %d update(s) received before the restart", replayed)
        for (chat_id, topic_id), count in expired.items():
            if chat_id is None:
                continue
            try:  # no retry; the notification service logs a failed send
                await self._notification_service.notify_origin(
                    self._notification_service.transport_name,
                    chat_id,
                    t("startup.stale_updates_skipped", count=count),
                    topic_id=topic_id,
                )
            except Exception:
                logger.warning("Could not send the skipped-messages notice", exc_info=True)

    async def _honour_stop_request(self) -> None:
        """Last startup step: a stop asked for before polling began ends it at once."""
        if self._stop_requested:
            self._dp.signal_stop()

    async def _stop_polling_owned(self, budget: ShutdownBudget) -> None:
        """End the polling task this bot owns, leaving no polling task behind.

        Stop through the dispatcher first, so ``start_polling`` ends by itself and reaps its
        children. A task that does not end in time loses its polling children first and is
        then cancelled. Polling that never started has no children and is just cancelled.
        """
        self._stop_requested = True
        task = self._polling_task
        if task is None or task.done():
            return
        self._stop_requested = True
        if self._dp.polling_children():
            self._dp.signal_stop()
            await _wait_done(task, budget.cap(_POLLING_STOP_SECONDS))
            if task.done():
                return
            for child in self._dp.polling_children():
                child.cancel()
            self._dp.signal_stop()
            await _wait_done(task, budget.cap(_POLLING_FORCE_SECONDS))
        if not task.done():
            task.cancel()
            # An orphaned stop-signal waiter of the cancelled start_polling ends with this signal.
            self._dp.signal_stop()
            await _wait_done(task, budget.cap(_POLLING_FORCE_SECONDS))
        # A child created just before the owner went away may only start now.
        await asyncio.sleep(0)
        stragglers = self._dp.polling_children()
        for child in stragglers:
            child.cancel()
        if stragglers:
            await asyncio.gather(*stragglers, return_exceptions=True)
        if not task.done():
            logger.warning("Telegram polling task did not end within the shutdown budget")

    async def run(self) -> int:
        """Start polling. Returns exit code (0 = normal, 42 = restart).

        The polling task is owned here: when ``run()`` is cancelled (for example when the
        supervisor stops this agent) polling is stopped and reaped before the cancellation goes on.
        """
        if self._stop_requested:
            return self._exit_code
        logger.info("Starting Telegram bot (aiogram, long-polling)...")
        # Updates the server still holds stay: whatever arrived while the bot was down is
        # processed, and a message that was only waiting is replayed from the journal.
        await self._bot.delete_webhook(drop_pending_updates=False)
        allowed_updates = self._dp.resolve_used_update_types()
        logger.info("Polling allowed_updates=%s", ",".join(allowed_updates))
        self._dp.startup.register(self._honour_stop_request)
        # Last startup handler: the orchestrator, recovery and the supervisor's hooks are ready.
        self._dp.startup.register(self._replay_update_journal)
        self._polling_task = asyncio.create_task(
            self._dp.start_polling(
                self._bot,
                allowed_updates=allowed_updates,
                close_bot_session=False,  # shutdown() closes it after the replies went out
                handle_signals=False,
            ),
            name=f"telegram-polling:{self._agent_name}",
        )
        try:
            # shield: this wait may be cancelled, the polling task only through _stop_polling_owned
            await asyncio.shield(self._polling_task)
        except asyncio.CancelledError:
            await self._stop_polling_owned(ShutdownBudget())
            raise
        return self._exit_code

    async def shutdown(self, budget: ShutdownBudget | None = None) -> None:
        """Shut the bot down in one fixed order; every wait is capped by the shared *budget*.

        1. stop receiving updates (polling ends, it is not left running);
        2. wait for the handlers that are sending the reply of a finished turn;
        3. run the orchestrator's pre-shutdown hooks (the CLI processes are still alive);
        4. shut the orchestrator down (kills the CLI processes of running turns);
        5. give the remaining handlers a moment to end;
        6. close the bot session.

        ``/restart``, a termination signal and the supervisor's stops all come through here.
        """
        budget = budget or ShutdownBudget()
        self._stop_requested = True  # no update starts a new turn from here on
        self._topic_naming_stopped = True
        naming_stop = asyncio.ensure_future(self._topic_namer.stop()) if self._topic_namer else None
        await _cancel_task(self._restart_watcher)
        await _cancel_task(self._group_audit_task)

        await self._stop_polling_owned(budget)
        await self._wait_for_naming_stop(naming_stop, budget)
        await self._reply_phase.wait_idle(budget.cap(_REPLY_DRAIN_SECONDS))
        if self._orchestrator:
            await self._orchestrator.run_pre_shutdown_hooks(budget)
            await self._orchestrator.shutdown(budget)
        await self._finish_handlers(budget.cap(_HANDLER_FINISH_SECONDS))

        # Release the Telegram polling session so a new bot instance can start.
        # Without this, Telegram rejects the next getUpdates call with
        # TelegramConflictError ("terminated by other getUpdates request").
        with contextlib.suppress(Exception):
            await wait_capped(
                self._bot.delete_webhook(drop_pending_updates=False),
                budget.remaining(),
                label="delete webhook",
            )
        await self._secret_input.close(budget_seconds=budget.cap(5.0))
        with contextlib.suppress(Exception):
            await self._bot.session.close()

        logger.info("Telegram bot shut down")

    @staticmethod
    async def _wait_for_naming_stop(
        naming_stop: asyncio.Future[None] | None, budget: ShutdownBudget
    ) -> None:
        """Give the cancelled topic-name tasks the time left, at most a few seconds, to clean up.

        Their cleanup (reaping a CLI process) has no end of its own; what is not done in time
        goes on in the background and the shutdown does not wait for it.
        """
        if naming_stop is None:
            return
        await asyncio.wait({naming_stop}, timeout=budget.cap(_NAMING_CLEANUP_SECONDS))
        if not naming_stop.done():
            logger.warning("Topic naming cleanup still running; the shutdown goes on")
        elif not naming_stop.cancelled():
            naming_stop.exception()  # mark it retrieved

    async def _finish_handlers(self, seconds: float) -> None:
        """Wait up to *seconds* for the update handlers that are still running."""
        handlers = self._dp.pending_handlers()
        if handlers and seconds > 0:
            await asyncio.wait(handlers, timeout=seconds)
