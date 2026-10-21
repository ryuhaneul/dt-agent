"""Wait context reaches CLI without changing routing, display or session previews."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from ductor_bot.cli.types import AgentResponse
from ductor_bot.messenger.queue_context import QueuedMessageLock
from ductor_bot.orchestrator.flows import (
    named_session_flow,
    named_session_streaming,
    normal,
    normal_streaming,
)
from ductor_bot.session.key import SessionKey


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("named", [False, True])
async def test_queued_flow_preserves_user_preview_and_routing(orch, streaming, named):
    key = SessionKey.telegram(1, 42)
    orch._config.user_timezone = "Asia/Seoul"
    execute = AsyncMock(return_value=AgentResponse(result="reply", session_id="synthetic-sid"))
    object.__setattr__(orch._cli_service, "execute", execute)
    object.__setattr__(orch._cli_service, "execute_streaming", execute)
    if named:
        ns = orch._named_sessions.create(1, "claude", "opus", "setup")
        orch._named_sessions.update_after_response(1, ns.name, "synthetic-sid")
    lock = asyncio.Lock()
    await lock.acquire()

    async def receive():
        async with QueuedMessageLock(lock, datetime(2026, 10, 5, 0, 15, tzinfo=UTC)):
            if named:
                flow = named_session_streaming if streaming else named_session_flow
                result = await flow(orch, key, ns.name, "media or text prompt")
            else:
                flow = normal_streaming if streaming else normal
                result = await flow(orch, key, "media or text prompt")
            assert "Queued user message" not in result.text

    task = asyncio.create_task(receive())
    await asyncio.sleep(0)
    lock.release()
    await asyncio.wait_for(task, 3)
    request = execute.await_args.args[0]
    assert request.prompt.startswith("media or text prompt")
    assert "Queued user message" not in request.prompt
    assert request.cli_prompt.endswith("\n\n" + request.prompt)
    assert "09:15:00 KST" in request.queue_context
    if named:
        ns = orch._named_sessions.get(1, ns.name)
        assert ns.last_prompt == "media or text prompt"
    else:
        from pathlib import Path

        for path in Path(orch.paths.ductor_home).glob("*.json"):
            assert "Queued user message" not in path.read_text()


async def test_direct_flow_has_no_queue_context(orch):
    execute = AsyncMock(return_value=AgentResponse(result="reply", session_id="synthetic-sid"))
    object.__setattr__(orch._cli_service, "execute", execute)
    await normal(orch, SessionKey.telegram(1), "original")
    request = execute.await_args.args[0]
    assert request.queue_context is None
    assert request.cli_prompt == request.prompt
    assert request.prompt.startswith("original")


@pytest.mark.parametrize("media", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_telegram_middleware_to_message_resolution_and_routing(
    orch, monkeypatch, caplog, media, streaming
):
    from aiogram.types import Chat, Message, User

    from ductor_bot.messenger.telegram import app
    from ductor_bot.messenger.telegram.middleware import SequentialMiddleware
    from tests.messenger.telegram.test_app import _make_tg_bot

    cfg = orch._config
    cfg.user_timezone = "Asia/Seoul"
    cfg.streaming.enabled = streaming
    tg, bot = _make_tg_bot(cfg)
    tg._orchestrator = orch
    received = []

    async def reply(*args, **kwargs):
        key, text = args[1:3]
        result = (
            await orch.handle_message_streaming(key, text)
            if streaming
            else await orch.handle_message(key, text)
        )
        received.append(result.text)
        return result.text

    tg._handle_non_streaming = AsyncMock(side_effect=reply)
    tg._handle_streaming = AsyncMock(side_effect=reply)
    execute = AsyncMock(return_value=AgentResponse(result="reply", session_id="synthetic"))
    object.__setattr__(orch._cli_service, "execute", execute)
    object.__setattr__(orch._cli_service, "execute_streaming", execute)
    monkeypatch.setattr(app, "has_media", lambda message: media)
    monkeypatch.setattr(app, "resolve_media_text", AsyncMock(return_value="synthetic media prompt"))
    msg = Message(
        message_id=1,
        date=datetime(2026, 10, 5, 0, 15, tzinfo=UTC),
        chat=Chat(id=1, type="private"),
        from_user=User(id=1, is_bot=False, first_name="S"),
        text=None if media else "original text",
    )
    mw = SequentialMiddleware()
    lock = mw.get_lock(SessionKey.telegram(1).lock_key)
    await lock.acquire()

    async def handler(event, data):
        await tg._on_message(event)

    task = asyncio.create_task(mw(handler, msg, {}))
    await asyncio.sleep(0)
    lock.release()
    await asyncio.wait_for(task, 3)
    request = execute.await_args.args[0]
    assert request.prompt.startswith("synthetic media prompt" if media else "original text")
    assert "09:15:00 KST" in request.queue_context
    assert received == ["reply"]
    assert "Queued user message" not in caplog.text
    assert msg.text == (None if media else "original text")
