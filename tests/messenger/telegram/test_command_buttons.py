"""Command buttons use the real typed-message dispatch pipeline."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from ductor_bot.messenger.commands import DIRECT_COMMANDS, MULTIAGENT_COMMANDS, ORCHESTRATOR_COMMANDS
from ductor_bot.messenger.telegram.app import TelegramBot
from tests.messenger.telegram.test_app import _make_config, _make_orchestrator, _make_tg_bot


def callback(data, *, chat_type="private", chat_id=1, callback_id="cb-1"):
    cb = MagicMock(spec=CallbackQuery)
    cb.id = callback_id
    cb.data = data
    cb.from_user = User(id=100, is_bot=False, first_name="Clicker")
    cb.answer = AsyncMock()
    cb.message = Message(
        message_id=42, date=datetime.now(timezone.utc), chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=200, is_bot=True, first_name="Bot"), text="ORIGINAL_BOT_BODY",
        message_thread_id=5, is_topic_message=True,
    )
    return cb


@pytest.mark.parametrize("data,handler", [
    ("/restart", "_on_restart"), ("/new", "_on_new"), ("/help", "_on_help"),
    ("/agent_restart helper", "_on_command"),
])
async def test_registered_handler_receives_clicked_command_with_user_topic_and_choice(data, handler):
    with patch.object(TelegramBot, handler, new_callable=AsyncMock) as command_handler:
        command_handler.aiogram_flag = {}
        bot, api = _make_tg_bot()
        bot._orchestrator = _make_orchestrator()
        bot._handle_streaming = AsyncMock()
        bot._handle_non_streaming = AsyncMock()
        cb = callback(data)
        await bot._on_callback_query(cb)
    command_handler.assert_awaited_once()
    message = command_handler.call_args.args[0]
    assert message.text == data
    assert message.from_user.id == 100
    assert message.message_id == 42
    assert message.message_thread_id == 5
    assert message.is_topic_message
    assert message.entities[0].type == "bot_command"
    api.edit_message_text.assert_awaited_once()
    bot._handle_streaming.assert_not_awaited()
    bot._handle_non_streaming.assert_not_awaited()


async def test_status_command_uses_quick_handler_even_while_chat_is_locked():
    bot, _ = _make_tg_bot()
    bot._orchestrator = _make_orchestrator()
    bot._handle_streaming = AsyncMock()
    with patch("ductor_bot.messenger.telegram.app.handle_command", new_callable=AsyncMock) as handler:
        async with bot._sequential.get_lock((1, 5)):
            await asyncio.wait_for(bot._on_callback_query(callback("/status")), timeout=1)
    handler.assert_awaited_once()
    assert handler.call_args.args[2].text == "/status"
    bot._handle_streaming.assert_not_awaited()


async def test_interrupt_command_uses_existing_interrupt_callback():
    with patch.object(TelegramBot, "_on_interrupt", new_callable=AsyncMock,
                      return_value=True) as handler:
        bot, _ = _make_tg_bot()
        bot._handle_streaming = AsyncMock()
        await bot._on_callback_query(callback("/interrupt"))
    handler.assert_awaited_once()
    assert handler.call_args.args[1].text == "/interrupt"
    bot._handle_streaming.assert_not_awaited()


@pytest.mark.parametrize("data", ["/unknown_command", " /restart"])
async def test_unknown_or_not_slash_prefixed_callback_stays_ai_prompt(data):
    bot, _ = _make_tg_bot()
    bot._handle_streaming = AsyncMock()
    await bot._on_callback_query(callback(data))
    bot._handle_streaming.assert_awaited_once()
    assert bot._handle_streaming.call_args.args[2] == data


@pytest.mark.parametrize("data,accepted", [("/restart", False),
    ("/restart@different_bot", False), ("/restart@test_bot", True)])
async def test_group_mentions_match_typed_command_gate(data, accepted):
    config = _make_config(group_mention_only=True)
    config.allowed_group_ids = [-1001]
    with patch.object(TelegramBot, "_on_restart", new_callable=AsyncMock) as handler:
        handler.aiogram_flag = {}
        bot, api = _make_tg_bot(config)
        bot._bot_username = "test_bot"
        bot._sequential.set_bot_username("test_bot")
        api.me = AsyncMock(return_value=User(id=200, is_bot=True, first_name="Bot", username="test_bot"))
        bot._handle_streaming = AsyncMock()
        await bot._on_callback_query(callback(data, chat_type="supergroup", chat_id=-1001))
    assert handler.await_count == int(accepted)
    bot._handle_streaming.assert_not_awaited()


async def test_button_clicks_reuse_message_id_without_deduping_distinct_callbacks():
    with patch.object(TelegramBot, "_on_restart", new_callable=AsyncMock) as handler:
        handler.aiogram_flag = {}
        bot, _ = _make_tg_bot()
        await bot._on_callback_query(callback("/restart", callback_id="one"))
        await bot._on_callback_query(callback("/restart", callback_id="two"))
        await bot._on_callback_query(callback("/restart", callback_id="two"))
    assert handler.await_count == 2


async def test_callback_outer_auth_and_message_auth_share_rejection_window(caplog):
    bot, _ = _make_tg_bot()
    cb = callback("/restart", chat_type="supergroup", chat_id=-1001)
    with caplog.at_level("INFO"):
        await bot._router.propagate_event("callback_query", cb, bot=bot._bot)
        await bot._router.propagate_event(
            "message", cb.message.model_copy(update={"from_user": cb.from_user}), bot=bot._bot,
        )
    cb.answer.assert_not_awaited()
    assert len([r for r in caplog.records if "Allowlist rejection" in r.message]) == 1
    # Callback and message rejections share the same (chat, clicker) limit.
    assert "ORIGINAL_BOT_BODY" not in caplog.text


@pytest.mark.parametrize("command", sorted(
    DIRECT_COMMANDS | ORCHESTRATOR_COMMANDS | MULTIAGENT_COMMANDS
))
async def test_each_registered_command_category_avoids_message_prompt(command):
    # Keep aiogram's actual filters/middleware. Stub only the final command actions.
    handlers = ["_on_start", "_on_help", "_on_info", "_on_stop_all", "_on_stop",
                "_on_restart", "_on_new", "_on_session", "_on_sessions", "_on_tasks",
                "_on_showfiles", "_on_usage", "_on_agent_commands", "_on_command", "_on_interrupt",
                "_on_abort", "_on_abort_all", "_on_quick_command"]
    with ExitStack() as stack:
        actions = []
        for name in handlers:
            action = AsyncMock(return_value=True)
            action.aiogram_flag = {}
            stack.enter_context(patch.object(TelegramBot, name, action))
            actions.append(action)
        bot, _ = _make_tg_bot()
        bot._on_message = AsyncMock()
        bot._handle_streaming = AsyncMock()
        bot._handle_non_streaming = AsyncMock()
        await bot._on_callback_query(callback("/" + command))
    assert sum(action.await_count for action in actions) == 1
    bot._handle_streaming.assert_not_awaited()
    bot._handle_non_streaming.assert_not_awaited()
