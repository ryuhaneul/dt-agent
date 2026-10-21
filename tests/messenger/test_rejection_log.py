"""Allowlist rejection diagnostics never include message bodies."""

from __future__ import annotations

import logging
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.messenger.matrix.bot import MatrixBot
from ductor_bot.messenger.rejection_log import RejectionLog
from ductor_bot.messenger.telegram.middleware import AuthMiddleware
from tests.messenger.slack.test_bot import _make_bot
from tests.messenger.telegram.test_middleware import _make_message


@pytest.mark.parametrize("chat_type,chat_id,user_id,groups", [
    ("private", 1, 999, set()),
    ("group", -1001, 100, set()),
    ("supergroup", -1001, 999, {-1001}),
])
async def test_telegram_rejection_metadata_without_body(caplog, chat_type, chat_id, user_id, groups):
    middleware = AuthMiddleware({100}, allowed_group_ids=groups)
    handler = AsyncMock()
    message = _make_message(chat_id, user_id, "DO_NOT_LOG_THIS_BODY", chat_type=chat_type,
                            topic_thread_id=5)
    with caplog.at_level(logging.INFO):
        await middleware(handler, message, {})
    handler.assert_not_awaited()
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.INFO
    assert f"chat_type={chat_type}" in record.message
    assert f"chat_id={chat_id}" in record.message
    assert f"user_id={user_id}" in record.message
    assert "topic_id=5" in record.message
    assert "DO_NOT_LOG_THIS_BODY" not in caplog.text


async def test_telegram_rejection_throttles_without_refreshing_window(caplog):
    middleware = AuthMiddleware({100})
    handler = AsyncMock()
    message = _make_message(user_id=999)
    with caplog.at_level(logging.INFO), patch(
        "ductor_bot.messenger.rejection_log.time.monotonic", return_value=0,
    ) as clock:
        await middleware(handler, message, {})
        clock.return_value = 599
        await middleware(handler, message, {})
        assert len(caplog.records) == 1
        clock.return_value = 600
        await middleware(handler, message, {})
        assert len(caplog.records) == 2
        await middleware(handler, _make_message(user_id=998), {})
        await middleware(handler, _make_message(chat_id=2, user_id=999), {})
        assert len(caplog.records) == 4


def test_rejection_cache_expires_and_is_bounded():
    rejection = RejectionLog(logging.getLogger("rejection-cache-test"))
    with patch("ductor_bot.messenger.rejection_log.time.monotonic", return_value=0) as clock:
        for user in range(1030):
            rejection.log("private", 1, user)
        assert len(rejection._last_logged) == 1024
        clock.return_value = 600
        rejection.log("private", 2, 1)
        assert len(rejection._last_logged) == 1


async def test_authorized_telegram_message_has_no_rejection_log(caplog):
    middleware = AuthMiddleware({100})
    with caplog.at_level(logging.INFO):
        await middleware(AsyncMock(), _make_message(), {})
    assert not caplog.records


async def test_matrix_rejection_logs_native_ids_thread_and_no_body(caplog):
    bot = SimpleNamespace(
        _client=SimpleNamespace(user_id="@bot:example.test"), _leaving_rooms=set(),
        _config=SimpleNamespace(matrix=SimpleNamespace(allowed_rooms=["!allowed:example.test"],
                                                       allowed_users=["@owner:example.test"]),
                                group_mention_only=False),
        _allowed_rooms_set={"!allowed:example.test"}, _is_dm_room=lambda room: False,
        _rejections=RejectionLog(logging.getLogger("matrix-rejection-test")),
    )
    bot._is_authorized = partial(MatrixBot._is_authorized, bot)
    room = SimpleNamespace(room_id="!denied:example.test")
    event = SimpleNamespace(sender="@denied:example.test", body="DO_NOT_LOG_MATRIX_BODY",
                            source={"content": {"body": "DO_NOT_LOG_MATRIX_BODY", "m.relates_to": {
                                "rel_type": "m.thread", "event_id": "$thread",
                            }}})
    with caplog.at_level(logging.INFO):
        assert not MatrixBot._should_process_event(bot, room, event, event.sender)
        assert not MatrixBot._should_process_event(bot, room, event, event.sender)
    assert len(caplog.records) == 1
    assert "chat_type=group chat_id=!denied:example.test user_id=@denied:example.test topic_id=$thread" in caplog.text
    assert "DO_NOT_LOG_MATRIX_BODY" not in caplog.text


async def test_slack_rejection_logs_native_ids_thread_and_no_body(caplog):
    bot = _make_bot()
    bot._rejections = RejectionLog(logging.getLogger("slack-rejection-test"))
    event = {"channel": "DENIED", "user": "U999", "text": "DO_NOT_LOG_SLACK_BODY",
             "channel_type": "channel", "thread_ts": "100.1", "ts": "100.2"}
    with caplog.at_level(logging.INFO):
        await bot._on_message(event)
        await bot._on_message(event)
    assert len(caplog.records) == 1
    assert "chat_type=group chat_id=DENIED user_id=U999 topic_id=100.1" in caplog.text
    assert "DO_NOT_LOG_SLACK_BODY" not in caplog.text
    bot._dispatch_with_lock.assert_not_awaited()
    bot._handle_command.assert_not_awaited()
