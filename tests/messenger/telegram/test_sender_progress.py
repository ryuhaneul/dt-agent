"""Detailed rich-send progress and legacy contract compatibility."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)

from ductor_bot.messenger.telegram import sender
from ductor_bot.messenger.telegram.sender import (
    SendRichOpts,
    send_rich_checked_progress,
    send_rich_progress_detailed,
)


async def test_partial_failure_and_resume(monkeypatch):
    monkeypatch.setattr(sender, "_ia_text_chunks", lambda _: ["first", "second", "third"])
    error = TelegramNetworkError(MagicMock(), "offline")
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[SimpleNamespace(message_id=1), error])
    progress = await send_rich_progress_detailed(bot, 42, "report")
    assert (progress.delivered, progress.sent_chunks, progress.failure) == (False, 1, "transient")
    assert progress.error == "TelegramNetworkError"
    assert progress.exception is None
    bot.send_message.reset_mock(side_effect=True)
    bot.send_message.side_effect = [SimpleNamespace(message_id=2), SimpleNamespace(message_id=3)]
    progress = await send_rich_progress_detailed(bot, 42, "report", start_chunk=1)
    assert progress.delivered
    assert progress.sent_chunks == 3
    assert progress.failure is None
    assert progress.error == ""
    assert progress.exception is None
    assert [c.kwargs["text"] for c in bot.send_message.await_args_list] == ["second", "third"]


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (TelegramNetworkError(MagicMock(), "offline"), "transient"),
        (TelegramServerError(MagicMock(), "server"), "transient"),
        (TelegramRetryAfter(MagicMock(), "busy", retry_after=1), "transient"),
        (TimeoutError(), "transient"),
        (ConnectionError(), "transient"),
        (RuntimeError("unknown"), "transient"),
        (TelegramBadRequest(MagicMock(), "topic missing"), "permanent"),
        (TelegramForbiddenError(MagicMock(), "blocked"), "permanent"),
        (TelegramNotFound(MagicMock(), "missing"), "permanent"),
        (TelegramUnauthorizedError(MagicMock(), "invalid"), "permanent"),
        (TelegramConflictError(MagicMock(), "conflict"), "permanent"),
        (TelegramEntityTooLarge(MagicMock(), "large"), "permanent"),
    ],
)
async def test_failure_classification_and_legacy_result(error, failure):
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=error)
    progress = await send_rich_progress_detailed(bot, 42, "report")
    assert not progress.delivered
    assert progress.sent_chunks == 0
    assert progress.failure == failure
    assert progress.error == type(error).__name__
    assert progress.exception is (
        error if failure == "permanent" or isinstance(error, TelegramRetryAfter) else None
    )
    assert await send_rich_checked_progress(bot, 42, "report") == (False, 0)


@pytest.mark.parametrize("blocked", [False, True])
async def test_file_unavailable(tmp_path, blocked):
    path = tmp_path / "report.txt"
    if blocked:
        path.write_text("report")
    opts = SendRichOpts(allowed_roots=[tmp_path / "other"] if blocked else [tmp_path])
    bot = MagicMock()
    progress = await send_rich_progress_detailed(bot, 42, f"<file:{path}>", opts)
    assert progress.failure == "permanent"
    assert progress.error == "file_unavailable"
    assert progress.exception is None
    assert progress.sent_chunks == 0
    bot.send_message.assert_not_called()


async def test_html_fallback_and_missing_confirmation():
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=[TelegramBadRequest(MagicMock(), "bad HTML"), SimpleNamespace(message_id=1)]
    )
    assert (await send_rich_progress_detailed(bot, 42, "**report**")).delivered
    assert bot.send_message.await_args_list[-1].kwargs["parse_mode"] is None
    error = TelegramForbiddenError(MagicMock(), "blocked")
    bot.send_message.side_effect = [TelegramBadRequest(MagicMock(), "bad HTML"), error]
    assert (await send_rich_progress_detailed(bot, 42, "report")).exception is error
    bot.send_message.side_effect = None
    bot.send_message.return_value = None
    progress = await send_rich_progress_detailed(bot, 42, "report")
    assert progress.failure == "transient"
    assert progress.error == "no_message_id"


@pytest.mark.parametrize(("text", "start"), [("", 0), ("report", -1), ("report", 2)])
async def test_legacy_boundaries(text, start):
    bot = MagicMock()
    assert await send_rich_checked_progress(bot, 42, text, start_chunk=start) == (False, start)
    bot.send_message.assert_not_called()
