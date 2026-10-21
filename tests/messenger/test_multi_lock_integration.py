"""Real transport construction shares user, result and private-question locks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from ductor_bot.bus.adapters import from_interagent_result
from ductor_bot.bus.envelope import Envelope
from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig
from ductor_bot.messenger.multi import MultiBotAdapter
from ductor_bot.multiagent.bus import AsyncInterAgentResult
from ductor_bot.multiagent.delegation import DelegationContext
from ductor_bot.session.key import SessionKey
from tests.messenger.telegram.test_middleware import _make_message
from tests.multiagent.test_delegation_context import agents  # noqa: F401


@pytest.fixture
def real_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MultiBotAdapter:
    nio = pytest.importorskip("nio")
    from ductor_bot.messenger.slack import bot as slack_module
    from ductor_bot.messenger.telegram import app as telegram_module

    if not slack_module._SLACK_AVAILABLE:
        pytest.skip("slack-bolt is required for real multi-transport constructor tests")
    monkeypatch.setattr(nio, "AsyncClient", Mock())
    monkeypatch.setattr(slack_module, "_SlackAsyncApp", Mock())
    monkeypatch.setattr(telegram_module, "Bot", Mock())
    config = AgentConfig(
        ductor_home=str(tmp_path / "multi-bot"),
        transports=["telegram", "matrix", "slack"],
        allowed_user_ids=[12345],
        allowed_group_ids=[-777],
    )
    return MultiBotAdapter(config)


def _assert_only_result_was_posted(
    adapter: MultiBotAdapter, transport: str, envelope: Envelope
) -> None:
    for registered in adapter._bus._transports:
        if registered.transport_name == transport:
            registered.deliver.assert_awaited_once_with(envelope)
        else:
            registered.deliver.assert_not_awaited()
        registered.deliver_broadcast.assert_not_awaited()


@pytest.mark.parametrize("transport", ["tg", "mx", "sl"])
async def test_real_multi_transport_user_result_and_question_are_serialized(
    real_adapter: MultiBotAdapter,
    agents,  # noqa: F811 - imported pytest fixture
    transport: str,
) -> None:
    bus, main, _ = agents
    adapter = real_adapter
    assert len(adapter._lock_pool) == 0
    assert adapter._bus.lock_pool is adapter._lock_pool
    for bot in adapter._all:
        assert bot._lock_pool is adapter._lock_pool
        assert bot._bus is adapter._bus
    main.wire_observers_to_bus(adapter._bus)
    assert main._lock_pool is adapter._lock_pool
    bot = adapter._all[{"tg": 0, "mx": 1, "sl": 2}[transport]]
    key = SessionKey(transport, -777, None if transport == "mx" else 42)
    session, _ = await main._sessions.resolve_session(key, provider="claude", model="sonnet")
    session.session_id = "same-provider-session"
    await main._sessions.update_session(session)
    for registered in adapter._bus._transports:
        registered.deliver = AsyncMock()
        registered.deliver_broadcast = AsyncMock()

    user_started = asyncio.Event()
    release_user = asyncio.Event()
    result_started = asyncio.Event()
    release_result = asyncio.Event()
    question_started = asyncio.Event()
    active = 0
    max_active = 0
    seen = []

    async def execute(request):
        nonlocal active, max_active
        assert adapter._lock_pool.get(key.lock_key).locked()
        assert request.resume_session == "same-provider-session"
        active += 1
        max_active = max(max_active, active)
        seen.append(request)
        try:
            if request.process_label == "main":
                user_started.set()
                await release_user.wait()
                text = "user answer"
            elif request.prompt == "async result prompt":
                result_started.set()
                await release_result.wait()
                text = "result answer"
            else:
                question_started.set()
                text = "private answer"
            return AgentResponse(result=text, session_id="same-provider-session")
        finally:
            active -= 1

    main._cli_service.execute.side_effect = execute

    async def user_handler(*_args, **_kwargs):
        return await main.handle_message(key, "synthetic user request")

    if transport == "tg":
        event = _make_message(
            chat_id=key.chat_id,
            topic_thread_id=key.topic_id,
            text="synthetic user request",
            chat_type="supergroup",
        )
        user = asyncio.create_task(bot._sequential(user_handler, event, {}))
    else:
        bot._dispatch_message = user_handler
        user = asyncio.create_task(
            bot._dispatch_with_lock(key, "synthetic user request", "synthetic-room", None)
        )
    tasks = [user]
    try:
        await asyncio.wait_for(user_started.wait(), 1)
        result = AsyncInterAgentResult(
            task_id="completed-task",
            sender="main",
            recipient="worker",
            message_preview="synthetic task",
            result_text="finished",
            chat_id=key.chat_id,
            topic_id=key.topic_id,
            transport=transport,
        )
        envelope = from_interagent_result(result, 12345, injection_prompt="async result prompt")
        delivery = asyncio.create_task(adapter._bus.submit(envelope))
        tasks.append(delivery)
        # Let the existing result queue for the user lock before the question.
        await asyncio.sleep(0)
        entry = DelegationContext(
            "active-task",
            "async",
            "main",
            "worker",
            chat_id=key.chat_id,
            topic_id=key.topic_id,
            transport=transport,
        )
        bus._delegations[entry.request_id] = entry
        question = asyncio.create_task(
            bus.send(
                "worker",
                "main",
                "synthetic private question",
                delegation_id=entry.request_id,
                ia_turn=True,
            )
        )
        tasks.append(question)
        await asyncio.sleep(0.01)
        assert not delivery.done()
        assert not question.done()
        assert not result_started.is_set()
        assert not question_started.is_set()
        release_user.set()
        await asyncio.wait_for(user, 1)
        await asyncio.wait_for(result_started.wait(), 1)
        assert not question.done()
        assert not question_started.is_set()
        release_result.set()
        outcome = await asyncio.wait_for(delivery, 1)
        answer = await asyncio.wait_for(question, 1)
        assert outcome.ok
        assert outcome.injected
        assert outcome.delivered
        assert answer.success
        assert answer.text == "private answer"
        assert max_active == 1
        assert len(seen) == 3
        assert [request.process_label == "main" for request in seen] == [True, False, False]
        assert seen[1].prompt == "async result prompt"
        assert seen[2].process_label == "interagent-question:worker"
        _assert_only_result_was_posted(adapter, transport, envelope)
        assert not adapter._lock_pool.get(key.lock_key).locked()
    finally:
        release_user.set()
        release_result.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
