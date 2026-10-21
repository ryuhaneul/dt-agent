"""Inter-agent results stay on their originating chat and topic."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import Envelope, Origin
from ductor_bot.messenger.matrix.bot import MatrixBot
from ductor_bot.messenger.matrix.transport import MatrixTransport
from ductor_bot.messenger.slack.bot import SlackBot
from ductor_bot.messenger.slack.transport import SlackTransport
from ductor_bot.messenger.telegram.app import TelegramBot
from ductor_bot.messenger.telegram.transport import TelegramTransport
from ductor_bot.multiagent.bus import AsyncInterAgentResult

_CASES = [
    (
        "tg",
        TelegramBot,
        TelegramTransport,
        "ductor_bot.messenger.telegram.transport.send_rich_checked_progress",
        (True, 1),
        123,
    ),
    (
        "mx",
        MatrixBot,
        MatrixTransport,
        "ductor_bot.messenger.matrix.transport.matrix_send_rich_checked_progress",
        (True, "$event", 1),
        "!origin:test",
    ),
    (
        "sl",
        SlackBot,
        SlackTransport,
        "ductor_bot.messenger.slack.transport.slack_send_rich_checked_progress",
        (True, "1.0", 1),
        "C-origin",
    ),
]


def _result(
    transport: str, *, chat_id: int = 123, topic_id: int | None = None
) -> AsyncInterAgentResult:
    return AsyncInterAgentResult(
        task_id="abc123",
        sender="sub",
        recipient="main",
        message_preview="Do the work",
        result_text="Done",
        success=True,
        original_message="Do the work",
        chat_id=chat_id,
        topic_id=topic_id,
        transport=transport,
    )


def _bot_with_bus(transport_class: type) -> tuple[MagicMock, AsyncMock]:
    bot = MagicMock()
    bot._agent_name = "main"
    bot._config.allowed_user_ids = [999]
    bot._default_chat_id.return_value = 999
    bot.id_map.int_to_room.return_value = "!origin:test"
    bot.id_map.int_to_channel.return_value = "C-origin"
    bot.id_map.int_to_thread.return_value = ("C-origin", "1.0")
    bot.file_roots.return_value = []
    bus = MessageBus()
    injector = AsyncMock()
    injector.inject_prompt.return_value = "Main's final report"
    bus.set_injector(injector)
    bus.register_transport(transport_class(bot))
    bot._bus = bus
    return bot, injector


@pytest.mark.parametrize("case", _CASES)
async def test_origin_chat_is_used_without_default_fallback(case: tuple) -> None:
    transport, bot_class, transport_class, send_path, success_value, send_target = case
    bot, injector = _bot_with_bus(transport_class)
    topic_id = None if transport == "mx" else 7
    result = _result(transport, topic_id=topic_id)
    with patch(send_path, new_callable=AsyncMock, return_value=success_value) as send:
        outcome = await bot_class.on_async_interagent_result(bot, result)
    assert outcome.ok
    assert injector.inject_prompt.await_args.args[1] == 123
    assert send.await_args.args[1] == send_target
    if transport == "tg":
        assert send.await_args.args[3].thread_id == 7
    if transport == "sl":
        assert send.await_args.args[3].thread_ts == "1.0"
    bot._default_chat_id.assert_not_called()


@pytest.mark.parametrize("case", _CASES)
async def test_no_origin_chat_uses_default_and_logs_reason(
    case: tuple, caplog: pytest.LogCaptureFixture
) -> None:
    transport, bot_class, transport_class, send_path, success_value, _send_target = case
    bot, injector = _bot_with_bus(transport_class)
    with (
        patch(send_path, new_callable=AsyncMock, return_value=success_value) as send,
        caplog.at_level(logging.INFO),
    ):
        outcome = await bot_class.on_async_interagent_result(bot, _result(transport, chat_id=0))
    assert outcome.ok
    assert injector.inject_prompt.await_args.args[1] == 999
    if transport == "tg":
        assert send.await_args.args[1] == 999
    else:
        bot._default_chat_id.assert_called_once()
    assert "no origin chat" in caplog.text


@pytest.mark.parametrize("case", _CASES)
async def test_topic_without_chat_rejected_without_fallback(case: tuple) -> None:
    transport, bot_class, transport_class, *_unused = case
    bot, injector = _bot_with_bus(transport_class)
    outcome = await bot_class.on_async_interagent_result(
        bot, _result(transport, chat_id=0, topic_id=7)
    )
    assert not outcome.ok
    assert outcome.error == "topic without chat_id"
    injector.inject_prompt.assert_not_awaited()
    bot._default_chat_id.assert_not_called()
    bot._notification_service.notify_all.assert_not_called()


@pytest.mark.parametrize("case", _CASES)
async def test_no_default_chat_never_broadcasts_ia_result(case: tuple) -> None:
    transport, bot_class, transport_class, *_unused = case
    bot, injector = _bot_with_bus(transport_class)
    bot._config.allowed_user_ids = []
    bot._default_chat_id.return_value = 0
    outcome = await bot_class.on_async_interagent_result(bot, _result(transport, chat_id=0))
    assert not outcome.ok
    assert outcome.error == "no chat_id"
    injector.inject_prompt.assert_not_awaited()
    bot._notification_service.notify_all.assert_not_called()


@pytest.mark.parametrize("thread", [None, ("C-other", "1.0")])
async def test_slack_unresolved_thread_does_not_post_to_channel(
    thread: tuple[str, str] | None,
) -> None:
    bot, _injector = _bot_with_bus(SlackTransport)
    bot.id_map.int_to_thread.return_value = thread
    env = Envelope(
        origin=Origin.INTERAGENT, chat_id=123, topic_id=7, transport="sl", result_text="report"
    )
    with patch(
        "ductor_bot.messenger.slack.transport.slack_send_rich_checked_progress",
        new_callable=AsyncMock,
    ) as send:
        await SlackTransport(bot).deliver(env)
    send.assert_not_awaited()
    assert not env.delivered
    assert env.delivery_error == "topic unresolved"


async def test_matrix_topic_is_rejected_before_injection_or_post() -> None:
    bot, injector = _bot_with_bus(MatrixTransport)
    outcome = await MatrixBot.on_async_interagent_result(bot, _result("mx", topic_id=7))
    assert not outcome.ok
    assert outcome.error == "topic unsupported"
    injector.inject_prompt.assert_not_awaited()
    env = Envelope(
        origin=Origin.INTERAGENT, chat_id=123, topic_id=7, transport="mx", result_text="report"
    )
    with patch(
        "ductor_bot.messenger.matrix.transport.matrix_send_rich_checked_progress",
        new_callable=AsyncMock,
    ) as send:
        await MatrixTransport(bot).deliver(env)
    send.assert_not_awaited()
    assert not env.delivered
    assert env.delivery_error == "topic unsupported"


async def test_telegram_missing_topic_fails_without_dm_fallback() -> None:
    bot, _injector = _bot_with_bus(TelegramTransport)
    env = Envelope(
        origin=Origin.INTERAGENT,
        chat_id=123,
        topic_id=7,
        transport="tg",
        result_text="report",
    )
    with patch(
        "ductor_bot.messenger.telegram.transport.send_rich_checked_progress",
        new_callable=AsyncMock,
        return_value=(False, 0),
    ) as send:
        await TelegramTransport(bot).deliver(env)
    send.assert_awaited_once()
    assert send.await_args.args[1] == 123
    assert send.await_args.args[3].thread_id == 7
    assert not env.delivered


@pytest.mark.parametrize("case", _CASES)
async def test_api_transport_is_rejected_before_messenger_injection(case: tuple) -> None:
    _transport, bot_class, transport_class, *_rest = case
    bot, injector = _bot_with_bus(transport_class)
    outcome = await bot_class.on_async_interagent_result(bot, _result("api"))
    assert not outcome.ok
    assert outcome.error == "transport mismatch"
    injector.inject_prompt.assert_not_awaited()
    bot._default_chat_id.assert_not_called()



@pytest.mark.parametrize("case", _CASES)
async def test_cron_broadcast_isolated_before_default_dm(case: tuple) -> None:
    transport, bot_class, transport_class, send_path, _success_value, _send_target = case
    bot, injector = _bot_with_bus(transport_class)
    result = _result(transport, chat_id=0)
    result.result_origin = "cron-broadcast"
    result.broadcast_ref = "job-1"
    bot.config.matrix.allowed_rooms = ["!public:test"]
    bot._last_active_room = None
    bot._broadcast_channels.return_value = ["C-public"]
    with patch(send_path, new_callable=AsyncMock, return_value=_success_value) as send:
        outcome = await bot_class.on_async_interagent_result(bot, result)
    assert outcome.ok
    assert outcome.injected
    call = injector.inject_prompt.await_args
    assert call.args[1] == 0
    assert call.kwargs["isolated"] is True
    assert call.kwargs["result_origin"] == "cron-broadcast"
    assert call.kwargs["broadcast_ref"] == "job-1"
    bot._default_chat_id.assert_not_called()
    send.assert_awaited_once()
    assert send.await_args.args[1] == {
        "tg": 999, "mx": "!public:test", "sl": "C-public",
    }[transport]


@pytest.mark.parametrize("case", _CASES)
@pytest.mark.parametrize("scenario", ["none", "partial", "all"])
async def test_cron_broadcast_checks_every_configured_recipient(
    case: tuple, scenario: str,
) -> None:
    transport, bot_class, transport_class, send_path, success, _target = case
    bot, injector = _bot_with_bus(transport_class)
    targets = {
        "tg": [101, 102, 103],
        "mx": ["!one:test", "!two:test", "!three:test"],
        "sl": ["C-one", "C-two", "C-three"],
    }[transport]
    bot._config.allowed_user_ids = targets if transport == "tg" else []
    bot.config.matrix.allowed_rooms = targets if transport == "mx" else []
    bot._last_active_room = None
    bot._broadcast_channels.return_value = targets if transport == "sl" else []
    if scenario == "none":
        bot._config.allowed_user_ids = []
        bot.config.matrix.allowed_rooms = []
        bot._broadcast_channels.return_value = []
        targets = []
    failed = {"tg": (False, 0), "mx": (False, None, 0), "sl": (False, None, 0)}[transport]
    values = [success, failed, success] if scenario == "partial" else [success] * len(targets)
    result = _result(transport, chat_id=0)
    result.result_origin = "cron-broadcast"
    with patch(send_path, new_callable=AsyncMock, side_effect=values) as send:
        outcome = await bot_class.on_async_interagent_result(bot, result)
    assert outcome.ok is (scenario == "all")
    assert outcome.injected
    assert send.await_count == len(targets)
    assert [call.args[1] for call in send.await_args_list] == targets
    assert all(call.kwargs["start_chunk"] == 0 for call in send.await_args_list)
    assert outcome.delivered_chunks == 0
    assert outcome.chunk_scheme == ""
    injector.inject_prompt.assert_awaited_once()
    bot._default_chat_id.assert_not_called()
