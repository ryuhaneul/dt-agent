"""Audit regressions through real delegation dispatch, using synthetic credentials."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from ductor_bot.bus.bus import MessageBus
from ductor_bot.cli.types import AgentResponse
from ductor_bot.multiagent.bus import AsyncSendOptions
from ductor_bot.multiagent.delegation import DelegationContext, safe_preview, source_header
from ductor_bot.session.named import NamedSession, interagent_session_name
from tests.multiagent.test_delegation_context import agents, seed_topic  # noqa: F401


@pytest.mark.parametrize("source_id", ["named-source", "unknown-background"])
async def test_async_named_or_unknown_source_never_resumes_foreground(
    agents,  # noqa: F811 - imported pytest fixture
    source_id,
):
    bus, main, worker = agents
    key, _ = await seed_topic(main)
    if source_id == "named-source":
        main._named_sessions.add(
            NamedSession(
                name="research",
                chat_id=key.chat_id,
                provider="codex",
                model="gpt-5.5",
                session_id=source_id,
                prompt_preview="synthetic",
                status="idle",
                created_at=0,
            )
        )
    messages = MessageBus()
    main.wire_observers_to_bus(messages)
    adapter = AsyncMock()
    adapter.transport_name = "tg"
    messages.register_transport(adapter)
    bus.set_async_result_handler("main", AsyncMock())
    snapshots = []

    async def execute(request):
        snapshots.append(bus._delegations[request.delegation_id])
        answer = await bus.send(
            "worker",
            "main",
            "Clarify research",
            ia_turn=True,
            delegation_id=request.delegation_id,
        )
        assert answer.success
        assert answer.text == "main answer"
        return AgentResponse(result="finished")

    worker._cli_service.execute.side_effect = execute
    # Holding the foreground lock must not block the dedicated fallback.
    async with messages.lock_pool.get(key.lock_key):
        task_id = bus.send_async(
            "main",
            "worker",
            "Research task",
            opts=AsyncSendOptions(
                chat_id=key.chat_id,
                topic_id=key.topic_id,
                source_session_id=source_id,
                silent=True,
            ),
        )
        await asyncio.wait_for(bus._async_tasks[task_id].asyncio_task, 1)
    request = main._cli_service.execute.call_args.args[0]
    assert request.resume_session is None
    assert request.chat_id == 12345
    assert "Delegation mode: async" in request.prompt
    assert f"Delegation task/request ID: {task_id}" in request.prompt
    assert "Original request preview: Research task" in request.prompt
    assert "Original topic: 42; name: Topic 42" in request.prompt
    if source_id == "named-source":
        assert snapshots[0].session_key == "tg:-777:42 @research"
        assert "Original session key: tg:-777:42 @research" in request.prompt
        assert "Provider: codex; provider session ID: named-source" in request.prompt
        assert str(main.paths.named_sessions_path) in request.prompt
    else:
        assert not snapshots[0].session_id
        assert "provider session ID: unconfirmed" in request.prompt
    assert (await main._sessions.get_active(key)).session_id == "source-42"
    assert main._named_sessions.get(12345, interagent_session_name("worker", -777, 42))
    adapter.deliver.assert_not_awaited()
    adapter.deliver_broadcast.assert_not_awaited()
    assert not bus._delegations


@pytest.mark.parametrize(
    "credential",
    [
        "xoxb-123456789012-123456789012-syntheticfakevalue",
        "https://synthetic-user:synthetic-password@example.invalid/status",
        "https://synthetic-user%3Asynthetic-password@example.invalid/status",
        "Bearer synthetic-bearer-value",
        "Basic c3ludGhldGljOnN5bnRoZXRpYw==",
        "ghp_syntheticcredential",
        "github_pat_syntheticcredential",
        "AKIAABCDEFGHIJKLMNOP",
        "eyJsynthetic.payload.signature",
    ],
)
def test_credential_preview_is_omitted_even_after_preview_boundary(credential):
    message = "ordinary work " * 25 + credential
    assert safe_preview(message) == "[request preview omitted: credential-like text]"
    context = DelegationContext("synthetic", "sync", "main", "worker", request_preview=message)
    assert credential not in source_header(context)
    assert "preview omitted" in source_header(context)


@pytest.mark.parametrize(
    "credential",
    [
        "xoxb-123456789012-123456789012-syntheticfakevalue",
        "https://synthetic-user:synthetic-password@example.invalid/status",
    ],
)
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_dispatch_does_not_duplicate_request_credential_in_question_header(
    agents,  # noqa: F811 - imported pytest fixture
    credential,
    mode,
):
    bus, main, worker = agents
    await seed_topic(main)
    main.wire_observers_to_bus(MessageBus())
    bus.set_async_result_handler("main", AsyncMock())

    async def execute(request):
        # The original request is delivered once, but not copied to the question header.
        assert credential in request.prompt
        snapshot = bus._delegations[request.delegation_id]
        assert credential not in snapshot.request_preview
        answer = await bus.send(
            "worker",
            "main",
            "Question without credentials",
            delegation_id=request.delegation_id,
            ia_turn=True,
        )
        assert answer.success == (mode == "async")
        return AgentResponse(result="finished")

    worker._cli_service.execute.side_effect = execute
    message = f"Inspect service {credential}"
    if mode == "sync":
        result = await asyncio.wait_for(
            bus.send("main", "worker", message, chat_id=-777, topic_id=42), 1
        )
        assert result.success
    else:
        # An unconfirmed background source takes the dedicated-header route.
        task_id = bus.send_async(
            "main",
            "worker",
            message,
            opts=AsyncSendOptions(
                chat_id=-777,
                topic_id=42,
                source_session_id="unknown-background",
                silent=True,
            ),
        )
        await asyncio.wait_for(bus._async_tasks[task_id].asyncio_task, 1)
    if mode == "sync":
        main._cli_service.execute.assert_not_awaited()
        assert main._named_sessions.list_active(12345) == []
        assert not bus._delegations
        return
    prompt = main._cli_service.execute.call_args.args[0].prompt
    assert credential not in prompt
    assert "Question without credentials" in prompt
    assert "Original request preview: [request preview omitted:" in prompt
    assert not bus._delegations


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_api_dispatch_captures_real_storage_key_and_private_question_route(
    agents,  # noqa: F811 - imported pytest fixture
    mode,
):
    bus, main, worker = agents
    key, transcript = await seed_topic(main)
    messages = MessageBus()
    main.wire_observers_to_bus(messages)
    bus.set_async_result_handler("main", AsyncMock())
    snapshots = []

    async def execute(request):
        assert request.origin_transport == "api"
        snapshot = bus._delegations[request.delegation_id]
        snapshots.append(snapshot)
        assert snapshot.transport == "api"
        assert snapshot.session_key == key.storage_key
        assert snapshot.session_id == "source-42"
        assert snapshot.provider == "claude"
        assert snapshot.transcript_path == transcript
        answer = await bus.send(
            "worker",
            "main",
            "API question",
            ia_turn=True,
            delegation_id=request.delegation_id,
        )
        assert answer.success == (mode == "async")
        return AgentResponse(result="finished")

    worker._cli_service.execute.side_effect = execute
    if mode == "sync":
        result = await asyncio.wait_for(
            bus.send(
                "main",
                "worker",
                "API work",
                chat_id=-777,
                topic_id=42,
                transport="api",
                source_session_id="source-42",
            ),
            1,
        )
        assert result.success
        main._cli_service.execute.assert_not_awaited()
        assert main._named_sessions.list_active(12345) == []
    else:
        task_id = bus.send_async(
            "main",
            "worker",
            "API work",
            opts=AsyncSendOptions(
                chat_id=-777,
                topic_id=42,
                transport="api",
                source_session_id="source-42",
                silent=True,
            ),
        )
        await asyncio.wait_for(bus._async_tasks[task_id].asyncio_task, 1)
        request = main._cli_service.execute.call_args.args[0]
        assert request.transport == "api"
        assert request.resume_session == "source-42"
        assert request.chat_id == -777
        assert request.topic_id == 42
        assert main._named_sessions.list_active(12345) == []
    assert len(snapshots) == 1
    assert not messages.lock_pool.get(key.lock_key).locked()
    assert not bus._delegations


async def test_encrypted_api_dispatch_to_dedicated_source_header(
    agents, monkeypatch,  # noqa: F811 - imported pytest fixture
):
    """Use the API's authenticated session key, not a manufactured API storage key."""
    pytest.importorskip("nacl")
    from types import SimpleNamespace

    from aiohttp.test_utils import TestClient, TestServer

    from ductor_bot.api.crypto import E2ESession
    from ductor_bot.bus.envelope import DeliveryOutcome
    import ductor_bot.orchestrator.injection as injection
    from ductor_bot.api.server import ApiServer
    from ductor_bot.config import ApiConfig
    from ductor_bot.session.key import SessionKey
    from tests.api.test_server_e2e import _build_app, _recv_encrypted, _send_encrypted

    bus, main, worker = agents
    key = SessionKey(chat_id=12345, topic_id=42)
    session, _ = await main._sessions.resolve_session(key, provider="claude", model="sonnet")
    session.session_id = "api-source"
    session.topic_name = "API channel"
    await main._sessions.update_session(session)
    transcript = main.paths.ductor_home / ".cli-auth/_shared-projects/project/api-source.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"message":"synthetic API history"}\n')
    messages = MessageBus()
    main.wire_observers_to_bus(messages)
    # The API user turn holds the source lock; a short test timeout covers async fallback.
    monkeypatch.setattr(injection, "_ASYNC_QUESTION_LOCK_WAIT", 0.01)
    delivered = AsyncMock(return_value=DeliveryOutcome(ok=True, injected=True, delivered=True))
    bus.set_async_result_handler("main", delivered)

    async def sub_execute(request):
        snapshot = bus._delegations[request.delegation_id]
        assert snapshot.transport == "api"
        assert snapshot.session_key == key.storage_key
        assert snapshot.session_id == "api-source"
        answer = await bus.send(
            "worker",
            "main",
            "Question from API work",
            delegation_id=request.delegation_id,
            ia_turn=True,
        )
        assert answer.success
        return AgentResponse(result="completed API work")

    async def handler(actual_key, text, **_callbacks):
        assert actual_key == key
        task_id = bus.send_async(
            "main", "worker", text,
            opts=AsyncSendOptions(
                chat_id=actual_key.chat_id, topic_id=actual_key.topic_id,
                transport="api", source_session_id=session.session_id,
                source_session_kind="foreground", silent=True,
            ),
        )
        await asyncio.wait_for(bus._async_tasks[task_id].asyncio_task, 1)
        result = delivered.call_args.args[0]
        assert result.success
        return SimpleNamespace(text=result.result_text, stream_fallback=False)

    worker._cli_service.execute.side_effect = sub_execute
    server = ApiServer(
        ApiConfig(enabled=True, token="synthetic-api-test", allow_public=True),
        default_chat_id=12345,
        lock_pool=messages.lock_pool,
    )
    server.set_message_handler(handler)
    async with TestClient(TestServer(_build_app(server))) as client:
        ws = await client.ws_connect("/ws")
        encryption = E2ESession()
        await ws.send_json(
            {
                "type": "auth",
                "token": "synthetic-api-test",
                "e2e_pk": encryption.local_pk_b64,
                "chat_id": 12345,
                "channel_id": 42,
            }
        )
        auth = await asyncio.wait_for(ws.receive_json(), 1)
        assert auth["type"] == "auth_ok"
        encryption.set_remote_key(auth["e2e_pk"])
        await _send_encrypted(ws, encryption, {"type": "message", "text": "API work"})
        result = await asyncio.wait_for(_recv_encrypted(ws, encryption), 1)
        assert result["text"] == "completed API work"
        await ws.close()
    request = main._cli_service.execute.call_args.args[0]
    assert request.resume_session is None
    assert request.origin_transport == "api"
    assert "Original chat: api:12345" in request.prompt
    assert "Original topic: 42; name: API channel" in request.prompt
    assert "Original session key: tg:12345:42" in request.prompt
    assert "Provider: claude; provider session ID: api-source" in request.prompt
    assert f"Conversation record: {transcript}" in request.prompt
    assert (await main._sessions.get_active(key)).session_id == "api-source"
    assert not messages.lock_pool.get(key.lock_key).locked()
    assert not bus._delegations
