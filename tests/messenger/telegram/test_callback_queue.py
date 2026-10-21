"""Button choices share the message queue without changing command ingress."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.i18n import t
from ductor_bot.messenger.queue_context import current_queue_context
from ductor_bot.messenger.telegram.app import TelegramBot
from tests.messenger.telegram.test_app import _make_config, _make_orchestrator, _make_tg_bot
from tests.messenger.telegram.test_command_buttons import callback


def setup_bot(*, mention_only=False):
    bot, api = _make_tg_bot(_make_config(group_mention_only=mention_only))
    bot._handle_streaming = AsyncMock()
    bot._handle_non_streaming = AsyncMock()
    bot._orchestrator = _make_orchestrator()
    api.delete_message = AsyncMock()
    ready = asyncio.Queue()

    async def sent(*_args, **_kwargs):
        ready.put_nowait(None)
        return MagicMock(message_id=90)

    api.send_message.side_effect = sent
    return bot, api, ready


async def queued(ready):
    await asyncio.wait_for(ready.get(), timeout=1)


async def test_busy_button_uses_existing_notice_cancel_button_and_reply():
    bot, api, ready = setup_bot()
    lock = bot._sequential.get_lock((1, 5))
    await lock.acquire()
    task = asyncio.create_task(bot._on_callback_query(callback("Approve")))
    try:
        await queued(ready)
        bot._handle_streaming.assert_not_awaited()
        sent = api.send_message.call_args
        assert sent.args == (1, t("notice.queue_waiting"))
        assert sent.kwargs["message_thread_id"] == 5
        assert sent.kwargs["reply_parameters"].message_id == 42
        button = sent.kwargs["reply_markup"].inline_keyboard[0][0]
        assert button.text == t("notice.queue_cancel_button")
        assert button.callback_data.startswith("mq:")
        assert bot._sequential._pending[1][0].text_preview == "Approve"
    finally:
        lock.release()
        await asyncio.wait_for(task, timeout=1)
    api.delete_message.assert_awaited_once_with(1, 90)
    bot._handle_streaming.assert_awaited_once()
    assert not bot._sequential.has_pending(1)
    assert not bot._button_choices


@pytest.mark.parametrize("streaming", [True, False])
async def test_idle_button_has_no_queue_indicator(streaming):
    bot, api, _ = setup_bot()
    bot._config.streaming.enabled = streaming
    await bot._on_callback_query(callback("Approve"))
    api.send_message.assert_not_awaited()
    handler = bot._handle_streaming if streaming else bot._handle_non_streaming
    handler.assert_awaited_once()


async def test_duplicate_reservation_precedes_choice_marking_and_covers_execution():
    bot, api, ready = setup_bot()
    marking = asyncio.Event()
    proceed = asyncio.Event()
    running = asyncio.Event()
    finish = asyncio.Event()

    async def mark(*_args):
        marking.set()
        await proceed.wait()

    async def handle(*_args, **_kwargs):
        running.set()
        await finish.wait()

    bot._mark_button_choice = AsyncMock(side_effect=mark)
    bot._handle_streaming.side_effect = handle
    lock = bot._sequential.get_lock((1, 5))
    await lock.acquire()
    first = asyncio.create_task(bot._on_callback_query(callback("Approve")))
    try:
        await asyncio.wait_for(marking.wait(), 1)
        second = callback("Approve", callback_id="second")
        await asyncio.wait_for(bot._on_callback_query(second), 1)
        second.answer.assert_awaited_once()
        bot._mark_button_choice.assert_awaited_once()
        proceed.set()
        await queued(ready)
        await bot._on_callback_query(callback("Approve", callback_id="third"))
        assert api.send_message.await_count == 1
        lock.release()
        await asyncio.wait_for(running.wait(), 1)
        await bot._on_callback_query(callback("Approve", callback_id="fourth"))
        assert bot._handle_streaming.await_count == 1
    finally:
        proceed.set()
        finish.set()
        if lock.locked() and not running.is_set():
            lock.release()
        await asyncio.wait_for(first, 1)
    assert not bot._button_choices
    await bot._on_callback_query(callback("Approve", callback_id="fifth"))
    assert bot._handle_streaming.await_count == 2


@pytest.mark.parametrize(("second_data", "second_message"), [("Reject", 42), ("Approve", 43)])
async def test_distinct_data_or_message_keeps_separate_queue_entries(second_data, second_message):
    bot, _, ready = setup_bot()
    lock = bot._sequential.get_lock((1, 5))
    await lock.acquire()
    tasks = [asyncio.create_task(bot._on_callback_query(callback("Approve")))]
    try:
        await queued(ready)
        cb = callback(second_data, callback_id="second")
        cb.message = cb.message.model_copy(update={"message_id": second_message})
        tasks.append(asyncio.create_task(bot._on_callback_query(cb)))
        await queued(ready)
        assert len(bot._sequential._pending[1]) == 2
    finally:
        lock.release()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
    assert bot._handle_streaming.await_count == 2


async def test_queue_cancel_callback_prevents_execution_and_allows_later_reclick():
    bot, api, ready = setup_bot()
    lock = bot._sequential.get_lock((1, 5))
    await lock.acquire()
    task = asyncio.create_task(bot._on_callback_query(callback("Approve")))
    try:
        await queued(ready)
        entry = bot._sequential._pending[1][0]
        await asyncio.wait_for(bot._on_callback_query(callback(f"mq:{entry.entry_id}")), 1)
        assert entry.cancelled
        assert api.edit_message_text.call_args.kwargs["text"] == t("notice.queue_cancelled")
    finally:
        lock.release()
        await asyncio.wait_for(task, 1)
    bot._handle_streaming.assert_not_awaited()
    assert not bot._sequential.has_pending(1)
    assert not bot._button_choices
    await bot._on_callback_query(callback("Approve", callback_id="again"))
    bot._handle_streaming.assert_awaited_once()


async def test_cancelled_choice_stays_reserved_until_lock_then_allows_reclick():
    bot, api, ready = setup_bot()
    lock = bot._sequential.get_lock((1, 5))
    await lock.acquire()
    task = asyncio.create_task(bot._on_callback_query(callback("Approve")))
    try:
        await queued(ready)
        entry = bot._sequential._pending[1][0]
        await bot._on_callback_query(callback(f"mq:{entry.entry_id}"))
        retry = callback("Approve", callback_id="retry-before-unlock")
        await asyncio.wait_for(bot._on_callback_query(retry), 1)
        retry.answer.assert_awaited_once()
        assert api.send_message.await_count == 1
        bot._handle_streaming.assert_not_awaited()
        assert bot._button_choices == {(1, 42, "Approve")}
        assert bot._sequential._pending[1] == [entry]
        assert not task.done()
    finally:
        lock.release()
        await asyncio.wait_for(task, 1)
    bot._handle_streaming.assert_not_awaited()
    assert not bot._button_choices
    assert not bot._sequential.has_pending(1)
    await bot._on_callback_query(callback("Approve", callback_id="retry-after-unlock"))
    bot._handle_streaming.assert_awaited_once()
    assert api.send_message.await_count == 1


@pytest.mark.parametrize("phase", ["before", "waiting", "cancelled"])
async def test_shutdown_or_task_cancellation_cleans_queue_and_reservation(phase):
    bot, api, ready = setup_bot()
    if phase == "before":
        bot._stop_requested = True
        await bot._on_callback_query(callback("Approve"))
        api.send_message.assert_not_awaited()
    else:
        lock = bot._sequential.get_lock((1, 5))
        await lock.acquire()
        task = asyncio.create_task(bot._on_callback_query(callback("Approve")))
        try:
            await queued(ready)
            bot._stop_requested = True
            if phase == "cancelled":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                lock.release()
                await asyncio.wait_for(task, 1)
            api.delete_message.assert_awaited_once_with(1, 90)
        finally:
            if lock.locked():
                lock.release()
    bot._handle_streaming.assert_not_awaited()
    assert not bot._sequential.has_pending(1)
    assert not bot._button_choices


@pytest.mark.parametrize("failure", ["mark", "handler"])
async def test_exception_releases_reservation_for_reclick(failure):
    bot, _, _ = setup_bot()
    failing = bot._handle_streaming
    if failure == "mark":
        bot._mark_button_choice = AsyncMock()
        failing = bot._mark_button_choice
    failing.side_effect = RuntimeError("test failure")
    with pytest.raises(RuntimeError, match="test failure"):
        await bot._on_callback_query(callback("Approve"))
    assert not bot._button_choices
    failing.side_effect = None
    await bot._on_callback_query(callback("Approve", callback_id="again"))
    assert failing.await_count == 2


async def test_callback_bypasses_message_ingress_and_uses_click_wait_metadata():
    bot, _, ready = setup_bot(mention_only=True)
    bot._sequential._dedup.check("1:42")
    bot._sequential._message_received_handler = MagicMock()
    bot._sequential._is_unaddressed_group = MagicMock(return_value=True)
    context = []

    async def handle(*_args, **_kwargs):
        context.append(current_queue_context("UTC"))

    bot._handle_streaming.side_effect = handle
    cb = callback("Approve", chat_type="supergroup")
    cb.message = cb.message.model_copy(update={"date": datetime(2000, 1, 1, tzinfo=UTC)})
    lock = bot._sequential.get_lock((1, 5))
    await lock.acquire()
    task = asyncio.create_task(bot._on_callback_query(cb))
    try:
        await queued(ready)
    finally:
        lock.release()
        await asyncio.wait_for(task, 1)
    assert "[Queued user message]" in context[0]
    assert "[Received:" in context[0]
    assert "Sent at: unknown" in context[0]
    assert "2000-01-01" not in context[0]
    bot._sequential._is_unaddressed_group.assert_not_called()
    bot._sequential._message_received_handler.assert_not_called()


async def test_stop_drains_mixed_topic_queue_and_preserves_other_topic():
    bot, _, ready = setup_bot()
    lock = bot._sequential.get_lock((1, 5))
    other_lock = bot._sequential.get_lock((1, 6))
    await lock.acquire()
    await other_lock.acquire()
    message_handler = AsyncMock()
    message = callback("unused").message.model_copy(update={"text": "typed", "message_id": 51})
    other = callback("Other", callback_id="other")
    other.message = other.message.model_copy(update={"message_thread_id": 6})
    tasks = []
    try:
        for operation in [
            bot._on_callback_query(callback("Approve")),
            bot._sequential(message_handler, message, {}),
            bot._on_callback_query(other),
        ]:
            tasks.append(asyncio.create_task(operation))
            await queued(ready)
        stop = callback("/stop", callback_id="stop")

        async def abort(chat_id, event):
            assert await bot._sequential.drain_topic(chat_id, event.message_thread_id) == 2
            return True

        bot._sequential.set_abort_handler(abort)
        await asyncio.wait_for(bot._on_callback_query(stop), 1)
        entries = bot._sequential._pending[1]
        assert [entry.cancelled for entry in entries] == [True, True, False]
    finally:
        lock.release()
        other_lock.release()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
    message_handler.assert_not_awaited()
    bot._handle_streaming.assert_awaited_once()
    assert bot._handle_streaming.call_args.args[2] == "Other"
    assert not bot._button_choices


async def test_command_callbacks_keep_existing_queue_quick_abort_and_dedup():
    with patch.object(TelegramBot, "_on_new", new_callable=AsyncMock) as new:
        new.aiogram_flag = {}
        bot, _, ready = setup_bot()
    bot._sequential.run_callback = AsyncMock(side_effect=AssertionError("general button entry"))
    bot._sequential.set_quick_command_handler(quick := AsyncMock(return_value=True))
    bot._sequential.set_abort_handler(abort := AsyncMock(return_value=True))
    lock = bot._sequential.get_lock((1, 5))
    await lock.acquire()
    task = asyncio.create_task(bot._on_callback_query(callback("/new", callback_id="new")))
    try:
        await queued(ready)
        new.assert_not_awaited()
        await bot._on_callback_query(callback("/status", callback_id="status"))
        await bot._on_callback_query(callback("/stop", callback_id="stop"))
        await bot._on_callback_query(callback("/new", callback_id="new"))
        quick.assert_awaited_once()
        abort.assert_awaited_once()
        assert len(bot._sequential._pending[1]) == 1
    finally:
        lock.release()
        await asyncio.wait_for(task, 1)
    new.assert_awaited_once()
    bot._sequential.run_callback.assert_not_awaited()


async def test_special_callback_avoids_general_button_queue():
    bot, _, _ = setup_bot()
    bot._route_special_callback = AsyncMock(return_value=True)
    bot._sequential.run_callback = AsyncMock()
    await bot._on_callback_query(callback("ms:p:claude"))
    bot._sequential.run_callback.assert_not_awaited()
