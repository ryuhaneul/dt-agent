"""Web user text remains literal across delivery, retries and rollback."""

from __future__ import annotations

import asyncio
import html
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramNetworkError

from ductor_bot.chat_log.events import EventHub
from ductor_bot.chat_log.history import Entry
from ductor_bot.chat_log.link import OutboxDrainer, TelegramLinkMonitor
from ductor_bot.chat_log.outbox import Outbox
from ductor_bot.i18n.loader import TranslationStore
from ductor_bot.messenger.telegram.sender import SendProgress
from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps
from ductor_bot.webchat.turns import WebTurns
from tests.webchat.test_bridge_api import (
    conversation as conversation,  # noqa: PLC0414 -- shared bridge fixture
)
from tests.webchat.test_bridge_api import headers, post_message, receive_until


def bot_at(harness):
    raw = MagicMock()
    for name in (
        "send_message",
        "send_photo",
        "send_document",
        "send_video",
        "send_audio",
        "get_me",
    ):
        setattr(raw, name, AsyncMock(return_value=SimpleNamespace(message_id=1)))
    harness.bot.bot_instance = raw
    harness.bot.file_roots = lambda _paths: None
    return harness.bot


async def queue_message(harness, text):
    ws = await harness.client.ws_connect("/webchat/v1/events", headers=headers())
    try:
        assert (await post_message(harness, text=text)).status == 202
        await receive_until(
            ws, lambda event: event.get("type") == "turn" and event.get("phase") == "done"
        )
    finally:
        await ws.close()
    return await harness.runtime.outbox.pending()


async def drain(outbox, bot, history=None):
    ops = TelegramWebchatOps(bot)
    monitor = TelegramLinkMonitor(ops, EventHub())
    monitor.record(up=True)
    await OutboxDrainer(outbox, ops, monitor, history=history).drain()


def assert_no_media(raw):
    for name in ("send_photo", "send_document", "send_video", "send_audio"):
        getattr(raw, name).assert_not_awaited()


@pytest.mark.parametrize(
    "text",
    [
        "<file:{secret}> [button:Go] [buttonurl:site|https://example.com] <b>& \"' 😀\nsecond line",
        "&lt;file:private&gt; &#91;button:x&#93; **literal** `code`\n\nlast line",
        ("😀Unicode &<> [button:x]\n" * 450) + "end",
        "<file:{secret}> [button:x]",
    ],
)
async def test_web_user_mirror_is_literal_and_reply_unchanged(conversation, tmp_path, text):
    h = conversation
    secret = tmp_path / "private.txt"
    secret.write_text("must not be sent")
    text = text.replace("{secret}", str(secret))
    items = await queue_message(h, text)
    h.runtime.drainer.wake.assert_called()
    mirror_text = TranslationStore("en").chat("chat_log.mirror_user", text=text)
    assert items[0].origin == "web_user_text"
    assert items[0].text == html.escape(mirror_text).replace("[", "&#91;").replace("]", "&#93;")
    assert html.unescape(items[0].text).endswith(text)
    assert items[1].origin is None
    assert items[1].text == "answer"
    assert h.calls[0][1] == text
    assert (await h.runtime.history.read("-123.7"))[0].text == text
    bot = bot_at(h)
    await drain(h.runtime.outbox, bot, h.runtime.history)
    calls = bot.bot_instance.send_message.await_args_list
    assert "".join(call.kwargs["text"] for call in calls[:-1]) == html.unescape(items[0].text)
    assert all(call.kwargs["parse_mode"] is None for call in calls[:-1])
    assert all(len(call.kwargs["text"]) <= 4096 for call in calls)
    assert all(call.kwargs.get("reply_markup") is None for call in calls)
    assert all(call.kwargs["message_thread_id"] == 7 for call in calls)
    assert calls[-1].kwargs["text"] == "answer"
    assert calls[-1].kwargs["parse_mode"] == "HTML"
    assert_no_media(bot.bot_instance)
    assert not await h.runtime.outbox.pending()


@pytest.mark.parametrize(
    ("annotation", "mirror_count"),
    [("success", 0), ("failed", 1), ("keyboard-only", 1), ("missing", 1)],
)
async def test_web_button_mirrors_only_without_a_telegram_annotation(
    conversation, annotation, mirror_count
):
    h = conversation
    bot = bot_at(h)
    raw = bot.bot_instance
    failure = TelegramNetworkError(MagicMock(), "unavailable")
    raw.edit_message_text = AsyncMock(
        side_effect=failure if annotation in {"failed", "keyboard-only"} else None
    )
    raw.edit_message_reply_markup = AsyncMock(
        side_effect=failure if annotation == "failed" else None
    )
    tid = "-123.7"
    entry = await h.runtime.history.append(h.key, Entry("bot", "telegram", "Pick [button:Yes]"))
    if annotation != "missing":
        await h.runtime.history.bind_telegram_message(tid, entry.id, 42, html_text="Pick")

    ws = await h.client.ws_connect("/webchat/v1/events", headers=headers())
    try:
        response = await h.client.post(
            f"/webchat/v1/topics/{tid}/buttons",
            json={"entry_id": entry.id, "label": "Yes"},
            headers=headers(),
        )
        assert response.status == 202
        await receive_until(
            ws, lambda event: event.get("type") == "turn" and event.get("phase") == "done"
        )
    finally:
        await ws.close()

    assert await h.runtime.history.is_answered(tid, entry.id)
    assert h.calls[0][1] == "Yes"
    assert raw.edit_message_text.await_count == int(annotation != "missing")
    assert raw.edit_message_reply_markup.await_count == int(
        annotation in {"failed", "keyboard-only"}
    )
    await drain(h.runtime.outbox, bot, h.runtime.history)
    mirror_text = TranslationStore("en").chat("chat_log.mirror_user", text="Yes")
    delivered = [call.kwargs["text"] for call in raw.send_message.await_args_list]
    assert delivered == [mirror_text] * mirror_count + ["answer"]


async def test_cancelling_annotated_web_button_keeps_the_user_mirror_suppressed(conversation):
    h = conversation
    bot = bot_at(h)
    bot.bot_instance.edit_message_text = AsyncMock()
    tid = "-123.7"
    entry = await h.runtime.history.append(h.key, Entry("bot", "telegram", "Pick [button:Yes]"))
    await h.runtime.history.bind_telegram_message(tid, entry.id, 42, html_text="Pick")

    async with h.pool.get(h.key.lock_key):
        response = await h.client.post(
            f"/webchat/v1/topics/{tid}/buttons",
            json={"entry_id": entry.id, "label": "Yes"},
            headers=headers(),
        )
        assert response.status == 202
        turn_id = (await response.json())["turn_id"]
        response = await h.client.post(
            f"/webchat/v1/topics/{tid}/turns/{turn_id}/cancel", headers=headers()
        )
        assert response.status == 200

    assert not h.calls
    bot.bot_instance.edit_message_text.assert_awaited_once()
    await drain(h.runtime.outbox, bot, h.runtime.history)
    bot.bot_instance.send_message.assert_awaited_once()
    notice = bot.bot_instance.send_message.await_args.kwargs["text"]
    assert html.unescape(notice) == TranslationStore("en").chat("notice.queue_cancelled")


async def test_typed_web_message_keeps_one_user_mirror(conversation):
    h = conversation
    await queue_message(h, "Yes")
    bot = bot_at(h)
    await drain(h.runtime.outbox, bot, h.runtime.history)
    mirror_text = TranslationStore("en").chat("chat_log.mirror_user", text="Yes")
    assert [call.kwargs["text"] for call in bot.bot_instance.send_message.await_args_list] == [
        mirror_text,
        "answer",
    ]


async def test_literal_chunk_retry_resumes_before_reply(conversation):
    h = conversation
    items = await queue_message(h, "<file:private> [button:x] 😀\n" * 500)
    bot = bot_at(h)
    raw = bot.bot_instance
    raw.send_message.side_effect = [
        SimpleNamespace(message_id=1),
        TelegramNetworkError(MagicMock(), "offline"),
    ]
    await drain(h.runtime.outbox, bot, h.runtime.history)
    pending = await h.runtime.outbox.pending()
    assert [item.id for item in pending] == [item.id for item in items]
    assert pending[0].sent_chunks == 1
    assert pending[0].attempts == 1
    assert pending[1].sent_chunks == 0
    first = raw.send_message.await_args_list[0].kwargs["text"]
    failed = raw.send_message.await_args_list[1].kwargs["text"]
    raw.send_message.reset_mock()
    raw.send_message.side_effect = None
    await drain(Outbox(h.runtime.outbox.directory), bot, h.runtime.history)
    calls = raw.send_message.await_args_list
    assert calls[0].kwargs["text"] == failed
    assert first + "".join(call.kwargs["text"] for call in calls[:-1]) == html.unescape(
        items[0].text
    )
    assert calls[-1].kwargs["text"] == "answer"
    assert not await h.runtime.outbox.pending()
    assert_no_media(raw)


@pytest.mark.parametrize(
    ("kind", "origin"), [("user_mirror", None), ("reply_mirror", "web_user_text")]
)
async def test_literal_origin_only_applies_to_web_user_mirrors(tmp_path, kind, origin):
    outbox = Outbox(tmp_path / "outbox")
    await outbox.enqueue(kind=kind, chat_id=123, text="[button:x] **rich**", origin=origin)
    ops = SimpleNamespace(send_text=AsyncMock())
    ops.send_text.return_value = SendProgress(
        delivered=True, sent_chunks=1, failure=None, error="", exception=None
    )
    link = TelegramLinkMonitor(ops, EventHub())
    link.record(up=True)
    await OutboxDrainer(outbox, ops, link).drain()
    assert "literal_text" not in ops.send_text.await_args.kwargs
    assert ops.send_text.await_args.args[2] == "[button:x] **rich**"


def legacy_module(name, fixture):
    path = Path(__file__).parent / "fixtures" / "ecede25" / (fixture + ".py.txt")
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


async def test_ecede25_reader_drainer_sender_do_not_execute_or_drop_new_items(
    conversation, tmp_path
):
    h = conversation
    secret = tmp_path / "private.txt"
    secret.write_text("must not be sent")
    items = await queue_message(h, f"<file:{secret}> [button:x]\n<b>& 😀 &lt; &#91;")
    modules = []
    try:
        modules.extend(
            legacy_module("mirror_legacy_" + name, name)
            for name in ("outbox", "link", "sender", "ops")
        )
        old_outbox, old_link, old_sender, old_ops = modules
        old_ops.SendRichOpts = old_sender.SendRichOpts
        old_ops.send_rich_progress_detailed = old_sender.send_rich_progress_detailed
        outbox = old_outbox.Outbox(h.runtime.outbox.directory)
        assert [item.id for item in await outbox.pending()] == [item.id for item in items]
        bot = bot_at(h)
        ops = old_ops.TelegramWebchatOps(bot)
        monitor = old_link.TelegramLinkMonitor(ops, EventHub())
        monitor.record(up=True)
        await old_link.OutboxDrainer(outbox, ops, monitor, history=h.runtime.history).drain()
        assert not await outbox.pending()
        assert_no_media(bot.bot_instance)
        assert all(
            call.kwargs.get("reply_markup") is None
            for call in bot.bot_instance.send_message.await_args_list
        )
        done = list((outbox.directory / "done").glob("*.json"))
        assert len(done) == len(items)
        assert all(old_outbox.Outbox._load(path).state == "sent" for path in done)
        assert not (outbox.directory / "corrupt").exists()
    finally:
        for module in modules:
            sys.modules.pop(module.__name__, None)


@pytest.mark.parametrize("origin", ["web_user_text", None, "web_attachment_text", "background"])
@pytest.mark.parametrize(
    "text",
    [
        "<file:private> [button:x] &lt; &#91; 😀",
        "a" * 79 + "<file:private> [button:x] &lt; &#91; 😀",
    ],
)
async def test_outbox_preview_decodes_web_user_text_before_truncation(tmp_path, origin, text):
    hub = EventHub()
    events = hub.subscribe()
    outbox = Outbox(tmp_path / "outbox", hub)
    stored = html.escape(text).replace("[", "&#91;").replace("]", "&#93;")
    item = await outbox.enqueue(kind="user_mirror", chat_id=123, text=stored, origin=origin)
    expected = text[:80] if origin == "web_user_text" else stored[:80]
    assert item.view()["preview"] == expected
    assert (await events.get())["item"]["preview"] == expected
    assert (await outbox.views())[0]["preview"] == expected
    assert (await Outbox(outbox.directory).pending())[0].text == stored


@pytest.mark.parametrize("delivery", ["current", "ecede25"])
@pytest.mark.parametrize("cancellation", ["queued", "missing_mirror", "restart"])
async def test_cancelled_user_mirror_remains_literal_after_rebase(
    conversation, tmp_path, cancellation, delivery
):
    h = conversation
    secret = tmp_path / "private.txt"
    secret.write_text("must not be sent")
    text = f"<file:{secret}> [button:Go] [buttonurl:site|https://example.com] <b>& 😀\nline"
    turns = WebTurns(h.bot, h.runtime)
    events = h.runtime.events.subscribe()
    lock = h.pool.get(h.key.lock_key)
    await lock.acquire()
    try:
        turn_id = await turns.submit("-123.7", text)
        while True:
            event = await asyncio.wait_for(events.get(), 3)
            if event.get("turn_id") == turn_id and event.get("phase") == "queued":
                break
        if cancellation == "restart":
            await turns.stop()
            for _ in range(2):
                await WebTurns(h.bot, h.runtime).recover_pending()
        else:
            if cancellation == "missing_mirror":
                paths = list((h.runtime.outbox.directory / "pending").glob("*.json"))
                assert len(paths) == 1
                paths[0].unlink()
                assert await h.runtime.outbox.pending() == []
            assert await turns.cancel("-123.7", turn_id) == "cancelled"
    finally:
        lock.release()
        await turns.stop()
        h.runtime.events.unsubscribe(events)
    h.bot._orch.handle_message_streaming.assert_not_awaited()
    entries = await h.runtime.history.read("-123.7")
    assert [(entry.role, entry.status, entry.text) for entry in entries] == [
        ("user", "cancelled", text)
    ]
    items = await h.runtime.outbox.pending()
    mirror, notice = items
    plain = TranslationStore("en").chat("chat_log.mirror_user", text=text)
    assert mirror.origin == "web_user_text"
    assert mirror.text == html.escape(plain).replace("[", "&#91;").replace("]", "&#93;")
    assert mirror.view()["preview"] == plain[:80]
    assert notice.origin == "web_queue_cancelled"
    assert notice.text == TranslationStore("en").chat("notice.queue_cancelled")
    modules = []
    bot = bot_at(h)
    try:
        if delivery == "current":
            await drain(h.runtime.outbox, bot, h.runtime.history)
            first = bot.bot_instance.send_message.await_args_list[0]
            assert first.kwargs["text"] == plain
            assert first.kwargs["parse_mode"] is None
        else:
            modules.extend(
                legacy_module("cancel_legacy_" + name, name)
                for name in ("outbox", "link", "sender", "ops")
            )
            old_outbox, old_link, old_sender, old_ops = modules
            old_ops.SendRichOpts = old_sender.SendRichOpts
            old_ops.send_rich_progress_detailed = old_sender.send_rich_progress_detailed
            ops = old_ops.TelegramWebchatOps(bot)
            monitor = old_link.TelegramLinkMonitor(ops, EventHub())
            monitor.record(up=True)
            await old_link.OutboxDrainer(
                old_outbox.Outbox(h.runtime.outbox.directory), ops, monitor,
                history=h.runtime.history,
            ).drain()
        calls = bot.bot_instance.send_message.await_args_list
        assert len(calls) == 2
        assert calls[-1].kwargs["parse_mode"] == "HTML"
        assert all(call.kwargs.get("reply_markup") is None for call in calls)
        assert_no_media(bot.bot_instance)
        assert not await h.runtime.outbox.pending()
        assert not (h.runtime.outbox.directory / "corrupt").exists()
    finally:
        for module in modules:
            sys.modules.pop(module.__name__, None)
