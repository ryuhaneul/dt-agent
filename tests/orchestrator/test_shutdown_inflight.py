"""A foreground turn cut off by a shutdown is announced once at the next start."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig
from ductor_bot.infra.recovery import RecoveryPlanner
from ductor_bot.infra.restart import (
    consume_restart_marker,
    write_restart_marker,
    write_restart_sentinel,
)
from ductor_bot.messenger.matrix import startup as matrix_startup
from ductor_bot.messenger.slack import startup as slack_startup
from ductor_bot.messenger.telegram import startup as telegram_startup
from ductor_bot.multiagent.supervisor import AgentSupervisor
from ductor_bot.orchestrator import flows
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.lifecycle import begin_shutdown
from ductor_bot.session.key import SessionKey
from ductor_bot.session.named import NamedSession
from ductor_bot.workspace.paths import DuctorPaths

TEXT = "original text of the user"
CHAT = 1


class GatedCli:
    """Synthetic CLI: a turn runs until the test lets it end."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self._release = asyncio.Event()
        self._response = AgentResponse(result="answer", session_id="sid")

    async def _run(self, *_args: object, **_kwargs: object) -> AgentResponse:
        self.started.set()
        await self._release.wait()
        return self._response

    execute = _run
    execute_streaming = _run

    def end(self, **fields: object) -> None:
        self._response = AgentResponse(session_id="sid", **{"result": "", **fields})  # type: ignore[arg-type]
        self._release.set()


async def _create(paths: DuctorPaths, config: AgentConfig, name: str = "main") -> Orchestrator:
    """The production factory (it registers the restart hook), with its probes stubbed."""
    auth = {"claude": AuthResult("claude", AuthStatus.AUTHENTICATED)}
    with (
        patch("ductor_bot.orchestrator.lifecycle.resolve_paths", return_value=paths),
        patch("ductor_bot.cli.auth.check_all_auth", return_value=auth),
        patch("ductor_bot.orchestrator.observers.watch_rule_files", new_callable=AsyncMock),
    ):
        return await Orchestrator.create(config, agent_name=name)


def _install(orch: Orchestrator) -> GatedCli:
    cli = GatedCli()
    object.__setattr__(orch, "_cli_service", cli)
    return cli


@pytest.fixture(autouse=True)
def _authenticated():
    ok = AuthResult("claude", AuthStatus.AUTHENTICATED)
    with patch("ductor_bot.cli.auth.check_provider_auth", return_value=ok):
        yield


@pytest.fixture
async def main_orch(workspace: tuple[DuctorPaths, AgentConfig]):
    paths, config = workspace
    orch = await _create(paths, config)
    yield orch
    await orch.shutdown()


class Spy:
    """Whether the shutdown mark was already set whenever a turn's ``finally`` ran."""

    def __init__(self, orch: Orchestrator) -> None:
        self.marked_at_finally: list[bool] = []
        tracker = orch.inflight_tracker
        real = tracker.keep_interrupted

        def keep(chat_id: int, topic_id: int | None = None) -> bool:
            self.marked_at_finally.append(tracker.shutting_down)
            return real(chat_id, topic_id)

        tracker.keep_interrupted = keep  # type: ignore[method-assign]


def _notices(orch: Orchestrator):
    inflight = orch.inflight_tracker
    return RecoveryPlanner(inflight=inflight, named_sessions=[], max_age_seconds=3600).plan()


async def _turn(orch: Orchestrator, flow: str):
    cli = _install(orch)
    run = flows.normal if flow == "normal" else flows.normal_streaming
    task = asyncio.create_task(run(orch, SessionKey(chat_id=CHAT), TEXT))
    await asyncio.wait_for(cli.started.wait(), 5)
    return cli, task


async def _finish(task: asyncio.Task) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


async def _killed_by_shutdown(orch: Orchestrator, cli: GatedCli, task: asyncio.Task) -> None:
    """What the shutdown does to the CLI: kill it (the registry notes it as aborted)."""
    await orch._process_registry.kill_all(CHAT)
    cli.end(is_error=True)
    await _finish(task)


# -- the shutdown triggers, each marking before it cancels or kills -----------------------


def _supervisor(orch: Orchestrator, name: str, turn: asyncio.Task | None) -> AgentSupervisor:
    sup = AgentSupervisor.__new__(AgentSupervisor)
    stack = MagicMock()
    stack.bot.orchestrator = orch
    stack.shutdown = AsyncMock()
    sup._stacks = {name: stack}
    sup._tasks = {name: turn} if turn else {}
    sup._health = {}
    sup._bus = None
    sup._cancel_redeliveries = AsyncMock()  # type: ignore[method-assign]
    sup._redelivery_scan_task = None
    sup._watcher = MagicMock(stop=AsyncMock())
    sup._shared_knowledge = None
    sup._task_hub = None
    sup._internal_api = None
    sup._agents_lock = asyncio.Lock()
    sup._stopping = False
    sup._stopping_names = set()
    sup._notice_tasks = {}
    return sup


async def _trigger_sentinel(orch: Orchestrator, _turn_task: asyncio.Task) -> None:
    write_restart_sentinel(CHAT, "restart", sentinel_path=orch.paths.ductor_home / "s.json")


async def _trigger_marker_write(orch: Orchestrator, _turn_task: asyncio.Task) -> None:
    write_restart_marker(marker_path=orch.paths.ductor_home / "restart-requested")


async def _trigger_marker_consume(orch: Orchestrator, _turn_task: asyncio.Task) -> None:
    marker = orch.paths.ductor_home / "restart-requested"
    marker.write_text("1")
    assert consume_restart_marker(marker_path=marker)


async def _trigger_stop_all(orch: Orchestrator, turn_task: asyncio.Task) -> None:
    await _supervisor(orch, "main", turn_task).stop_all()  # cancels the main task


async def _trigger_stop_agent(orch: Orchestrator, turn_task: asyncio.Task) -> None:
    await _supervisor(orch, "sub", turn_task).stop_agent("sub")  # cancels the sub's task


async def _trigger_sub_restart(orch: Orchestrator, _turn_task: asyncio.Task) -> None:
    sup = _supervisor(orch, "sub", None)
    sup._rebuild_stack = AsyncMock()  # type: ignore[method-assign]
    stack = sup._stacks["sub"]

    async def shutdown() -> None:  # the stack's shutdown kills the CLI
        await orch._process_registry.kill_all(CHAT)

    stack.shutdown = shutdown
    await sup._handle_restart_exit("sub", stack, MagicMock())


TRIGGERS = [
    pytest.param(_trigger_sentinel, id="restart-sentinel"),
    pytest.param(_trigger_marker_write, id="restart-marker-write"),
    pytest.param(_trigger_marker_consume, id="restart-marker-consume"),
    pytest.param(_trigger_stop_all, id="supervisor-stop-all"),
    pytest.param(_trigger_stop_agent, id="supervisor-stop-agent"),
    pytest.param(_trigger_sub_restart, id="sub-restart"),
]


@pytest.mark.parametrize("flow", ["normal", "streaming"])
@pytest.mark.parametrize("trigger", TRIGGERS)
async def test_interrupted_turn_is_announced_once(main_orch, trigger, flow) -> None:
    spy = Spy(main_orch)
    cli, task = await _turn(main_orch, flow)

    await trigger(main_orch, task)  # may already cancel the turn
    if not task.done():
        await _killed_by_shutdown(main_orch, cli, task)
    await _finish(task)

    assert spy.marked_at_finally == [True]
    actions = _notices(main_orch)
    assert [(a.chat_id, a.kind, a.prompt_preview) for a in actions] == [(CHAT, "foreground", TEXT)]


@pytest.mark.parametrize("flow", ["normal", "streaming"])
async def test_sigterm_marks_before_the_cancel(
    main_orch, monkeypatch: pytest.MonkeyPatch, flow: str
) -> None:
    from ductor_bot import __main__ as entry

    events: list[str] = []
    spy = Spy(main_orch)
    cli = _install(main_orch)
    run = flows.normal if flow == "normal" else flows.normal_streaming

    class FakeSupervisor:
        begin_shutdown = AgentSupervisor.begin_shutdown

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            stack = MagicMock()
            stack.bot.orchestrator = main_orch
            self._stacks = {"main": stack}

        async def start(self) -> int:
            try:
                return int(bool(await run(main_orch, SessionKey(chat_id=CHAT), TEXT)))
            except asyncio.CancelledError:
                events.append("cancelled")
                raise

        async def stop_all(self) -> None:
            events.append("stop_all")

    real_begin = FakeSupervisor.begin_shutdown

    def marking(self: FakeSupervisor, name: str | None = None) -> None:
        events.append("mark")
        real_begin(self, name)

    FakeSupervisor.begin_shutdown = marking  # type: ignore[method-assign,assignment]
    monkeypatch.setattr("ductor_bot.multiagent.supervisor.AgentSupervisor", FakeSupervisor)
    monkeypatch.setattr(entry, "_validate_transports", lambda _config: None)
    monkeypatch.setattr("ductor_bot.infra.pidlock.acquire_instance_lock", lambda _path: None)
    monkeypatch.setattr("ductor_bot.infra.pidlock.acquire_lock", lambda **_kw: None)
    monkeypatch.setattr(entry, "atomic_bytes_save", lambda *_args: None)
    monkeypatch.setattr("ductor_bot.infra.pidlock.release_lock", lambda **_kw: None)

    running = asyncio.create_task(entry.run_bot(AgentConfig()))
    await asyncio.wait_for(cli.started.wait(), 5)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(running, 5)

    assert events[:3] == ["mark", "cancelled", "stop_all"]
    assert spy.marked_at_finally == [True]
    assert [(a.chat_id, a.prompt_preview) for a in _notices(main_orch)] == [(CHAT, TEXT)]


async def test_sub_agent_foreground_turn_is_announced_too(
    tmp_path: Path,
    main_orch,
) -> None:
    home = tmp_path / "sub-home"
    home.mkdir()
    sub = Orchestrator(AgentConfig(), DuctorPaths(home), agent_name="sub")
    spy = Spy(sub)
    cli, task = await _turn(sub, "normal")

    await _trigger_stop_agent(sub, task)
    await _finish(task)

    assert spy.marked_at_finally == [True]
    assert [(a.chat_id, a.prompt_preview) for a in _notices(sub)] == [(CHAT, TEXT)]
    assert _notices(main_orch) == []  # another agent's home is untouched
    del cli


# -- no notice ---------------------------------------------------------------------------


@pytest.mark.parametrize("flow", ["normal", "streaming"])
async def test_user_stop_is_not_announced(main_orch, flow) -> None:
    cli, task = await _turn(main_orch, flow)

    await main_orch.abort(CHAT)  # the user's /stop kills the CLI
    cli.end(is_error=True)
    await _finish(task)

    assert _notices(main_orch) == []


@pytest.mark.parametrize("flow", ["normal", "streaming"])
async def test_user_stop_followed_by_shutdown_is_not_announced(main_orch, flow) -> None:
    cli, task = await _turn(main_orch, flow)

    await main_orch.abort(CHAT)  # stopped by the user first ...
    begin_shutdown(main_orch)  # ... then the shutdown starts before the turn unwinds
    cli.end(is_error=True)
    await _finish(task)

    assert _notices(main_orch) == []


@pytest.mark.parametrize("flow", ["normal", "streaming"])
async def test_normal_completion_is_not_announced(main_orch, flow) -> None:
    cli, task = await _turn(main_orch, flow)

    cli.end(result="answer")
    await _finish(task)

    assert _notices(main_orch) == []


@pytest.mark.parametrize("flow", ["normal", "streaming"])
async def test_completion_during_shutdown_is_not_announced(main_orch, flow) -> None:
    cli, task = await _turn(main_orch, flow)

    begin_shutdown(main_orch)
    cli.end(result="answer")  # the turn finished by itself
    await _finish(task)

    assert _notices(main_orch) == []


@pytest.mark.parametrize("flow", ["normal", "streaming"])
async def test_ordinary_error_is_not_announced(main_orch, flow) -> None:
    cli, task = await _turn(main_orch, flow)

    cli.end(is_error=True, result="boom")
    await _finish(task)

    assert _notices(main_orch) == []


# -- next start --------------------------------------------------------------------------


def _bot(orch: Orchestrator, notify: AsyncMock) -> SimpleNamespace:
    async def notify_origin(
        _transport: str, chat_id: int, text: str, *, topic_id: int | None = None
    ) -> bool:
        await notify(chat_id, text)  # the Telegram startup announces through notify_origin
        return True

    return SimpleNamespace(
        _orchestrator=orch,
        _orch=orch,
        _config=SimpleNamespace(timeouts=SimpleNamespace(normal=600)),
        config=SimpleNamespace(timeouts=SimpleNamespace(normal=600)),
        notification_service=SimpleNamespace(
            notify=notify, notify_origin=notify_origin, transport_name="tg"
        ),
        notify_startup=AsyncMock(),
    )


async def _recover(messenger: str, bot: SimpleNamespace) -> None:
    if messenger == "telegram":
        await telegram_startup._handle_recovery(bot, None)
    elif messenger == "matrix":
        await matrix_startup._handle_recovery(bot)
    else:
        await slack_startup._handle_recovery(bot)


async def _interrupt(orch: Orchestrator) -> None:
    cli, task = await _turn(orch, "normal")
    begin_shutdown(orch)
    await _killed_by_shutdown(orch, cli, task)


@pytest.mark.parametrize("messenger", ["telegram", "matrix", "slack"])
async def test_next_start_announces_once_with_the_original_text(
    workspace: tuple[DuctorPaths, AgentConfig], main_orch, messenger: str
) -> None:
    await _interrupt(main_orch)
    paths, config = workspace

    # Two starts in a row: only the first one hears about the interruption.
    notes: list[tuple[int, str]] = []
    for _ in range(2):
        restarted = Orchestrator(config, paths)
        notify = AsyncMock(side_effect=lambda chat, text: notes.append((chat, text)))
        bot = _bot(restarted, notify)
        restarted.submit_named_followup_bg = MagicMock()  # type: ignore[method-assign]
        await _recover(messenger, bot)
        restarted.submit_named_followup_bg.assert_not_called()  # no automatic re-run

    assert len(notes) == 1
    chat, text = notes[0]
    assert chat == CHAT
    assert TEXT in text
    assert "[Delegated" not in text


@pytest.mark.parametrize("messenger", ["telegram", "matrix", "slack"])
async def test_named_background_sessions_still_resume_automatically(
    workspace: tuple[DuctorPaths, AgentConfig], messenger: str
) -> None:
    paths, config = workspace
    orch = Orchestrator(config, paths)
    session = NamedSession(
        name="ns1",
        chat_id=CHAT,
        provider="claude",
        model="sonnet",
        session_id="sid-1",
        prompt_preview="named work",
        status="idle",
        created_at=0.0,
    )
    session.last_prompt = "named work"
    orch.named_sessions.pop_recovered_running = MagicMock(return_value=[session])  # type: ignore[method-assign]
    orch.submit_named_followup_bg = MagicMock()  # type: ignore[method-assign]
    notify = AsyncMock()

    await _recover(messenger, _bot(orch, notify))

    orch.submit_named_followup_bg.assert_called_once_with(
        CHAT, "ns1", "named work", message_id=0, thread_id=None
    )
    notify.assert_awaited_once()


def test_restart_hook_is_scoped_to_its_own_home(tmp_path: Path) -> None:
    from ductor_bot.infra.restart import register_restart_hook, unregister_restart_hook

    calls: list[str] = []
    register_restart_hook(tmp_path / "a", partial(calls.append, "a"))
    try:
        write_restart_marker(marker_path=tmp_path / "b" / "restart-requested")
        assert calls == []
        write_restart_marker(marker_path=tmp_path / "a" / "restart-requested")
        assert calls == ["a"]
        # A marker found while starting up asks for no shutdown.
        (tmp_path / "a" / "restart-requested").write_text("1")
        assert consume_restart_marker(
            marker_path=tmp_path / "a" / "restart-requested", at_startup=True
        )
        assert calls == ["a"]
    finally:
        unregister_restart_hook(tmp_path / "a")


# -- topics: a stop is scoped to its own topic ------------------------------------------


class TopicCli:
    """Synthetic CLI for several topics of one chat: each turn ends when the test says so."""

    def __init__(self) -> None:
        self.started: dict[int, asyncio.Event] = {}
        self.released: dict[int, asyncio.Event] = {}
        self.errors: set[int] = set()

    def _event(self, table: dict[int, asyncio.Event], topic: int) -> asyncio.Event:
        return table.setdefault(topic, asyncio.Event())

    async def _run(self, request: object, *_args: object, **_kwargs: object) -> AgentResponse:
        topic = request.topic_id  # type: ignore[attr-defined]
        self._event(self.started, topic).set()
        await self._event(self.released, topic).wait()
        if topic in self.errors:
            return AgentResponse(result="", session_id="sid", is_error=True)
        return AgentResponse(result="answer", session_id="sid")

    execute = _run
    execute_streaming = _run

    def end(self, topic: int, *, error: bool = False) -> None:
        if error:
            self.errors.add(topic)
        self._event(self.released, topic).set()


async def _topic_turns(orch: Orchestrator, *topics: int):
    cli = TopicCli()
    object.__setattr__(orch, "_cli_service", cli)
    tasks = {}
    for topic in topics:
        key = SessionKey(chat_id=CHAT, topic_id=topic)
        tasks[topic] = asyncio.create_task(flows.normal(orch, key, f"{TEXT} {topic}"))
        await asyncio.wait_for(cli._event(cli.started, topic).wait(), 5)
    return cli, tasks


async def _user_stops(orch: Orchestrator, topic: int | None) -> None:
    """The user's /stop: of one topic (with a process of it) or of the whole chat."""
    if topic is None:
        await orch.abort(CHAT)
        return
    process = MagicMock(returncode=None, pid=1)
    orch._process_registry.register(CHAT, process, "main", topic_id=topic)
    with patch("ductor_bot.cli.process_registry._kill_processes", AsyncMock(return_value=1)):
        await orch._process_registry.kill_by_chat_topic(CHAT, topic)


async def _shutdown_cancels(orch: Orchestrator, tasks: dict[int, asyncio.Task]) -> None:
    begin_shutdown(orch)
    for task in tasks.values():
        task.cancel()
    for task in tasks.values():
        await _finish(task)


async def test_stop_of_another_topic_keeps_the_interrupted_topic_record(main_orch) -> None:
    _cli, tasks = await _topic_turns(main_orch, 2)
    await _user_stops(main_orch, 1)  # the user stopped something in topic 1

    await _shutdown_cancels(main_orch, tasks)

    assert [(a.chat_id, a.prompt_preview) for a in _notices(main_orch)] == [(CHAT, f"{TEXT} 2")]


async def test_stop_of_the_same_topic_is_not_announced(main_orch) -> None:
    _cli, tasks = await _topic_turns(main_orch, 2)
    await _user_stops(main_orch, 2)

    await _shutdown_cancels(main_orch, tasks)

    assert _notices(main_orch) == []


async def test_stop_of_the_whole_chat_is_not_announced_for_any_topic(main_orch) -> None:
    _cli, tasks = await _topic_turns(main_orch, 1, 2)
    await _user_stops(main_orch, None)

    await _shutdown_cancels(main_orch, tasks)

    assert _notices(main_orch) == []


async def test_stopped_topic_is_dropped_while_the_other_stays(main_orch) -> None:
    _cli, tasks = await _topic_turns(main_orch, 1, 2)
    await _user_stops(main_orch, 1)

    await _shutdown_cancels(main_orch, tasks)

    assert [a.prompt_preview for a in _notices(main_orch)] == [f"{TEXT} 2"]


async def test_two_topics_cut_off_by_the_shutdown_give_two_notices_once(
    workspace: tuple[DuctorPaths, AgentConfig], main_orch
) -> None:
    _cli, tasks = await _topic_turns(main_orch, 1, 2)
    await _shutdown_cancels(main_orch, tasks)
    paths, config = workspace

    notes: list[tuple[int, str]] = []
    for _ in range(2):  # two normal starts in a row
        restarted = Orchestrator(config, paths)
        notify = AsyncMock(side_effect=lambda chat, text: notes.append((chat, text)))
        await matrix_startup._handle_recovery(_bot(restarted, notify))

    assert sorted(text.count(TEXT) for _chat, text in notes) == [1, 1]
    assert len(notes) == 2
    assert any(f"{TEXT} 1" in text for _c, text in notes)
    assert any(f"{TEXT} 2" in text for _c, text in notes)


async def test_finished_turn_of_one_topic_leaves_the_other_record(main_orch) -> None:
    cli, tasks = await _topic_turns(main_orch, 1, 2)

    cli.end(1)
    await _finish(tasks[1])
    assert main_orch.inflight_tracker.active_scopes() == [(CHAT, 2)]

    await _shutdown_cancels(main_orch, {2: tasks[2]})
    assert [a.prompt_preview for a in _notices(main_orch)] == [f"{TEXT} 2"]


async def test_old_format_records_are_still_read_and_announced(main_orch) -> None:
    path = main_orch.paths.inflight_turns_path
    old = {
        "chat_id": CHAT,
        "provider": "claude",
        "model": "opus",
        "session_id": "sid",
        "prompt_preview": "from the old format",
        "started_at": datetime.now(UTC).isoformat(),
        "is_recovery": False,
        "path": "normal",
    }
    path.write_text(json.dumps({"turns": {str(CHAT): old}}))

    assert [(a.chat_id, a.prompt_preview) for a in _notices(main_orch)] == [
        (CHAT, "from the old format")
    ]
    assert main_orch.inflight_tracker.active_scopes() == [(CHAT, None)]
    main_orch.inflight_tracker.complete(CHAT)  # the old key is still the no-topic key
    assert _notices(main_orch) == []


async def test_new_files_keep_the_old_shape_for_chats_without_a_topic(main_orch) -> None:
    cli = GatedCli()
    object.__setattr__(main_orch, "_cli_service", cli)
    task = asyncio.create_task(flows.normal(main_orch, SessionKey(chat_id=CHAT), TEXT))
    await asyncio.wait_for(cli.started.wait(), 5)

    turns = json.loads(main_orch.paths.inflight_turns_path.read_text())["turns"]
    assert list(turns) == [str(CHAT)]  # key as before
    assert {
        "chat_id",
        "provider",
        "model",
        "session_id",
        "prompt_preview",
        "started_at",
        "is_recovery",
        "path",
    } <= set(turns[str(CHAT)])  # every old field is still there

    cli.end(result="answer")
    await _finish(task)
