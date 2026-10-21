"""Delivery of the weekly memory lint report turn to the user (Telegram).

The turn runs in a dedicated "Memory Lint" topic when the bot's private chat is in topic
mode (``getMe.has_topics_enabled``) and ``memory_lint.report`` is ``topic``; otherwise it
runs in the plain DM. The topic is created once and its thread id persisted; a topic the
user deleted is detected by a failed delivery and re-created once.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from ductor_bot.bus.adapters import from_webhook_wake
from ductor_bot.bus.envelope import LockMode
from ductor_bot.i18n import t
from ductor_bot.infra.atomic_io import atomic_text_save
from ductor_bot.log_context import scoped_log_context
from ductor_bot.memory_lint.target import apply_lint_target, has_target
from ductor_bot.session.key import SessionKey

if TYPE_CHECKING:
    from ductor_bot.messenger.telegram.app import TelegramBot

logger = logging.getLogger(__name__)

_STATE_KEY = "report_topic"


class MemoryLintReporter:
    """Runs one agent turn with the lint prompt and delivers the answer."""

    def __init__(self, bot: TelegramBot) -> None:
        self._bot = bot
        self._warned_dm = False

    # -- persisted topic id ----------------------------------------------------

    def _state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._bot._orch.paths.memory_lint_state_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, state: dict[str, Any]) -> None:
        atomic_text_save(
            self._bot._orch.paths.memory_lint_state_path, json.dumps(state, indent=2) + "\n"
        )

    def _stored_topic(self, chat_id: int) -> int | None:
        stored = self._state().get(_STATE_KEY)
        if (
            isinstance(stored, dict)
            and stored.get("chat_id") == chat_id
            and isinstance(stored.get("topic_id"), int)
        ):
            return int(stored["topic_id"])
        return None

    def _remember_topic(self, chat_id: int, topic_id: int | None) -> None:
        state = self._state()
        if topic_id is None:
            state.pop(_STATE_KEY, None)
        else:
            state[_STATE_KEY] = {"chat_id": chat_id, "topic_id": topic_id}
        self._save_state(state)

    # -- topic handling --------------------------------------------------------

    async def _topic_mode(self) -> bool:
        try:
            me = await self._bot.bot_instance.get_me()
        except Exception:
            logger.warning("Memory lint: getMe failed; using the plain DM", exc_info=True)
            return False
        return bool(getattr(me, "has_topics_enabled", False))

    async def _create_topic(self, chat_id: int) -> int | None:
        try:
            topic = await self._bot.bot_instance.create_forum_topic(
                chat_id=chat_id, name=t("memory_lint.topic_name")
            )
        except Exception:
            logger.warning("Memory lint: could not create the report topic", exc_info=True)
            return None
        self._remember_topic(chat_id, topic.message_thread_id)
        logger.info("Memory lint report topic created: thread=%s", topic.message_thread_id)
        return int(topic.message_thread_id)

    # -- the report turn -------------------------------------------------------

    async def report(self, prompt: str) -> None:
        # The lint turn is no continuation of whatever this task logged before.
        with scoped_log_context(clear_session=True):
            await self._report(prompt)

    async def _report(self, prompt: str) -> None:
        bot = self._bot
        users = bot.config.allowed_user_ids
        if not users:
            logger.info("Memory lint: no allowed user to report to")
            return
        chat_id = users[0]
        topic_id: int | None = None
        if bot.config.memory_lint.report == "topic" and await self._topic_mode():
            topic_id = self._stored_topic(chat_id) or await self._create_topic(chat_id)

        key = SessionKey(chat_id=chat_id, topic_id=topic_id)
        async with bot.lock_pool.get(key.lock_key):
            await self._apply_target(key)
            result = await bot._orch.handle_message(key, prompt, counts_as_activity=False)
        if not result.text:
            return
        if await self._deliver(chat_id, topic_id, prompt, result.text):
            return
        if topic_id is None:
            return
        # The stored topic may have been deleted: create a fresh one and deliver there once.
        logger.info("Memory lint: delivery to topic %s failed; re-creating it", topic_id)
        self._remember_topic(chat_id, None)
        fresh = await self._create_topic(chat_id)
        if fresh is not None:
            dst = SessionKey(chat_id=chat_id, topic_id=fresh)
            # The new topic's first user turn waits until the report session has moved there.
            async with bot.lock_pool.get(dst.lock_key):
                if await self._deliver(chat_id, fresh, prompt, result.text):
                    await self._move_session(key, dst)
                    return
        # No topic works: hand the already generated answer to the plain DM once.
        logger.warning("Memory lint: topic delivery failed; sending the report to the plain DM")
        await self._deliver(chat_id, None, prompt, result.text)

    async def _move_session(self, src: SessionKey, dst: SessionKey) -> None:
        """Let the report session continue in the re-created topic (never fails the report)."""
        try:
            moved = await self._bot._orch._sessions.move_session(src, dst)
        except Exception as exc:
            logger.warning(
                "Memory lint: could not move the report session (%s)", type(exc).__name__
            )
            return
        if moved:
            logger.info("Memory lint: report session continues in the new topic")

    async def _apply_target(self, key: SessionKey) -> None:
        """Set the configured model on the Memory Lint topic session (never on the DM)."""
        cfg = self._bot.config.memory_lint
        if not has_target(cfg):
            return
        if key.topic_id is None:
            if not self._warned_dm:
                self._warned_dm = True
                logger.warning(
                    "Memory lint: provider/model/reasoning_effort apply to the Memory Lint topic "
                    "only; the plain DM session is left unchanged"
                )
            return
        problem = await apply_lint_target(self._bot._orch, key, cfg)
        if problem:
            logger.warning("Memory lint: configured model not applied: %s", problem)

    async def _deliver(self, chat_id: int, topic_id: int | None, prompt: str, text: str) -> bool:
        env = from_webhook_wake(chat_id, prompt)
        env.topic_id = topic_id
        env.result_text = text
        env.lock_mode = LockMode.NONE  # session lock was released after the turn
        await self._bot._bus.submit(env)
        return env.delivered
