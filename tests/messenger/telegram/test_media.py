"""Tests for bot media handling: download, index, prompt injection."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.types import Message

from ductor_bot.files.prompt import MediaInfo


def _make_message(
    *,
    text: str | None = None,
    photo: bool = False,
    voice: bool = False,
    document: bool = False,
    video: bool = False,
    audio: bool = False,
    sticker: bool = False,
    video_note: bool = False,
    caption: str | None = None,
    chat_type: str = "private",
) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.chat = MagicMock()
    msg.chat.id = 1
    msg.chat.type = chat_type
    msg.text = text
    msg.caption = caption
    msg.caption_entities = None
    msg.entities = None
    msg.reply_to_message = None
    msg.answer = MagicMock()

    # Media attributes
    msg.photo = None
    msg.voice = None
    msg.document = None
    msg.video = None
    msg.audio = None
    msg.sticker = None
    msg.video_note = None

    if photo:
        p = MagicMock()
        p.file_unique_id = "abc123"
        msg.photo = [p]  # Last element = highest quality

    if voice:
        v = MagicMock()
        v.file_unique_id = "voice1"
        v.mime_type = "audio/ogg"
        msg.voice = v

    if document:
        d = MagicMock()
        d.file_unique_id = "doc1"
        d.file_name = "report.pdf"
        d.mime_type = "application/pdf"
        msg.document = d

    if video:
        v = MagicMock()
        v.file_unique_id = "vid1"
        v.file_name = None
        v.mime_type = "video/mp4"
        msg.video = v

    if audio:
        a = MagicMock()
        a.file_unique_id = "aud1"
        a.file_name = "song.mp3"
        a.mime_type = "audio/mpeg"
        msg.audio = a

    if sticker:
        s = MagicMock()
        s.file_unique_id = "stk1"
        s.is_animated = False
        s.is_video = False
        msg.sticker = s

    if video_note:
        vn = MagicMock()
        vn.file_unique_id = "vn1"
        msg.video_note = vn

    return msg


# ---------------------------------------------------------------------------
# has_media
# ---------------------------------------------------------------------------


class TestHasMedia:
    def test_text_only(self) -> None:
        from ductor_bot.messenger.telegram.media import has_media

        assert has_media(_make_message(text="hello")) is False

    def test_photo(self) -> None:
        from ductor_bot.messenger.telegram.media import has_media

        assert has_media(_make_message(photo=True)) is True

    def test_voice(self) -> None:
        from ductor_bot.messenger.telegram.media import has_media

        assert has_media(_make_message(voice=True)) is True

    def test_document(self) -> None:
        from ductor_bot.messenger.telegram.media import has_media

        assert has_media(_make_message(document=True)) is True

    def test_video(self) -> None:
        from ductor_bot.messenger.telegram.media import has_media

        assert has_media(_make_message(video=True)) is True

    def test_sticker(self) -> None:
        from ductor_bot.messenger.telegram.media import has_media

        assert has_media(_make_message(sticker=True)) is True

    def test_video_note(self) -> None:
        from ductor_bot.messenger.telegram.media import has_media

        assert has_media(_make_message(video_note=True)) is True


# ---------------------------------------------------------------------------
# is_media_addressed
# ---------------------------------------------------------------------------


class TestIsMediaAddressed:
    def test_reply_to_bot(self) -> None:
        from ductor_bot.messenger.telegram.media import is_media_addressed

        msg = _make_message(photo=True, chat_type="group")
        msg.reply_to_message = MagicMock()
        msg.reply_to_message.from_user = MagicMock()
        msg.reply_to_message.from_user.id = 42

        assert is_media_addressed(msg, bot_id=42, bot_username="mybot") is True

    def test_caption_mention(self) -> None:
        from ductor_bot.messenger.telegram.media import is_media_addressed

        msg = _make_message(photo=True, caption="@mybot look at this", chat_type="group")
        entity = MagicMock()
        entity.type = "mention"
        entity.offset = 0
        entity.length = 6
        msg.caption_entities = [entity]

        assert is_media_addressed(msg, bot_id=42, bot_username="mybot") is True

    def test_not_addressed(self) -> None:
        from ductor_bot.messenger.telegram.media import is_media_addressed

        msg = _make_message(photo=True, chat_type="group")
        assert is_media_addressed(msg, bot_id=42, bot_username="mybot") is False


class TestIsMessageAddressed:
    """Tests for the generalized is_message_addressed function."""

    def test_reply_to_bot(self) -> None:
        from ductor_bot.messenger.telegram.media import is_message_addressed

        msg = _make_message(text="hello", chat_type="group")
        msg.entities = None
        msg.reply_to_message = MagicMock()
        msg.reply_to_message.from_user = MagicMock()
        msg.reply_to_message.from_user.id = 42

        assert is_message_addressed(msg, bot_id=42, bot_username="mybot") is True

    def test_text_mention(self) -> None:
        from ductor_bot.messenger.telegram.media import is_message_addressed

        msg = _make_message(text="@mybot what time is it?", chat_type="group")
        entity = MagicMock()
        entity.type = "mention"
        entity.offset = 0
        entity.length = 6
        msg.entities = [entity]
        msg.reply_to_message = None

        assert is_message_addressed(msg, bot_id=42, bot_username="mybot") is True

    def test_text_mention_case_insensitive(self) -> None:
        from ductor_bot.messenger.telegram.media import is_message_addressed

        msg = _make_message(text="@MyBot hey", chat_type="group")
        entity = MagicMock()
        entity.type = "mention"
        entity.offset = 0
        entity.length = 6
        msg.entities = [entity]
        msg.reply_to_message = None

        assert is_message_addressed(msg, bot_id=42, bot_username="mybot") is True

    def test_not_addressed_plain_text(self) -> None:
        from ductor_bot.messenger.telegram.media import is_message_addressed

        msg = _make_message(text="just chatting", chat_type="group")
        msg.entities = None
        msg.reply_to_message = None

        assert is_message_addressed(msg, bot_id=42, bot_username="mybot") is False

    def test_reply_to_other_user(self) -> None:
        from ductor_bot.messenger.telegram.media import is_message_addressed

        msg = _make_message(text="hello", chat_type="group")
        msg.entities = None
        msg.reply_to_message = MagicMock()
        msg.reply_to_message.from_user = MagicMock()
        msg.reply_to_message.from_user.id = 999

        assert is_message_addressed(msg, bot_id=42, bot_username="mybot") is False

    def test_mention_other_bot(self) -> None:
        from ductor_bot.messenger.telegram.media import is_message_addressed

        msg = _make_message(text="@otherbot hey", chat_type="group")
        entity = MagicMock()
        entity.type = "mention"
        entity.offset = 0
        entity.length = 9
        msg.entities = [entity]
        msg.reply_to_message = None

        assert is_message_addressed(msg, bot_id=42, bot_username="mybot") is False

    def test_caption_mention_still_works(self) -> None:
        from ductor_bot.messenger.telegram.media import is_message_addressed

        msg = _make_message(photo=True, caption="@mybot look", chat_type="group")
        entity = MagicMock()
        entity.type = "mention"
        entity.offset = 0
        entity.length = 6
        msg.caption_entities = [entity]
        msg.entities = None
        msg.reply_to_message = None

        assert is_message_addressed(msg, bot_id=42, bot_username="mybot") is True

    def test_no_bot_username(self) -> None:
        from ductor_bot.messenger.telegram.media import is_message_addressed

        msg = _make_message(text="@mybot hey", chat_type="group")
        msg.entities = [MagicMock(type="mention", offset=0, length=6)]
        msg.reply_to_message = None

        assert is_message_addressed(msg, bot_id=42, bot_username=None) is False

    def test_no_bot_id_reply(self) -> None:
        from ductor_bot.messenger.telegram.media import is_message_addressed

        msg = _make_message(text="hello", chat_type="group")
        msg.entities = None
        msg.reply_to_message = MagicMock()
        msg.reply_to_message.from_user = MagicMock()
        msg.reply_to_message.from_user.id = 42

        assert is_message_addressed(msg, bot_id=None, bot_username="mybot") is False


# ---------------------------------------------------------------------------
# Media extractors
# ---------------------------------------------------------------------------


class TestResolveMedia:
    def test_photo_extraction(self) -> None:
        from ductor_bot.messenger.telegram.media import _resolve_media

        msg = _make_message(photo=True)
        kind, obj, name, mime = _resolve_media(msg)

        assert kind == "photo"
        assert obj is not None
        assert name == "photo_abc123.jpg"
        assert mime == "image/jpeg"

    def test_voice_extraction(self) -> None:
        from ductor_bot.messenger.telegram.media import _resolve_media

        msg = _make_message(voice=True)
        kind, _obj, name, mime = _resolve_media(msg)

        assert kind == "voice"
        assert name == "voice_voice1.ogg"
        assert mime == "audio/ogg"

    def test_document_extraction(self) -> None:
        from ductor_bot.messenger.telegram.media import _resolve_media

        msg = _make_message(document=True)
        kind, _obj, name, mime = _resolve_media(msg)

        assert kind == "document"
        assert name == "report.pdf"
        assert mime == "application/pdf"

    def test_video_extraction(self) -> None:
        from ductor_bot.messenger.telegram.media import _resolve_media

        msg = _make_message(video=True)
        kind, _obj, name, mime = _resolve_media(msg)

        assert kind == "video"
        assert name == "video_vid1.mp4"
        assert mime == "video/mp4"

    def test_sticker_static(self) -> None:
        from ductor_bot.messenger.telegram.media import _resolve_media

        msg = _make_message(sticker=True)
        kind, _, name, mime = _resolve_media(msg)

        assert kind == "sticker"
        assert name == "sticker_stk1.webp"
        assert mime == "image/webp"

    def test_video_note_extraction(self) -> None:
        from ductor_bot.messenger.telegram.media import _resolve_media

        msg = _make_message(video_note=True)
        kind, _, name, mime = _resolve_media(msg)

        assert kind == "video_note"
        assert name == "videonote_vn1.mp4"
        assert mime == "video/mp4"

    def test_no_media(self) -> None:
        from ductor_bot.messenger.telegram.media import _resolve_media

        msg = _make_message(text="hello")
        kind, obj, _name, _mime = _resolve_media(msg)

        assert kind is None
        assert obj is None


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class TestUpdateIndex:
    def test_builds_index(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.media import update_index

        day_dir = tmp_path / "2025-06-15"
        day_dir.mkdir()
        (day_dir / "photo_abc.jpg").write_bytes(b"\xff\xd8" * 10)
        (day_dir / "voice_xyz.ogg").write_bytes(b"\x00" * 50)

        update_index(tmp_path)

        index_path = tmp_path / "_index.yaml"
        assert index_path.exists()

        data = yaml.safe_load(index_path.read_text())
        assert data["total_files"] == 2
        assert "2025-06-15" in data["tree"]
        files = data["tree"]["2025-06-15"]
        names = {f["name"] for f in files}
        assert "photo_abc.jpg" in names
        assert "voice_xyz.ogg" in names

    def test_skips_non_date_dirs(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.media import update_index

        (tmp_path / "random_dir").mkdir()
        (tmp_path / "random_dir" / "file.txt").write_text("x")

        update_index(tmp_path)

        data = yaml.safe_load((tmp_path / "_index.yaml").read_text())
        assert data["total_files"] == 0

    def test_empty_dir(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.media import update_index

        update_index(tmp_path)

        data = yaml.safe_load((tmp_path / "_index.yaml").read_text())
        assert data["total_files"] == 0
        assert data["tree"] == {}


# ---------------------------------------------------------------------------
# Telegram-specific build_media_prompt wrapper
# ---------------------------------------------------------------------------


class TestTelegramBuildMediaPrompt:
    def test_includes_telegram_transport(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.media import build_media_prompt

        info = MediaInfo(
            path=tmp_path / "photo.jpg",
            media_type="image/jpeg",
            file_name="photo.jpg",
            caption=None,
            original_type="photo",
        )
        prompt = build_media_prompt(info, tmp_path)
        assert "via Telegram" in prompt

    def test_voice_hint(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.media import build_media_prompt

        info = MediaInfo(
            path=tmp_path / "voice.ogg",
            media_type="audio/ogg",
            file_name="voice.ogg",
            caption=None,
            original_type="voice",
        )
        prompt = build_media_prompt(info, tmp_path)
        assert "transcribe_audio.py" in prompt

    def test_caption(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.media import build_media_prompt

        info = MediaInfo(
            path=tmp_path / "photo.jpg",
            media_type="image/jpeg",
            file_name="photo.jpg",
            caption="Hello!",
            original_type="photo",
        )
        prompt = build_media_prompt(info, tmp_path)
        assert "User message: Hello!" in prompt


# ---------------------------------------------------------------------------
# Download retry + graceful degradation
# ---------------------------------------------------------------------------


def _net_error() -> TelegramNetworkError:
    return TelegramNetworkError(MagicMock(), "boom")


def _retry_after(seconds: float) -> TelegramRetryAfter:
    return TelegramRetryAfter(MagicMock(), "slow down", retry_after=seconds)


class TestDownloadWithRetry:
    async def test_transient_then_success(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram import media

        dest = tmp_path / "f.bin"
        bot = MagicMock()
        bot.download = AsyncMock(side_effect=[_net_error(), _net_error(), None])

        with patch.object(media.asyncio, "sleep", new=AsyncMock()) as sleep:
            await media._download_with_retry(bot, MagicMock(), dest)

        assert bot.download.await_count == 3
        assert sleep.await_count == 2  # backoff before attempts 2 and 3

    async def test_all_attempts_fail_raises(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram import media

        dest = tmp_path / "f.bin"
        bot = MagicMock()
        bot.download = AsyncMock(side_effect=_net_error())

        with (
            patch.object(media.asyncio, "sleep", new=AsyncMock()),
            pytest.raises(TelegramNetworkError),
        ):
            await media._download_with_retry(bot, MagicMock(), dest)
        assert bot.download.await_count == media._DOWNLOAD_ATTEMPTS

    async def test_retry_after_within_cap_sleeps_and_retries(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram import media

        dest = tmp_path / "f.bin"
        bot = MagicMock()
        bot.download = AsyncMock(side_effect=[_retry_after(3), None])

        with patch.object(media.asyncio, "sleep", new=AsyncMock()) as sleep:
            await media._download_with_retry(bot, MagicMock(), dest)

        assert bot.download.await_count == 2
        sleep.assert_awaited_once_with(3)

    async def test_retry_after_sleep_is_capped(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram import media

        dest = tmp_path / "f.bin"
        bot = MagicMock()
        # retry_after == cap is allowed (not > cap); sleep is min(retry_after, cap).
        bot.download = AsyncMock(side_effect=[_retry_after(media._RETRY_AFTER_CAP), None])

        with patch.object(media.asyncio, "sleep", new=AsyncMock()) as sleep:
            await media._download_with_retry(bot, MagicMock(), dest)

        sleep.assert_awaited_once_with(media._RETRY_AFTER_CAP)

    async def test_retry_after_over_cap_fails_fast(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram import media

        dest = tmp_path / "f.bin"
        bot = MagicMock()
        bot.download = AsyncMock(side_effect=_retry_after(media._RETRY_AFTER_CAP + 1))

        with (
            patch.object(media.asyncio, "sleep", new=AsyncMock()) as sleep,
            pytest.raises(TelegramRetryAfter),
        ):
            await media._download_with_retry(bot, MagicMock(), dest)

        assert bot.download.await_count == 1  # no retry
        sleep.assert_not_awaited()  # no wait

    async def test_cancelled_not_retried(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram import media

        dest = tmp_path / "f.bin"
        bot = MagicMock()
        bot.download = AsyncMock(side_effect=asyncio.CancelledError())

        with (
            patch.object(media.asyncio, "sleep", new=AsyncMock()) as sleep,
            pytest.raises(asyncio.CancelledError),
        ):
            await media._download_with_retry(bot, MagicMock(), dest)

        assert bot.download.await_count == 1  # not retried
        sleep.assert_not_awaited()

    async def test_partial_file_removed_before_each_attempt(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram import media

        dest = MagicMock(spec=Path)
        bot = MagicMock()
        bot.download = AsyncMock(side_effect=[_net_error(), None])

        with patch.object(media.asyncio, "sleep", new=AsyncMock()):
            await media._download_with_retry(bot, dest, dest)

        # unlink(missing_ok=True) runs before every attempt to clear any partial
        # file from a prior failure (2 attempts -> 2 unlinks).
        assert dest.unlink.call_count == 2
        dest.unlink.assert_called_with(missing_ok=True)


class TestResolveMediaTextGraceful:
    async def test_download_fail_with_caption_returns_prompt(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram import media

        msg = _make_message(photo=True, caption="describe this")
        bot = MagicMock()

        with patch.object(media, "download_media", new=AsyncMock(side_effect=_net_error())):
            result = await media.resolve_media_text(bot, msg, tmp_path, tmp_path)

        assert result is not None
        assert "describe this" in result
        assert "could not be downloaded" in result
        msg.answer.assert_not_called()

    async def test_download_fail_no_caption_returns_none(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram import media

        msg = _make_message(photo=True, caption=None)
        msg.answer = AsyncMock()
        bot = MagicMock()

        with patch.object(media, "download_media", new=AsyncMock(side_effect=_net_error())):
            result = await media.resolve_media_text(bot, msg, tmp_path, tmp_path)

        assert result is None
        msg.answer.assert_awaited_once()
        assert "Could not download" in msg.answer.await_args.args[0]

    async def test_happy_path_unchanged(self, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram import media

        msg = _make_message(photo=True, caption="hi")
        bot = MagicMock()
        info = MediaInfo(
            path=tmp_path / "p.jpg",
            media_type="image/jpeg",
            file_name="p.jpg",
            caption="hi",
            original_type="photo",
        )

        with (
            patch.object(media, "download_media", new=AsyncMock(return_value=info)),
            patch.object(media, "update_index", new=MagicMock()),
        ):
            result = await media.resolve_media_text(bot, msg, tmp_path, tmp_path)

        assert result is not None
        assert "could not be downloaded" not in result
        msg.answer.assert_not_called()
