"""Tests for send_rich and send_file utilities."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest


class TestSendRich:
    """Test rich text sending with HTML conversion and file extraction."""

    async def test_plain_text_sent_as_html(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_rich(bot, 1, "Hello world")
        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 1
        assert "Hello world" in call_kwargs["text"]

    async def test_returns_true_on_full_delivery(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        assert await send_rich(bot, 1, "Hello world") is True

    async def test_returns_false_on_network_error(self) -> None:
        """a swallowed TelegramNetworkError must be reported to the caller."""
        from aiogram.exceptions import TelegramNetworkError

        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock(
            side_effect=TelegramNetworkError(method=MagicMock(), message="boom")
        )
        assert await send_rich(bot, 1, "Hello world") is False

    async def test_file_tags_extracted_and_sent(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich

        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock()

        text = f"Here is a file <file:{test_file}>"
        await send_rich(bot, 1, text, SendRichOpts(allowed_roots=[tmp_path]))
        bot.send_message.assert_called_once()
        bot.send_document.assert_called_once()

    async def test_reply_to_first_chunk(self) -> None:
        from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        reply_msg = MagicMock()
        reply_msg.message_id = 42

        await send_rich(
            bot, 1, "reply text", SendRichOpts(reply_to_message_id=reply_msg.message_id)
        )
        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args.kwargs
        assert call_kwargs["reply_parameters"].message_id == 42

    async def test_empty_text_with_file_still_sends_file(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich

        test_file = tmp_path / "data.csv"
        test_file.write_text("a,b,c")

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock()

        await send_rich(bot, 1, f"<file:{test_file}>", SendRichOpts(allowed_roots=[tmp_path]))
        bot.send_document.assert_called_once()

    async def test_html_fallback_on_bad_request(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()
        # First call fails with TelegramBadRequest, second succeeds (plain text)
        from aiogram.exceptions import TelegramBadRequest

        bot.send_message = AsyncMock(
            side_effect=[TelegramBadRequest(MagicMock(), "bad HTML"), None],
        )

        await send_rich(bot, 1, "test")
        assert bot.send_message.call_count == 2


class TestSendRichButtons:
    """Test button keyboard integration in send_rich."""

    async def test_send_rich_with_buttons_attaches_keyboard(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_reply_markup = AsyncMock()

        await send_rich(bot, 1, "Pick:\n\n[button:Yes] [button:No]")
        bot.edit_message_reply_markup.assert_called_once()
        markup = bot.edit_message_reply_markup.call_args.kwargs["reply_markup"]
        assert len(markup.inline_keyboard) == 1
        assert markup.inline_keyboard[0][0].text == "Yes"
        assert markup.inline_keyboard[0][1].text == "No"

    async def test_send_rich_without_buttons_no_keyboard(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.edit_message_reply_markup = AsyncMock()

        await send_rich(bot, 1, "No buttons")
        bot.edit_message_reply_markup.assert_not_called()

    async def test_send_rich_buttons_stripped_from_displayed_text(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 10
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_reply_markup = AsyncMock()

        await send_rich(bot, 1, "Hello\n\n[button:Go]")
        call_text = bot.send_message.call_args.kwargs["text"]
        assert "[button:" not in call_text
        assert "Hello" in call_text

    async def test_send_rich_buttons_with_reply_to(self) -> None:
        from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich

        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 77
        bot.send_message = AsyncMock(return_value=sent_msg)
        reply_msg = MagicMock()
        reply_msg.message_id = 99
        bot.edit_message_reply_markup = AsyncMock()

        await send_rich(
            bot, 1, "X\n[button:Ok]", SendRichOpts(reply_to_message_id=reply_msg.message_id)
        )
        bot.edit_message_reply_markup.assert_called_once()
        assert bot.edit_message_reply_markup.call_args.kwargs["message_id"] == 77


class TestSendFile:
    """Test individual file sending."""

    async def test_image_sent_as_photo(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0")  # JPEG magic bytes

        bot = MagicMock()
        bot.send_photo = AsyncMock()
        await send_file(bot, chat_id=1, path=img)
        bot.send_photo.assert_called_once()

    async def test_non_image_sent_as_document(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-1.4")

        bot = MagicMock()
        bot.send_document = AsyncMock()
        await send_file(bot, chat_id=1, path=doc)
        bot.send_document.assert_called_once()

    async def test_unsupported_image_sent_as_document(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        img = tmp_path / "photo.heic"
        img.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_photo = AsyncMock()
        bot.send_document = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="image/heic"):
            await send_file(bot, chat_id=1, path=img)

        bot.send_photo.assert_not_called()
        bot.send_document.assert_called_once()

    async def test_supported_video_sent_as_video(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_video = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="video/mp4"):
            await send_file(bot, chat_id=1, path=video)

        bot.send_video.assert_called_once()

    async def test_unsupported_video_sent_as_document(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        video = tmp_path / "clip.webm"
        video.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_video = AsyncMock()
        bot.send_document = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="video/webm"):
            await send_file(bot, chat_id=1, path=video)

        bot.send_video.assert_not_called()
        bot.send_document.assert_called_once()

    async def test_supported_audio_sent_as_audio(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        audio = tmp_path / "sound.mp3"
        audio.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_audio = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="audio/mpeg"):
            await send_file(bot, chat_id=1, path=audio)

        bot.send_audio.assert_called_once()

    async def test_unsupported_audio_sent_as_document(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        audio = tmp_path / "sound.wav"
        audio.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_audio = AsyncMock()
        bot.send_document = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="audio/wav"):
            await send_file(bot, chat_id=1, path=audio)

        bot.send_audio.assert_not_called()
        bot.send_document.assert_called_once()

    async def test_photo_rejected_retries_as_document(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0")

        bot = MagicMock()
        bot.send_photo = AsyncMock(side_effect=TelegramBadRequest(MagicMock(), "bad photo"))
        bot.send_document = AsyncMock()
        await send_file(bot, chat_id=1, path=img)
        bot.send_photo.assert_called_once()
        bot.send_document.assert_called_once()

    async def test_video_rejected_retries_as_document(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_video = AsyncMock(side_effect=TelegramBadRequest(MagicMock(), "bad video"))
        bot.send_document = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="video/mp4"):
            await send_file(bot, chat_id=1, path=video)

        bot.send_video.assert_called_once()
        bot.send_document.assert_called_once()

    async def test_audio_rejected_retries_as_document(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        audio = tmp_path / "sound.mp3"
        audio.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_audio = AsyncMock(side_effect=TelegramBadRequest(MagicMock(), "bad audio"))
        bot.send_document = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="audio/mpeg"):
            await send_file(bot, chat_id=1, path=audio)

        bot.send_audio.assert_called_once()
        bot.send_document.assert_called_once()

    async def test_missing_file_sends_error(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_file(bot, chat_id=1, path=tmp_path / "missing.txt")
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args.kwargs["text"]
        assert "not found" in text.lower()
        assert str(tmp_path / "missing.txt") in text

    async def test_blocked_path_sends_warning(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        f = tmp_path / "secret.txt"
        f.write_text("secret")

        bot = MagicMock()
        bot.send_message = AsyncMock()
        # allowed_roots is empty list = nothing allowed
        await send_file(bot, chat_id=1, path=f, allowed_roots=[Path("/nonexistent")])
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args.kwargs["text"].lower()
        assert "outside" in text
        assert "file_access" in text


class TestExtractFilePaths:
    """Test file path extraction from text."""

    def test_single_file(self) -> None:
        from ductor_bot.messenger.telegram.sender import extract_file_paths

        assert extract_file_paths("see <file:/tmp/a.txt>") == ["/tmp/a.txt"]

    def test_multiple_files(self) -> None:
        from ductor_bot.messenger.telegram.sender import extract_file_paths

        result = extract_file_paths("<file:/a> and <file:/b>")
        assert result == ["/a", "/b"]

    def test_no_files(self) -> None:
        from ductor_bot.messenger.telegram.sender import extract_file_paths

        assert extract_file_paths("no files here") == []


class TestSendFilesFromText:
    """Test post-streaming file extraction and delivery."""

    async def test_sends_files_from_tags(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_files_from_text

        f1 = tmp_path / "a.pdf"
        f1.write_bytes(b"%PDF")
        f2 = tmp_path / "b.csv"
        f2.write_text("x,y")

        bot = MagicMock()
        bot.send_document = AsyncMock()

        text = f"Here are files <file:{f1}> and <file:{f2}>"
        await send_files_from_text(bot, chat_id=1, text=text)
        assert bot.send_document.call_count == 2

    async def test_no_tags_does_nothing(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_files_from_text

        bot = MagicMock()
        bot.send_document = AsyncMock()
        bot.send_photo = AsyncMock()
        bot.send_message = AsyncMock()

        await send_files_from_text(bot, chat_id=1, text="No files here")
        bot.send_document.assert_not_called()
        bot.send_photo.assert_not_called()

    async def test_image_sent_as_photo(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_files_from_text

        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG")

        bot = MagicMock()
        bot.send_photo = AsyncMock()

        await send_files_from_text(bot, chat_id=1, text=f"<file:{img}>")

    async def test_windows_slash_drive_tag_normalized_before_send(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_files_from_text

        bot = MagicMock()

        with (
            patch("ductor_bot.files.tags.is_windows", return_value=True),
            patch(
                "ductor_bot.messenger.telegram.sender.send_file", new_callable=AsyncMock
            ) as mock_send_file,
        ):
            await send_files_from_text(
                bot, chat_id=1, text="<file:/C/Users/alice/output_to_user/out.zip>"
            )

        sent_path = mock_send_file.call_args.args[2]
        assert str(sent_path).replace("\\", "/") == "C:/Users/alice/output_to_user/out.zip"


class TestWindowsTagNormalizationInSendRich:
    async def test_send_rich_normalizes_windows_file_tag(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()

        with (
            patch("ductor_bot.files.tags.is_windows", return_value=True),
            patch(
                "ductor_bot.messenger.telegram.sender.send_file", new_callable=AsyncMock
            ) as mock_send_file,
        ):
            await send_rich(bot, 1, "<file:/C/Users/alice/result.apk>")

        sent_path = mock_send_file.call_args.args[2]
        assert str(sent_path).replace("\\", "/") == "C:/Users/alice/result.apk"


class TestForumTopicSupport:
    """Test message_thread_id propagation through sender functions."""

    async def test_send_rich_passes_thread_id(self) -> None:
        from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_rich(bot, 1, "Hello", SendRichOpts(thread_id=77))
        assert bot.send_message.call_args.kwargs["message_thread_id"] == 77

    async def test_send_rich_thread_id_none_by_default(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_rich(bot, 1, "Hello")
        assert bot.send_message.call_args.kwargs.get("message_thread_id") is None

    async def test_send_rich_passes_thread_id_to_files(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich

        doc = tmp_path / "data.csv"
        doc.write_text("a,b")

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock()
        await send_rich(bot, 1, f"Here <file:{doc}>", SendRichOpts(thread_id=55))
        assert bot.send_document.call_args.kwargs["message_thread_id"] == 55

    async def test_send_file_passes_thread_id_to_document(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        doc = tmp_path / "test.pdf"
        doc.write_bytes(b"%PDF")

        bot = MagicMock()
        bot.send_document = AsyncMock()
        await send_file(bot, chat_id=1, path=doc, thread_id=55)
        assert bot.send_document.call_args.kwargs["message_thread_id"] == 55

    async def test_send_file_passes_thread_id_to_photo(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0")

        bot = MagicMock()
        bot.send_photo = AsyncMock()
        await send_file(bot, chat_id=1, path=img, thread_id=55)
        assert bot.send_photo.call_args.kwargs["message_thread_id"] == 55

    async def test_send_file_passes_thread_id_to_video(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_video = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="video/mp4"):
            await send_file(bot, chat_id=1, path=video, thread_id=55)

        assert bot.send_video.call_args.kwargs["message_thread_id"] == 55

    async def test_send_file_passes_thread_id_to_audio(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        audio = tmp_path / "sound.mp3"
        audio.write_bytes(b"not-relevant")

        bot = MagicMock()
        bot.send_audio = AsyncMock()
        with patch("ductor_bot.messenger.telegram.sender.guess_mime", return_value="audio/mpeg"):
            await send_file(bot, chat_id=1, path=audio, thread_id=55)

        assert bot.send_audio.call_args.kwargs["message_thread_id"] == 55

    async def test_send_file_error_message_passes_thread_id(self) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_file(bot, chat_id=1, path=Path("/nonexistent.txt"), thread_id=33)
        assert bot.send_message.call_args.kwargs["message_thread_id"] == 33

    async def test_send_file_blocked_path_passes_thread_id(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        f = tmp_path / "secret.txt"
        f.write_text("secret")

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_file(bot, chat_id=1, path=f, allowed_roots=[Path("/nowhere")], thread_id=33)
        assert bot.send_message.call_args.kwargs["message_thread_id"] == 33

    async def test_send_files_from_text_passes_thread_id(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_files_from_text

        f = tmp_path / "data.csv"
        f.write_text("a,b")

        bot = MagicMock()
        bot.send_document = AsyncMock()
        await send_files_from_text(bot, chat_id=1, text=f"<file:{f}>", thread_id=44)
        assert bot.send_document.call_args.kwargs["message_thread_id"] == 44

    async def test_html_fallback_preserves_thread_id(self) -> None:
        from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich

        bot = MagicMock()
        bot.send_message = AsyncMock(
            side_effect=[TelegramBadRequest(MagicMock(), "bad HTML"), None],
        )
        await send_rich(bot, 1, "test", SendRichOpts(thread_id=88))
        for call in bot.send_message.call_args_list:
            assert call.kwargs["message_thread_id"] == 88


async def test_ia_chunks_stop_at_second_failure_and_resume(monkeypatch) -> None:
    from ductor_bot.messenger.telegram import sender
    from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich_checked_progress

    monkeypatch.setattr(sender, "_ia_text_chunks", lambda _text: ["first", "second", "third"])
    bot = MagicMock()
    bad_html = TelegramBadRequest(MagicMock(), "bad HTML")
    bad_plain = TelegramBadRequest(MagicMock(), "bad plain")
    bot.send_message = AsyncMock(side_effect=[
        SimpleNamespace(message_id=1), bad_html, bad_plain,
    ])
    opts = SendRichOpts(thread_id=77)
    assert await send_rich_checked_progress(bot, 42, "three chunks", opts) == (False, 1)
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == [
        "first", "second", "second",
    ]
    assert all(call.kwargs["message_thread_id"] == 77 for call in bot.send_message.await_args_list)

    bot.send_message.reset_mock()
    bot.send_message.side_effect = [
        SimpleNamespace(message_id=2), SimpleNamespace(message_id=3),
    ]
    assert await send_rich_checked_progress(
        bot, 42, "three chunks", opts, start_chunk=1,
    ) == (True, 3)
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == [
        "second", "third",
    ]
    assert all(call.kwargs["message_thread_id"] == 77 for call in bot.send_message.await_args_list)


async def test_ia_html_failure_plain_success_keeps_chunk_sequence(monkeypatch) -> None:
    from ductor_bot.messenger.telegram import sender
    from ductor_bot.messenger.telegram.sender import send_rich_checked_progress

    monkeypatch.setattr(sender, "_ia_text_chunks", lambda _text: ["first", "second", "third"])
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[
        SimpleNamespace(message_id=1),
        TelegramBadRequest(MagicMock(), "bad HTML"),
        SimpleNamespace(message_id=2),
        SimpleNamespace(message_id=3),
    ])
    assert await send_rich_checked_progress(bot, 42, "three chunks") == (True, 3)
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == [
        "first", "second", "second", "third",
    ]
    assert bot.send_message.await_args_list[2].kwargs["parse_mode"] is None


async def test_ia_first_chunk_failure_resends_all(monkeypatch) -> None:
    from ductor_bot.messenger.telegram import sender
    from ductor_bot.messenger.telegram.sender import send_rich_checked_progress

    monkeypatch.setattr(sender, "_ia_text_chunks", lambda _text: ["first", "second", "third"])
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[
        TelegramBadRequest(MagicMock(), "bad HTML"),
        TelegramBadRequest(MagicMock(), "bad plain"),
    ])
    assert await send_rich_checked_progress(bot, 42, "three chunks") == (False, 0)
    assert bot.send_message.await_count == 2
    bot.send_message.reset_mock()
    bot.send_message.side_effect = [
        SimpleNamespace(message_id=1), SimpleNamespace(message_id=2),
        SimpleNamespace(message_id=3),
    ]
    assert await send_rich_checked_progress(bot, 42, "three chunks") == (True, 3)
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == [
        "first", "second", "third",
    ]


async def test_ia_source_chunk_boundaries_are_deterministic_with_html_fallback() -> None:
    from ductor_bot.messenger.telegram.formatting import markdown_to_telegram_html
    from ductor_bot.messenger.telegram.sender import _ia_text_chunks, send_rich_checked_progress

    source = "&" * 5000
    first = _ia_text_chunks(source)
    second = _ia_text_chunks(source)
    assert first == second
    assert len(first) > 2
    assert "".join(first) == source
    assert all(len(chunk) <= 4096 for chunk in first)
    assert all(len(markdown_to_telegram_html(chunk)) <= 4096 for chunk in first)

    bot = MagicMock()
    responses = [TelegramBadRequest(MagicMock(), "bad HTML"), SimpleNamespace(message_id=1)]
    responses.extend(SimpleNamespace(message_id=i + 2) for i in range(len(first) - 1))
    bot.send_message = AsyncMock(side_effect=responses)
    assert await send_rich_checked_progress(bot, 42, source) == (True, len(first))
    assert bot.send_message.await_args_list[1].kwargs["text"] == first[0]
    html_calls = [
        call.kwargs["text"] for call in bot.send_message.await_args_list
        if call.kwargs["parse_mode"] is not None
    ]
    assert html_calls == [markdown_to_telegram_html(chunk) for chunk in first]


async def test_ia_file_chunk_requires_message_id_and_keeps_topic(tmp_path: Path) -> None:
    from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich_checked_progress

    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")
    bot = MagicMock()
    bot.send_document = AsyncMock(return_value=SimpleNamespace(message_id=None))
    opts = SendRichOpts(allowed_roots=[tmp_path], thread_id=88)
    text = f"<file:{file_path}>"
    assert await send_rich_checked_progress(bot, 42, text, opts) == (False, 0)
    assert bot.send_document.await_args.kwargs["message_thread_id"] == 88
    bot.send_document.return_value = SimpleNamespace(message_id=7)
    assert await send_rich_checked_progress(bot, 42, text, opts) == (True, 1)


async def test_ia_file_after_text_resumes_without_resending_text(tmp_path: Path) -> None:
    from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich_checked_progress

    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    bot.send_document = AsyncMock(return_value=SimpleNamespace(message_id=None))
    opts = SendRichOpts(allowed_roots=[tmp_path], thread_id=91)
    text = f"Report <file:{file_path}>"

    assert await send_rich_checked_progress(bot, 42, text, opts) == (False, 1)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["message_thread_id"] == 91
    assert bot.send_document.await_args.kwargs["message_thread_id"] == 91

    bot.send_message.reset_mock()
    bot.send_document.return_value = SimpleNamespace(message_id=2)
    assert await send_rich_checked_progress(
        bot, 42, text, opts, start_chunk=1,
    ) == (True, 2)
    bot.send_message.assert_not_awaited()
    assert bot.send_document.await_count == 2


async def test_ia_network_error_does_not_plain_resend(
    caplog,
) -> None:
    from aiogram.exceptions import TelegramNetworkError

    from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich_checked_progress

    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=TelegramNetworkError(MagicMock(), "request timeout"),
    )
    opts = SendRichOpts(ia_task_id="abcdef123456")
    assert await send_rich_checked_progress(bot, 42, "final report", opts) == (False, 0)
    assert bot.send_message.await_count == 1
    assert bot.send_message.await_args.kwargs["parse_mode"] is not None
    assert "task=abcdef123456 chunk=0 error=TelegramNetworkError" in caplog.text
    assert "duplicate possible on retry" in caplog.text


async def test_ia_non_parsing_bad_request_does_not_plain_resend() -> None:
    from ductor_bot.messenger.telegram.sender import send_rich_checked_progress

    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=TelegramBadRequest(MagicMock(), "chat not found"),
    )
    assert await send_rich_checked_progress(bot, 42, "final report") == (False, 0)
    assert bot.send_message.await_count == 1


async def test_ia_entity_rejection_uses_one_plain_fallback() -> None:
    from ductor_bot.messenger.telegram.sender import send_rich_checked_progress

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[
        TelegramBadRequest(MagicMock(), "can't parse entities: unsupported start tag"),
        SimpleNamespace(message_id=7),
    ])
    assert await send_rich_checked_progress(bot, 42, "**report**") == (True, 1)
    assert bot.send_message.await_count == 2
    assert bot.send_message.await_args_list[1].kwargs["parse_mode"] is None


async def test_ia_buttons_attach_to_last_text_chunk_and_resume(monkeypatch) -> None:
    from ductor_bot.messenger.telegram import sender
    from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich_checked_progress

    monkeypatch.setattr(
        sender, "_ia_text_chunks",
        lambda _text: ["first", "second"],
    )
    text = "first second [button:Continue]"
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[
        SimpleNamespace(message_id=1), SimpleNamespace(message_id=2),
    ])
    opts = SendRichOpts(thread_id=88)
    assert await send_rich_checked_progress(bot, 42, text, opts) == (True, 2)
    calls = bot.send_message.await_args_list
    assert "reply_markup" not in calls[0].kwargs
    assert calls[1].kwargs["reply_markup"].inline_keyboard[0][0].text == "Continue"
    assert "[button:" not in calls[1].kwargs["text"]
    assert all(call.kwargs["message_thread_id"] == 88 for call in calls)

    bot.send_message.reset_mock()
    bot.send_message.side_effect = [SimpleNamespace(message_id=3)]
    assert await send_rich_checked_progress(
        bot, 42, text, opts, start_chunk=1,
    ) == (True, 2)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text == (
        "Continue"
    )


async def test_ia_button_chunk_boundaries_ignore_send_format() -> None:
    from ductor_bot.messenger.telegram.sender import _ia_text_chunks, send_rich_checked_progress

    text = "x" * 4300 + "\n[button:Continue]"
    original_chunks = _ia_text_chunks(text)
    assert original_chunks == _ia_text_chunks(text)
    assert len(original_chunks) > 1

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[
        TelegramBadRequest(MagicMock(), "can't parse entities"),
        SimpleNamespace(message_id=1),
        *[SimpleNamespace(message_id=i + 2) for i in range(len(original_chunks) - 1)],
    ])
    assert await send_rich_checked_progress(bot, 42, text) == (True, len(original_chunks))
    assert original_chunks == _ia_text_chunks(text)
    assert bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text == (
        "Continue"
    )


async def test_ia_button_only_tail_attaches_to_last_visible_chunk() -> None:
    from ductor_bot.messenger.telegram.sender import send_rich_checked_progress

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9))
    assert await send_rich_checked_progress(bot, 42, "final[button:Go]") == (True, 1)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text == "Go"

async def test_ia_file_only_report_keeps_button_on_file(tmp_path: Path) -> None:
    from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich_checked_progress

    report = tmp_path / "report.txt"
    report.write_text("report")
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_document = AsyncMock(return_value=SimpleNamespace(message_id=21))
    text = f"[button:Go] <file:{report}>"
    opts = SendRichOpts(allowed_roots=[tmp_path], thread_id=88)
    assert await send_rich_checked_progress(bot, 42, text, opts) == (True, 1)
    bot.send_message.assert_not_awaited()
    kwargs = bot.send_document.await_args.kwargs
    assert kwargs["reply_markup"].inline_keyboard[0][0].text == "Go"
    assert kwargs["message_thread_id"] == 88


async def test_ia_last_file_gets_button_when_report_has_text_and_resume(tmp_path: Path) -> None:
    from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich_checked_progress

    report = tmp_path / "report.txt"
    report.write_text("report")
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=20))
    bot.send_document = AsyncMock(return_value=SimpleNamespace(message_id=21))
    text = f"Finished [button:Go] <file:{report}>"
    opts = SendRichOpts(allowed_roots=[tmp_path])
    assert await send_rich_checked_progress(bot, 42, text, opts) == (True, 2)
    assert "reply_markup" not in bot.send_message.await_args.kwargs
    assert bot.send_document.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text == "Go"

    bot.send_message.reset_mock()
    bot.send_document.reset_mock()
    assert await send_rich_checked_progress(
        bot, 42, text, opts, start_chunk=1,
    ) == (True, 2)
    bot.send_message.assert_not_awaited()
    assert bot.send_document.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text == (
        "Go"
    )


async def test_ia_photo_file_gets_button(tmp_path: Path) -> None:
    from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich_checked_progress

    report = tmp_path / "report.png"
    report.write_bytes(b"png")
    bot = MagicMock()
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=31))
    assert await send_rich_checked_progress(
        bot, 42, f"<file:{report}> [button:Open]",
        SendRichOpts(allowed_roots=[tmp_path]),
    ) == (True, 1)
    assert bot.send_photo.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text == (
        "Open"
    )


async def test_ia_button_marker_is_removed_before_chunk_boundary() -> None:
    from ductor_bot.messenger.telegram.sender import send_rich_checked_progress

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=41))
    text = "x" * 4090 + "[button:Go]" + "y" * 20
    assert await send_rich_checked_progress(bot, 42, text) == (True, 2)
    sent = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert len(sent) == 2
    assert all("[butto" not in chunk and "n:Go]" not in chunk for chunk in sent)
    assert "".join(sent) == "x" * 4090 + "y" * 20
    assert "reply_markup" not in bot.send_message.await_args_list[0].kwargs
    assert bot.send_message.await_args_list[1].kwargs["reply_markup"].inline_keyboard[0][0].text == (
        "Go"
    )


async def test_ia_button_only_report_sends_keyboard_with_prompt() -> None:
    from ductor_bot.messenger.telegram.sender import send_rich_checked_progress

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=51))
    assert await send_rich_checked_progress(bot, 42, "[button:Go]") == (True, 1)
    assert bot.send_message.await_args.kwargs["text"] == "Choose an option:"
    assert bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text == (
        "Go"
    )


async def test_ia_file_path_button_marker_is_not_parsed_as_button(tmp_path: Path) -> None:
    from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich_checked_progress

    intended = tmp_path / "[button:Go].txt"
    unintended = tmp_path / ".txt"
    intended.write_text("intended")
    unintended.write_text("unintended")
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_document = AsyncMock(return_value=SimpleNamespace(message_id=91))
    opts = SendRichOpts(allowed_roots=[tmp_path])

    assert await send_rich_checked_progress(bot, 42, f"<file:{intended}>", opts) == (True, 1)
    document = bot.send_document.await_args.kwargs["document"]
    assert Path(document.path) == intended
    assert Path(document.path) != unintended
    assert "reply_markup" not in bot.send_document.await_args.kwargs
    bot.send_message.assert_not_awaited()

    bot.send_document.reset_mock()
    assert await send_rich_checked_progress(
        bot, 42, f"[button:Open] <file:{intended}>", opts,
    ) == (True, 1)
    kwargs = bot.send_document.await_args.kwargs
    assert Path(kwargs["document"].path) == intended
    assert [
        button.text for row in kwargs["reply_markup"].inline_keyboard for button in row
    ] == ["Open"]


async def test_ia_file_tag_inside_button_label_is_literal(tmp_path: Path) -> None:
    from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich_checked_progress

    intended = tmp_path / "intended.txt"
    intended.write_text("sensitive")
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=101))
    bot.send_document = AsyncMock()
    text = f"[button:Open <file:{intended}>]"
    assert await send_rich_checked_progress(
        bot, 42, text, SendRichOpts(allowed_roots=[tmp_path]),
    ) == (True, 1)
    bot.send_document.assert_not_awaited()
    button = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == f"Open <file:{intended}>"


async def test_ia_file_tag_cannot_join_fragments_into_button(tmp_path: Path) -> None:
    from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich_checked_progress

    intended = tmp_path / "intended.txt"
    intended.write_text("content")
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=102))
    bot.send_document = AsyncMock(return_value=SimpleNamespace(message_id=103))
    text = f"[but<file:{intended}>ton:Go]"
    assert await send_rich_checked_progress(
        bot, 42, text, SendRichOpts(allowed_roots=[tmp_path]),
    ) == (True, 2)
    assert "[but ton:Go]" in bot.send_message.await_args.kwargs["text"]
    assert "reply_markup" not in bot.send_message.await_args.kwargs
    assert Path(bot.send_document.await_args.kwargs["document"].path) == intended
    assert "reply_markup" not in bot.send_document.await_args.kwargs


async def test_ia_button_removal_cannot_join_fragments_into_new_button() -> None:
    from ductor_bot.messenger.telegram.sender import send_rich_checked_progress

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=104))
    assert await send_rich_checked_progress(
        bot, 42, "[but[button:Inner]ton:Outer]",
    ) == (True, 1)
    kwargs = bot.send_message.await_args.kwargs
    assert "[but ton:Outer]" in kwargs["text"]
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert [button.text for button in buttons] == ["Inner"]


async def test_ia_button_removal_cannot_create_file_tag(tmp_path: Path) -> None:
    from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich_checked_progress

    intended = tmp_path / "intended.txt"
    intended.write_text("sensitive")
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=105))
    bot.send_document = AsyncMock()
    text = f"<fi[button:Go]le:{intended}>"
    assert await send_rich_checked_progress(
        bot, 42, text, SendRichOpts(allowed_roots=[tmp_path]),
    ) == (True, 1)
    bot.send_document.assert_not_awaited()
    assert "&lt;fi le:" in bot.send_message.await_args.kwargs["text"]
    button = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Go"


async def test_ia_interleaved_markers_keep_selected_order(tmp_path: Path) -> None:
    from ductor_bot.messenger.telegram.sender import _parse_ia_report

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    text = f"A [button:One] <file:{first}> B [button:Two] <file:{second}>"
    cleaned, files, markup = _parse_ia_report(text)
    assert cleaned == "A   B"
    assert files == [str(first), str(second)]
    assert markup is not None
    assert [button.text for row in markup.inline_keyboard for button in row] == [
        "One", "Two",
    ]


async def test_non_ia_file_path_button_marker_is_left_intact(tmp_path: Path) -> None:
    from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich

    intended = tmp_path / "[button:Go].txt"
    intended.write_text("intended")
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_document = AsyncMock()
    assert await send_rich(
        bot, 42, f"<file:{intended}>", SendRichOpts(allowed_roots=[tmp_path]),
    ) is True
    assert Path(bot.send_document.await_args.kwargs["document"].path) == intended
    bot.edit_message_reply_markup.assert_not_called()


async def test_ia_button_only_prompt_uses_selected_language() -> None:
    from ductor_bot import i18n
    from ductor_bot.messenger.telegram.sender import send_rich_checked_progress

    previous_language = i18n._get_store().language
    try:
        for language, expected in (("en", "Choose an option:"), ("ko", "옵션을 선택해 주세요:")):
            i18n.init(language)
            bot = MagicMock()
            bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=92))
            assert await send_rich_checked_progress(bot, 42, "[button:Go]") == (True, 1)
            assert bot.send_message.await_args.kwargs["text"] == expected
            assert bot.send_message.await_args.kwargs["reply_markup"] is not None
    finally:
        i18n.init(previous_language)


def test_ia_button_only_prompt_translated_in_all_supported_languages() -> None:
    from ductor_bot.i18n import LANGUAGES
    from ductor_bot.i18n.loader import TranslationStore

    for language in LANGUAGES:
        store = TranslationStore(language)
        assert "telegram.choose_option" in store._chat
        assert store.chat("telegram.choose_option")
