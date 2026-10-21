"""Provenance, synchronous question rejection and dispatch lifetime regressions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.executor import build_subprocess_env
from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig
from ductor_bot.multiagent.bus import (
    AsyncSendOptions, InterAgentBus, SYNC_DELEGATION_QUESTION_REJECTED,
)
from ductor_bot.multiagent.delegation import DelegationContext, capture_delegation, safe_preview
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.session.key import SessionKey
from ductor_bot.session.named import NamedSession, interagent_session_name
from ductor_bot.workspace.paths import DuctorPaths


@pytest.fixture
def agents(tmp_path: Path) -> tuple[InterAgentBus, Orchestrator, Orchestrator]:
    bus = InterAgentBus()
    orchs = []
    for name in ("main", "worker"):
        home = tmp_path / name
        home.mkdir()
        config = AgentConfig(allowed_user_ids=[12345], model="sonnet")
        orch = Orchestrator(config, DuctorPaths(home), agent_name=name)
        orch._cli_service.execute = AsyncMock(
            return_value=AgentResponse(result=f"{name} answer", session_id=f"{name}-named")
        )
        stack = MagicMock()
        stack.bot.orchestrator = orch
        stack.config = config
        stack.bot.notification_service.notify = AsyncMock()
        stack.bot.notification_service.notify_all = AsyncMock()
        bus.register(name, stack)
        orchs.append(orch)
    return bus, orchs[0], orchs[1]


async def seed_topic(orch: Orchestrator, topic_id: int = 42) -> tuple[SessionKey, str]:
    key = SessionKey("tg", -777, topic_id)
    session, _ = await orch._sessions.resolve_session(key, provider="claude", model="sonnet")
    session.session_id = f"source-{topic_id}"
    session.topic_name = f"Topic {topic_id}"
    await orch._sessions.update_session(session)
    path = (
        orch.paths.ductor_home / ".cli-auth/_shared-projects/project" / f"source-{topic_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"message":"synthetic history"}\n')
    return key, str(path)


async def test_sync_instruction_and_question_rejection_no_deadlock(agents, caplog) -> None:
    bus, main, worker = agents
    key, _ = await seed_topic(main)
    main.bot_post = AsyncMock()
    caplog.set_level("INFO", logger="ductor_bot.multiagent.bus")

    async def sub_execute(request):
        assert "synchronous delegation" in request.prompt
        assert "Do not send question messages to main" in request.prompt
        assert "the server rejects them" in request.prompt
        assert "final report" in request.prompt
        assert "do not invent missing values" in request.prompt
        answer = await bus.send(
            "worker", "main", "Which path?", ia_turn=True,
            delegation_id=request.delegation_id, chat_id=999, topic_id=99,
        )
        assert not answer.success and answer.text == ""
        assert answer.error == SYNC_DELEGATION_QUESTION_REJECTED
        # The sub can continue and report missing information without another call.
        return AgentResponse(result="Completed checks; path unset. Question: Which path?",
                             session_id="worker-named")

    worker._cli_service.execute.side_effect = sub_execute
    result = await asyncio.wait_for(
        bus.send("main", "worker", "Inspect the build config", chat_id=-777,
                 topic_id=42, source_session_kind="foreground"), timeout=1,
    )
    assert result.success and "path unset. Question: Which path?" in result.text
    main._cli_service.execute.assert_not_awaited()
    assert main._named_sessions.list_active(12345) == []
    assert (await main._sessions.get_active(key)).session_id == "source-42"
    main.bot_post.assert_not_awaited()
    records = [record.getMessage() for record in caplog.records if "worker -> main" in record.getMessage()]
    assert len(records) == 1 and "rejected" in records[0]
    assert "Which path?" not in records[0]
    assert not bus._delegations


async def test_concurrent_delegations_keep_distinct_topic_provenance(agents) -> None:
    bus, main, worker = agents
    await seed_topic(main, 42)
    await seed_topic(main, 43)
    both_started = asyncio.Event()
    ids = []
    snapshots = []

    async def sub_execute(request):
        ids.append(request.delegation_id)
        snapshots.append(bus._delegations[request.delegation_id])
        if len(ids) == 2:
            both_started.set()
        await both_started.wait()
        return_question = await bus.send(
            "worker",
            "main",
            "Question",
            delegation_id=request.delegation_id,
        )
        assert not return_question.success
        assert return_question.error == SYNC_DELEGATION_QUESTION_REJECTED
        return AgentResponse(result="ok")

    worker._cli_service.execute.side_effect = sub_execute
    results = await asyncio.wait_for(
        asyncio.gather(
            bus.send("main", "worker", "topic A", chat_id=-777, topic_id=42),
            bus.send("main", "worker", "topic B", chat_id=-777, topic_id=43),
        ),
        timeout=1,
    )
    assert all(result.success for result in results)
    assert len(set(ids)) == 2
    main._cli_service.execute.assert_not_awaited()
    assert {(snapshot.topic_id, snapshot.request_preview) for snapshot in snapshots} == {
        (42, "topic A"), (43, "topic B"),
    }
    assert not bus._delegations


async def test_async_dispatch_id_is_real_task_id_and_has_no_sync_instruction(agents) -> None:
    bus, main, worker = agents
    await seed_topic(main)
    bus.set_async_result_handler("main", AsyncMock())
    captured = []
    finished = asyncio.Event()

    async def sub_execute(request):
        captured.append(request)
        context = bus._delegations[request.delegation_id]
        assert context.mode == "async"
        assert context.session_id == "source-42"
        assert "Do not send question messages to main" not in request.prompt
        finished.set()
        return AgentResponse(result="async done")

    worker._cli_service.execute.side_effect = sub_execute
    task_id = bus.send_async(
        "main",
        "worker",
        "Async work",
        opts=AsyncSendOptions(
            source_session_kind="foreground",
            chat_id=-777,
            topic_id=42,
            silent=True,
        ),
    )
    assert task_id
    task = bus._async_tasks[task_id].asyncio_task
    await asyncio.wait_for(finished.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=1)
    assert captured[0].delegation_id == task_id
    assert not bus._delegations


@pytest.mark.parametrize("ia_turn", [False, True])
async def test_missing_context_keeps_named_routing_marks_only_delegated_unknown(
    agents, ia_turn
) -> None:
    bus, main, _ = agents
    result = await bus.send(
        "worker",
        "main",
        "Question",
        ia_turn=ia_turn,
        delegation_id="expired",
        chat_id=-777,
        topic_id=42,
    )
    assert result.success
    request = main._cli_service.execute.call_args.args[0]
    assert request.chat_id == 12345 and request.origin_topic_id == 42
    assert ("[DELEGATION QUESTION SOURCE]" in request.prompt) == ia_turn
    if ia_turn:
        assert "Delegation mode: unknown" in request.prompt
        assert "Delegation task/request ID: unconfirmed" in request.prompt
    assert not request.delegation_id


async def test_other_sender_cannot_borrow_delegation_context(agents) -> None:
    bus, main, _ = agents
    context = DelegationContext("active", "sync", "main", "worker", chat_id=-777, topic_id=42)
    bus._delegations[context.request_id] = context
    await bus.send("other-worker", "main", "Question", delegation_id="active", ia_turn=True)
    prompt = main._cli_service.execute.call_args.args[0].prompt
    assert "Delegation mode: unknown" in prompt and "Original chat: tg:0" in prompt
    assert "active" not in prompt


@pytest.mark.parametrize("failure", ["timeout", "exception", "cancel", "stale"])
async def test_dispatch_context_lifetime_and_retry_identity(agents, failure) -> None:
    bus, _, worker = agents
    started = asyncio.Event()
    seen = []

    async def execute(request):
        started.set()
        seen.append(request.delegation_id)
        assert request.delegation_id in bus._delegations
        if failure in {"timeout", "cancel"}:
            await asyncio.Event().wait()
        if failure == "exception":
            raise RuntimeError("synthetic error")
        if failure == "stale" and len(seen) == 1:
            return AgentResponse(result="No conversation found with session ID: old", is_error=True)
        return AgentResponse(result="recovered", session_id="fresh")

    worker._cli_service.execute.side_effect = execute
    # Only the timeout case needs a short request deadline.
    send_timeout = 0.03 if failure == "timeout" else 1
    task = asyncio.create_task(bus.send("main", "worker", "Do work", send_timeout=send_timeout))
    await asyncio.wait_for(started.wait(), timeout=1)
    if failure == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    else:
        result = await asyncio.wait_for(task, timeout=1)
        if failure == "timeout":
            assert not result.success and "Timeout" in result.error
        if failure == "stale":
            assert result.success and seen[0] == seen[1]
    assert not bus._delegations
    assert worker._named_sessions.get(12345, "ia-main").status == "idle"


async def test_source_snapshot_timeout_is_inside_sync_deadline(agents, monkeypatch) -> None:
    bus, _, worker = agents

    async def slow_capture(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(bus, "_capture_delegation", slow_capture)
    result = await asyncio.wait_for(
        bus.send("main", "worker", "Work", send_timeout=0.02), timeout=1
    )
    assert not result.success and "Timeout" in result.error
    worker._cli_service.execute.assert_not_awaited()
    assert not bus._delegations


async def test_snapshot_matches_named_session_and_never_claims_wrong_id(agents) -> None:
    _, main, _ = agents
    await seed_topic(main)
    ns = NamedSession(
        name="research",
        chat_id=-777,
        provider="codex",
        model="gpt-5.5",
        session_id="named-source",
        prompt_preview="synthetic",
        status="idle",
        created_at=0,
    )
    main._named_sessions.add(ns)
    context = DelegationContext("dispatch", "sync", "main", "worker", chat_id=-777, topic_id=42)
    snapshot = await capture_delegation(main, context, "named-source")
    assert snapshot.session_key == "tg:-777:42 @research"
    assert snapshot.provider == "codex" and snapshot.session_id == "named-source"
    assert snapshot.metadata_path == str(main.paths.named_sessions_path)
    assert snapshot.transcript_path == ""  # No unverified path invented.
    unknown = await capture_delegation(main, context, "nonexistent")
    assert not unknown.provider and not unknown.session_id


@pytest.mark.parametrize(
    "message",
    [
        "token=synthetic-value",
        "api_key: fake",
        "password=example",
        "use sk-fake-example",
        "123456:abcdefghijklmnopqrstuvwxyz_fake",
    ],
)
def test_preview_omits_credential_like_request(message) -> None:
    assert safe_preview(message) == "[request preview omitted: credential-like text]"


def test_preview_truncated_to_200_chars() -> None:
    assert safe_preview("A" * 400) == "A" * 200


def test_dispatch_id_and_source_session_env_are_overridden_and_cleared(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DUCTOR_DELEGATION_ID", "inherited")
    monkeypatch.setenv("DUCTOR_SESSION_ID", "inherited-session")
    env = build_subprocess_env(CLIConfig(ductor_home=tmp_path))
    assert "DUCTOR_DELEGATION_ID" not in env and "DUCTOR_SESSION_ID" not in env
    env = build_subprocess_env(
        CLIConfig(
            ductor_home=tmp_path,
            interagent_turn=True,
            delegation_id="current",
            session_id="provider-current",
        )
    )
    assert env["DUCTOR_DELEGATION_ID"] == "current"
    assert env["DUCTOR_SESSION_ID"] == "provider-current"


def test_cli_service_carries_request_identity_into_subprocess(agents, monkeypatch) -> None:
    from ductor_bot.cli.types import AgentRequest
    import ductor_bot.cli.service as service

    _, main, _ = agents
    create_cli = MagicMock()
    monkeypatch.setattr(service, "create_cli", create_cli)
    main._cli_service._make_cli(
        AgentRequest(
            prompt="synthetic",
            chat_id=-777,
            topic_id=42,
            resume_session="source-42",
            interagent_turn=True,
            delegation_id="dispatch-42",
        )
    )
    config = create_cli.call_args.args[0]
    env = build_subprocess_env(config)
    assert env["DUCTOR_SESSION_ID"] == "source-42"
    assert env["DUCTOR_DELEGATION_ID"] == "dispatch-42"
    assert env["DUCTOR_IA_TURN"] == "1"


def test_sync_tool_forwards_dispatch_identity_without_request_text(monkeypatch) -> None:
    from tests.orchestrator.test_interagent import _load_tool, _run_tool

    monkeypatch.setenv("DUCTOR_IA_TURN", "1")
    monkeypatch.setenv("DUCTOR_DELEGATION_ID", "real-dispatch")
    monkeypatch.setenv("DUCTOR_SESSION_ID", "worker-session")
    tool = _load_tool("ask_agent.py")
    body = _run_tool(tool, ["ask_agent.py", "main", "synthetic question"], monkeypatch)
    assert body["delegation_id"] == "real-dispatch"
    assert body["ia_turn"] is True
    assert body["source_session_id"] == "worker-session"
    assert "mode" not in body and "request_preview" not in body


async def test_sync_question_rejection_keeps_parent_timeout_and_cleanup(agents) -> None:
    bus, main, worker = agents
    rejected = asyncio.Event()

    async def sub_execute(request):
        answer = await bus.send("worker", "main", "Question", delegation_id=request.delegation_id)
        assert not answer.success and answer.error == SYNC_DELEGATION_QUESTION_REJECTED
        rejected.set()
        await asyncio.Event().wait()

    worker._cli_service.execute.side_effect = sub_execute
    task = asyncio.create_task(bus.send("main", "worker", "Work", send_timeout=0.03))
    await asyncio.wait_for(rejected.wait(), timeout=1)
    result = await asyncio.wait_for(task, timeout=1)
    assert not result.success and "Timeout" in result.error
    main._cli_service.execute.assert_not_awaited()
    assert main._named_sessions.list_active(12345) == []
    assert not bus._delegations
    assert worker._named_sessions.get(12345, "ia-main").status == "idle"


async def test_http_question_id_is_forwarded_to_server_registry(agents) -> None:
    from aiohttp.test_utils import TestClient, TestServer
    from ductor_bot.multiagent.internal_api import InternalAgentAPI

    bus, main, _ = agents
    bus._delegations["server-id"] = DelegationContext(
        "server-id",
        "async",
        "main",
        "worker",
        chat_id=-777,
        topic_id=42,
        session_key="tg:-777:42",
        session_id="original",
        request_preview="original work",
    )
    api = InternalAgentAPI(bus, port=0)
    async with TestClient(TestServer(api._app)) as client:
        response = await client.post(
            "/interagent/send",
            json={
                "from": "worker",
                "to": "main",
                "message": "Question",
                "ia_turn": True,
                "delegation_id": "server-id",
                "chat_id": 999,
                "topic_id": 99,
            },
        )
        body = await response.json()
    assert body["success"] and body["text"] == "main answer"
    request = main._cli_service.execute.call_args.args[0]
    assert request.origin_chat_id == -777 and request.origin_topic_id == 42
    assert "Delegation task/request ID: server-id" in request.prompt
    assert "Original request preview: original work" in request.prompt


@pytest.mark.parametrize("error", [OSError, ValueError, TypeError])
async def test_unreadable_source_metadata_is_unconfirmed_not_guessed(agents, error) -> None:
    bus, main, worker = agents
    main._sessions.get_active = AsyncMock(side_effect=error("synthetic metadata failure"))

    async def execute(request):
        question = await bus.send("worker", "main", "Question", delegation_id=request.delegation_id)
        assert question.success
        return AgentResponse(result="ok")

    worker._cli_service.execute.side_effect = execute
    delivered = AsyncMock()
    bus.set_async_result_handler("main", delivered)
    task_id = bus.send_async("main", "worker", "Work",
                             opts=AsyncSendOptions(chat_id=-777, topic_id=42, silent=True))
    await asyncio.wait_for(bus._async_tasks[task_id].asyncio_task, 1)
    assert delivered.call_args.args[0].success
    prompt = main._cli_service.execute.call_args.args[0].prompt
    assert "Original session key: tg:-777:42" in prompt
    assert "Provider: unconfirmed; provider session ID: unconfirmed" in prompt
    assert not bus._delegations


async def test_async_snapshot_is_bounded_by_the_queue_wait_not_the_execution_budget(
    agents, monkeypatch
) -> None:
    """The provenance snapshot is preparation: it counts toward the queue-wait limit, and the
    execution limit starts only after admission (lane time contract)."""
    import ductor_bot.multiagent.bus as bus_module

    bus, _, worker = agents
    monkeypatch.setattr(bus_module, "_ASYNC_TIMEOUT", 0.06)
    original_capture = bus._capture_delegation

    async def slow_capture(*args):
        await asyncio.sleep(0.04)
        return await original_capture(*args)

    async def slow_execute(request):
        await asyncio.sleep(0.04)
        return AgentResponse(result="in time")

    monkeypatch.setattr(bus, "_capture_delegation", slow_capture)
    worker._cli_service.execute.side_effect = slow_execute
    delivered = AsyncMock()
    bus.set_async_result_handler("main", delivered)
    task_id = bus.send_async("main", "worker", "Work", opts=AsyncSendOptions(silent=True))
    await asyncio.wait_for(bus._async_tasks[task_id].asyncio_task, timeout=1)
    assert delivered.call_args.args[0].success  # 0.04 + 0.04 > 0.06, but execution started late
    assert not bus._delegations

    bus._agents["worker"].config.interagent.max_queue_wait_seconds = 0.02  # shorter than the preparation
    task_id = bus.send_async("main", "worker", "Work", opts=AsyncSendOptions(silent=True))
    await asyncio.wait_for(bus._async_tasks[task_id].asyncio_task, timeout=1)
    result = delivered.call_args.args[0]
    assert not result.success
    assert bus_module.QUEUE_WAIT_TIMEOUT in result.error
    assert "Timeout after" not in result.error
    assert not bus._delegations
