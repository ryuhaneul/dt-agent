"""Telegram send time of delayed messages, and the quiet allowlist log for the bot's own messages."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

from ductor_bot.cli.types import AgentRequest
from ductor_bot.infra import input_context as ic
from ductor_bot.messenger.queue_context import (
    QueuedMessageLock,
    current_queue_context,
    without_queue_context,
)
from ductor_bot.messenger.telegram.middleware import AuthMiddleware

NOW = 1_780_000_000.0  # a fixed receipt time (seconds, whole)
USER = 100
BOT = 555


def _message(
    *, sent_at: float | None, user_id: int = USER, chat_type: str = "private"
) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.chat = MagicMock()
    msg.chat.id = 1
    msg.chat.type = chat_type
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.text = "hello"
    msg.message_id = 1
    msg.is_topic_message = False
    msg.message_thread_id = None
    msg.date = None if sent_at is None else datetime.fromtimestamp(sent_at, UTC)
    return msg


async def _context_seen_by_handler(event: object, *, timezone: str = "UTC") -> str | None:
    """Run the auth middleware and return the receipt line the next layer would see."""
    seen: list[str | None] = []

    async def handler(_event: object, _data: dict[str, object]) -> str:
        seen.append(ic.current_input_context(timezone))
        return "ok"

    mw = AuthMiddleware(allowed_user_ids={USER})
    with ic.received_at(NOW):
        await mw(handler, event, {})  # type: ignore[arg-type]
    assert seen, "the handler was not reached"
    return seen[0]


# -- the threshold ------------------------------------------------------------------------------


@pytest.mark.parametrize("delay", [0, 1, 9, 9.9])
async def test_a_slightly_delayed_message_shows_no_sent_time(delay: float) -> None:
    line = await _context_seen_by_handler(_message(sent_at=NOW - delay))
    assert line is not None
    assert line.startswith("[Received: ")
    assert "[Sent:" not in line


@pytest.mark.parametrize("delay", [10, 11, 3600])
async def test_a_message_delayed_ten_seconds_or_more_shows_its_sent_time(delay: float) -> None:
    line = await _context_seen_by_handler(_message(sent_at=NOW - delay))
    assert line is not None
    received = datetime.fromtimestamp(NOW, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    sent = datetime.fromtimestamp(NOW - delay, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    assert line == f"[Received: {received}] [Sent: {sent}]"


async def test_the_sent_time_uses_the_given_timezone_in_the_same_format() -> None:
    line = await _context_seen_by_handler(_message(sent_at=NOW - 60), timezone="Asia/Seoul")
    assert line is not None
    assert line.count(" KST]") == 2  # both stamps in the user's timezone


async def test_a_message_without_a_send_time_or_from_the_future_shows_none() -> None:
    assert "[Sent:" not in (await _context_seen_by_handler(_message(sent_at=None)) or "")
    assert "[Sent:" not in (await _context_seen_by_handler(_message(sent_at=NOW + 30)) or "")


async def test_the_wait_comes_after_the_sent_time() -> None:
    seen: list[str | None] = []

    async def handler(_event: object, _data: dict[str, object]) -> None:
        ic.add_input_wait(5)
        seen.append(ic.current_input_context("UTC"))

    with ic.received_at(NOW):
        await AuthMiddleware(allowed_user_ids={USER})(handler, _message(sent_at=NOW - 20), {})  # type: ignore[arg-type]
    assert seen[0] is not None
    assert seen[0].index("[Sent:") < seen[0].index("[Waited: 5s]")


async def test_a_callback_never_carries_the_old_bot_message_time() -> None:
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = MagicMock()
    callback.from_user.id = USER
    callback.message = _message(sent_at=NOW - 3600)  # the bot's own, hour-old message
    line = await _context_seen_by_handler(callback)
    assert line is not None
    assert "[Sent:" not in line


# -- queued messages, CLI prompt, prefix removal --------------------------------------------------


async def test_a_queued_message_states_its_send_time_once() -> None:
    lock = asyncio.Lock()
    await lock.acquire()
    sent = datetime.fromtimestamp(NOW - 120, UTC)
    seen: list[AgentRequest] = []

    async def handler(_event: object, _data: dict[str, object]) -> None:
        queued = QueuedMessageLock(lock, sent)

        async def release_soon() -> None:
            await asyncio.sleep(0.01)
            lock.release()

        waiter = asyncio.create_task(release_soon())
        async with queued:
            block = current_queue_context("UTC")
            seen.append(
                AgentRequest(
                    prompt="hello",
                    queue_context=block,
                    received_context=ic.current_input_context("UTC"),
                )
            )
        await waiter

    with ic.received_at(NOW):
        await AuthMiddleware(allowed_user_ids={USER})(handler, _message(sent_at=NOW - 120), {})  # type: ignore[arg-type]

    prompt = seen[0].cli_prompt
    assert prompt.count("Sent at:") == 1
    assert "[Sent:" not in prompt  # not repeated as a bracket suffix
    assert prompt.count("[Received:") == 1
    assert prompt.endswith("\n\nhello")


async def test_a_delayed_message_without_a_queue_has_the_sent_time_once_in_the_cli_prompt() -> None:
    seen: list[str] = []

    async def handler(_event: object, _data: dict[str, object]) -> None:
        request = AgentRequest(prompt="hello", received_context=ic.current_input_context("UTC"))
        seen.append(request.cli_prompt)

    with ic.received_at(NOW):
        await AuthMiddleware(allowed_user_ids={USER})(handler, _message(sent_at=NOW - 30), {})  # type: ignore[arg-type]

    assert seen[0].count("[Sent:") == 1
    assert seen[0].endswith("\nhello")


@pytest.mark.parametrize(
    "line",
    [
        "[Received: 2026-10-07 12:00:00 UTC]",
        "[Received: 2026-10-07 12:00:00 UTC] [Waited: 5s]",
        "[Received: 2026-10-07 12:00:00 UTC] [Sent: 2026-10-07 11:00:00 UTC]",
        "[Received: 2026-10-07 12:00:00 UTC] [Sent: 2026-10-07 11:00:00 UTC] [Waited: 5s]",
    ],
)
def test_every_receipt_line_shape_is_removed_from_command_previews(line: str) -> None:
    assert without_queue_context(f"{line}\nclaude -p do it") == "claude -p do it"
    assert without_queue_context("no receipt\nline") == "no receipt\nline"


# -- the bot's own messages in the allowlist log -------------------------------------------------------


def _rejection_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "Allowlist rejection" in r.getMessage()]


async def test_the_bots_own_message_is_dropped_without_a_rejection_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mw = AuthMiddleware(allowed_user_ids={USER})
    mw.set_bot_id(BOT)
    handler = AsyncMock()

    with caplog.at_level(logging.INFO, logger="ductor_bot.messenger.telegram.middleware"):
        result = await mw(handler, _message(sent_at=NOW, user_id=BOT), {})

    assert result is None  # still dropped, only quiet
    handler.assert_not_called()
    assert _rejection_logs(caplog) == []


async def test_another_sender_is_still_logged(caplog: pytest.LogCaptureFixture) -> None:
    mw = AuthMiddleware(allowed_user_ids={USER})
    mw.set_bot_id(BOT)
    handler = AsyncMock()

    with caplog.at_level(logging.INFO, logger="ductor_bot.messenger.telegram.middleware"):
        result = await mw(handler, _message(sent_at=NOW, user_id=999), {})

    assert result is None
    assert len(_rejection_logs(caplog)) == 1
    assert "user_id=999" in _rejection_logs(caplog)[0]


async def test_before_the_bot_id_is_known_nothing_is_hidden(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mw = AuthMiddleware(allowed_user_ids={USER})
    with caplog.at_level(logging.INFO, logger="ductor_bot.messenger.telegram.middleware"):
        await mw(AsyncMock(), _message(sent_at=NOW, user_id=BOT), {})
    assert len(_rejection_logs(caplog)) == 1
