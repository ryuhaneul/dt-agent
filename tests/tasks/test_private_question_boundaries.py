"""Private questions keep provider output private and retain their original run."""

from __future__ import annotations

# Imported fixtures are requested by name below.
# ruff: noqa: F811
import asyncio
import json
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ductor_bot.bus.bus import MessageBus
from ductor_bot.cli.service import CLIService
from ductor_bot.cli.types import AgentResponse
from ductor_bot.orchestrator.injection import _inject_prompt
from ductor_bot.session.key import SessionKey
from tests.multiagent.test_delegation_context import agents  # noqa: F401
from tests.tasks.test_parent_answers import (
    ANSWER,
    FORWARDED,
    QUESTION,
    delivered,
    finish,
    state,  # noqa: F401
)


async def parent(agents, provider="claude", transport="tg"):
    orch = agents[1]
    bus = MessageBus()
    orch.wire_observers_to_bus(bus)
    key = SessionKey(transport, 42, 9)
    session, _ = await orch._sessions.resolve_session(key, provider=provider, model="test-model")
    session.session_id = "parent-session"
    await orch._sessions.update_session(session)
    return orch, bus.lock_pool.get(key.lock_key), session


def output(provider, case):
    if case == "stderr_only":
        return b"", (QUESTION + ANSWER).encode(), 1
    if case == "malformed":
        return (QUESTION + ANSWER).encode(), QUESTION.encode(), 1
    if case == "array":
        return json.dumps([{"text": QUESTION + ANSWER}]).encode(), QUESTION.encode(), 1
    error = case == "error"
    payload = {"result": ANSWER, "is_error": error, "session_id": "parent-session"}
    if provider == "codex":
        payload = {"type": "item.completed", "item": {"type": "agent_message", "text": ANSWER}}
    return json.dumps(payload).encode(), QUESTION.encode(), int(error)


def fake_subprocess(monkeypatch, provider, case):
    stdout, stderr, code = output(provider, case)
    process = SimpleNamespace(
        pid=456789,
        returncode=code,
        communicate=AsyncMock(return_value=(stdout, stderr)),
        wait=AsyncMock(return_value=code),
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    for name in ("claude", "codex", "grok"):
        monkeypatch.setattr(f"ductor_bot.cli.{name}_provider.which", lambda _name: "fake-cli")
    monkeypatch.setattr("ductor_bot.cli.gemini_provider.find_gemini_cli", lambda: "fake-cli")
    monkeypatch.setattr("ductor_bot.cli.gemini_provider.find_gemini_cli_js", lambda: None)
    monkeypatch.setattr(
        "ductor_bot.cli.gemini_provider.GeminiCLI._resolve_exec",
        lambda self, cmd, _path: (cmd, str(self._working_dir), {}),
    )
    return spawn


CASES = [
    (p, c, "tg")
    for p in ("claude", "codex", "gemini", "grok")
    for c in ("success", "error", "malformed", "array", "stderr_only")
]
CASES += [(p, "error", t) for p in ("claude", "codex") for t in ("mx", "sl")]


@pytest.mark.parametrize(("provider", "case", "transport"), CASES)
async def test_private_provider_output_is_not_logged(  # noqa: PLR0917
    state, agents, monkeypatch, caplog, provider, case, transport
):
    caplog.set_level("DEBUG")
    orch, _, _ = await parent(agents, provider, transport)
    orch._cli_service.execute = MethodType(CLIService.execute, orch._cli_service)
    spawn = fake_subprocess(monkeypatch, provider, case)
    entry = state.registry.get(state.task_id)
    entry.origin_transport = transport
    state.hub.set_task_question_answerer("main", orch.answer_task_question)

    response = await state.hub.ask_parent(state.task_id, QUESTION, connection_alive=lambda: True)
    spawn.assert_awaited_once()
    if case == "success":
        assert response == "Answer from parent:\n" + ANSWER
        assert await delivered(state) == []
        assert not state.fixed.has_pending_question
    else:
        assert response == FORWARDED
        assert len(await delivered(state)) == 1
        assert state.public[0][3:] == (42, 9, transport)
        assert state.public[0][1].count(QUESTION) == 1
        assert ANSWER not in repr(state.public)
        assert state.fixed.has_pending_question
    assert QUESTION.splitlines()[0] not in caplog.text
    assert ANSWER not in caplog.text
    assert ANSWER not in repr(state.results)


@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
async def test_ordinary_injection_retains_provider_content_logs(
    agents, monkeypatch, caplog, provider
):
    caplog.set_level("DEBUG")
    orch, _, _ = await parent(agents, provider)
    orch._cli_service.execute = MethodType(CLIService.execute, orch._cli_service)
    spawn = fake_subprocess(monkeypatch, provider, "error")
    with pytest.raises(RuntimeError, match="injected turn did not complete"):
        await _inject_prompt(orch, "Ordinary injection", 42, "ordinary", topic_id=9)
    spawn.assert_awaited_once()
    if provider == "claude":
        assert "CLI stderr: " + QUESTION[:500] in caplog.text
        assert "CLI error: " + ANSWER in caplog.text
    elif provider == "codex":
        assert "Codex stderr (exit=1): " + QUESTION[:500] in caplog.text
        assert "Codex error exit=1: " + ANSWER in caplog.text
    else:
        assert "Grok stderr: " + QUESTION[:500] in caplog.text
        assert "Grok error: " + ANSWER in caplog.text


@pytest.mark.parametrize("initial_id", ["parent-session", ""])
async def test_resumed_run_cannot_supply_the_old_questions_origin(state, agents, initial_id):
    orch, lock, session = await parent(agents)
    state.registry.get(state.task_id).origin_session_id = initial_id
    entered = asyncio.Event()

    async def answer(*args):
        entered.set()
        return await orch.answer_task_question(*args)

    state.hub.set_task_question_answerer("main", answer)
    await lock.acquire()
    pending = asyncio.create_task(
        state.hub.ask_parent(state.task_id, QUESTION, connection_alive=lambda: True)
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await finish(state)
        assert state.task_id not in state.hub._in_flight
        session.session_id = "new-parent"
        await orch._sessions.update_session(session)
        state.started, state.finish = asyncio.Event(), asyncio.Event()
        state.hub.resume(
            state.task_id,
            "Different follow up",
            chat_id=42,
            thread_id=9,
            origin_transport="tg",
            origin_session_kind="foreground",
            origin_session_id="new-parent",
        )
        new = state.hub._in_flight[state.task_id]
        state.run = new.asyncio_task
        await asyncio.wait_for(state.started.wait(), 1)
    finally:
        lock.release()
    assert await asyncio.wait_for(pending, 1) == FORWARDED
    orch._cli_service.execute.assert_not_awaited()
    assert len(await delivered(state)) == 1
    assert state.public[0][3:] == (42, 9, "tg")
    assert state.public[0][1].startswith(
        "This question was asked during an earlier run; the task has since been resumed."
    )
    assert state.public[0][1].count(QUESTION) == 1
    assert not new.has_pending_question
    current = state.registry.get(state.task_id)
    assert (current.status, current.origin_session_id) == ("running", "new-parent")
    assert current.origin_session_kind == "foreground"
    assert current.origin_transport == "tg"


async def test_first_turn_binding_within_original_run_still_answers(state, agents):
    orch, lock, _ = await parent(agents)
    orch._cli_service.execute.return_value = AgentResponse(
        result=ANSWER, session_id="parent-session"
    )
    entry = state.registry.get(state.task_id)
    entry.origin_session_id = ""
    entered = asyncio.Event()

    async def answer(*args):
        entered.set()
        return await orch.answer_task_question(*args)

    state.hub.set_task_question_answerer("main", answer)
    await lock.acquire()
    pending = asyncio.create_task(
        state.hub.ask_parent(state.task_id, QUESTION, connection_alive=lambda: True)
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        state.hub.bind_first_turn_origin("main", 42, 9, "tg", "parent-session", entry.created_at)
    finally:
        lock.release()
    assert await asyncio.wait_for(pending, 1) == "Answer from parent:\n" + ANSWER
    orch._cli_service.execute.assert_awaited_once()
    assert not state.fixed.has_pending_question
    assert await delivered(state) == []
