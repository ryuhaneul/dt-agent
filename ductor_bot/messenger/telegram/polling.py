"""A dispatcher whose polling tasks can be reached, so a stopped bot leaves none behind.

``Dispatcher.start_polling`` starts its ``_polling`` task and a stop-signal waiter as children
and only waits for them with ``asyncio.wait``. Cancelling ``start_polling`` (as a supervisor
does to stop an agent) therefore leaves those children running: the old bot keeps asking
Telegram for updates and collides with the next instance of the same bot. This subclass records
the polling children and exposes a non-blocking stop request so the owner can end polling
through the dispatcher's own shutdown path and reap whatever remains.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from functools import partial
from typing import TYPE_CHECKING, Any

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from ductor_bot.infra.input_context import received_at as receipt_scope

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from ductor_bot.messenger.telegram.secret_input import SecretInput
    from ductor_bot.messenger.telegram.update_journal import UpdateJournal

logger = logging.getLogger(__name__)


_SAVE_RETRY_FIRST_SECONDS = 0.5
_SAVE_RETRY_MAX_SECONDS = 30.0


async def _until_saved(save: Callable[[], None], update_id: int) -> None:
    """Run *save* until it works, pausing longer after each failure (a warning each time)."""
    pause = _SAVE_RETRY_FIRST_SECONDS
    while True:
        try:
            save()
        except OSError as exc:
            logger.warning(
                "Telegram update journal could not be saved (update %d, %s); trying again in %.1fs",
                update_id,
                type(exc).__name__,
                pause,
            )
            await asyncio.sleep(pause)
            pause = min(pause * 2, _SAVE_RETRY_MAX_SECONDS)
        else:
            return


class PollingDispatcher(Dispatcher):
    """``Dispatcher`` that tracks its polling tasks and accepts a non-blocking stop."""

    def __init__(
        self,
        *args: Any,
        journal: UpdateJournal | None = None,
        secret_input: SecretInput | None = None,
        **kwargs: Any,
    ) -> None:
        """With a *journal*, every received update is recorded before it is handed on."""
        super().__init__(*args, **kwargs)
        self._polling_children: set[asyncio.Task[Any]] = set()
        self._journal = journal
        self._secret_input = secret_input

    async def _listen_updates(self, bot: Bot, **kwargs: Any) -> AsyncGenerator[Update, None]:  # type: ignore[override]
        """Read updates like the library, writing each one to the journal before it is yielded.

        The next ``getUpdates`` (which confirms the batch to the server) only goes out after the
        reader has been through the whole batch, so the record comes first.
        """
        async for update in super()._listen_updates(bot, **kwargs):
            if self._secret_input is not None and await self._secret_input.intercept(update):
                if self._journal is not None:
                    await _until_saved(
                        partial(self._journal.record_handled, update.update_id), update.update_id
                    )
                continue
            if self._journal is None or await self._admit(update, self._journal):
                yield update

    @staticmethod
    async def _admit(update: Update, journal: UpdateJournal) -> bool:
        """Journal *update*; False when it must not be processed (started before, or in hand).

        A save that fails is retried with a growing pause and the update is not handed on in the
        meantime. The reader is suspended here, so the next ``getUpdates`` (which would confirm
        this update to the server) does not go out before the update is on disk.
        """
        update_id = update.update_id
        if update.callback_query is not None:
            if journal.is_handled(update_id):
                return False
            # a button press is not replayed after a restart
            await _until_saved(lambda: journal.record_handled(update_id), update_id)
            return True
        message = update.message
        if message is None:
            return True
        topic_id = message.message_thread_id if message.is_topic_message else None
        try:
            payload = update.model_dump(
                mode="json", by_alias=True, exclude_none=True, exclude_unset=True
            )
        except Exception as exc:
            logger.error(  # noqa: TRY400 - traceback may expose message content
                "Telegram update journal serialization failed (update %d, %s)",
                update_id,
                type(exc).__name__,
            )
            if journal.is_handled(update_id):
                return False
            # An unserializable update is handled now and is not replayed after a restart.
            await _until_saved(lambda: journal.record_handled(update_id), update_id)
            return True
        await _until_saved(
            lambda: journal.record_pending(
                update_id,
                payload,
                message.chat.id,
                topic_id,
            ),
            update_id,
        )
        return journal.claim(update_id)

    def replay_journal(self, bot: Bot, *, stale_before: float = 0.0, **workflow: Any) -> int:
        """Feed the journal's pending updates, oldest id first, and return how many.

        Call it from the last startup handler: the orchestrator and everything the startup
        handlers set up are ready by then. *workflow* is the data the polling loop passes on.
        Updates received before *stale_before* are left alone (still pending).
        """
        replayed = 0
        for entry in self._journal.pending() if self._journal is not None else []:
            if entry.received_at < stale_before:
                continue
            if self._journal is None or not self._journal.claim(entry.update_id):
                continue
            try:
                update = Update.model_validate(entry.update, context={"bot": bot})
            except ValueError:
                logger.warning("Dropped an unreadable journal entry (update %d)", entry.update_id)
                with contextlib.suppress(OSError):  # it stays pending and is dropped again later
                    self._journal.mark_started(entry.update_id)
                continue
            self.replay_update(bot, update, entry.received_at, **workflow)
            replayed += 1
        return replayed

    def replay_update(
        self, bot: Bot, update: Update, received: float, **kwargs: Any
    ) -> asyncio.Task[Any]:
        """Process *update* like the polling loop does (a task, not awaited).

        The original arrival time is the receipt of the replayed update.
        """
        with receipt_scope(received):
            task = asyncio.create_task(self._process_update(bot=bot, update=update, **kwargs))
        self._handle_update_tasks.add(task)
        task.add_done_callback(self._handle_update_tasks.discard)
        return task

    async def _polling(self, bot: Bot, *args: Any, **kwargs: Any) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._polling_children.add(task)
        try:
            await super()._polling(bot, *args, **kwargs)
        finally:
            if task is not None:
                self._polling_children.discard(task)

    def polling_children(self) -> list[asyncio.Task[Any]]:
        """The polling tasks that are still alive."""
        return [task for task in self._polling_children if not task.done()]

    def pending_handlers(self) -> list[asyncio.Task[Any]]:
        """Update handlers that are still running (not the task asking)."""
        current = asyncio.current_task()
        return [t for t in self._handle_update_tasks if not t.done() and t is not current]

    def signal_stop(self) -> None:
        """Ask ``start_polling`` to end (what ``stop_polling`` does) without waiting for it."""
        signal = self._stop_signal
        if signal is not None:
            signal.set()
