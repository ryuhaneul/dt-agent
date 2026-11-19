"""Authorized user/topic input, group mention policy, dedup and shared callbacks."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ductor_bot.messenger.telegram.callbacks import (
    _selector_data,
    selector_button_data,
    selector_callback_lock_key,
    selector_callback_response,
)
from ductor_bot.session import SessionKey
from tests.messenger.telegram.test_app import _make_callback_query, _make_message, _make_tg_bot
from tests.orchestrator.conftest import (
    orch as orch,  # noqa: PLC0414 -- register shared pytest fixtures
)
from tests.orchestrator.conftest import (
    workspace as workspace,  # noqa: PLC0414 -- register shared pytest fixtures
)


@pytest.fixture
def ac_bot(tmp_path, orch):
    cfg = orch._config
    cfg.ductor_home = str(tmp_path)
    cfg.allowed_user_ids = [100, 101]
    cfg.allowed_group_ids = [-1001]
    cfg.group_mention_only = True
    bot, _ = _make_tg_bot(cfg)
    bot._orchestrator = orch
    bot._bot_username = "my_bot"
    bot._bot_id = 9
    bot._sequential.set_bot_username("my_bot")
    bot._sequential.set_bot_id(9)
    bot._sequential.set_message_received_handler(lambda _: None)
    bot._send_command_response = AsyncMock(return_value=True)
    return bot


async def _receive(bot, message, normal):
    async def route(event, data):
        return await bot._sequential(normal, event, data)

    return await bot._auth(route, message, {})


def _message(text, message_id, user=100, topic=42):
    message = _make_message(
        chat_id=-1001,
        message_id=message_id,
        text=text,
        chat_type="supergroup",
        user_id=user,
        topic_thread_id=topic,
    )
    message.entities = []
    message.caption_entities = []
    message.caption = None
    message.date = None
    return message


async def test_mention_only_accepts_owner_but_not_other_users_or_topics(ac_bot):
    bot = ac_bot
    key = SessionKey(chat_id=-1001, topic_id=42)
    await selector_callback_response(bot, key, "ac:topic:input", user_id=100)
    normal = AsyncMock()
    await _receive(bot, _message("400000", 1, user=101), normal)
    await _receive(bot, _message("400000", 2, topic=43), normal)
    normal.assert_not_called()
    assert (key, 100) in bot._orch._autocompact_inputs
    bot._send_command_response.assert_not_called()
    await _receive(bot, _message("not a number", 3), normal)
    assert "100,000" in bot._send_command_response.call_args.args[1]
    assert (key, 100) in bot._orch._autocompact_inputs
    await _receive(bot, _message("300000", 4), normal)
    assert (await bot._orch._sessions.get_active(key)).autocompact == 300000
    assert not bot._orch._autocompact_inputs
    normal.assert_not_called()
    bot._orch._cli_service.execute.assert_not_called()


async def test_no_mention_policy_deduplicates_completed_input(ac_bot):
    bot = ac_bot
    bot._sequential._group_mention_only = False
    key = SessionKey(chat_id=-1001, topic_id=42)
    await selector_callback_response(bot, key, "ac:topic:input", user_id=100)
    normal = AsyncMock()
    await _receive(bot, _message("400000", 1, user=101), normal)
    assert normal.await_count == 1
    message = _message("300000", 2)
    await _receive(bot, message, normal)
    await _receive(bot, message, normal)
    assert normal.await_count == 1
    assert bot._send_command_response.await_count == 1
    assert (await bot._orch._sessions.get_active(key)).autocompact == 300000


async def test_cancel_is_consumed_and_clears_only_owner(ac_bot):
    bot = ac_bot
    key = SessionKey(chat_id=-1001, topic_id=42)
    await selector_callback_response(bot, key, "ac:topic:input", user_id=100)
    await selector_callback_response(bot, key, "ac:topic:input", user_id=101)
    normal = AsyncMock()
    await _receive(bot, _message("/cancel", 1), normal)
    assert (key, 100) not in bot._orch._autocompact_inputs
    assert (key, 101) in bot._orch._autocompact_inputs
    normal.assert_not_called()
    assert "cancelled" in bot._send_command_response.call_args.args[1]


async def test_disallowed_actor_cannot_change_pending_input_or_click(ac_bot):
    bot = ac_bot
    key = SessionKey(chat_id=-1001, topic_id=42)
    await selector_callback_response(bot, key, "ac:topic:input", user_id=100)
    normal = AsyncMock()
    await _receive(bot, _message("300000", 1, user=999), normal)
    normal.assert_not_called()
    bot._send_command_response.assert_not_called()
    assert (key, 100) in bot._orch._autocompact_inputs
    bot._run_selector_callback = AsyncMock()
    callback = _make_callback_query("ac:topic:off", chat_id=-1001, user_id=999, topic_thread_id=42)
    await bot._on_callback_query(callback)
    bot._run_selector_callback.assert_not_called()
    assert await bot._orch._sessions.get_active(key) is None


async def test_telegram_callback_passes_real_actor_to_shared_dispatcher(ac_bot):
    bot = ac_bot
    bot._run_selector_callback = AsyncMock()
    callback = _make_callback_query(
        "ac:topic:input", chat_id=-1001, user_id=100, topic_thread_id=42
    )
    await bot._on_callback_query(callback)
    assert bot._run_selector_callback.await_args.kwargs["user_id"] == 100


def test_ac_namespaces_have_shared_selector_kind_and_lock():
    key = SessionKey(chat_id=-1001, topic_id=42)
    assert selector_button_data("ac:topic:input")
    assert _selector_data("ac:cancel")
    assert selector_callback_lock_key(key, "ac:global:off") == key.lock_key


async def test_automation_input_is_authorized_isolated_and_never_leaks_to_cli(ac_bot):
    from tests.orchestrator.test_autocompact_automation import callback, seed

    bot = ac_bot
    seed(bot._orch)
    key = SessionKey(chat_id=-1001, topic_id=42)
    data = callback("cron", "job-0", "input")
    await selector_callback_response(bot, key, data, user_id=100)
    normal = AsyncMock()
    await _receive(bot, _message("400000", 21, user=999), normal)
    await _receive(bot, _message("400000", 22, user=101), normal)
    await _receive(bot, _message("400000", 23, topic=43), normal)
    assert (key, 100) in bot._orch._autocompact_inputs
    bot._send_command_response.assert_not_called()
    await _receive(bot, _message("bad", 24), normal)
    assert "100,000" in bot._send_command_response.call_args.args[1]
    await _receive(bot, _message("300000", 25), normal)
    assert bot._orch._cron_manager.get_job("job-0").autocompact == 300000
    assert not bot._orch._autocompact_inputs
    normal.assert_not_called()
    bot._orch._cli_service.execute.assert_not_called()
    assert await bot._orch._sessions.get_active(key) is None
    await selector_callback_response(bot, key, data, user_id=100)
    await _receive(bot, _message("/cancel", 26), normal)
    assert not bot._orch._autocompact_inputs


async def test_automation_callback_denies_disallowed_actor(ac_bot):
    from tests.orchestrator.test_autocompact_automation import callback, seed

    bot = ac_bot
    seed(bot._orch)
    bot._run_selector_callback = AsyncMock()
    query = _make_callback_query(
        callback("cron", "job-0", "off"), chat_id=-1001, user_id=999, topic_thread_id=42
    )
    await bot._on_callback_query(query)
    bot._run_selector_callback.assert_not_called()
    assert bot._orch._cron_manager.get_job("job-0").autocompact is None
