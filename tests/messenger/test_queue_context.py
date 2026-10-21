"""Queued metadata with fake transports and real session locks."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.cli.base import format_cli_cmd
from ductor_bot.cli.types import AgentRequest
from ductor_bot.messenger import queue_context as qc
from ductor_bot.messenger.matrix.bot import MatrixBot
from ductor_bot.messenger.slack.bot import SlackBot
from ductor_bot.messenger.telegram.middleware import SequentialMiddleware
from ductor_bot.session.key import SessionKey

SENT = datetime(2026, 10, 5, 0, 15, tzinfo=UTC)


def message(text="original", message_id=1):
    return Message(
        message_id=message_id,
        date=SENT,
        chat=Chat(id=100, type="private"),
        from_user=User(id=100, is_bot=False, first_name="Synthetic"),
        text=text,
    )


async def test_unqueued_text_unchanged():
    lock = asyncio.Lock()
    async with qc.QueuedMessageLock(lock, SENT):
        assert qc.current_queue_context("Asia/Seoul") is None
        req = AgentRequest(prompt="original")
        assert req.cli_prompt == req.prompt == "original"
    assert not lock.locked() and qc.current_queue_context("UTC") is None


async def test_multiple_waiters_time_position_order_and_task_isolation(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(qc, "time", SimpleNamespace(monotonic=lambda: now[0]))
    lock = asyncio.Lock()
    await lock.acquire()
    received = []

    async def receive(n):
        async with qc.QueuedMessageLock(lock, SENT):
            received.append((n, qc.current_queue_context("Asia/Seoul")))

            async def child():
                assert qc.current_queue_context("UTC") is None

            await asyncio.create_task(child())

    tasks = [asyncio.create_task(receive(n)) for n in (1, 2)]
    await asyncio.sleep(0)
    now[0] = 85
    lock.release()
    await asyncio.wait_for(asyncio.gather(*tasks), 1)
    assert [n for n, _ in received] == [1, 2]
    for n, block in received:
        assert "2026-10-05 09:15:00 KST" in block and "Waited: 1m 15s" in block
        assert f"Queue position at arrival: {n}" in block and qc.QUEUE_NOTICE in block
    assert lock not in qc._waiters and qc.current_queue_context("UTC") is None


async def test_cancelled_waiter_never_processed_or_leaked():
    lock = asyncio.Lock()
    await lock.acquire()
    called = []

    async def receive():
        async with qc.QueuedMessageLock(lock, SENT):
            called.append(True)

    task = asyncio.create_task(receive())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not called and lock not in qc._waiters
    lock.release()


@pytest.mark.parametrize(
    "text,data", [("/restart", {}), ("button", {"command_callback_id": "synthetic"})]
)
async def test_telegram_commands_and_command_callbacks_have_no_metadata(text, data):
    mw = SequentialMiddleware()
    lock = mw.get_lock(SessionKey.telegram(100).lock_key)
    await lock.acquire()
    seen = []

    async def handler(event, _data):
        seen.append((event.text, qc.current_queue_context("UTC")))

    task = asyncio.create_task(mw(handler, message(text), data))
    await asyncio.sleep(0)
    lock.release()
    await asyncio.wait_for(task, 1)
    assert seen == [(text, None)]


async def test_telegram_callback_query_has_no_queue_metadata():
    mw = SequentialMiddleware()
    query = CallbackQuery(
        id="synthetic",
        from_user=User(id=100, is_bot=False, first_name="S"),
        chat_instance="synthetic",
        message=message(),
        data="prompt",
    )
    handler = AsyncMock()
    await mw(handler, query, {})
    handler.assert_awaited_once()
    assert qc.current_queue_context("UTC") is None


async def test_telegram_text_and_media_no_indicator_or_log_leak(caplog):
    mw = SequentialMiddleware()
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=999)
    mw.set_bot(bot)
    caplog.set_level(logging.DEBUG)
    lock = mw.get_lock(SessionKey.telegram(100).lock_key)
    await lock.acquire()
    seen = []

    async def handler(event, _data):
        seen.append((event.text, qc.current_queue_context("Asia/Seoul")))

    tasks = [
        asyncio.create_task(mw(handler, message("raw text", 1), {})),
        asyncio.create_task(mw(handler, message(None, 2), {})),
    ]
    await asyncio.sleep(0)
    lock.release()
    await asyncio.wait_for(asyncio.gather(*tasks), 1)
    assert [text for text, _ in seen] == ["raw text", None]
    for n, (_, block) in enumerate(seen, 1):
        assert f"Queue position at arrival: {n}" in block
    assert "Queued user message" not in caplog.text
    assert all("Queued user message" not in str(call) for call in bot.mock_calls)


async def test_cancelled_telegram_entry_not_dispatched():
    mw = SequentialMiddleware()
    lock = mw.get_lock(SessionKey.telegram(100).lock_key)
    await lock.acquire()
    handler = AsyncMock()
    task = asyncio.create_task(mw(handler, message(), {}))
    await asyncio.sleep(0)
    assert await mw.cancel_entry(100, mw._pending[100][0].entry_id)
    lock.release()
    await asyncio.wait_for(task, 1)
    handler.assert_not_awaited()
    assert lock not in qc._waiters


@pytest.mark.parametrize("transport", ["matrix", "slack"])
@pytest.mark.parametrize("annotate", [False, True])
async def test_matrix_slack_lock_dispatch(transport, annotate):
    bot = object.__new__(MatrixBot if transport == "matrix" else SlackBot)
    bot._lock_pool = LockPool()
    seen = []

    async def handler(_key, text, *args, **kwargs):
        seen.append((text, qc.current_queue_context("Asia/Seoul")))

    bot._dispatch_message = handler
    key = SessionKey.for_transport("mx" if transport == "matrix" else "sl", 100)
    lock = bot._lock_pool.get(key.lock_key)
    await lock.acquire()
    if transport == "matrix":
        text = "media prompt" if annotate else "!unknown"
        coro = bot._dispatch_with_lock(
            key, text, "room", SimpleNamespace(server_timestamp=SENT.timestamp() * 1000)
        )
    else:
        text = "original" if annotate else "button prompt"
        coro = bot._dispatch_with_lock(
            key, text, "channel", None, sent_at=str(SENT.timestamp()) if annotate else None
        )
    task = asyncio.create_task(coro)
    await asyncio.sleep(0)
    lock.release()
    await asyncio.wait_for(task, 1)
    assert seen[0][0] == text
    if annotate:
        assert "09:15:00 KST" in seen[0][1] and "Queue position at arrival: 1" in seen[0][1]
    else:
        assert seen[0][1] is None


@pytest.mark.parametrize("value", [None, "bad", float("inf"), object()])
def test_missing_time_not_invented(value):
    assert qc.message_time(value) is None


@pytest.mark.parametrize(
    "command",
    [
        ["claude", "--", "INPUT"],
        ["codex", "exec", "INPUT"],
        ["gemini", "-p", "INPUT"],
        ["grok", "-p", "INPUT"],
    ],
)
async def test_cli_preview_hides_metadata(command):
    lock = asyncio.Lock()
    await lock.acquire()

    async def receive():
        async with qc.QueuedMessageLock(lock, SENT):
            req = AgentRequest(
                prompt="original preview", queue_context=qc.current_queue_context("UTC")
            )
            argv = [req.cli_prompt if item == "INPUT" else item for item in command]
            assert "Queued user message" in argv[-1]
            preview = format_cli_cmd(argv, opt_prefix=None)
            assert (
                "Queued user message" not in preview
                and "Waited:" not in preview
                and "original preview" in preview
            )
            assert req.prompt == "original preview"

    task = asyncio.create_task(receive())
    await asyncio.sleep(0)
    lock.release()
    await task


async def test_wait_notice_survives_indicator_delay_and_lock_release():
    mw = SequentialMiddleware()
    lock = mw.get_lock(SessionKey.telegram(100).lock_key)
    await lock.acquire()
    bot = AsyncMock()

    async def indicator(*args, **kwargs):
        lock.release()
        await asyncio.sleep(0)
        return SimpleNamespace(message_id=999)

    bot.send_message.side_effect = indicator
    mw.set_bot(bot)
    seen = []

    async def handler(event, data):
        seen.append(qc.current_queue_context("UTC"))

    await mw(handler, message(), {})
    assert "Queue position at arrival: 1" in seen[0]
    assert lock not in qc._waiters


async def test_cancel_during_indicator_cleans_pending_bookkeeping():
    mw = SequentialMiddleware()
    lock = mw.get_lock(SessionKey.telegram(100).lock_key)
    await lock.acquire()
    bot = AsyncMock()
    started = asyncio.Event()

    async def indicator(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    bot.send_message.side_effect = indicator
    mw.set_bot(bot)
    handler = AsyncMock()
    task = asyncio.create_task(mw(handler, message(), {}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lock not in qc._waiters
    assert not mw.has_pending(100)
    handler.assert_not_awaited()
    lock.release()
