"""Telegram connectivity and resumable delivery for the chat log runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ductor_bot.messenger.telegram.buttons import extract_buttons_for_session
from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich_progress_detailed

if TYPE_CHECKING:
    from ductor_bot.messenger.telegram.app import TelegramBot
    from ductor_bot.messenger.telegram.sender import SendProgress


class TelegramWebchatOps:
    def __init__(self, bot: TelegramBot) -> None:
        self._bot = bot

    async def probe(self) -> bool:
        async with asyncio.timeout(10):
            await self._bot.bot_instance.get_me()
        return True

    async def send_text(  # noqa: PLR0913 -- persisted delivery fields and progress callback
        self,
        chat_id: int,
        topic_id: int | None,
        text: str,
        start_chunk: int,
        buttons_session: str | None = None,
        *,
        on_chunk: Callable[[int], Awaitable[None]] | None = None,
    ) -> SendProgress:
        markup = None
        if buttons_session is not None:
            text, markup = extract_buttons_for_session(text, buttons_session)
        return await send_rich_progress_detailed(
            self._bot.bot_instance,
            chat_id,
            text,
            SendRichOpts(
                thread_id=topic_id,
                allowed_roots=self._bot.file_roots(self._bot._orch.paths),
                reply_markup=markup,
                skip_unavailable_files=True,
            ),
            start_chunk=start_chunk,
            on_chunk=on_chunk,
        )

    async def create_topic(self, chat_id: int, name: str) -> int:
        topic = await self._bot.bot_instance.create_forum_topic(chat_id=chat_id, name=name)
        return topic.message_thread_id

    async def rename_topic(self, chat_id: int, topic_id: int, name: str) -> None:
        await self._bot.bot_instance.edit_forum_topic(
            chat_id=chat_id, message_thread_id=topic_id, name=name
        )

    async def delete_topic(self, chat_id: int, topic_id: int) -> bool:
        return bool(
            await self._bot.bot_instance.delete_forum_topic(
                chat_id=chat_id, message_thread_id=topic_id
            )
        )
