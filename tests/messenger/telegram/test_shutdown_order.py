"""A shutdown ends polling, lets finished replies go out, then kills the CLI and closes the session."""

# ruff: noqa: F811  (the fixtures are imported from the modules that define them)

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Dispatcher

from ductor_bot.infra.shutdown_budget import ShutdownBudget
from ductor_bot.messenger.telegram import app as tg_app
from ductor_bot.messenger.telegram.message_dispatch import (
    NonStreamingDispatch,
    run_non_streaming_message,
)
from ductor_bot.messenger.telegram.polling import PollingDispatcher
from ductor_bot.messenger.telegram.reply_phase import ReplyPhase
from ductor_bot.multiagent import supervisor as supervisor_module
from ductor_bot.session.key import SessionKey
from tests.messenger.telegram.test_polling_ownership import (  # noqa: F401
    FakeTelegramApi,
    _until,
    api,
    make_bot,
)
from tests.multiagent.test_supervisor_lifecycle_lock import (  # noqa: F401
    WAIT,
    _declare_two,
    _StopAwareStack,
    _World,
    world,
)
from tests.orchestrator.conftest import workspace  # noqa: F401
from tests.orchestrator.test_shutdown_inflight import (  # noqa: F401
    CHAT,
    TEXT,
    _authenticated,
    _finish,
    _install,
    _notices,
    _turn,
    main_orch,
)


class _Recorder:
    """The orchestrator side of a shutdown, recording the order of what happens."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.paths = SimpleNamespace(ductor_home=Path("unused"))
        self.state: dict[str, object] = {}
        self.killed = asyncio.Event()
        self.bot: tg_app.TelegramBot | None = None

    async def run_pre_shutdown_hooks(self, _budget: object = None) -> None:
        if self.bot is not None:
            polling = self.bot._polling_task
            self.state["polling_done"] = polling is None or polling.done()
            self.state["replies_active"] = self.bot._reply_phase.active
        self.events.append("hook")

    async def shutdown(self, _budget: object = None) -> None:
        self.events.append("kill")
        self.killed.set()


async def _running_bot(
    api: FakeTelegramApi, make_bot, events: list[str]
) -> tuple[tg_app.TelegramBot, _Recorder, asyncio.Task[int]]:
    bot = make_bot()
    orch = _Recorder(events)
    orch.bot = bot
    bot._orchestrator = orch  # type: ignore[assignment]
    session = bot._bot.session
    real_close = session.close

    async def close() -> None:
        # aiogram also calls close() on a session it is about to re-create; only a real close counts
        if session._session is not None and not session._session.closed:
            events.append("close")
        await real_close()

    session.close = close  # type: ignore[method-assign]
    run = asyncio.create_task(bot.run())
    await _until(lambda: api.get_updates >= 1 and bot._polling_task is not None)
    return bot, orch, run


def _start_reply(bot: tg_app.TelegramBot, events: list[str], seconds: float) -> asyncio.Task[None]:
    async def reply() -> None:
        with bot._reply_phase.sending():
            await asyncio.sleep(seconds)
            events.append("reply-sent")

    return asyncio.create_task(reply())


def _start_running_turn(
    bot: tg_app.TelegramBot, orch: _Recorder, events: list[str]
) -> asyncio.Task[None]:
    """A handler whose turn is still running: it ends only when the CLI is killed."""

    async def turn() -> None:
        await orch.killed.wait()
        events.append("turn-end")

    task = asyncio.create_task(turn())
    bot._dp._handle_update_tasks.add(task)
    task.add_done_callback(bot._dp._handle_update_tasks.discard)
    return task


# -- the order of the steps ------------------------------------------------------------------


async def test_steps_run_in_order_and_the_session_closes_last(
    api: FakeTelegramApi, make_bot
) -> None:
    events: list[str] = []
    bot, orch, run = await _running_bot(api, make_bot, events)
    reply = _start_reply(bot, events, 0.3)
    turn = _start_running_turn(bot, orch, events)
    await asyncio.sleep(0.05)

    bot.request_stop()
    await asyncio.wait_for(run, 5)
    await asyncio.wait_for(bot.shutdown(), 10)

    # the reply went out before anything was killed; the running turn was not waited for
    assert events == ["reply-sent", "hook", "kill", "turn-end", "close"]
    assert orch.state == {"polling_done": True, "replies_active": 0}
    assert reply.done()
    assert turn.done()


async def test_a_reply_in_flight_is_delivered_before_the_session_closes(
    api: FakeTelegramApi, make_bot
) -> None:
    events: list[str] = []
    bot, _orch, run = await _running_bot(api, make_bot, events)
    sent: list[bool] = []

    async def reply() -> None:
        with bot._reply_phase.sending():
            await asyncio.sleep(0.4)
            # a send through the bot session works only while the session is still open
            await bot._bot.delete_webhook()
            sent.append(not bot._bot.session._session.closed)  # type: ignore[union-attr]
            events.append("reply-sent")

    reply_task = asyncio.create_task(reply())
    await asyncio.sleep(0.05)

    bot.request_stop()
    await asyncio.wait_for(run, 5)
    await asyncio.wait_for(bot.shutdown(), 10)

    assert reply_task.done()
    assert reply_task.exception() is None
    assert sent == [True]
    assert events.index("reply-sent") < events.index("close")


async def test_restart_command_path_uses_the_same_order(
    api: FakeTelegramApi, make_bot, tmp_path: Path
) -> None:
    events: list[str] = []
    bot, orch, run = await _running_bot(api, make_bot, events)
    orch.paths = SimpleNamespace(ductor_home=tmp_path)
    reply = _start_reply(bot, events, 0.2)
    _start_running_turn(bot, orch, events)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1), message_id=7, message_thread_id=None, is_topic_message=False
    )

    with patch.object(tg_app, "send_rich", new=AsyncMock()):
        await bot._on_restart(message)  # type: ignore[arg-type]
    assert await asyncio.wait_for(run, 5) == tg_app.EXIT_RESTART
    await asyncio.wait_for(bot.shutdown(), 10)

    assert events == ["reply-sent", "hook", "kill", "turn-end", "close"]
    assert reply.done()
    assert orch.state["polling_done"] is True


async def test_shutdown_without_a_started_bot_still_closes_the_session(make_bot) -> None:
    events: list[str] = []
    bot = make_bot()
    orch = _Recorder(events)
    bot._orchestrator = orch  # type: ignore[assignment]

    await asyncio.wait_for(bot.shutdown(), 5)

    assert events == ["hook", "kill"]
    assert bot._bot.session._session is None or bot._bot.session._session.closed


# -- a running CLI turn is not waited for ----------------------------------------------------


@pytest.mark.parametrize("flow", ["normal", "streaming"])
async def test_running_cli_turn_is_killed_not_waited_for_and_is_recorded(
    main_orch, make_bot, flow: str
) -> None:
    events: list[str] = []
    bot = make_bot()
    bot._orchestrator = main_orch
    registry = main_orch._process_registry
    cli, task = await _turn(main_orch, flow)
    bot._dp._handle_update_tasks.add(task)

    async def kill_all_active(_budget: object = None) -> int:
        events.append("kill")
        await registry.kill_all(CHAT)
        cli.end(is_error=True)
        return 1

    registry.kill_all_active = kill_all_active  # type: ignore[method-assign]
    started = time.monotonic()

    await asyncio.wait_for(bot.shutdown(), 10)
    await _finish(task)

    assert time.monotonic() - started < 2.0  # nothing waited for the long turn
    assert events == ["kill"]
    actions = _notices(main_orch)
    assert [(a.chat_id, a.kind, a.prompt_preview) for a in actions] == [(CHAT, "foreground", TEXT)]


async def test_pre_shutdown_hook_runs_once_before_the_kill(main_orch) -> None:
    events: list[str] = []
    registry = main_orch._process_registry

    async def kill_all_active(_budget: object = None) -> int:
        events.append("kill")
        return 0

    async def hook() -> None:
        events.append("hook")

    registry.kill_all_active = kill_all_active  # type: ignore[method-assign]
    main_orch.register_pre_shutdown_hook(hook)

    await main_orch.run_pre_shutdown_hooks()
    await main_orch.shutdown()

    assert events == ["hook", "kill"]


# -- the budget ---------------------------------------------------------------------------------


async def test_an_exhausted_budget_goes_straight_to_the_next_step(
    api: FakeTelegramApi, make_bot
) -> None:
    events: list[str] = []
    bot, _orch, run = await _running_bot(api, make_bot, events)
    never_ends = _start_reply(bot, events, 60)
    bot.request_stop()
    await asyncio.wait_for(run, 5)
    started = time.monotonic()

    await asyncio.wait_for(bot.shutdown(ShutdownBudget(0)), 10)

    assert time.monotonic() - started < 1.0
    assert events == ["hook", "kill", "close"]  # the reply was not waited for
    never_ends.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await never_ends


async def test_the_reply_wait_is_capped(
    api: FakeTelegramApi, make_bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tg_app, "_REPLY_DRAIN_SECONDS", 0.3)
    events: list[str] = []
    bot, _orch, run = await _running_bot(api, make_bot, events)
    never_ends = _start_reply(bot, events, 60)
    bot.request_stop()
    await asyncio.wait_for(run, 5)
    started = time.monotonic()

    await asyncio.wait_for(bot.shutdown(), 10)

    assert 0.25 <= time.monotonic() - started < 2.0
    assert events == ["hook", "kill", "close"]
    never_ends.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await never_ends


async def test_stops_of_two_sub_agents_and_main_share_one_budget(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor_module, "ShutdownBudget", lambda: ShutdownBudget(0.6))
    world.stack_class = _StopAwareStack
    world.honour_stop = False  # the bots ignore the stop request, so each wait runs to its cap
    names = _declare_two(world)
    await world.supervisor._on_agents_changed()
    for stack in world.alive:
        await asyncio.wait_for(stack.started.wait(), WAIT)
    main = _StopAwareStack(world, "main", world.config, 99)
    world.supervisor._stacks["main"] = main  # type: ignore[assignment]
    world.supervisor._tasks["main"] = asyncio.create_task(main.run())
    await asyncio.wait_for(main.started.wait(), WAIT)
    started = time.monotonic()

    await asyncio.wait_for(world.supervisor.stop_all(), 20)

    elapsed = time.monotonic() - started
    assert elapsed < 1.0  # one budget (0.6 s); a budget for each stop would take 1.2 s or more
    assert all(stack.cancelled for stack in world.created)
    assert main.cancelled
    assert all(stack.shutdowns == 1 for stack in world.created)
    assert names  # both sub-agents were declared and stopped


# -- marking the reply delivery ------------------------------------------------------------------


async def test_reply_phase_starts_when_the_turn_ends_and_ends_after_the_send() -> None:
    phase = ReplyPhase()
    seen: dict[str, int] = {}

    async def handle_message(*_args: object) -> object:
        seen["during_turn"] = phase.active
        return SimpleNamespace(text="reply", model_name=None)

    async def send_rich(*_args: object, **_kwargs: object) -> None:
        seen["during_send"] = phase.active

    dispatch = NonStreamingDispatch(
        bot=MagicMock(),
        orchestrator=SimpleNamespace(handle_message=handle_message),  # type: ignore[arg-type]
        key=SessionKey(chat_id=1),
        text="hello",
        allowed_roots=None,
        reply_phase=phase,
    )
    with (
        patch("ductor_bot.messenger.telegram.message_dispatch.send_rich", send_rich),
        patch("ductor_bot.messenger.telegram.message_dispatch.TypingContext") as typing_ctx,
    ):
        typing_ctx.return_value.__aenter__ = AsyncMock()
        typing_ctx.return_value.__aexit__ = AsyncMock()
        await run_non_streaming_message(dispatch)

    assert seen == {"during_turn": 0, "during_send": 1}
    assert phase.active == 0


async def test_reply_phase_ends_when_the_send_fails() -> None:
    phase = ReplyPhase()

    async def handle_message(*_args: object) -> object:
        return SimpleNamespace(text="reply", model_name=None)

    dispatch = NonStreamingDispatch(
        bot=MagicMock(),
        orchestrator=SimpleNamespace(handle_message=handle_message),  # type: ignore[arg-type]
        key=SessionKey(chat_id=1),
        text="hello",
        allowed_roots=None,
        reply_phase=phase,
    )
    with (
        patch(
            "ductor_bot.messenger.telegram.message_dispatch.send_rich",
            AsyncMock(side_effect=RuntimeError("send failed")),
        ),
        patch("ductor_bot.messenger.telegram.message_dispatch.TypingContext") as typing_ctx,
    ):
        typing_ctx.return_value.__aenter__ = AsyncMock()
        typing_ctx.return_value.__aexit__ = AsyncMock()
        with pytest.raises(RuntimeError):
            await run_non_streaming_message(dispatch)

    assert phase.active == 0
    assert await phase.wait_idle(0.1)


async def test_bot_hands_its_reply_phase_to_both_flows(make_bot) -> None:
    bot = make_bot()
    bot._orchestrator = SimpleNamespace(paths=SimpleNamespace(workspace=Path("w")))  # type: ignore[assignment]
    seen: list[ReplyPhase | None] = []

    async def capture(dispatch: object) -> str:
        seen.append(dispatch.reply_phase)  # type: ignore[attr-defined]
        return ""

    message = MagicMock()
    with (
        patch.object(tg_app, "run_non_streaming_message", capture),
        patch.object(tg_app, "run_streaming_message", capture),
        patch.object(bot, "file_roots", return_value=[]),
    ):
        await bot._handle_non_streaming(message, SessionKey(chat_id=1), "x")
        await bot._handle_streaming(message, SessionKey(chat_id=1), "x")

    assert seen == [bot._reply_phase, bot._reply_phase]


# -- the aiogram internals this relies on -------------------------------------------------------


async def test_dispatcher_keeps_its_running_handlers_in_a_set() -> None:
    assert isinstance(Dispatcher()._handle_update_tasks, set)
    assert PollingDispatcher().pending_handlers() == []


# -- the waits below the bot are capped by the same budget --------------------------------------


async def test_wait_capped_cancels_a_step_that_runs_over(caplog: pytest.LogCaptureFixture) -> None:
    from ductor_bot.infra.shutdown_budget import wait_capped

    done: list[str] = []

    async def quick() -> None:
        done.append("quick")

    async def slow() -> None:
        await asyncio.sleep(60)

    async def broken() -> None:
        msg = "boom"
        raise ValueError(msg)

    started = time.monotonic()
    with caplog.at_level("WARNING"):
        assert await wait_capped(quick(), 1.0, label="quick") is True
        assert await wait_capped(slow(), 0.2, label="slow") is False
        assert await wait_capped(broken(), 1.0, label="broken") is False
    assert time.monotonic() - started < 2.0
    assert done == ["quick"]
    assert "Shutdown step 'slow' did not finish" in caplog.text
    assert "Shutdown step 'broken' failed (ValueError)" in caplog.text


async def test_cli_kill_skips_the_grace_when_the_budget_is_used_up() -> None:
    from ductor_bot.cli.process_registry import ProcessRegistry

    process = MagicMock(spec=asyncio.subprocess.Process)
    process.pid = 4242
    process.returncode = None
    process.wait = AsyncMock(return_value=None)
    registry = ProcessRegistry()
    registry.register(chat_id=CHAT, process=process, label="main")
    started = time.monotonic()

    with (
        patch("ductor_bot.cli.process_registry.terminate_process_tree") as terminate,
        patch("ductor_bot.cli.process_registry.force_kill_process_tree") as force_kill,
    ):
        killed = await registry.kill_all_active(ShutdownBudget(0))

    assert killed == 1
    terminate.assert_called_once_with(4242)
    force_kill.assert_called_once_with(4242)  # the SIGKILL still goes out
    assert time.monotonic() - started < 1.0  # the two-second grace was skipped


async def test_task_hub_shutdown_does_not_wait_past_the_budget() -> None:
    from ductor_bot.tasks.hub import TaskHub

    hub = TaskHub.__new__(TaskHub)
    hub._maintenance_task = None
    hub._in_flight = {}
    hub._pending_resumes = {}
    stuck = asyncio.create_task(asyncio.sleep(60))
    hub._pending_deliveries = {stuck}
    started = time.monotonic()

    await asyncio.wait_for(hub.shutdown(ShutdownBudget(0.3)), 5)

    assert time.monotonic() - started < 1.5
    assert stuck.cancelled() or stuck.done()


async def test_observers_stop_is_capped_by_the_budget(main_orch) -> None:
    from ductor_bot.orchestrator import lifecycle

    async def stuck() -> None:
        await asyncio.sleep(60)

    main_orch._observers.stop_all = stuck  # type: ignore[method-assign]
    started = time.monotonic()

    try:
        await asyncio.wait_for(lifecycle.shutdown(main_orch, ShutdownBudget(0.3)), 5)
    finally:
        main_orch._observers.stop_all = AsyncMock()  # type: ignore[method-assign]

    assert time.monotonic() - started < 2.0


# -- the naming tasks' cleanup is inside the budget ---------------------------------------------------------------


class _SlowNaming:
    """A topic namer whose cancelled tasks never finish their cleanup."""

    def __init__(self) -> None:
        self.stops: list[asyncio.Task[object]] = []

    async def stop(self) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.stops.append(task)
        await asyncio.sleep(3600)


async def test_slow_naming_cleanup_does_not_hold_the_shutdown_when_the_budget_is_used_up(
    api: FakeTelegramApi, make_bot
) -> None:
    events: list[str] = []
    bot, _orch, run = await _running_bot(api, make_bot, events)
    namer = _SlowNaming()
    bot._topic_namer = namer  # type: ignore[assignment]
    bot.request_stop()
    await asyncio.wait_for(run, 5)
    started = time.monotonic()

    await asyncio.wait_for(bot.shutdown(ShutdownBudget(0)), 5)

    assert time.monotonic() - started < 1.0  # no waiting for the naming cleanup
    assert events == ["hook", "kill", "close"]  # polling had stopped, the session was closed
    assert bot._polling_task is not None
    assert bot._polling_task.done()
    for task in namer.stops:
        task.cancel()


async def test_naming_cleanup_wait_is_capped_by_what_is_left(
    api: FakeTelegramApi, make_bot
) -> None:
    events: list[str] = []
    bot, _orch, run = await _running_bot(api, make_bot, events)
    namer = _SlowNaming()
    bot._topic_namer = namer  # type: ignore[assignment]
    bot.request_stop()
    await asyncio.wait_for(run, 5)
    started = time.monotonic()

    await asyncio.wait_for(bot.shutdown(ShutdownBudget(0.4)), 5)

    assert 0.3 <= time.monotonic() - started < 1.5
    assert events == ["hook", "kill", "close"]
    for task in namer.stops:
        task.cancel()


class _NamingStack(_StopAwareStack):
    """A stack whose shutdown is the real Telegram bot shutdown, with a stuck topic namer."""

    async def shutdown(self, budget: object = None) -> None:
        self.shutdowns += 1
        await self.world.bots[self.name].shutdown(budget)  # type: ignore[attr-defined]


async def test_two_sub_agents_and_main_with_stuck_naming_stay_inside_the_budget(
    world: _World, make_bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor_module, "ShutdownBudget", lambda: ShutdownBudget(1.0))
    world.stack_class = _NamingStack
    world.honour_stop = True
    names = _declare_two(world)
    namers: list[_SlowNaming] = []
    world.bots = {}  # type: ignore[attr-defined]
    for name in (*names, "main"):
        bot = make_bot()
        bot._orchestrator = _Recorder([])  # type: ignore[assignment]
        bot._topic_namer = _SlowNaming()  # type: ignore[assignment]
        namers.append(bot._topic_namer)  # type: ignore[arg-type]
        world.bots[name] = bot  # type: ignore[attr-defined]
    await world.supervisor._on_agents_changed()
    for stack in world.alive:
        await asyncio.wait_for(stack.started.wait(), WAIT)
    main = _NamingStack(world, "main", world.config, 99)
    world.supervisor._stacks["main"] = main  # type: ignore[assignment]
    world.supervisor._tasks["main"] = asyncio.create_task(main.run())
    await asyncio.wait_for(main.started.wait(), WAIT)
    started = time.monotonic()

    await asyncio.wait_for(world.supervisor.stop_all(), 20)

    assert time.monotonic() - started < 3.0  # one budget (1 s) plus the cancel floors
    assert all(stack.shutdowns == 1 for stack in world.created)
    assert main.shutdowns == 1
    for namer in namers:
        for task in namer.stops:
            task.cancel()


async def test_orchestrator_refuses_a_new_turn_once_its_shutdown_has_begun(main_orch) -> None:
    from ductor_bot.orchestrator import lifecycle

    cli = _install(main_orch)
    lifecycle.begin_shutdown(main_orch)

    key = SessionKey(chat_id=CHAT)
    plain = await asyncio.wait_for(main_orch.handle_message(key, "hello"), 5)
    streamed = await asyncio.wait_for(main_orch.handle_message_streaming(key, "hello"), 5)

    assert "shutting down" in plain.text
    assert "shutting down" in streamed.text
    assert not cli.started.is_set()  # no CLI turn was started
