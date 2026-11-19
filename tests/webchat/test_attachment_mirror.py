"""Web attachments retain outbox order, retry state and rollback-safe text."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from PIL import Image

from ductor_bot.chat_log.events import EventHub
from ductor_bot.chat_log.link import OutboxDrainer, TelegramLinkMonitor
from ductor_bot.chat_log.outbox import Outbox
from ductor_bot.i18n import LANGUAGES
from ductor_bot.i18n.loader import TranslationStore
from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps
from ductor_bot.webchat.uploads import UPLOADS
from ductor_bot.workspace.paths import DuctorPaths
from tests.webchat.test_bridge_api import (
    conversation as conversation,  # noqa: PLC0414 -- shared bridge fixture
)
from tests.webchat.test_bridge_api import headers, receive_until


def image_payload(image_format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (40, 30), "blue").save(output, image_format)
    return output.getvalue()


def bot_at(tmp_path):
    raw = MagicMock()
    for name in ("send_message", "send_photo", "send_document", "send_video", "send_audio"):
        setattr(raw, name, AsyncMock(return_value=SimpleNamespace(message_id=1)))
    return SimpleNamespace(
        bot_instance=raw,
        _orch=SimpleNamespace(paths=DuctorPaths(ductor_home=tmp_path)),
        file_roots=lambda _paths: None,
    )


async def upload(harness, payload, name, mime):
    store = harness.client.server.app[UPLOADS]
    session = await store.start(
        "-123.7", "browser", {"name": name, "size": len(payload), "mime": mime}
    )

    async def data():
        yield payload

    await store.put(session.id, "-123.7", "browser", 0, data())
    return await store.complete(
        session.id, "-123.7", "browser", hashlib.sha256(payload).hexdigest()
    )


async def queue_message(harness, uploads, caption="Read these"):
    ws = await harness.client.ws_connect("/webchat/v1/events", headers=headers())
    try:
        response = await harness.client.post(
            "/webchat/v1/topics/-123.7/messages",
            json={
                "text": caption,
                "attachment_ids": [item.id for item in uploads],
                "client_id": "mirror",
            },
            headers={**headers(), "X-Dtw-Owner": "browser"},
        )
        assert response.status == 202
        await receive_until(
            ws, lambda event: event.get("type") == "turn" and event.get("phase") == "done"
        )
    finally:
        await ws.close()
    return await harness.runtime.outbox.pending()


async def drain(outbox, bot, *, history=None):
    ops = TelegramWebchatOps(bot)
    monitor = TelegramLinkMonitor(ops, EventHub())
    monitor.record(up=True)
    await OutboxDrainer(outbox, ops, monitor, history=history).drain()


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "WEBP", "GIF", "BMP"])
async def test_photo_formats_and_document_exception(tmp_path, image_format):
    bot = bot_at(tmp_path)
    path = tmp_path / "image.data"
    path.write_bytes(image_payload(image_format))
    result = await TelegramWebchatOps(bot).send_text(
        123,
        7,
        f"<file:{path}>",
        0,
        web_attachment_name="original.image",
    )
    assert result.delivered
    method = (
        bot.bot_instance.send_document if image_format == "BMP" else bot.bot_instance.send_photo
    )
    method.assert_awaited_once()
    call = method.await_args.kwargs
    assert call["message_thread_id"] == 7
    field = "document" if image_format == "BMP" else "photo"
    assert call[field].filename == "original.image"


@pytest.mark.parametrize(("mime", "suffix"), [("audio/mpeg", ".mp3"), ("video/mp4", ".mp4")])
async def test_other_media_are_documents(tmp_path, monkeypatch, mime, suffix):
    bot = bot_at(tmp_path)
    path = tmp_path / ("media" + suffix)
    path.write_bytes(b"sample")
    monkeypatch.setattr("ductor_bot.messenger.telegram.sender.guess_mime", lambda _path: mime)
    assert (
        await TelegramWebchatOps(bot).send_text(
            123, 7, f"<file:{path}>", 0, web_attachment_name=path.name
        )
    ).delivered
    bot.bot_instance.send_document.assert_awaited_once()
    bot.bot_instance.send_audio.assert_not_awaited()
    bot.bot_instance.send_video.assert_not_awaited()


@pytest.mark.parametrize(
    ("photo", "size", "notice"),
    [
        (True, 10_000_000, False),
        (True, 10_000_001, True),
        (False, 50_000_000, False),
        (False, 50_000_001, True),
    ],
)
async def test_size_boundaries_are_single_delivery_chunks(
    tmp_path, monkeypatch, photo, size, notice
):
    bot = bot_at(tmp_path)
    path = tmp_path / ("photo.png" if photo else "report.pdf")
    path.write_bytes(image_payload() if photo else b"%PDF-1.7\n")
    with path.open("ab") as stream:
        stream.truncate(size)
    monkeypatch.setattr("ductor_bot.messenger.telegram.sender.t", TranslationStore("en").chat)
    progress = AsyncMock()
    result = await TelegramWebchatOps(bot).send_text(
        123,
        7,
        f"<file:{path}>",
        0,
        web_attachment_name="original <file:private> [button:x]",
        on_chunk=progress,
    )
    assert result.delivered
    assert result.sent_chunks == 1
    progress.assert_awaited_once_with(1)
    if notice:
        call = bot.bot_instance.send_message.await_args.kwargs
        assert "Attached from web:" in call["text"]
        assert "original <file:private> [button:x]" in call["text"]
        assert call["parse_mode"] is None
        bot.bot_instance.send_photo.assert_not_awaited()
        bot.bot_instance.send_document.assert_not_awaited()
    elif photo:
        bot.bot_instance.send_photo.assert_awaited_once()
    else:
        bot.bot_instance.send_document.assert_awaited_once()


async def test_photo_rejection_uses_existing_document_fallback(tmp_path):
    bot = bot_at(tmp_path)
    path = tmp_path / "photo.png"
    path.write_bytes(image_payload())
    bot.bot_instance.send_photo.side_effect = TelegramBadRequest(MagicMock(), "bad dimensions")
    result = await TelegramWebchatOps(bot).send_text(
        123, 7, f"<file:{path}>", 0, web_attachment_name="real.png"
    )
    assert result.delivered
    assert bot.bot_instance.send_document.await_args.kwargs["document"].filename == "real.png"


async def test_bridge_order_literal_caption_and_same_topic(conversation, tmp_path):
    h = conversation
    first = await upload(h, image_payload(), "photo.png", "image/png")
    second = await upload(h, b"%PDF-1.7\n", "report.pdf", "application/pdf")
    secret = tmp_path / "private.txt"
    secret.write_text("must not be sent")
    caption = f"<file:{secret}> [button:x] &lt; &#91; **literal**"
    items = await queue_message(h, [first, second], caption)
    assert [item.origin for item in items] == [
        "web_user_text",
        "web_attachment",
        "web_attachment",
        None,
    ]
    assert [item.kind for item in items] == ["user_mirror"] * 3 + ["reply_mirror"]
    assert [item.title for item in items[1:3]] == ["photo.png", "report.pdf"]
    assert len({item.history_id for item in items[:3]}) == 1
    assert all(item.topic_id == 7 for item in items)
    raw = bot_at(tmp_path).bot_instance
    h.bot.bot_instance = raw
    h.bot.file_roots = lambda _paths: None
    order = []

    async def text(**kwargs):
        order.append(("text", kwargs["text"]))
        return SimpleNamespace(message_id=1)

    async def photo(**_kwargs):
        order.append(("photo", None))
        return SimpleNamespace(message_id=2)

    async def document(**kwargs):
        order.append(("document", kwargs["document"].filename))
        return SimpleNamespace(message_id=3)

    raw.send_message.side_effect = text
    raw.send_photo.side_effect = photo
    raw.send_document.side_effect = document
    await drain(h.runtime.outbox, h.bot, history=h.runtime.history)
    assert [kind for kind, _value in order] == ["text", "photo", "document", "text"]
    assert order[0][1] == "🌐 Sent from web\n" + caption
    assert raw.send_message.await_args_list[0].kwargs["parse_mode"] is None
    assert "[INCOMING FILE]" in h.calls[0][1]
    assert not await h.runtime.outbox.pending()


async def test_restart_retry_keeps_prior_attachments_and_reply_order(conversation, tmp_path):
    h = conversation
    first = await upload(h, image_payload(), "photo.png", "image/png")
    second = await upload(h, b"%PDF-1.7\n", "report.pdf", "application/pdf")
    items = await queue_message(h, [first, second])
    h.bot.bot_instance = bot_at(tmp_path).bot_instance
    h.bot.file_roots = lambda _paths: None
    h.bot.bot_instance.send_document.side_effect = TelegramNetworkError(MagicMock(), "offline")
    await drain(h.runtime.outbox, h.bot)
    pending = await h.runtime.outbox.pending()
    assert [item.id for item in pending] == [items[2].id, items[3].id]
    assert pending[0].attempts == 1
    assert pending[0].sent_chunks == 0
    assert h.bot.bot_instance.send_photo.await_count == 1
    assert h.bot.bot_instance.send_message.await_count == 1
    restarted = Outbox(h.runtime.outbox.directory)
    h.bot.bot_instance.send_document.side_effect = None
    await drain(restarted, h.bot)
    assert not await restarted.pending()
    assert h.bot.bot_instance.send_photo.await_count == 1
    assert h.bot.bot_instance.send_message.await_count == 2
    assert h.bot.bot_instance.send_document.await_count == 2
    assert await restarted.mirror_state(items[0].history_id) == "sent"


async def test_deleted_file_uses_existing_unavailable_notice(conversation, tmp_path):
    h = conversation
    first = await upload(h, image_payload(), "photo.png", "image/png")
    await queue_message(h, [first])
    (h.paths.workspace / "webchat_files" / first.path).unlink()
    h.bot.bot_instance = bot_at(tmp_path).bot_instance
    h.bot.file_roots = lambda _paths: None
    await drain(h.runtime.outbox, h.bot)
    h.bot.bot_instance.send_photo.assert_not_awaited()
    assert h.bot.bot_instance.send_message.await_count == 3  # caption, unavailable notice, reply
    assert not await h.runtime.outbox.pending()


@pytest.mark.parametrize("locale", LANGUAGES)
def test_attachment_notice_locales(locale):
    value = TranslationStore(locale).chat(
        "chat_log.mirror_attachment", name="file.pdf", size="50.1 MB"
    )
    assert "file.pdf" in value
    assert "50.1 MB" in value
    assert "chat_log.mirror_attachment" not in value


def legacy_module(tmp_path, name, relative):
    fixture = "ops.py" if relative.endswith("/webchat_ops.py") else Path(relative).name
    source = (Path(__file__).parent / "fixtures/ecede25" / (fixture + ".txt")).read_text()
    path = tmp_path / (name + ".py")
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def test_ecede25_reader_drainer_and_sender_rollback(conversation, tmp_path):
    h = conversation
    first = await upload(h, image_payload(), "photo.png", "image/png")
    secret = tmp_path / "private.txt"
    secret.write_text("must not be sent")
    caption = f"<file:{secret}> [button:x]"
    items = await queue_message(h, [first], caption)
    modules = []
    try:
        for name, relative in [
            ("outbox", "chat_log/outbox.py"),
            ("link", "chat_log/link.py"),
            ("sender", "messenger/telegram/sender.py"),
            ("ops", "messenger/telegram/webchat_ops.py"),
        ]:
            modules.append(
                legacy_module(tmp_path, "attachment_legacy_" + name, "ductor_bot/" + relative)
            )
        old_outbox, old_link, old_sender, old_ops = modules
        old_ops.SendRichOpts = old_sender.SendRichOpts
        old_ops.send_rich_progress_detailed = old_sender.send_rich_progress_detailed
        outbox = old_outbox.Outbox(h.runtime.outbox.directory)
        loaded = await outbox.pending()
        assert [item.id for item in loaded] == [item.id for item in items]
        assert [item.origin for item in loaded[:2]] == ["web_user_text", "web_attachment"]
        bot = bot_at(tmp_path)
        ops = old_ops.TelegramWebchatOps(bot)
        monitor = old_link.TelegramLinkMonitor(ops, EventHub())
        monitor.record(up=True)
        await old_link.OutboxDrainer(outbox, ops, monitor).drain()
        assert not await outbox.pending()
        bot.bot_instance.send_photo.assert_awaited_once()
        bot.bot_instance.send_document.assert_not_awaited()
        sent_path = bot.bot_instance.send_photo.await_args.kwargs["photo"].path
        assert Path(sent_path) == h.paths.workspace / "webchat_files" / first.path
        assert all(
            call.kwargs.get("reply_markup") is None
            for call in bot.bot_instance.send_message.await_args_list
        )
        assert len(list((outbox.directory / "done").glob("*.json"))) == len(items)
        assert not (outbox.directory / "corrupt").exists()
    finally:
        for module in modules:
            sys.modules.pop(module.__name__, None)


async def test_oversize_notice_retry_preserves_its_outbox_position(tmp_path, monkeypatch):
    bot = bot_at(tmp_path)
    path = tmp_path / "large.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    with path.open("ab") as stream:
        stream.truncate(50_000_001)
    outbox = Outbox(tmp_path / "outbox")
    first = await outbox.enqueue(
        kind="user_mirror",
        chat_id=123,
        topic_id=7,
        text=f"<file:{path}>",
        origin="web_attachment",
        title="report.pdf",
    )
    second = await outbox.enqueue(kind="reply_mirror", chat_id=123, topic_id=7, text="reply")
    monkeypatch.setattr("ductor_bot.messenger.telegram.sender.t", TranslationStore("en").chat)
    bot.bot_instance.send_message.side_effect = TelegramNetworkError(MagicMock(), "offline")
    await drain(outbox, bot)
    pending = await outbox.pending()
    assert [item.id for item in pending] == [first.id, second.id]
    assert pending[0].sent_chunks == 0
    bot.bot_instance.send_message.side_effect = None
    await drain(Outbox(outbox.directory), bot)
    assert not await outbox.pending()
    calls = bot.bot_instance.send_message.await_args_list
    assert calls[0].kwargs["text"] == calls[1].kwargs["text"]
    assert calls[-1].kwargs["text"] == "reply"
    bot.bot_instance.send_document.assert_not_awaited()
