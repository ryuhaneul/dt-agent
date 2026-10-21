"""Async questions share source sessions and locks without posting chat replies."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from ductor_bot.bus.adapters import from_interagent_result
from ductor_bot.bus.bus import MessageBus
from ductor_bot.cli.types import AgentResponse
from ductor_bot.messenger.telegram.middleware import SequentialMiddleware
from ductor_bot.multiagent.bus import AsyncInterAgentResult, AsyncSendOptions
from ductor_bot.multiagent.delegation import DelegationContext
from ductor_bot.orchestrator import injection
from ductor_bot.session.key import SessionKey
from ductor_bot.session.named import interagent_session_name
from tests.messenger.telegram.test_middleware import _make_message
from tests.multiagent.test_delegation_context import agents, seed_topic  # noqa: F401


@pytest.fixture
def wired(agents):  # noqa: F811 - imported pytest fixture
    bus, main, worker = agents
    messages = MessageBus()
    main.wire_observers_to_bus(messages)
    transport = AsyncMock()
    transport.transport_name = "tg"
    messages.register_transport(transport)
    return bus, main, worker, messages, transport


def context(topic_id=42, *, mode="async", transport="tg"):
    return DelegationContext(
        "test-task",
        mode,
        "main",
        "worker",
        chat_id=-777,
        topic_id=topic_id,
        transport=transport,
        topic_name=f"Topic {topic_id}",
        session_key=f"{'tg' if transport == 'api' else transport}:-777:{topic_id}",
        provider="claude",
        session_id="source-42",
        request_preview="original task",
    )


async def question(bus, topic_id=42):
    entry = context(topic_id)
    bus._delegations[entry.request_id] = entry
    return await bus.send(
        "worker",
        "main",
        "Clarify the task",
        delegation_id=entry.request_id,
        ia_turn=True,
        chat_id=999,
        topic_id=99,
    )


async def test_real_async_dispatch_resumes_current_provider_session_without_chat_post(wired):
    bus, main, worker, messages, transport = wired
    key, _ = await seed_topic(main)
    bus.set_async_result_handler("main", AsyncMock())
    main._cli_service.execute.return_value = AgentResponse(
        result="private clarification",
        session_id="current-codex",
    )
    answers = []

    async def sub_execute(request):
        entry = bus._delegations[request.delegation_id]
        assert entry.mode == "async"
        assert entry.session_id == "source-42"
        # Main changed its selected provider after dispatch. Resume what is
        # active at question time, rather than the historical dispatch snapshot.
        active, _ = await main._sessions.resolve_session(
            key,
            provider="codex",
            model="gpt-5.5",
            reasoning_effort="high",
        )
        active.session_id = "current-codex"
        await main._sessions.update_session(active)
        response = await bus.send(
            "worker",
            "main",
            "Which build target?",
            ia_turn=True,
            delegation_id=request.delegation_id,
            chat_id=999,
            topic_id=99,
        )
        assert response.success
        answers.append(response.text)
        return AgentResponse(result="sub finished")

    worker._cli_service.execute.side_effect = sub_execute
    task_id = bus.send_async(
        "main",
        "worker",
        "Build the app",
        opts=AsyncSendOptions(
            source_session_kind="foreground",
            chat_id=key.chat_id,
            topic_id=key.topic_id,
            silent=True,
        ),
    )
    await asyncio.wait_for(bus._async_tasks[task_id].asyncio_task, 1)
    assert answers == ["private clarification"]
    request = main._cli_service.execute.call_args.args[0]
    assert request.chat_id == -777
    assert request.topic_id == 42
    assert request.transport == "tg"
    assert request.resume_session == "current-codex"
    assert request.provider_override == "codex"
    assert request.model_override == "gpt-5.5"
    assert request.effort_override == "high"
    assert "Sub-agent 'worker'" in request.prompt
    assert task_id in request.prompt
    assert "returned only to 'worker'" in request.prompt
    assert "not posted to the user chat" in request.prompt
    assert main._named_sessions.list_active(12345) == []
    active = await main._sessions.get_active(key)
    assert active.provider == "codex"
    assert active.session_id == "current-codex"
    assert not messages.lock_pool.get(key.lock_key).locked()
    assert not bus._delegations
    transport.deliver.assert_not_awaited()
    transport.deliver_broadcast.assert_not_awaited()
    for stack in bus._agents.values():
        stack.bot.notification_service.notify.assert_not_awaited()
        stack.bot.notification_service.notify_all.assert_not_awaited()


async def test_waits_for_real_telegram_user_turn_then_resumes_updated_session(wired):
    bus, main, _, messages, transport = wired
    key, _ = await seed_topic(main)
    middleware = SequentialMiddleware(lock_pool=messages.lock_pool)
    assert main._lock_pool.get(key.lock_key) is middleware.get_lock(key.lock_key)
    user_started = asyncio.Event()
    release_user = asyncio.Event()
    question_started = asyncio.Event()
    seen = []

    async def execute(request):
        seen.append(request)
        if request.process_label == "main":
            user_started.set()
            await release_user.wait()
            return AgentResponse(result="user reply", session_id="after-user")
        assert middleware.get_lock(key.lock_key).locked()
        question_started.set()
        return AgentResponse(result="private answer", session_id="after-question")

    main._cli_service.execute.side_effect = execute
    msg = _make_message(
        chat_id=-777, topic_thread_id=42, text="user request", chat_type="supergroup"
    )

    async def user_handler(event, _data):
        return await main.handle_message(key, event.text)

    user = asyncio.create_task(middleware(user_handler, msg, {}))
    await asyncio.wait_for(user_started.wait(), 1)
    pending = asyncio.create_task(question(bus))
    await asyncio.sleep(0.01)
    assert not question_started.is_set()
    release_user.set()
    await asyncio.wait_for(user, 1)
    reply = await asyncio.wait_for(pending, 1)
    assert reply.success
    assert reply.text == "private answer"
    assert question_started.is_set()
    assert len(seen) == 2
    assert seen[1].resume_session == "after-user"
    assert (await main._sessions.get_active(key)).session_id == "after-question"
    assert not messages.lock_pool.get(key.lock_key).locked()
    transport.deliver.assert_not_awaited()


async def test_waits_for_real_async_result_injection_using_same_lock(wired):
    bus, main, _, messages, transport = wired
    key, _ = await seed_topic(main)
    injection_started = asyncio.Event()
    release_result = asyncio.Event()
    question_started = asyncio.Event()
    seen = []

    async def execute(request):
        seen.append(request)
        if request.prompt == "async result prompt":
            injection_started.set()
            await release_result.wait()
            return AgentResponse(result="visible final report", session_id="after-result")
        question_started.set()
        return AgentResponse(result="private answer", session_id="after-question")

    main._cli_service.execute.side_effect = execute
    async_result = AsyncInterAgentResult(
        task_id="other-task",
        sender="main",
        recipient="other-sub",
        message_preview="other work",
        result_text="finished",
        chat_id=-777,
        topic_id=42,
        transport="tg",
    )
    env = from_interagent_result(async_result, 12345, injection_prompt="async result prompt")
    delivery = asyncio.create_task(messages.submit(env))
    await asyncio.wait_for(injection_started.wait(), 1)
    pending = asyncio.create_task(question(bus))
    await asyncio.sleep(0.01)
    assert not question_started.is_set()
    release_result.set()
    await asyncio.wait_for(delivery, 1)
    reply = await asyncio.wait_for(pending, 1)
    assert reply.success
    assert reply.text == "private answer"
    assert len(seen) == 2
    assert seen[1].resume_session == "after-result"
    # Only the existing result envelope was posted; the question has no envelope.
    transport.deliver.assert_awaited_once_with(env)
    transport.deliver_broadcast.assert_not_awaited()
    assert not messages.lock_pool.get(key.lock_key).locked()


async def test_wait_timeout_uses_c19a_named_fallback_and_source_header(wired, monkeypatch):
    bus, main, worker, messages, transport = wired
    key, transcript = await seed_topic(main)
    monkeypatch.setattr(injection, "_ASYNC_QUESTION_LOCK_WAIT", 0.02)
    bus.set_async_result_handler("main", AsyncMock())
    answers = []

    async def sub_execute(request):
        answer = await bus.send("worker", "main", "Question", delegation_id=request.delegation_id)
        answers.append(answer)
        return AgentResponse(result="finished")

    worker._cli_service.execute.side_effect = sub_execute
    lock = messages.lock_pool.get(key.lock_key)
    await lock.acquire()
    try:
        task_id = bus.send_async(
            "main",
            "worker",
            "original task",
            opts=AsyncSendOptions(
                source_session_kind="foreground",
                chat_id=-777,
                topic_id=42,
                silent=True,
            ),
        )
        await asyncio.wait_for(bus._async_tasks[task_id].asyncio_task, 1)
        assert answers[0].success
        assert answers[0].text == "main answer"
        request = main._cli_service.execute.call_args.args[0]
        assert request.chat_id == 12345
        assert request.resume_session is None
        for field in (
            "Delegation mode: async",
            "Original chat: tg:-777",
            "Original topic: 42",
            "name: Topic 42",
            "Original session key: tg:-777:42",
            "provider session ID: source-42",
            f"Delegation task/request ID: {task_id}",
            "Original request preview: original task",
            f"Conversation record: {transcript}",
            str(main.paths.sessions_path),
            "find the original conversation record",
        ):
            assert field in request.prompt
        assert main._named_sessions.get(12345, interagent_session_name("worker", -777, 42))
        assert (await main._sessions.get_active(key)).session_id == "source-42"
        assert lock.locked()  # Timeout must not release another turn's lock.
        assert not bus._delegations
    finally:
        lock.release()
    transport.deliver.assert_not_awaited()


@pytest.mark.parametrize(
    "case", ["unwired", "no-chat", "no-session", "no-session-id", "bad-metadata"]
)
async def test_unavailable_source_safely_falls_back_without_unprotected_cli(wired, case):
    _, main, _, messages, transport = wired
    key, _ = await seed_topic(main)
    entry = context()
    if case == "unwired":
        main._lock_pool = None
    elif case == "no-chat":
        from dataclasses import replace

        entry = replace(entry, chat_id=0)
    elif case == "no-session":
        main.paths.sessions_path.unlink()
    elif case == "no-session-id":
        await main._sessions.reset_provider_session(key, "claude", "sonnet")
    else:
        main._sessions.get_active = AsyncMock(side_effect=OSError("synthetic failure"))
    result, name, _ = await asyncio.wait_for(
        main.handle_interagent_message(
            "worker",
            "Question",
            source_chat_id=entry.chat_id,
            source_topic_id=entry.topic_id,
            question_context=entry,
        ),
        1,
    )
    assert result == "main answer"
    assert name == interagent_session_name("worker", entry.chat_id, entry.topic_id)
    request = main._cli_service.execute.call_args.args[0]
    assert request.chat_id == 12345
    assert "[DELEGATION QUESTION SOURCE]" in request.prompt
    assert not messages.lock_pool.get(key.lock_key).locked()
    transport.deliver.assert_not_awaited()


@pytest.mark.parametrize("phase", ["waiting", "reading", "executing"])
async def test_cancellation_releases_only_our_acquisition_and_does_not_replay(wired, phase):
    bus, main, _, messages, _ = wired
    key, _ = await seed_topic(main)
    lock = messages.lock_pool.get(key.lock_key)
    started = asyncio.Event()
    if phase == "waiting":
        await lock.acquire()
    elif phase == "reading":

        async def read(_key):
            started.set()
            await asyncio.Event().wait()

        main._sessions.get_active = read
    else:

        async def execute(_request):
            started.set()
            await asyncio.Event().wait()

        main._cli_service.execute.side_effect = execute
    pending = asyncio.create_task(question(bus))
    if phase == "waiting":
        await asyncio.sleep(0.01)
    else:
        await asyncio.wait_for(started.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 1)
    assert lock.locked() == (phase == "waiting")
    assert main._named_sessions.list_active(12345) == []
    if phase != "executing":
        main._cli_service.execute.assert_not_awaited()
    if phase == "waiting":
        lock.release()


async def test_outer_sync_deadline_cancels_active_execution_and_releases_lock(wired):
    bus, main, _, messages, _ = wired
    key, _ = await seed_topic(main)
    started = asyncio.Event()

    async def execute(_request):
        started.set()
        await asyncio.Event().wait()

    main._cli_service.execute.side_effect = execute
    bus._delegations["test-task"] = context()
    result = await asyncio.wait_for(
        bus.send(
            "worker",
            "main",
            "Question",
            delegation_id="test-task",
            send_timeout=0.03,
        ),
        1,
    )
    assert started.is_set()
    assert not result.success
    assert "Timeout" in result.error
    assert not messages.lock_pool.get(key.lock_key).locked()
    assert main._named_sessions.list_active(12345) == []


async def test_active_cli_exception_returns_error_without_duplicate_named_execution(wired):
    bus, main, _, messages, _ = wired
    key, _ = await seed_topic(main)
    main._cli_service.execute.side_effect = RuntimeError("synthetic CLI failure")
    reply = await asyncio.wait_for(question(bus), 1)
    assert not reply.success
    assert "RuntimeError" in reply.error
    main._cli_service.execute.assert_awaited_once()
    assert not messages.lock_pool.get(key.lock_key).locked()
    assert main._named_sessions.list_active(12345) == []


async def test_two_questions_for_same_topic_serialize_and_resume_latest_session(wired):
    bus, main, _, messages, _ = wired
    key, _ = await seed_topic(main)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    ids = []
    count = 0

    async def execute(request):
        nonlocal count
        count += 1
        ids.append(request.resume_session)
        if count == 1:
            first_started.set()
            await release_first.wait()
            return AgentResponse(result="first", session_id="after-first")
        second_started.set()
        return AgentResponse(result="second", session_id="after-second")

    main._cli_service.execute.side_effect = execute
    first = asyncio.create_task(question(bus))
    await asyncio.wait_for(first_started.wait(), 1)
    second = asyncio.create_task(question(bus))
    await asyncio.sleep(0.01)
    assert not second_started.is_set()
    release_first.set()
    replies = await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert [reply.text for reply in replies] == ["first", "second"]
    assert ids == ["source-42", "after-first"]
    assert (await main._sessions.get_active(key)).session_id == "after-second"
    assert not messages.lock_pool.get(key.lock_key).locked()


@pytest.mark.parametrize("transport", ["mx", "sl", "api"])
async def test_transport_neutral_active_question_routing(wired, transport):
    _, main, _, messages, adapter = wired
    key = SessionKey("tg" if transport == "api" else transport, -777, 42)
    active, _ = await main._sessions.resolve_session(key, provider="claude", model="sonnet")
    active.session_id = "transport-source"
    await main._sessions.update_session(active)
    entry = context(transport=transport)
    result, name, notice = await asyncio.wait_for(
        main.handle_interagent_message(
            "worker",
            "Question",
            question_context=entry,
        ),
        1,
    )
    assert result == "main answer"
    assert name == ""
    assert notice == ""
    request = main._cli_service.execute.call_args.args[0]
    assert request.resume_session == "transport-source"
    assert request.transport == transport
    assert request.chat_id == -777
    assert request.topic_id == 42
    assert not messages.lock_pool.get(key.lock_key).locked()
    adapter.deliver.assert_not_awaited()


async def test_api_receives_shared_pool_before_server_starts(wired, monkeypatch):
    from ductor_bot.api.server import ApiServer
    from ductor_bot.orchestrator.lifecycle import start_api_server

    _, main, _, messages, _ = wired
    main._config.api.token = "synthetic-token"
    started = []

    async def start(server):
        assert server._lock_pool is messages.lock_pool
        started.append(server)

    monkeypatch.setattr(ApiServer, "start", start)
    await start_api_server(main, main._config, main.paths)
    assert started == [main._api_stop.__self__]


async def test_orchestrator_factory_forwards_startup_lock_pool(wired, monkeypatch):
    from ductor_bot.orchestrator import lifecycle
    from ductor_bot.orchestrator.core import Orchestrator

    _, main, _, messages, _ = wired
    create = AsyncMock(return_value=main)
    monkeypatch.setattr(lifecycle, "create_orchestrator", create)
    assert await Orchestrator.create(main._config, lock_pool=messages.lock_pool) is main
    create.assert_awaited_once_with(
        main._config,
        agent_name="main",
        lock_pool=messages.lock_pool,
    )


async def test_outer_deadline_while_waiting_preserves_owner_lock(wired):
    bus, main, _, messages, _ = wired
    key, _ = await seed_topic(main)
    lock = messages.lock_pool.get(key.lock_key)
    await lock.acquire()
    bus._delegations["test-task"] = context()
    try:
        reply = await asyncio.wait_for(
            bus.send("worker", "main", "Question", delegation_id="test-task", send_timeout=0.02),
            1,
        )
        assert not reply.success
        assert lock.locked()
        main._cli_service.execute.assert_not_awaited()
    finally:
        lock.release()


async def test_busy_other_topic_does_not_block_question(wired):
    bus, main, _, messages, _ = wired
    key, _ = await seed_topic(main)
    other = messages.lock_pool.get(SessionKey("tg", key.chat_id, 99).lock_key)
    await other.acquire()
    try:
        reply = await asyncio.wait_for(question(bus), 1)
        assert reply.success
        assert reply.text == "main answer"
        assert other.locked()
    finally:
        other.release()


async def test_empty_active_answer_does_not_retry_in_named_session(wired):
    bus, main, _, messages, _ = wired
    key, _ = await seed_topic(main)
    main._cli_service.execute.return_value = AgentResponse(result="", session_id="empty-answer")
    reply = await asyncio.wait_for(question(bus), 1)
    assert reply.success
    assert reply.text == ""
    main._cli_service.execute.assert_awaited_once()
    assert main._named_sessions.list_active(12345) == []
    assert not messages.lock_pool.get(key.lock_key).locked()


async def test_outer_deadline_cancels_named_fallback_after_lock_wait(wired, monkeypatch):
    bus, main, _, messages, _ = wired
    key, _ = await seed_topic(main)
    lock = messages.lock_pool.get(key.lock_key)
    await lock.acquire()
    monkeypatch.setattr(injection, "_ASYNC_QUESTION_LOCK_WAIT", 0.01)
    started = asyncio.Event()

    async def execute(request):
        assert "original task" in request.prompt
        started.set()
        await asyncio.Event().wait()

    main._cli_service.execute.side_effect = execute
    bus._delegations["test-task"] = context()
    try:
        reply = await asyncio.wait_for(
            bus.send("worker", "main", "Question", delegation_id="test-task", send_timeout=0.04),
            1,
        )
        assert not reply.success
        assert started.is_set()
        assert lock.locked()
        ns = main._named_sessions.get(12345, interagent_session_name("worker", -777, 42))
        assert ns is not None
        assert ns.status == "idle"
    finally:
        lock.release()
