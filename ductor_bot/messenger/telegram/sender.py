"""Send utilities: rich text with file references, file sending."""

from __future__ import annotations

import asyncio
import html as html_mod
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import FSInputFile, InlineKeyboardMarkup, ReplyParameters

from ductor_bot.files.tags import FILE_PATH_RE, extract_file_paths, guess_mime, path_from_file_tag
from ductor_bot.i18n import t
from ductor_bot.messenger.send_opts import BaseSendOpts
from ductor_bot.messenger.telegram.buttons import extract_buttons, strip_button_syntax
from ductor_bot.messenger.telegram.formatting import (
    markdown_to_telegram_html,
    split_html_message,
)
from ductor_bot.security.paths import is_path_safe

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message


@dataclass(slots=True)
class SendRichOpts(BaseSendOpts):
    """Optional parameters for :func:`send_rich`."""

    reply_to_message_id: int | None = None
    reply_markup: InlineKeyboardMarkup | None = None
    thread_id: int | None = None
    ia_task_id: str = ""


class _MarkupKwargs(TypedDict, total=False):
    reply_markup: InlineKeyboardMarkup


class _DocumentKwargs(_MarkupKwargs):
    chat_id: int
    document: FSInputFile
    message_thread_id: int | None


class _TextKwargs(_MarkupKwargs, total=False):
    chat_id: int
    message_thread_id: int | None
    reply_parameters: ReplyParameters


logger = logging.getLogger(__name__)

_PHOTO_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
_VIDEO_SUFFIXES = frozenset({".mp4"})
_AUDIO_SUFFIXES = frozenset({".mp3", ".m4a"})

# IA reports select the first token at each original position; nested syntax is literal.
_IA_TOKEN_RE = re.compile(r"<file:(?P<file>[^>]+)>|\[button:(?P<button>[^\]]+)\]")


def _select_telegram_upload_mode(path: Path, mime: str) -> str:
    """Return best Telegram upload mode for this file.

    Non-matching or unsupported formats are sent as document.
    """
    suffix = path.suffix.lower()
    if mime.startswith("image/") and suffix in _PHOTO_SUFFIXES:
        return "photo"
    if mime.startswith("video/") and suffix in _VIDEO_SUFFIXES:
        return "video"
    if mime.startswith("audio/") and suffix in _AUDIO_SUFFIXES:
        return "audio"
    return "document"


async def _send_document(
    bot: Bot,
    chat_id: int,
    path: Path,
    thread_id: int | None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    kwargs: _DocumentKwargs = {
        "chat_id": chat_id,
        "document": FSInputFile(path),
        "message_thread_id": thread_id,
    }
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    return await bot.send_document(**kwargs)


async def _send_by_mode(  # noqa: PLR0913 -- upload routing and markup options
    bot: Bot,
    chat_id: int,
    path: Path,
    *,
    upload_mode: str,
    thread_id: int | None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    if upload_mode == "document":
        return await _send_document(bot, chat_id, path, thread_id, reply_markup)

    input_file = FSInputFile(path)
    markup_kwargs: _MarkupKwargs = (
        {"reply_markup": reply_markup} if reply_markup is not None else {}
    )

    try:
        if upload_mode == "photo":
            return await bot.send_photo(
                chat_id=chat_id,
                photo=input_file,
                message_thread_id=thread_id,
                **markup_kwargs,
            )
        if upload_mode == "video":
            return await bot.send_video(
                chat_id=chat_id,
                video=input_file,
                message_thread_id=thread_id,
                **markup_kwargs,
            )
        if upload_mode == "audio":
            return await bot.send_audio(
                chat_id=chat_id,
                audio=input_file,
                message_thread_id=thread_id,
                **markup_kwargs,
            )
        return await _send_document(bot, chat_id, path, thread_id, reply_markup)
    except TelegramBadRequest:
        logger.info(
            "%s upload rejected, retrying as document: %s",
            upload_mode.capitalize(),
            path.name,
        )
        return await _send_document(bot, chat_id, path, thread_id, reply_markup)


async def send_files_from_text(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    allowed_roots: Sequence[Path] | None = None,
    thread_id: int | None = None,
) -> None:
    """Extract ``<file:/path>`` tags from *text* and send each file.

    Use after streaming, where text was already sent but file tags need
    separate handling.
    """
    for fp in extract_file_paths(text):
        await send_file(
            bot,
            chat_id,
            path_from_file_tag(fp),
            allowed_roots=allowed_roots,
            thread_id=thread_id,
        )


async def _send_text_chunks(
    bot: Bot,
    chat_id: int,
    clean_text: str,
    *,
    reply_to_message_id: int | None = None,
    thread_id: int | None = None,
) -> tuple[Message | None, bool]:
    """Send *clean_text* as HTML chunks, falling back to plain text on error.

    Returns ``(last_message, delivered)`` — *delivered* is ``False`` when a
    network error cut the send short (#160).
    """
    last_msg: Message | None = None
    html_text = markdown_to_telegram_html(clean_text)
    chunks = split_html_message(html_text)
    for i, chunk in enumerate(chunks):
        try:
            if reply_to_message_id and i == 0:
                last_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    reply_parameters=ReplyParameters(
                        message_id=reply_to_message_id,
                        allow_sending_without_reply=True,
                    ),
                    message_thread_id=thread_id,
                )
            else:
                last_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=thread_id,
                )
        except TelegramNetworkError:
            logger.warning("Network error sending chunk %d/%d, retrying once", i + 1, len(chunks))
            await asyncio.sleep(1)
            try:
                last_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=thread_id,
                )
            except (TelegramNetworkError, TelegramBadRequest):
                logger.exception("Retry failed for chunk %d/%d, message lost", i + 1, len(chunks))
                return last_msg, False
        except TelegramBadRequest:
            logger.warning(
                "HTML send failed at chunk %d/%d, falling back to plain text", i, len(chunks)
            )
            # Only resend unsent chunks (i onwards) to avoid duplicating
            # content that was already delivered as HTML.
            remaining = "\n\n".join(chunks[i:])
            plain = html_mod.unescape(re.sub(r"<[^>]+>", "", remaining))
            for pc in split_html_message(plain):
                last_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=pc,
                    parse_mode=None,
                    message_thread_id=thread_id,
                )
            break
    return last_msg, True


async def send_rich(
    bot: Bot,
    chat_id: int,
    text: str,
    opts: SendRichOpts | None = None,
) -> bool:
    """Parse <file:/path> tags, send text first, then files.

    When *opts.reply_markup* is provided it is used directly; otherwise buttons
    are extracted from ``[button:...]`` markers in the text.

    Returns ``True`` when the text was fully delivered, ``False`` when a
    network error cut it short (#160). File/button failures do not affect
    the return value.
    """
    o = opts or SendRichOpts()
    file_paths = FILE_PATH_RE.findall(text)
    clean_text = FILE_PATH_RE.sub("", text).strip()
    logger.debug("Sending rich text chars=%d files=%d", len(clean_text), len(file_paths))

    button_markup = o.reply_markup if o.reply_markup is not None else extract_buttons(clean_text)[1]
    last_msg: Message | None = None
    delivered = True

    if clean_text:
        last_msg, delivered = await _send_text_chunks(
            bot,
            chat_id,
            clean_text,
            reply_to_message_id=o.reply_to_message_id,
            thread_id=o.thread_id,
        )

    if button_markup is not None and last_msg is not None:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=last_msg.message_id,
                reply_markup=button_markup,
            )
        except TelegramNetworkError:
            logger.debug("Network error attaching keyboard (likely shutdown)")
        except TelegramBadRequest:
            logger.warning("Failed to attach button keyboard in send_rich")

    for fp in file_paths:
        await send_file(
            bot,
            chat_id,
            path_from_file_tag(fp),
            allowed_roots=o.allowed_roots,
            thread_id=o.thread_id,
        )

    return delivered


def _ia_text_chunks(text: str) -> list[str]:
    """Split source text before formatting so fallback cannot change offsets."""
    chunks: list[str] = []
    while text:
        size = min(len(text), 4096)
        while len(markdown_to_telegram_html(text[:size])) > 4096:
            size = max(1, size // 2)
        chunks.append(text[:size])
        text = text[size:]
    return chunks


def _has_message_id(response: object) -> bool:
    message_id = getattr(response, "message_id", None)
    return type(message_id) is int and message_id > 0


def _is_html_parse_error(exc: TelegramBadRequest) -> bool:
    """Only a confirmed entity/HTML rejection can safely use plain fallback."""
    message = exc.message.lower()
    return any(
        term in message
        for term in (
            "can't parse entities",
            "can't parse entity",
            "unsupported start tag",
            "can't find end tag",
            "bad html",
        )
    )


async def _send_ia_text_chunk(  # noqa: PLR0913 -- sender and chunk options are independent
    bot: Bot,
    chat_id: int,
    raw: str,
    opts: SendRichOpts,
    index: int,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    kwargs: _TextKwargs = {
        "chat_id": chat_id,
        "message_thread_id": opts.thread_id,
    }
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    if index == 0 and opts.reply_to_message_id:
        kwargs["reply_parameters"] = ReplyParameters(
            message_id=opts.reply_to_message_id,
            allow_sending_without_reply=True,
        )
    try:
        response = await bot.send_message(
            **kwargs,
            text=markdown_to_telegram_html(raw),
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest as exc:
        rejection = exc
    except Exception as exc:
        # A timeout can follow server acceptance. Retrying later may duplicate this chunk.
        logger.warning(
            "Telegram IA chunk outcome uncertain task=%s chunk=%d error=%s; "
            "duplicate possible on retry",
            opts.ia_task_id,
            index,
            type(exc).__name__,
        )
        return False
    else:
        # Some test callers return an exception object in place of raising it.
        if not isinstance(response, TelegramBadRequest):
            return _has_message_id(response)
        rejection = response

    if not _is_html_parse_error(rejection):
        logger.warning(
            "Telegram IA HTML chunk rejected task=%s chunk=%d error=%s",
            opts.ia_task_id,
            index,
            type(rejection).__name__,
        )
        return False
    logger.warning(
        "Telegram IA HTML parse rejected task=%s chunk=%d; using plain text",
        opts.ia_task_id,
        index,
    )

    try:
        response = await bot.send_message(
            **kwargs,
            text=strip_button_syntax(raw),
            parse_mode=None,
        )
        return _has_message_id(response)
    except Exception as exc:
        logger.warning(
            "Telegram IA plain chunk failed task=%s chunk=%d error=%s; duplicate possible on retry",
            opts.ia_task_id,
            index,
            type(exc).__name__,
        )
        return False


async def _send_ia_file_chunk(
    bot: Bot,
    chat_id: int,
    file_tag: str,
    opts: SendRichOpts,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    try:
        path = path_from_file_tag(file_tag)
        if (
            opts.allowed_roots is not None and not is_path_safe(path, opts.allowed_roots)
        ) or not await asyncio.to_thread(path.exists):
            return False
        mime = guess_mime(path)
        response = await _send_by_mode(
            bot,
            chat_id,
            path,
            upload_mode=_select_telegram_upload_mode(path, mime),
            thread_id=opts.thread_id,
            reply_markup=reply_markup,
        )
        return _has_message_id(response)
    except Exception as exc:
        logger.warning("Telegram IA file chunk failed: %s", type(exc).__name__)
        return False


def _parse_ia_report(text: str) -> tuple[str, list[str], InlineKeyboardMarkup | None]:
    """Extract IA files and buttons without interpreting tokens inside tokens."""
    tokens = list(_IA_TOKEN_RE.finditer(text))
    files: list[str] = []
    display_parts: list[str] = []
    visible_prefix = ""
    cursor = 0
    for index, token in enumerate(tokens):
        segment = text[cursor : token.start()]
        visible_prefix += segment
        display_parts.append(segment)

        next_start = tokens[index + 1].start() if index + 1 < len(tokens) else len(text)
        next_segment = text[token.end() : next_start]
        candidate = visible_prefix + next_segment
        boundary = len(visible_prefix)
        # Removing a token must not join fragments into a new file/button token.
        separator = (
            " "
            if any(
                match.start() < boundary < match.end() for match in _IA_TOKEN_RE.finditer(candidate)
            )
            else ""
        )
        visible_prefix += separator
        if token.group("file") is not None:
            files.append(token.group("file"))
            display_parts.append(separator)
        else:
            # Keep selected buttons for the existing row/callback parser.
            display_parts.append(separator + token.group(0))
        cursor = token.end()

    display_parts.append(text[cursor:])
    clean_text, markup = extract_buttons("".join(display_parts).strip())
    return clean_text, files, markup


async def send_rich_checked_progress(
    bot: Bot,
    chat_id: int,
    text: str,
    opts: SendRichOpts | None = None,
    *,
    start_chunk: int = 0,
) -> tuple[bool, int]:
    """Send IA text and files in fixed chunks, stopping at the first failure."""
    o = opts or SendRichOpts()
    # The bus hashes the original report; IA-only display parsing is stable.
    clean_text, files, extracted_markup = _parse_ia_report(text)
    markup = o.reply_markup if o.reply_markup is not None else extracted_markup
    if not clean_text and not files and markup is not None:
        # Telegram cannot attach a keyboard without a message.
        clean_text = t("telegram.choose_option")
    chunks = [
        chunk for chunk in _ia_text_chunks(clean_text) if markdown_to_telegram_html(chunk).strip()
    ]
    total = len(chunks) + len(files)
    if total == 0 or start_chunk < 0 or start_chunk > total:
        return False, start_chunk

    for index in range(start_chunk, total):
        last_markup = markup if index == total - 1 else None
        if index < len(chunks):
            sent = await _send_ia_text_chunk(
                bot,
                chat_id,
                chunks[index],
                o,
                index,
                reply_markup=last_markup,
            )
        else:
            sent = await _send_ia_file_chunk(
                bot,
                chat_id,
                files[index - len(chunks)],
                o,
                reply_markup=last_markup,
            )
        if not sent:
            return False, index
    return True, total


async def send_file(
    bot: Bot,
    chat_id: int,
    path: Path,
    *,
    allowed_roots: Sequence[Path] | None = None,
    thread_id: int | None = None,
) -> None:
    """Send a local file with Telegram media/document routing."""
    if allowed_roots is not None and not is_path_safe(path, allowed_roots):
        logger.warning("File path blocked (outside allowed roots): %s", path)
        await bot.send_message(
            chat_id=chat_id,
            text=t("notice.file_outside", name=path.name),
            parse_mode="HTML",
            message_thread_id=thread_id,
        )
        return

    if not await asyncio.to_thread(path.exists):
        logger.warning("File not found, skipping: %s", path)
        await bot.send_message(
            chat_id=chat_id,
            text=t("notice.file_not_found", name=path.name, path=path),
            parse_mode=None,
            message_thread_id=thread_id,
        )
        return

    try:
        mime = guess_mime(path)
        upload_mode = _select_telegram_upload_mode(path, mime)
        await _send_by_mode(
            bot,
            chat_id,
            path,
            upload_mode=upload_mode,
            thread_id=thread_id,
        )

        logger.info("Sent file: %s (%s)", path.name, mime)
    except TelegramNetworkError:
        logger.debug("Network error sending file (likely shutdown), skipping: %s", path)
    except OSError:
        logger.exception("Failed to send file: %s", path)
        await bot.send_message(
            chat_id=chat_id,
            text=t("notice.file_send_failed", name=path.name),
            parse_mode=None,
            message_thread_id=thread_id,
        )
    except TelegramBadRequest:
        logger.exception("Telegram rejected file upload: %s", path)
        await bot.send_message(
            chat_id=chat_id,
            text=t("notice.file_send_failed", name=path.name),
            parse_mode=None,
            message_thread_id=thread_id,
        )
