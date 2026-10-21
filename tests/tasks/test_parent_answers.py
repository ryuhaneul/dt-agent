"""Private task answers preserve execution ownership and unanswered questions."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.cli.types import AgentRequest, AgentResponse
from ductor_bot.messenger.matrix.bot import MatrixBot
from ductor_bot.messenger.slack.bot import SlackBot
from ductor_bot.messenger.telegram.app import TelegramBot
from ductor_bot.multiagent.internal_api import InternalAgentAPI
from ductor_bot.multiagent.supervisor import AgentSupervisor
from ductor_bot.tasks import hub as module
from ductor_bot.tasks.hub import TaskHub
from ductor_bot.tasks.models import TaskSubmit
from ductor_bot.tasks.registry import TaskRegistry

QUESTION = "private-question-sentinel\n" + "Full question detail. " * 20
ANSWER = "private-answer-sentinel"
FORWARDED = (
    "Question forwarded to parent agent. "
    "Finish your current work — you will be resumed with the answer."
)


@pytest.fixture
async def state(tmp_path):
    registry = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
    cli = MagicMock()
    current = SimpleNamespace(
        started=asyncio.Event(),
        finish=asyncio.Event(),
        results=[],
        public=[],
        response=AgentResponse(result="Task result", session_id="task-session"),
    )

    async def execute(_request):
        done = current.finish
        current.started.set()
        await done.wait()
        return current.response

    cli.execute = AsyncMock(side_effect=execute)
    process_registry = MagicMock(kill_for_task=AsyncMock())
    hub = TaskHub(
        registry,
        MagicMock(workspace=tmp_path),
        cli_service=cli,
        process_registry=process_registry,
        config=SimpleNamespace(enabled=True, max_parallel=10, timeout_seconds=3600),
    )
    hub._prepare_request = AsyncMock(return_value=AgentRequest(prompt="Work", chat_id=42))
    hub.set_result_handler("main", AsyncMock(side_effect=current.results.append))
    hub.set_question_handler(
        "main", AsyncMock(side_effect=lambda *args: current.public.append(args))
    )
    current.answerer = AsyncMock(return_value=(ANSWER, ""))
    hub.set_task_question_answerer("main", current.answerer)
    current.hub = hub
    current.registry = registry
    current.cli = cli
    current.process_registry = process_registry
    current.task_id = hub.submit(
        TaskSubmit(
            42,
            "Synthetic work",
            0,
            9,
            "main",
            name="Synthetic task",
            provider_override="claude",
            model_override="sonnet",
            origin_transport="tg",
            origin_session_kind="foreground",
            origin_session_id="parent-session",
        )
    )
    current.fixed = hub._in_flight[current.task_id]
    current.run = current.fixed.asyncio_task
    await asyncio.wait_for(current.started.wait(), 1)
    yield current
    await hub.shutdown()


async def ask(state, alive=lambda: True):
    return await state.hub.ask_parent(state.task_id, QUESTION, connection_alive=alive)


async def finish(state):
    state.finish.set()
    await asyncio.wait_for(state.run, 1)
    await asyncio.sleep(0)


async def delivered(state):
    await asyncio.sleep(0)
    return state.public


async def blocked_answer(state, *, late=False, reason="busy"):
    entered, release = asyncio.Event(), asyncio.Event()
    alive = [True]
    resolved = []

    async def answer(*args):
        entered.set()
        await release.wait()
        resolved.append(args[6]())
        if late:
            alive[0] = False
            return ANSWER, ""
        return None, reason

    state.answerer.side_effect = answer
    pending = asyncio.create_task(ask(state, lambda: alive[0]))
    await asyncio.wait_for(entered.wait(), 1)
    return pending, release, resolved


def assert_private(state, caplog):
    assert ANSWER not in caplog.text
    assert QUESTION not in caplog.text
    assert ANSWER not in repr(state.public)
    assert ANSWER not in repr(state.results)
    assert ANSWER not in state.registry._path.read_text()


async def test_answer_continues_and_finishes_done(state, caplog):
    caplog.set_level("INFO")
    assert await ask(state) == "Answer from parent:\n" + ANSWER
    assert not state.fixed.has_pending_question
    assert await delivered(state) == []
    await finish(state)
    entry = state.registry.get(state.task_id)
    assert entry.status == "done"
    assert entry.question_count == 1
    assert entry.last_question == QUESTION[:200]
    assert state.fixed.status_final
    assert "result=answered reason= handoff=none" in caplog.text
    assert_private(state, caplog)
    saved = state.registry._path.read_text()
    for field in ("run_seq", "status_final", "cancel_requested"):
        assert field not in saved


@pytest.mark.parametrize(
    "reason",
    [
        "no_session",
        "busy",
        "session_mismatch",
        "client_gone",
        "error",
        "deadline",
        "empty",
        "needs_user",
    ],
)
async def test_fallback_marks_running_task_waiting_and_hands_off_once(state, caplog, reason):
    caplog.set_level("INFO")
    state.answerer.return_value = None, reason
    assert await ask(state) == FORWARDED
    assert state.fixed.has_pending_question
    assert len(await delivered(state)) == 1
    assert QUESTION in state.public[0][1]
    assert "Resume it after this run has finished" in state.public[0][1]
    await finish(state)
    assert state.registry.get(state.task_id).status == "waiting"
    assert_private(state, caplog)


@pytest.mark.parametrize(
    "case",
    [
        "named",
        "background",
        "unknown",
        "",
        "legacy",
        "transport",
        "chat",
        "answerer",
        "inflight",
        "final",
        "disconnected",
    ],
)
async def test_ineligible_questions_do_not_execute_parent(state, case):
    entry = state.registry.get(state.task_id)
    if case in {"named", "background", "unknown", "", "legacy"}:
        entry.origin_session_kind = "" if case == "legacy" else case
    elif case == "transport":
        entry.origin_transport = ""
    elif case == "chat":
        entry.chat_id = 0
    elif case == "answerer":
        state.hub._question_answerers.clear()
    elif case == "inflight":
        state.hub._in_flight.pop(state.task_id)
    elif case == "final":
        state.fixed.status_final = True
    assert await ask(state, lambda: case != "disconnected") == FORWARDED
    state.answerer.assert_not_awaited()
    assert len(await delivered(state)) == 1
    if case == "inflight":
        state.hub._in_flight[state.task_id] = state.fixed


@pytest.mark.parametrize(
    ("bot_class", "transport"),
    [
        (TelegramBot, "tg"),
        (MatrixBot, "mx"),
        (SlackBot, "sl"),
    ],
)
async def test_late_answer_is_absent_from_public_envelope_and_results(
    state, caplog, bot_class, transport
):
    caplog.set_level("INFO")
    entry = state.registry.get(state.task_id)
    entry.origin_transport = transport
    envelopes = []
    bot = SimpleNamespace(
        _bus=SimpleNamespace(submit=AsyncMock(side_effect=envelopes.append)),
        notification_service=SimpleNamespace(notify=AsyncMock(), notify_all=AsyncMock()),
    )

    async def handler(*args):
        await bot_class.on_task_question(bot, *args)

    state.hub.set_question_handler("main", handler)
    pending, release, _ = await blocked_answer(state, late=True)
    release.set()
    assert await pending == FORWARDED
    await asyncio.sleep(0)
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert (envelope.chat_id, envelope.topic_id, envelope.transport) == (42, 9, transport)
    assert QUESTION in envelope.prompt
    assert ANSWER not in repr(envelope)
    bot.notification_service.notify.assert_not_awaited()
    bot.notification_service.notify_all.assert_not_awaited()
    await finish(state)
    assert state.registry.get(state.task_id).status == "waiting"
    assert "result=late reason=client_gone handoff=posted" in caplog.text
    assert_private(state, caplog)


@pytest.mark.parametrize("late", [False, True])
async def test_done_during_final_delivery_stays_waiting_after_delivery(state, caplog, late):
    caplog.set_level("INFO")
    delivery_started, release_delivery = asyncio.Event(), asyncio.Event()

    async def deliver(_result):
        delivery_started.set()
        await release_delivery.wait()

    state.hub.set_result_handler("main", deliver)
    pending, release_answer, _ = await blocked_answer(state, late=late)
    state.finish.set()
    await asyncio.wait_for(delivery_started.wait(), 1)
    assert state.hub._in_flight[state.task_id] is state.fixed
    assert state.fixed.status_final
    assert state.registry.get(state.task_id).status == "done"
    release_answer.set()
    assert await pending == FORWARDED
    assert state.registry.get(state.task_id).status == "waiting"
    assert not state.fixed.has_pending_question
    assert len(await delivered(state)) == 1
    release_delivery.set()
    await asyncio.wait_for(state.run, 1)
    assert state.registry.get(state.task_id).status == "waiting"
    assert_private(state, caplog)


@pytest.mark.parametrize("status", ["done", "failed", "waiting", "cancelled"])
@pytest.mark.parametrize("reason", ["busy", "needs_user"])
async def test_finished_execution_handoff_preserves_resumable_state(state, caplog, status, reason):
    caplog.set_level("INFO")
    pending, release, _ = await blocked_answer(state, reason=reason)
    if status == "failed":
        state.response = AgentResponse(result="Timeout", timed_out=True)
    elif status == "cancelled":
        state.response = AgentResponse(result="Interrupted", is_error=True, returncode=-9)
    elif status == "waiting":
        state.fixed.has_pending_question = True
    await finish(state)
    assert state.task_id not in state.hub._in_flight
    assert state.registry.get(state.task_id).status == status
    assert not state.fixed.cancel_requested
    release.set()
    assert await pending == FORWARDED
    assert state.registry.get(state.task_id).status == ("waiting" if status == "done" else status)
    assert len(await delivered(state)) == 1
    assert_private(state, caplog)


@pytest.mark.parametrize("cancel_all", [False, True])
@pytest.mark.parametrize("late", [False, True])
async def test_explicitly_cancelled_execution_discards_unanswered_question(
    state, caplog, cancel_all, late
):
    caplog.set_level("INFO")
    pending, release, _ = await blocked_answer(state, late=late)

    async def kill(_task_id):
        assert state.fixed.cancel_requested

    state.process_registry.kill_for_task.side_effect = kill
    if cancel_all:
        assert await state.hub.cancel_all(42) == 1
    else:
        assert await state.hub.cancel(state.task_id)
    assert state.fixed.status_final
    assert state.registry.get(state.task_id).status == "cancelled"
    release.set()
    assert await pending == FORWARDED
    assert await delivered(state) == []
    assert "handoff=skipped_cancelled" in caplog.text
    assert_private(state, caplog)


@pytest.mark.parametrize("late", [False, True])
async def test_new_run_is_unchanged_and_old_question_uses_old_destination(state, caplog, late):
    caplog.set_level("INFO")
    pending, release, _ = await blocked_answer(state, late=late)
    await finish(state)
    old = state.fixed
    state.started, state.finish = asyncio.Event(), asyncio.Event()
    state.hub.resume(
        state.task_id,
        "Different follow up",
        chat_id=55,
        thread_id=20,
        origin_transport="mx",
        origin_session_kind="foreground",
        origin_session_id="new-parent",
    )
    new = state.hub._in_flight[state.task_id]
    state.run = new.asyncio_task
    await asyncio.wait_for(state.started.wait(), 1)
    assert new.run_seq == old.run_seq + 1
    release.set()
    assert await pending == FORWARDED
    assert len(await delivered(state)) == 1
    assert state.public[0][3:] == (42, 9, "tg")
    assert state.public[0][1].startswith(
        "This question was asked during an earlier run; the task has since been resumed."
    )
    assert QUESTION in state.public[0][1]
    assert not old.has_pending_question
    assert not new.has_pending_question
    entry = state.registry.get(state.task_id)
    assert (entry.status, entry.chat_id, entry.thread_id) == ("running", 55, 20)
    assert_private(state, caplog)
    await finish(state)


@pytest.mark.parametrize("late", [False, True])
async def test_deleted_record_is_not_recreated_or_handed_off(state, caplog, late):
    caplog.set_level("INFO")
    pending, release, resolved = await blocked_answer(state, late=late)
    await finish(state)
    assert state.registry.delete(state.task_id)
    release.set()
    assert await pending == FORWARDED
    assert resolved == ["parent-session"]
    assert state.registry.get(state.task_id) is None
    assert await delivered(state) == []
    assert "handoff=skipped_deleted" in caplog.text
    assert_private(state, caplog)


async def test_handoff_runs_after_private_lock_is_released(state, tmp_path):
    from ductor_bot.bus.bus import MessageBus
    from ductor_bot.config import AgentConfig
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.session.key import SessionKey
    from ductor_bot.workspace.paths import DuctorPaths

    home = tmp_path / "parent"
    home.mkdir()
    parent = Orchestrator(AgentConfig(allowed_user_ids=[42], model="sonnet"), DuctorPaths(home))
    bus = MessageBus()
    parent.wire_observers_to_bus(bus)
    key = SessionKey("tg", 42, 9)
    session, _ = await parent._sessions.resolve_session(key, provider="claude", model="sonnet")
    session.session_id = "parent-session"
    await parent._sessions.update_session(session)
    parent._cli_service.execute = AsyncMock(
        return_value=AgentResponse(
            result="NEEDS_USER",
            session_id="parent-session",
        )
    )
    state.hub.set_task_question_answerer("main", parent.answer_task_question)

    async def handoff(*args):
        assert not bus.lock_pool.get(key.lock_key).locked()
        state.public.append(args)

    state.hub.set_question_handler("main", handoff)
    assert await ask(state) == FORWARDED
    assert len(await delivered(state)) == 1
    parent._cli_service.execute.assert_awaited_once()


async def test_origin_session_id_is_read_when_the_answerer_resolves_it(state):
    entry = state.registry.get(state.task_id)
    entry.origin_session_id = ""

    async def answer(*args):
        entry.origin_session_id = "bound-parent-session"
        assert args[6]() == "bound-parent-session"
        return ANSWER, ""

    state.answerer.side_effect = answer
    assert await ask(state) == "Answer from parent:\n" + ANSWER


async def test_questions_serialize_without_resetting_the_second_deadline(state, monkeypatch):
    monkeypatch.setattr(module, "_QUESTION_TIMEOUT", 0.12)
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def answer(*args):
        seen.append((args[-1], asyncio.get_running_loop().time()))
        if len(seen) == 1:
            entered.set()
            await release.wait()
        return ANSWER, ""

    state.answerer.side_effect = answer
    first = asyncio.create_task(ask(state))
    await asyncio.wait_for(entered.wait(), 1)
    second_received = asyncio.get_running_loop().time()
    second = asyncio.create_task(ask(state))
    await asyncio.sleep(0.05)
    assert len(seen) == 1
    release.set()
    assert await first == "Answer from parent:\n" + ANSWER
    assert await second == "Answer from parent:\n" + ANSWER
    assert seen[1][0] - second_received == pytest.approx(0.12, abs=0.02)
    assert seen[1][0] - seen[1][1] < 0.09
    assert state.registry.get(state.task_id).question_count == 2


async def test_task_lock_wait_uses_intake_deadline_and_preserves_owner(state, monkeypatch, caplog):
    monkeypatch.setattr(module, "_QUESTION_TIMEOUT", 0.02)
    caplog.set_level("INFO")
    lock = asyncio.Lock()
    state.hub._question_locks[state.task_id] = lock
    await lock.acquire()
    try:
        assert await ask(state) == FORWARDED
        assert lock.locked()
        state.answerer.assert_not_awaited()
        assert "reason=deadline handoff=posted" in caplog.text
    finally:
        lock.release()
    assert len(await delivered(state)) == 1


async def test_parent_error_and_deadline_fall_back_without_leaking_error_text(
    state, monkeypatch, caplog
):
    caplog.set_level("INFO")
    state.answerer.side_effect = RuntimeError(ANSWER)
    assert await ask(state) == FORWARDED
    assert "reason=error" in caplog.text
    monkeypatch.setattr(module, "_QUESTION_TIMEOUT", 0.02)
    cancelled = asyncio.Event()

    async def block(*_args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    state.answerer.side_effect = block
    assert await ask(state) == FORWARDED
    assert cancelled.is_set()
    assert not state.hub._question_locks[state.task_id].locked()
    assert_private(state, caplog)


@pytest.mark.parametrize("transport", [None, SimpleNamespace(is_closing=lambda: True)])
async def test_api_disconnected_on_entry_skips_parent(state, transport):
    request = SimpleNamespace(
        json=AsyncMock(return_value={"task_id": state.task_id, "question": QUESTION}),
        transport=transport,
    )
    api = InternalAgentAPI(bus=None, port=0)
    api.set_task_hub(state.hub)
    response = await api._handle_task_ask_parent(request)
    payload = json.loads(response.text)
    assert payload == {"success": True, "answer": FORWARDED}
    state.answerer.assert_not_awaited()
    assert len(await delivered(state)) == 1


async def test_waiting_http_question_does_not_block_other_requests(state, aiohttp_client):
    entered, release = asyncio.Event(), asyncio.Event()

    async def answer(*_args):
        entered.set()
        await release.wait()
        return ANSWER, ""

    state.answerer.side_effect = answer
    api = InternalAgentAPI(bus=None, port=0)
    api.set_task_hub(state.hub)
    client = await aiohttp_client(api._app)
    pending = asyncio.create_task(
        client.post(
            "/tasks/ask_parent",
            json={"task_id": state.task_id, "question": QUESTION},
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    other = await asyncio.wait_for(client.get("/tasks/list"), 1)
    assert other.status == 200
    assert not pending.done()
    release.set()
    response = await pending
    assert (await response.json())["answer"] == "Answer from parent:\n" + ANSWER


@pytest.mark.parametrize("name", ["main", "worker"])
def test_supervisor_wires_private_answerer_for_every_stack(name):
    hub = MagicMock()
    orch = MagicMock()
    stack = SimpleNamespace(
        name=name,
        bot=SimpleNamespace(
            orchestrator=orch, on_task_result=AsyncMock(), on_task_question=AsyncMock()
        ),
        paths=MagicMock(),
        config=SimpleNamespace(claude_system_prompt_snapshot="off", allowed_user_ids=[]),
    )
    AgentSupervisor._wire_task_hub(SimpleNamespace(_task_hub=hub), stack)
    hub.set_task_question_answerer.assert_called_once_with(name, orch.answer_task_question)


@pytest.mark.parametrize("answer", ["Answer from parent:\n" + ANSWER, FORWARDED])
def test_tool_waits_with_server_timeout_and_prints_server_answer(monkeypatch, capsys, answer):
    path = (
        Path(__file__).resolve().parents[2]
        / "ductor_bot/_home_defaults/workspace/tools/task_tools/ask_parent.py"
    )
    spec = importlib.util.spec_from_file_location("task_parent_tool", path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    captured = []

    def post(_url, body, *, timeout):
        captured.append((body, timeout))
        return {"success": True, "answer": answer}

    monkeypatch.setattr(tool, "_load_shared", lambda: (lambda _path: "unused", post))
    monkeypatch.setattr(tool, "_load_parser", lambda: lambda *_a, **_kw: ({}, [QUESTION]))
    monkeypatch.setenv("DUCTOR_TASK_ID", "sample-task")
    tool.main()
    assert captured == [({"task_id": "sample-task", "question": QUESTION}, 285)]
    assert capsys.readouterr().out == answer + "\n"
