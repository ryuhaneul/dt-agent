"""Bridge regressions using real application functions and scheduling barriers.

These tests reproduce failures present before the fixes.
Only temporary paths are used; no TCP listeners or real Telegram calls.
"""

import asyncio
import inspect
import json
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiohttp.test_utils import make_mocked_request

from ductor_bot.bus.adapters import from_heartbeat
from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.chat_log.events import EventHub
from ductor_bot.chat_log.history import Entry, HistoryStore
from ductor_bot.chat_log.outbox import Outbox
from ductor_bot.chat_log.recorder import HistoryRecorder
from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.cli.types import AgentRequest, AgentResponse
from ductor_bot.config import AgentConfig
from ductor_bot.messenger.telegram.topic import TopicNameCache
from ductor_bot.messenger.telegram.transport import TelegramTransport
from ductor_bot.multiagent.shared_knowledge import _sync_agent_io
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.flows import _finalize_turn
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.session.key import SessionKey
from ductor_bot.session.manager import SessionManager
from ductor_bot.webchat import files as files_module
from ductor_bot.webchat.bridge import create_bridge_app
from ductor_bot.webchat.files import WebFileError, WebFiles, _split_shared
from ductor_bot.webchat.topics import WebTopics
from ductor_bot.webchat.turns import WebTurns
from ductor_bot.workspace.paths import DuctorPaths


def harness(tmp_path):
    paths = DuctorPaths(ductor_home=tmp_path)
    config = AgentConfig(allowed_user_ids=[-123], language="en")
    hub = EventHub()
    sessions = SessionManager(tmp_path / "sessions.json", config)
    registry = ProcessRegistry()
    runtime = SimpleNamespace(
        events=hub,
        history=HistoryStore(paths.chat_log_dir / "history", hub),
        outbox=Outbox(paths.chat_log_dir / "outbox", hub),
        drainer=SimpleNamespace(wake=MagicMock()),
        ops=SimpleNamespace(delete_topic=AsyncMock(return_value=True)),
    )
    runtime.recorder = HistoryRecorder(runtime.history, hub)
    orch = SimpleNamespace(
        paths=paths,
        _sessions=sessions,
        _lock_pool=LockPool(),
        _process_registry=registry,
        _history_recorder=runtime.recorder,
        _inflight_tracker=SimpleNamespace(shutting_down=False),
        _observers=SimpleNamespace(heartbeat=SimpleNamespace(notify_activity=MagicMock())),
        is_chat_busy=lambda *_: False,
    )
    bot = SimpleNamespace(config=config, _orch=orch, _topic_names=TopicNameCache())
    return paths, bot, runtime


@pytest.mark.asyncio
async def test_aborted_partial_reply_is_persisted(tmp_path):
    """Persist an aborted response through core, finalize, recorder and WebTurns."""
    _, bot, runtime = harness(tmp_path)
    orch = bot._orch
    key = SessionKey.telegram(-123, 7)
    session, _ = await orch._sessions.resolve_session(key)
    started, finish = asyncio.Event(), asyncio.Event()

    async def route(dispatch):
        await dispatch.on_text_delta("PARTIAL_REPLY")
        started.set()
        await finish.wait()
        return await _finalize_turn(
            orch,
            key,
            AgentRequest(prompt="hello"),
            session,
            AgentResponse(result="PARTIAL_REPLY"),
            flow_label="Streaming",
        )

    async def kill(chat_id, topic_id):
        # Set the state that kill_by_chat_topic leaves after terminating a process.
        orch._process_registry._aborted_topics.add((chat_id, topic_id))
        finish.set()
        return 1

    orch._route_message = route
    orch._process_registry.kill_by_chat_topic = kill
    orch._handle_message_impl = MethodType(Orchestrator._handle_message_impl, orch)
    orch.handle_message_streaming = MethodType(Orchestrator.handle_message_streaming, orch)
    turns = WebTurns(bot, runtime)
    turn_id = turns.submit("-123.7", "hello")
    task = turns._tasks["-123.7"]
    await asyncio.wait_for(started.wait(), 2)
    await turns.abort("-123.7")
    await asyncio.wait_for(task, 2)
    entries = await runtime.history.read("-123.7")
    pending = await runtime.outbox.pending()
    reply = next(item for item in pending if item.kind == "reply_mirror")
    assert "PARTIAL_REPLY" in reply.text
    assert any(
        entry.id == reply.history_id
        and entry.turn_id == turn_id
        and entry.text == "PARTIAL_REPLY"
        and entry.status == "aborted"
        for entry in entries
    ), f"reply_mirror={reply.text!r}; history={entries!r}"


@pytest.mark.asyncio
async def test_other_topic_save_cannot_resurrect_deleted_session(tmp_path, monkeypatch):
    """Race a resolve_session snapshot save against delete_session."""
    _, bot, runtime = harness(tmp_path)
    sessions = bot._orch._sessions
    key = SessionKey.telegram(-123, 7)
    await sessions.resolve_session(key)
    turns = SimpleNamespace(blocked=set(), abort=AsyncMock(return_value=0))
    topics = WebTopics(bot, runtime, turns)
    captured, release = asyncio.Event(), asyncio.Event()
    original = sessions._save

    async def delayed_save(rows):
        if asyncio.current_task().get_name() == "other-topic-turn":
            captured.set()
            await release.wait()
        await original(rows)

    monkeypatch.setattr(sessions, "_save", delayed_save)
    other = asyncio.create_task(
        sessions.resolve_session(SessionKey.telegram(-123, 8)),
        name="other-topic-turn",
    )
    await asyncio.wait_for(captured.wait(), 2)
    try:
        deletion = await topics.delete("-123.7")
        assert deletion["telegram_deleted"] and deletion["session_removed"]  # noqa: PT018
        assert await sessions.get_active(key) is None
    finally:
        release.set()
        await asyncio.wait_for(other, 2)
    assert await sessions.get_active(key) is None, (
        "A successful deletion was undone by another topic saving its session"
    )


@pytest.mark.asyncio
async def test_mainmemory_save_cannot_restore_stale_shared_block(tmp_path, monkeypatch):
    """Reject a sync update before replacement without writing the web draft."""
    paths = DuctorPaths(ductor_home=tmp_path)
    memory = paths.mainmemory_path
    memory.parent.mkdir(parents=True)
    shared = paths.ductor_home / "SHAREDMEMORY.md"
    shared.write_text("old shared\n")
    memory.write_text(
        "personal\n\n--- SHARED KNOWLEDGE START ---\nold shared\n--- SHARED KNOWLEDGE END ---\n"
    )
    editor = WebFiles(paths)
    base = (await editor.get("MAINMEMORY.md"))["sha256"]
    original = files_module.atomic_bytes_save

    writes = 0

    def concurrent_sync(path, data):
        nonlocal writes
        original(path, data)
        if path == memory:
            writes += 1
        else:
            # Backup I/O finishes before the final conflict check.
            shared.write_text("NEW SHARED CONTENT\n")
            assert _sync_agent_io(shared, memory)
            assert "NEW SHARED CONTENT" in memory.read_text()

    monkeypatch.setattr(files_module, "atomic_bytes_save", concurrent_sync)
    with pytest.raises(WebFileError) as caught:
        await editor.put("MAINMEMORY.md", "edited personal", base)
    assert caught.value.status == 409
    assert writes == 0
    assert "NEW SHARED CONTENT" in memory.read_text()
    personal, _ = _split_shared(memory.read_text())
    assert personal.strip() == "personal"


@pytest.mark.parametrize("delete_fails", [False, True])
async def test_late_bus_delivery_cannot_recreate_deleted_topic(tmp_path, delete_fails):
    """Complete an in-flight heartbeat after topic deletion or deletion failure."""
    _, bot, runtime = harness(tmp_path)
    recorder = bot._orch._history_recorder
    runtime.recorder = recorder
    key = SessionKey.telegram(-123, 7)
    await bot._orch._sessions.resolve_session(key)
    await runtime.history.append(key, Entry(role="user", source="telegram", text="old"))
    started, release = asyncio.Event(), asyncio.Event()

    async def send(**_kwargs):
        started.set()
        await release.wait()
        raise TelegramNetworkError(MagicMock(), "Synthetic network outage")

    bot.bot_instance = SimpleNamespace(send_message=send)
    bot.file_roots = lambda _paths: [tmp_path]
    transport = TelegramTransport(bot)
    transport.set_outbox(runtime.outbox, is_blocked=recorder.blocked.__contains__)
    bus = MessageBus()
    bus.register_transport(transport)
    bus.set_delivery_observer(recorder.on_delivered)
    turns = WebTurns(bot, runtime)
    topics = WebTopics(bot, runtime, turns)
    if delete_fails:
        runtime.ops.delete_topic.side_effect = ConnectionError("Synthetic deletion failure")
    delivery = asyncio.create_task(bus.submit(from_heartbeat(-123, "alert", topic_id=7)))
    await asyncio.wait_for(started.wait(), 2)
    try:
        if delete_fails:
            with pytest.raises(ConnectionError):
                await topics.delete("-123.7")
        else:
            deletion = await topics.delete("-123.7")
            assert deletion["telegram_deleted"]
            assert await runtime.history.count("-123.7") == 0
            assert not await runtime.outbox.has_pending()
    finally:
        release.set()
        await asyncio.wait_for(delivery, 2)
    rows = await runtime.history.read("-123.7")
    queued = await runtime.outbox.pending()
    if delete_fails:
        assert len(rows) == 2
        assert len(queued) == 1
        assert "-123.7" not in recorder.blocked
        assert "-123.7" not in turns.blocked
    else:
        assert rows == []
        assert queued == []
        assert "-123.7" in recorder.blocked
        assert "-123.7" in turns.blocked


async def seed_memory(tmp_path):
    paths = DuctorPaths(ductor_home=tmp_path)
    memory = paths.mainmemory_path
    memory.parent.mkdir(parents=True)
    shared = paths.ductor_home / "SHAREDMEMORY.md"
    shared.write_text("shared 0\n")
    memory.write_text(
        "ORIGINAL PERSONAL\n\n--- SHARED KNOWLEDGE START ---\n"
        "shared 0\n--- SHARED KNOWLEDGE END ---\n"
    )
    editor = WebFiles(paths)
    base = (await editor.get("MAINMEMORY.md"))["sha256"]
    return editor, memory, shared, base


@pytest.mark.asyncio
async def test_retry_preserves_external_personal_update(tmp_path, monkeypatch):
    editor, memory, shared, base = await seed_memory(tmp_path)
    render = editor._memory_bytes
    calls = 0

    def concurrent_update(content):
        nonlocal calls
        calls += 1
        if calls == 3:
            # Initial validation and the first write have finished. The first
            # iteration has already read `latest`; shared-source verification follows.
            assert "WEB DRAFT" in memory.read_text()
            shared.write_text("shared 1\n")
            memory.write_text(
                "EXTERNAL PERSONAL UPDATE\n\n--- SHARED KNOWLEDGE START ---\n"
                "shared 0\n--- SHARED KNOWLEDGE END ---\n"
            )
            assert _sync_agent_io(shared, memory)
            assert "EXTERNAL PERSONAL UPDATE" in memory.read_text()
        return render(content)

    monkeypatch.setattr(editor, "_memory_bytes", concurrent_update)
    try:
        await editor.put("MAINMEMORY.md", "WEB DRAFT", base)
    except WebFileError as exc:
        assert exc.status == 409  # noqa: PT017
    if calls >= 3:
        assert "EXTERNAL PERSONAL UPDATE" in memory.read_text(), (
            "The new retry overwrote a personal update made after the first write"
        )
    else:
        assert calls == 1
        assert "WEB DRAFT" in memory.read_text()


@pytest.mark.asyncio
async def test_rejected_put_does_not_commit_personal_draft(tmp_path, monkeypatch):
    editor, memory, shared, base = await seed_memory(tmp_path)
    save = files_module.atomic_bytes_save
    writes = 0

    def concurrent_shared_update(path, data):
        nonlocal writes
        save(path, data)
        if path == memory:
            writes += 1
            # A different writer changes the shared source during every attempt.
            shared.write_text(f"shared {writes}\n")

    monkeypatch.setattr(files_module, "atomic_bytes_save", concurrent_shared_update)
    try:
        await editor.put("MAINMEMORY.md", "DRAFT REPORTED UNSAVED", base)
    except WebFileError as exc:
        assert exc.status == 409  # noqa: PT017
        personal, _ = _split_shared(memory.read_text())
        assert personal.strip() == "ORIGINAL PERSONAL", (
            f"PUT returned 409 after committing the draft: {personal.strip()!r}"
        )
    else:
        assert writes == 1
        personal, _ = _split_shared(memory.read_text())
        assert personal.strip() == "DRAFT REPORTED UNSAVED"
        assert _sync_agent_io(shared, memory)
        assert "shared 1" in memory.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["abort", "shutdown"])
async def test_next_preparing_turn_does_not_inherit_completed_state(tmp_path, monkeypatch, operation):
    """Stop and shutdown cancel a replacement turn before provider startup."""
    _, bot, runtime = harness(tmp_path)
    tid = "-123.7"
    await bot._orch._sessions.resolve_session(SessionKey.telegram(-123, 7))
    app = create_bridge_app(bot, runtime, "ephemeral-test-token")
    app.freeze()
    route = next(
        route
        for route in app.router.routes()
        if route.method == "POST" and route.resource.canonical.endswith("/{tid}/messages")
    )
    state = inspect.getclosurevars(route.handler).nonlocals
    turns, topics = state["turns"], state["topics"]
    first_ready, finish_first = asyncio.Event(), asyncio.Event()
    second_lookup, release_lookup = asyncio.Event(), asyncio.Event()
    second_ready, finish_second = asyncio.Event(), asyncio.Event()
    original_lookup = topics.lookup
    lookup_count = 0
    executed = []

    async def lookup(target):
        nonlocal lookup_count
        result = await original_lookup(target)
        lookup_count += 1
        if lookup_count == 2:
            # The second request has finished its reads but resumes immediately
            # after the old turn ends, before its Task done callback is serviced.
            second_lookup.set()
            await release_lookup.wait()
        return result

    monkeypatch.setattr(topics, "lookup", lookup)
    publish = runtime.events.publish

    def observe_completion(event):
        publish(event)
        if event.get("type") == "turn" and event.get("phase") == "done":
            release_lookup.set()

    monkeypatch.setattr(runtime.events, "publish", observe_completion)

    async def handle(key, text, **_callbacks):
        if text == "first":
            first_ready.set()
            await finish_first.wait()
        else:
            second_ready.set()
            await finish_second.wait()
            executed.append("second provider started")
        dispatch = SimpleNamespace(
            key=key, text=text, transport_override=None, counts_as_activity=True
        )
        await runtime.recorder.begin(dispatch)
        result = OrchestratorResult(text="answer " + text)
        await runtime.recorder.finish(dispatch, result, status="ok")
        return result

    monkeypatch.setattr(bot._orch, "handle_message_streaming", handle, raising=False)

    async def request(suffix, text=None):
        req = make_mocked_request(
            "POST",
            f"/webchat/v1/topics/{tid}/{suffix}",
            headers={"X-Dtw-Bridge-Token": "ephemeral-test-token"},
            app=app,
        )
        req._read_bytes = json.dumps({"text": text, "client_id": "browser"}).encode()
        return await app._handle(req)

    assert (await request("messages", "first")).status == 202
    await asyncio.wait_for(first_ready.wait(), 2)
    second_request = asyncio.create_task(request("messages", "second"))
    await asyncio.wait_for(second_lookup.wait(), 2)
    finish_first.set()
    assert (await asyncio.wait_for(second_request, 2)).status == 202
    await asyncio.wait_for(second_ready.wait(), 2)
    second = turns._tasks[tid]
    stopping = None
    try:
        if operation == "abort":
            response = await request("abort")
            assert response.status == 200
            assert json.loads(response.text) == {"killed": 0}
            assert not turns.busy(tid), "HTTP Stop left the second preparing turn running"
        else:
            stopping = asyncio.create_task(turns.stop())
            try:
                await asyncio.wait_for(asyncio.shield(stopping), 2)
                finished = True
            except TimeoutError:
                finished = False
            assert finished, "Shutdown waited for a preparing turn instead of cancelling it"
        assert not executed
    finally:
        finish_second.set()
        await asyncio.gather(second, return_exceptions=True)
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        await app.shutdown()
        await app.cleanup()
