"""First-turn provenance regressions adapted from an independent reproduction."""

import asyncio
import os
from unittest.mock import AsyncMock, Mock

import pytest

from ductor_bot.background.observer import BackgroundObserver
from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import DeliveryOutcome
from ductor_bot.cli.executor import build_subprocess_env
from ductor_bot.cli.types import AgentResponse
from ductor_bot.multiagent.bus import SYNC_DELEGATION_QUESTION_REJECTED
from ductor_bot.orchestrator.core import NamedSessionRequest
from tests.multiagent.test_delegation_context import agents, seed_topic  # noqa: F401
from tests.orchestrator.test_interagent import _load_tool, _run_tool


async def dispatch_payload(bus, body, *, mode="async"):
    """Forward the tool payload through the actual internal HTTP dispatcher."""
    from aiohttp.test_utils import TestClient, TestServer

    from ductor_bot.multiagent.internal_api import InternalAgentAPI

    api = InternalAgentAPI(bus, port=0)
    async with TestClient(TestServer(api._app)) as client:
        response = await client.post(
            f"/interagent/send{'_async' if mode == 'async' else ''}", json=body
        )
        data = await response.json()
        assert response.status == 200
        assert data["success"]
        return data["task_id"] if mode == "async" else ""


@pytest.mark.parametrize("tool_name", ["ask_agent.py", "ask_agent_async.py"])
async def test_new_named_background_dispatch_never_borrows_foreground(
    agents,  # noqa: F811 - imported pytest fixture
    monkeypatch,
    tool_name,
):
    import ductor_bot.cli.service as cli_service

    bus, main, worker = agents
    key, _ = await seed_topic(main)
    main.wire_observers_to_bus(MessageBus())
    bus.set_async_result_handler(
        "main", AsyncMock(return_value=DeliveryOutcome(ok=True, injected=True, delivered=True))
    )
    observer = BackgroundObserver(
        main.paths, timeout_seconds=5, cli_service=main._cli_service, config=main._config
    )
    observer.set_result_handler(AsyncMock())
    main._observers.background = observer
    create_cli = Mock()
    monkeypatch.setattr(cli_service, "create_cli", create_cli)
    observations = {}
    private_requests = []

    async def answer_or_initial(request):
        if request.process_label.startswith("ns:"):
            assert request.resume_session is None
            name = request.process_label[3:]
            original = main._named_sessions.get(key.chat_id, name)
            assert original is not None
            assert original.status == "running"
            assert not original.session_id
            main._cli_service._make_cli(request)
            config = create_cli.call_args.args[0]
            env = build_subprocess_env(config)
            observations["request_label"] = request.process_label
            observations["named_provider"] = request.provider_override
            observations["source_id_exported"] = "DUCTOR_SESSION_ID" in env
            with monkeypatch.context() as mp:
                for k, v in env.items():
                    if k.startswith("DUCTOR_"):
                        mp.setenv(k, v)
                tool = _load_tool(tool_name)
                body = _run_tool(tool, [tool_name, "worker", "New named research"], mp)
            observations["tool_sent_source_id"] = "source_session_id" in body
            assert env["DUCTOR_SESSION_KIND"] == "named"
            assert env["DUCTOR_SESSION_NAME"] == name
            assert body["source_session_kind"] == "named"
            assert body["source_session_name"] == name
            assert not body.get("ia_turn")
            mode = "async" if tool_name == "ask_agent_async.py" else "sync"
            tid = await dispatch_payload(bus, body, mode=mode)
            if tid:
                await asyncio.wait_for(bus._async_tasks[tid].asyncio_task, 1)
            return AgentResponse(result="research complete", session_id="actual-new-named-id")
        private_requests.append(request)
        return AgentResponse(result="clarification from selected session", session_id="source-42")

    async def sub(request):
        entry = bus._delegations[request.delegation_id]
        observations.update(
            captured_key=entry.session_key,
            captured_provider=entry.provider,
            captured_id=entry.session_id,
        )
        assert entry.session_kind == "named"
        assert entry.session_name
        assert entry.provider == "codex"
        assert entry.session_id == ""
        answer = await bus.send(
            "worker",
            "main",
            "Clarify this research",
            delegation_id=request.delegation_id,
            ia_turn=True,
        )
        if tool_name == "ask_agent.py":
            assert not answer.success
            assert answer.error == SYNC_DELEGATION_QUESTION_REJECTED
        else:
            assert answer.success
        return AgentResponse(result="sub complete")

    main._cli_service.execute.side_effect = answer_or_initial
    worker._cli_service.execute.side_effect = sub
    task_id, name = main.submit_named_session(
        key.chat_id,
        "Start new named research",
        NamedSessionRequest(
            message_id=1,
            thread_id=key.topic_id,
            transport="tg",
            provider_override="codex",
            model_override="gpt-5.5",
        ),
    )
    task = observer._tasks[task_id].asyncio_task
    await asyncio.wait_for(task, 2)
    if tool_name == "ask_agent.py":
        assert not private_requests
        assert (await main._sessions.get_active(key)).session_id == "source-42"
        assert main._named_sessions.get(key.chat_id, name).session_id == ""
        assert not bus._delegations
        return
    assert len(private_requests) == 1
    request = private_requests[0]
    observations.update(
        case="new_named_first_turn",
        named_session_name=name,
        question_resume=request.resume_session,
        question_provider=request.provider_override,
        question_fallback="[DELEGATION QUESTION SOURCE]" in request.prompt,
    )
    assert request.resume_session != "source-42", (
        "New Named Session first-turn question resumed unrelated foreground"
    )
    assert "[DELEGATION QUESTION SOURCE]" in request.prompt
    assert f"Original session kind: named; name: {name}" in request.prompt
    assert f"Original session key: tg:-777:42 @{name}" in request.prompt
    assert "Provider: codex; provider session ID: unconfirmed" in request.prompt
    assert (await main._sessions.get_active(key)).session_id == "source-42"
    assert main._named_sessions.get(key.chat_id, name).session_id == ""
    assert not bus._delegations


@pytest.mark.parametrize("existing_id", ["", "existing-foreground-id"])
async def test_new_foreground_dispatch_waits_until_provider_id_is_available(
    agents,  # noqa: F811 - imported pytest fixture
    monkeypatch,
    existing_id,
):
    import ductor_bot.cli.service as cli_service
    from ductor_bot.messenger.telegram.middleware import SequentialMiddleware
    from ductor_bot.session.key import SessionKey
    from tests.messenger.telegram.test_middleware import _make_message

    bus, main, worker = agents
    key = SessionKey("tg", -777, 42)
    messages = MessageBus()
    main.wire_observers_to_bus(messages)
    session, _ = await main._sessions.resolve_session(key, provider="claude", model="sonnet")
    assert not session.session_id
    session.session_id = existing_id
    await main._sessions.update_session(session)
    bus.set_async_result_handler(
        "main", AsyncMock(return_value=DeliveryOutcome(ok=True, injected=True, delivered=True))
    )
    create_cli = Mock()
    monkeypatch.setattr(cli_service, "create_cli", create_cli)
    started = asyncio.Event()
    questioning = asyncio.Event()
    release = asyncio.Event()
    tids = []
    questions = []
    observations = {}

    async def execute(request):
        if request.process_label == "main":
            assert request.resume_session == (existing_id or None)
            main._cli_service._make_cli(request)
            env = build_subprocess_env(create_cli.call_args.args[0])
            assert env.get("DUCTOR_SESSION_ID", "") == existing_id
            with monkeypatch.context() as mp:
                for k, v in env.items():
                    if k.startswith("DUCTOR_"):
                        mp.setenv(k, v)
                body = _run_tool(
                    _load_tool("ask_agent_async.py"),
                    ["ask_agent_async.py", "--silent", "worker", "New foreground work"],
                    mp,
                )
            assert env["DUCTOR_SESSION_KIND"] == "foreground"
            assert "DUCTOR_SESSION_NAME" not in env
            assert body["source_session_kind"] == "foreground"
            tids.append(await dispatch_payload(bus, body))
            started.set()
            await release.wait()
            return AgentResponse(
                result="foreground finished dispatching", session_id="new-foreground-id"
            )
        questions.append(request)
        return AgentResponse(
            result="private answer", session_id=request.resume_session or "fallback-id"
        )

    async def sub(request):
        entry = bus._delegations[request.delegation_id]
        observations.update(
            captured_key=entry.session_key,
            captured_provider=entry.provider,
            captured_id=entry.session_id,
        )
        assert entry.session_kind == "foreground"
        assert entry.session_id == existing_id
        questioning.set()
        result = await bus.send(
            "worker",
            "main",
            "First-turn question",
            delegation_id=request.delegation_id,
            ia_turn=True,
        )
        assert result.success
        return AgentResponse(result="done")

    main._cli_service.execute.side_effect = execute
    worker._cli_service.execute.side_effect = sub
    middleware = SequentialMiddleware(lock_pool=messages.lock_pool)
    event = _make_message(
        chat_id=key.chat_id,
        topic_thread_id=key.topic_id,
        text="normal first user turn",
        chat_type="supergroup",
    )

    async def handler(event, _data):
        return await main.handle_message(key, event.text)

    user = asyncio.create_task(middleware(handler, event, {}))
    try:
        await asyncio.wait_for(started.wait(), 1)
        async_task = bus._async_tasks[tids[0]].asyncio_task
        await asyncio.wait_for(questioning.wait(), 1)
        await asyncio.sleep(0.025)
        observations["answered_before_foreground_finished"] = bool(questions)
        observations["owner_lock_held"] = messages.lock_pool.get(key.lock_key).locked()
        release.set()
        await asyncio.wait_for(user, 1)
        await asyncio.wait_for(async_task, 1)
        assert len(questions) == 1
        observations.update(
            case="new_foreground_first_turn",
            question_resume=questions[0].resume_session,
            question_fallback="[DELEGATION QUESTION SOURCE]" in questions[0].prompt,
        )
        assert not observations["answered_before_foreground_finished"], (
            "Fresh ordinary foreground question bypassed the 60s shared-lock wait"
        )
        assert questions[0].resume_session == "new-foreground-id"
    finally:
        release.set()
        if not user.done():
            user.cancel()
        await asyncio.gather(user, return_exceptions=True)


@pytest.mark.parametrize("tool_name", ["ask_agent.py", "ask_agent_async.py"])
@pytest.mark.parametrize(
    ("kind", "name"), [("foreground", ""), ("named", "first-research"), ("background", "")]
)
def test_cli_environment_and_both_tools_carry_kind_without_provider_id(
    agents,  # noqa: F811 - imported pytest fixture
    monkeypatch,
    tool_name,
    kind,
    name,
):
    import ductor_bot.cli.service as cli_service
    from ductor_bot.cli.types import AgentRequest

    _, main, _ = agents
    create_cli = Mock()
    monkeypatch.setattr(cli_service, "create_cli", create_cli)
    monkeypatch.setenv("DUCTOR_SESSION_NAME", "inherited-wrong-name")
    monkeypatch.setenv("DUCTOR_SESSION_KIND", "inherited-wrong-kind")
    main._cli_service._make_cli(
        AgentRequest(
            prompt="synthetic",
            chat_id=-777,
            topic_id=42,
            session_kind=kind,
            session_name=name,
        )
    )
    env = build_subprocess_env(create_cli.call_args.args[0])
    assert env["DUCTOR_SESSION_KIND"] == kind
    assert env.get("DUCTOR_SESSION_NAME", "") == name
    assert "DUCTOR_SESSION_ID" not in env
    for key in list(os.environ):
        if key.startswith("DUCTOR_"):
            monkeypatch.delenv(key)
    for key, value in env.items():
        if key.startswith("DUCTOR_"):
            monkeypatch.setenv(key, value)
    body = _run_tool(_load_tool(tool_name), [tool_name, "worker", "synthetic"], monkeypatch)
    assert body["source_session_kind"] == kind
    assert body.get("source_session_name", "") == name
    assert "source_session_id" not in body


async def test_missing_kind_and_provider_id_do_not_guess_foreground(agents):  # noqa: F811
    from ductor_bot.multiagent.bus import AsyncSendOptions

    bus, main, worker = agents
    key, _ = await seed_topic(main)
    messages = MessageBus()
    main.wire_observers_to_bus(messages)
    bus.set_async_result_handler("main", AsyncMock())

    async def execute(request):
        snapshot = bus._delegations[request.delegation_id]
        assert snapshot.session_kind == "unknown"
        assert snapshot.session_id == ""
        answer = await bus.send(
            "worker",
            "main",
            "Question",
            ia_turn=True,
            delegation_id=request.delegation_id,
        )
        assert answer.success
        return AgentResponse(result="finished")

    worker._cli_service.execute.side_effect = execute
    async with messages.lock_pool.get(key.lock_key):
        task_id = bus.send_async(
            "main",
            "worker",
            "Work",
            opts=AsyncSendOptions(chat_id=-777, topic_id=42, silent=True),
        )
        await asyncio.wait_for(bus._async_tasks[task_id].asyncio_task, 1)
    request = main._cli_service.execute.call_args.args[0]
    assert request.resume_session is None
    assert "Original session kind: unknown" in request.prompt
    assert (await main._sessions.get_active(key)).session_id == "source-42"


async def test_existing_foreground_waits_default_60_seconds_before_private_fallback(agents):  # noqa: F811
    import time

    from ductor_bot.multiagent.bus import AsyncSendOptions
    from ductor_bot.orchestrator import injection

    bus, main, worker = agents
    key, _ = await seed_topic(main)
    messages = MessageBus()
    main.wire_observers_to_bus(messages)
    bus.set_async_result_handler("main", AsyncMock())
    assert injection._ASYNC_QUESTION_LOCK_WAIT == 60
    waits = []

    async def execute(request):
        start = time.monotonic()
        answer = await bus.send(
            "worker",
            "main",
            "Question",
            ia_turn=True,
            delegation_id=request.delegation_id,
        )
        waits.append(time.monotonic() - start)
        assert answer.success
        return AgentResponse(result="finished")

    worker._cli_service.execute.side_effect = execute
    async with messages.lock_pool.get(key.lock_key):
        task_id = bus.send_async(
            "main",
            "worker",
            "Work",
            opts=AsyncSendOptions(
                chat_id=-777,
                topic_id=42,
                silent=True,
                source_session_kind="foreground",
            ),
        )
        await asyncio.wait_for(bus._async_tasks[task_id].asyncio_task, 65)
        assert messages.lock_pool.get(key.lock_key).locked()
    assert 60 <= waits[0] < 65
    request = main._cli_service.execute.call_args.args[0]
    assert request.resume_session is None
    assert "Original session kind: foreground" in request.prompt
    assert "provider session ID: source-42" in request.prompt
    assert not bus._delegations
