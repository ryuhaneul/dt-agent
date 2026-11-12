"""Independent conversation recording, buffering and Telegram connectivity lifetime."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from ductor_bot.chat_log.events import EventHub
from ductor_bot.chat_log.history import HistoryStore
from ductor_bot.chat_log.link import OutboxDrainer, TelegramLinkMonitor
from ductor_bot.chat_log.outbox import Outbox
from ductor_bot.chat_log.prune import RetentionPruner
from ductor_bot.chat_log.recorder import HistoryRecorder
from ductor_bot.chat_log.settings import chat_log_active, validate_chat_log
from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps

if TYPE_CHECKING:
    from ductor_bot.messenger.telegram.app import TelegramBot

logger = logging.getLogger(__name__)
_retention_sleep = asyncio.sleep


class ChatLogRuntime:
    def __init__(self, bot: TelegramBot) -> None:
        self._bot = bot
        self.events = EventHub()
        directory = bot._orch.paths.chat_log_dir
        self.history = HistoryStore(directory / "history", self.events)
        self.outbox = Outbox(directory / "outbox", self.events)
        self.recorder = HistoryRecorder(self.history, self.events, bot.config.allowed_user_ids)
        self.ops = TelegramWebchatOps(bot)
        self.link = TelegramLinkMonitor(self.ops, self.events)
        self.drainer = OutboxDrainer(self.outbox, self.ops, self.link)
        self.link.on_up = self.drainer.wake
        self._stopped = False
        self._retention_task: asyncio.Task[None] | None = None
        self._pruner = RetentionPruner(
            self.history, self.outbox, bot._orch.paths.webchat_dir / "backups"
        )

    @classmethod
    async def start(cls, bot: TelegramBot) -> ChatLogRuntime | None:
        if not bot.config.chat_log.enabled or bot._agent_name != "main":
            return None
        if bot._chat_log_runtime is not None:
            return bot._chat_log_runtime
        if bot._chat_log_checked:
            return None
        bot._chat_log_checked = True
        errors = validate_chat_log(bot.config)
        if errors:
            logger.error("Chat log disabled: %s", ", ".join(errors))
            return None
        if not chat_log_active(bot.config, bot._agent_name):
            return None
        runtime = cls(bot)
        bot._chat_log_runtime = runtime
        bot._orch.set_history_recorder(runtime.recorder)
        bot._bus.set_delivery_observer(runtime.recorder.on_delivered)
        bot._telegram_transport.set_outbox(
            runtime.outbox,
            on_failure=runtime.link.report_failure,
            on_retry_after=runtime.drainer.defer_retry,
            is_blocked=runtime.recorder.blocked.__contains__,
        )
        runtime.drainer.start()
        runtime.link.start()
        if bot.config.chat_log.retention_days or bot.config.chat_log.backup_retention_days:
            runtime._retention_task = asyncio.create_task(
                runtime._run_retention(), name="chat-log-retention"
            )
        logger.info("Chat log runtime started")
        return runtime

    async def _run_retention(self) -> None:
        while True:
            try:
                await self._pruner.prune(
                    history_days=self._bot.config.chat_log.retention_days,
                    backup_days=self._bot.config.chat_log.backup_retention_days,
                )
            except Exception as exc:
                logger.warning("Chat log retention failed: %s", type(exc).__name__)
            await _retention_sleep(86400)

    async def _stop_retention(self) -> None:
        if self._retention_task is not None:
            self._retention_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._retention_task
            self._retention_task = None

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            await asyncio.gather(self.link.stop(), self.drainer.stop(), self._stop_retention())
        finally:
            self._bot._telegram_transport.set_outbox(None)
            self._bot._bus.set_delivery_observer(None)
            self._bot._orch.set_history_recorder(None)
