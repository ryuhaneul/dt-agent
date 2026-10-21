"""Optional injection typing preserves transport and private-question contracts."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError

from ductor_bot.bus.bus import MessageBus, TransportAdapter
from ductor_bot.bus.envelope import Envelope, Origin
from ductor_bot.cli.types import AgentResponse
from ductor_bot.messenger.matrix.transport import MatrixTransport
from ductor_bot.messenger.telegram.transport import TelegramTransport
from tests.multiagent.test_delegation_context import agents, seed_topic  # noqa: F401
from tests.orchestrator.test_task_question_answer import ask


def envelope(transport="tg"):
    return Envelope(
        origin=Origin.TASK_RESULT, chat_id=1, topic_id=5, transport=transport,
        prompt="Process result", needs_injection=True,
    )


def wire(adapter):
    bus = MessageBus()
    bus.register_transport(adapter)
    injector = MagicMock()
    injector.inject_prompt = AsyncMock(return_value="Reply")
    bus.set_injector(injector)
    return bus, injector


class LegacyTransport:
    """An adapter satisfying only the original delivery protocol."""

    transport_name = "sl"

    def __init__(self):
        self.deliver = AsyncMock()
        self.deliver_broadcast = AsyncMock()


async def test_telegram_typing_uses_envelope_chat_thread_and_stops(monkeypatch):
    monkeypatch.setattr("ductor_bot.messenger.telegram.typing._TYPING_INTERVAL", 0.001)
    api = MagicMock()
    sent = asyncio.Event()
    api.send_chat_action = AsyncMock(side_effect=lambda **_kwargs: sent.set())
    adapter = TelegramTransport(MagicMock(bot_instance=api))
    adapter.deliver = AsyncMock()
    bus, injector = wire(adapter)

    async def inject(*_args, **_kwargs):
        await asyncio.wait_for(sent.wait(), 1)
        return "Reply"

    injector.inject_prompt.side_effect = inject
    result = await bus.submit(envelope())
    assert result.ok
    api.send_chat_action.assert_awaited_with(
        chat_id=1, action=ChatAction.TYPING, message_thread_id=5,
    )
    count = api.send_chat_action.await_count
    await asyncio.sleep(0.01)
    assert api.send_chat_action.await_count == count
    injector.inject_prompt.assert_awaited_once()
    adapter.deliver.assert_awaited_once()


@pytest.mark.parametrize("stage", ["factory", "enter", "exit", "suppress"])
async def test_typing_context_failures_or_suppression_do_not_change_success(stage):
    adapter = LegacyTransport()
    adapter.transport_name = "tg"
    context = MagicMock()
    context.__aenter__ = AsyncMock()
    context.__aexit__ = AsyncMock(return_value=stage == "suppress")
    adapter.injection_typing = MagicMock(return_value=context)
    if stage == "factory":
        adapter.injection_typing.side_effect = RuntimeError("typing factory")
    elif stage == "enter":
        context.__aenter__.side_effect = RuntimeError("typing enter")
    elif stage == "exit":
        context.__aexit__.side_effect = RuntimeError("typing exit")
    bus, injector = wire(adapter)
    env = envelope()
    result = await bus.submit(env)
    assert result.ok
    assert env.result_text == "Reply"
    injector.inject_prompt.assert_awaited_once()
    adapter.deliver.assert_awaited_once_with(env)
    adapter.injection_typing.assert_called_once_with(env)
    assert context.__aexit__.await_count == int(stage not in {"factory", "enter"})


async def test_repeated_telegram_send_failure_preserves_injection_and_delivery(monkeypatch):
    monkeypatch.setattr("ductor_bot.messenger.telegram.typing._TYPING_INTERVAL", 0.001)
    api = MagicMock()
    failed = asyncio.Event()
    calls = 0

    async def action(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            failed.set()
            raise TelegramAPIError(method=MagicMock(), message="typing unavailable")

    api.send_chat_action = AsyncMock(side_effect=action)
    adapter = TelegramTransport(MagicMock(bot_instance=api))
    adapter.deliver = AsyncMock()
    bus, injector = wire(adapter)

    async def inject(*_args, **_kwargs):
        await asyncio.wait_for(failed.wait(), 1)
        return "Reply"

    injector.inject_prompt.side_effect = inject
    assert (await bus.submit(envelope())).ok
    assert calls == 2
    adapter.deliver.assert_awaited_once()
    injector.inject_prompt.assert_awaited_once()


@pytest.mark.parametrize("transport", ["sl", "headless", "mock"])
async def test_legacy_protocol_dynamic_mock_and_headless_still_inject(transport):
    adapter = LegacyTransport()
    if transport == "mock":
        adapter = AsyncMock()
        adapter.transport_name = "tg"
    if transport != "mock":
        assert isinstance(adapter, TransportAdapter)
    bus, injector = wire(adapter)
    if transport == "headless":
        bus = MessageBus()
        bus.set_injector(injector)
    env = envelope(adapter.transport_name)
    await bus.submit(env)
    assert env.injected
    assert env.result_text == "Reply"
    injector.inject_prompt.assert_awaited_once()
    if transport != "headless":
        adapter.deliver.assert_awaited_once()
    if transport == "mock":
        assert "injection_typing" not in adapter._mock_children


@pytest.mark.parametrize("known", [True, False])
async def test_matrix_typing_uses_resolved_room_or_noop(known):
    bot = MagicMock()
    bot.id_map.int_to_room.return_value = "!room:example.test" if known else None
    bot.client.room_typing = AsyncMock()
    adapter = MatrixTransport(bot)
    adapter.deliver = AsyncMock()
    bus, injector = wire(adapter)

    async def inject(*_args, **_kwargs):
        if known:
            bot.client.room_typing.assert_awaited_once_with(
                "!room:example.test", typing_state=True, timeout=30000,
            )
        return "Reply"

    injector.inject_prompt.side_effect = inject
    assert (await bus.submit(envelope("mx"))).ok
    if known:
        assert bot.client.room_typing.await_count == 2
        bot.client.room_typing.assert_awaited_with("!room:example.test", typing_state=False)
    else:
        bot.client.room_typing.assert_not_awaited()


@pytest.mark.parametrize("failure", [RuntimeError("injection"), asyncio.CancelledError()])
async def test_injection_errors_and_cancellation_keep_semantics_and_stop_typing(failure):
    adapter = LegacyTransport()
    adapter.transport_name = "tg"
    exited = asyncio.Event()

    @asynccontextmanager
    async def typing(_env):
        try:
            yield
        finally:
            exited.set()

    adapter.injection_typing = typing
    bus, injector = wire(adapter)
    injector.inject_prompt.side_effect = failure
    env = envelope()
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await bus.submit(env)
        adapter.deliver.assert_not_awaited()
    else:
        await bus.submit(env)
        assert env.is_error
        assert not env.injected
        adapter.deliver.assert_awaited_once()
    assert exited.is_set()
    injector.inject_prompt.assert_awaited_once()


async def test_no_injection_has_no_typing():
    adapter = LegacyTransport()
    adapter.transport_name = "tg"
    adapter.injection_typing = MagicMock(side_effect=AssertionError("unexpected typing"))
    bus, injector = wire(adapter)
    env = envelope()
    env.needs_injection = False
    await bus.submit(env)
    adapter.injection_typing.assert_not_called()
    injector.inject_prompt.assert_not_awaited()
    adapter.deliver.assert_awaited_once()


async def test_private_task_question_bypasses_bus_typing(agents):  # noqa: F811
    _, main, _ = agents
    messages = MessageBus()
    adapter = LegacyTransport()
    adapter.transport_name = "tg"
    adapter.injection_typing = MagicMock(side_effect=AssertionError("private typing"))
    messages.register_transport(adapter)
    main.wire_observers_to_bus(messages)
    await seed_topic(main)
    main._cli_service.execute.return_value = AgentResponse(
        result="private answer", session_id="after-answer",
    )
    assert await ask(main) == ("private answer", "")
    adapter.injection_typing.assert_not_called()
    adapter.deliver.assert_not_awaited()
    adapter.deliver_broadcast.assert_not_awaited()
