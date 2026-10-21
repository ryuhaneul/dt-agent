"""Async inter-agent injection and delivery acknowledgement."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.bus.adapters import (
    build_interagent_injection_prompt,
    from_interagent_result,
)
from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import DeliveryOutcome
from ductor_bot.multiagent.bus import AsyncInterAgentResult


def _result(*, success: bool = True, error: str | None = None) -> AsyncInterAgentResult:
    return AsyncInterAgentResult(
        task_id="task-1",
        sender="main",
        recipient="developer",
        message_preview="Fix the issue",
        result_text="work complete" if success else "",
        success=success,
        error=error,
        original_message="Fix the issue in module A",
        chat_id=123,
        topic_id=7,
        transport="tg",
    )


async def _submit_result(
    result: AsyncInterAgentResult,
    *,
    injection_error: bool = False,
    injection_response: str = "Main's final report",
) -> tuple[object, object, AsyncMock]:
    bus = MessageBus()
    injector = AsyncMock()
    if injection_error:
        injector.inject_prompt = AsyncMock(side_effect=RuntimeError("injection failed"))
    else:
        injector.inject_prompt = AsyncMock(return_value=injection_response)
    bus.set_injector(injector)
    transport = AsyncMock()
    transport.transport_name = "tg"
    bus.register_transport(transport)
    prompt = build_interagent_injection_prompt(
        result, agent_name="main", transport_label="Telegram chat"
    )
    env = from_interagent_result(result, 999, injection_prompt=prompt)
    outcome = await bus.submit(env)
    return outcome, env, transport


async def test_injection_exception_is_not_success() -> None:
    outcome, env, transport = await _submit_result(_result(), injection_error=True)
    assert not outcome.ok
    assert not outcome.injected
    assert not env.injected
    assert env.result_text == ""
    assert outcome.error == "injection failed"
    transport.deliver.assert_not_awaited()
    transport.deliver_broadcast.assert_not_awaited()


@pytest.mark.parametrize("response", ["", " \n  "])
async def test_empty_injection_response_is_not_success(response: str) -> None:
    outcome, env, transport = await _submit_result(_result(), injection_response=response)
    assert not outcome.ok
    assert not outcome.injected
    assert not outcome.delivered
    assert outcome.error == "empty injection response"
    assert env.result_text == ""
    transport.deliver.assert_not_awaited()


async def test_success_result_requires_injection_and_report_delivery() -> None:
    outcome, env, transport = await _submit_result(_result())
    assert outcome.ok
    assert outcome.injected
    assert outcome.delivered
    assert env.result_text == "Main's final report"
    assert (env.chat_id, env.topic_id) == (123, 7)
    transport.deliver.assert_awaited_once_with(env)


@pytest.mark.parametrize("error", ["timeout", "interrupted by restart"])
async def test_failed_result_is_injected_and_final_report_posted(error: str) -> None:
    result = _result(success=False, error=error)
    outcome, env, transport = await _submit_result(result)
    assert outcome.ok
    assert outcome.injected
    assert outcome.delivered
    assert error in env.prompt
    assert result.original_message in env.prompt
    assert "Decide whether to retry" in env.prompt
    assert env.result_text == "Main's final report"
    transport.deliver.assert_awaited_once_with(env)


@pytest.mark.parametrize(
    ("bot_path", "transport"),
    [
        ("ductor_bot.messenger.telegram.app.TelegramBot", "tg"),
        ("ductor_bot.messenger.matrix.bot.MatrixBot", "mx"),
        ("ductor_bot.messenger.slack.bot.SlackBot", "sl"),
    ],
)
async def test_transport_bot_returns_bus_outcome_and_preserves_origin(
    bot_path: str, transport: str
) -> None:
    from importlib import import_module

    module_name, class_name = bot_path.rsplit(".", 1)
    bot_cls = getattr(import_module(module_name), class_name)
    bot = MagicMock()
    bot._agent_name = "main"
    bot._config.allowed_user_ids = [999]
    expected = DeliveryOutcome(ok=True, injected=True, delivered=True)
    bot._bus.submit = AsyncMock(return_value=expected)
    result = _result()
    result.transport = transport
    if transport == "mx":
        result.topic_id = None

    assert await bot_cls.on_async_interagent_result(bot, result) == expected
    envelope = bot._bus.submit.await_args.args[0]
    assert (envelope.chat_id, envelope.topic_id, envelope.transport) == (
        123,
        result.topic_id,
        transport,
    )
    assert envelope.prompt


async def test_slack_bot_rejects_mismatched_transport_before_injection() -> None:
    from ductor_bot.messenger.slack.bot import SlackBot

    bot = MagicMock()
    result = _result()
    result.transport = "tg"
    outcome = await SlackBot.on_async_interagent_result(bot, result)
    assert outcome == DeliveryOutcome(
        ok=False, injected=False, delivered=False, error="transport mismatch"
    )
    bot._bus.submit.assert_not_called()
    bot._notification_service.notify_all.assert_not_called()


async def test_slack_bot_accepts_missing_transport() -> None:
    from ductor_bot.messenger.slack.bot import SlackBot

    bot = MagicMock()
    bot._agent_name = "main"
    expected = DeliveryOutcome(ok=True, injected=True, delivered=True)
    bot._bus.submit = AsyncMock(return_value=expected)
    result = _result()
    result.transport = ""
    assert await SlackBot.on_async_interagent_result(bot, result) == expected
    bot._bus.submit.assert_awaited_once()


async def test_report_delivery_flag_controls_outcome() -> None:
    bus = MessageBus()
    injector = AsyncMock()
    injector.inject_prompt = AsyncMock(return_value="Main's final report")
    bus.set_injector(injector)
    transport = AsyncMock()
    transport.transport_name = "tg"

    async def fail_delivery(envelope: object) -> None:
        envelope.delivered = False
        envelope.delivery_error = "send failed"

    transport.deliver = AsyncMock(side_effect=fail_delivery)
    bus.register_transport(transport)
    result = _result()
    prompt = build_interagent_injection_prompt(
        result, agent_name="main", transport_label="Telegram chat"
    )
    outcome = await bus.submit(from_interagent_result(result, 999, injection_prompt=prompt))
    assert not outcome.ok
    assert outcome.injected
    assert not outcome.delivered
    assert outcome.error == "send failed"


@pytest.mark.parametrize(
    ("bot_path", "transport_name"),
    [
        ("ductor_bot.messenger.telegram.app.TelegramBot", "tg"),
        ("ductor_bot.messenger.matrix.bot.MatrixBot", "mx"),
        ("ductor_bot.messenger.slack.bot.SlackBot", "sl"),
    ],
)
@pytest.mark.parametrize("broadcast", [False, True])
@pytest.mark.parametrize("failure", ["exception", "empty"])
async def test_failed_injection_never_sends_subagent_text(
    bot_path: str, transport_name: str, broadcast: bool, failure: str
) -> None:
    from importlib import import_module

    module_name, class_name = bot_path.rsplit(".", 1)
    bot_cls = getattr(import_module(module_name), class_name)
    message_bus = MessageBus()
    injector = AsyncMock()
    if failure == "exception":
        injector.inject_prompt.side_effect = RuntimeError("injection failed")
    else:
        injector.inject_prompt.return_value = ""
    message_bus.set_injector(injector)
    transports = []
    for name in ("tg", "mx", "sl"):
        transport = AsyncMock()
        transport.transport_name = name
        message_bus.register_transport(transport)
        transports.append(transport)
    bot = MagicMock()
    bot._agent_name = "main"
    bot._config.allowed_user_ids = [999]
    bot._bus = message_bus
    result = _result()
    result.transport = transport_name
    if transport_name == "mx":
        result.topic_id = None
    if broadcast:
        result.chat_id = 0
        result.topic_id = None
        result.result_origin = "cron-broadcast"
        result.broadcast_ref = "job-1"

    outcome = await bot_cls.on_async_interagent_result(bot, result)
    assert not outcome.ok
    assert not outcome.injected
    assert not outcome.delivered
    assert outcome.error == (
        "injection failed" if failure == "exception" else "empty injection response"
    )
    for transport in transports:
        transport.deliver.assert_not_awaited()
        transport.deliver_broadcast.assert_not_awaited()
