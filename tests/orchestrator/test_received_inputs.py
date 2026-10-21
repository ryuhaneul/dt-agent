"""Receipt metadata reaches actual user/bus CLI requests, never stored previews."""

import asyncio
import importlib.util
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ductor_bot.bus.adapters import (
    build_interagent_injection_prompt,
    from_interagent_result,
    from_task_question,
    from_task_result,
)
from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import DeliveryOutcome, Envelope, LockMode, Origin
from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.cli.base import format_cli_cmd
from ductor_bot.cli.stream_events import ResultEvent
from ductor_bot.cli.types import AgentRequest, AgentResponse, CLIResponse
from ductor_bot.infra import input_context as ic
from ductor_bot.messenger import queue_context as qc
from ductor_bot.messenger.matrix.bot import MatrixBot
from ductor_bot.messenger.slack.bot import SlackBot
from ductor_bot.messenger.telegram.middleware import SequentialMiddleware
from ductor_bot.multiagent.bus import (
    SYNC_DELEGATION_QUESTION_REJECTED,
    AsyncInterAgentResult,
    AsyncSendOptions,
)
from ductor_bot.multiagent.delegation import DelegationContext
from ductor_bot.multiagent.internal_api import InternalAgentAPI
from ductor_bot.orchestrator import injection
from ductor_bot.orchestrator.flows import (
    named_session_flow,
    named_session_streaming,
    normal,
    normal_streaming,
)
from ductor_bot.session.key import SessionKey
from ductor_bot.tasks.models import TaskResult
from tests.cli.test_service import _make_service
from tests.messenger.test_queue_context import message
from tests.multiagent.test_delegation_context import agents, seed_topic  # noqa: F401

STAMP = datetime(2026, 10, 5, 12, 30, 12, tzinfo=UTC).timestamp()
LINE = "[Received: 2026-10-05 21:30:12 KST]"


@pytest.fixture
def clock(monkeypatch):
    now = [STAMP, 10.0]
    monkeypatch.setattr(ic, "time", SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[1]))
    return now


def test_timezones_intake_time_and_nested_restore(clock):
    with ic.received_at():
        clock[0] += 100
        assert ic.current_input_context("Asia/Seoul") == LINE
        assert ic.current_input_context("invalid-zone") == "[Received: 2026-10-05 12:30:12 UTC]"
        with ic.received_at(clear=True):
            assert ic.current_input_context("UTC") is None
        assert ic.current_input_context("Asia/Seoul") == LINE
    assert ic.current_input_context("UTC") is None


@pytest.mark.parametrize("transport", ["tg", "mx", "sl"])
@pytest.mark.parametrize("queued", [False, True])
async def test_transport_lock_paths_capture_receipt_and_single_queue_block(
    clock, transport, queued, caplog
):
    captured = []
    key = SessionKey(transport, 100)
    pool = LockPool()
    lock = pool.get(key.lock_key)
    if queued:
        await lock.acquire()

    async def dispatch(*args, **kwargs):
        req = AgentRequest(
            prompt="original media/text",
            received_context=ic.current_input_context("Asia/Seoul"),
            queue_context=qc.current_queue_context("Asia/Seoul"),
        )
        captured.append(req)

    if transport == "tg":
        mw = SequentialMiddleware(lock_pool=pool)
        execution = mw(dispatch, message(), {})
    else:
        bot = object.__new__(MatrixBot if transport == "mx" else SlackBot)
        bot._lock_pool = pool
        bot._dispatch_message = dispatch

        async def ingress():
            with ic.received_at():
                if transport == "mx":
                    await bot._dispatch_with_lock(
                        key, "media/text", "room", SimpleNamespace(server_timestamp=0)
                    )
                else:
                    await bot._dispatch_with_lock(key, "media/text", "channel", None, sent_at="0")

        execution = ingress()
    task = asyncio.create_task(execution)
    await asyncio.sleep(0)
    clock[0] += 75
    clock[1] += 75
    if queued:
        lock.release()
    await task
    request = captured[0]
    assert request.cli_prompt.count(LINE) == 1
    assert request.prompt == "original media/text"
    if queued:
        assert request.cli_prompt.startswith("[Queued user message]\n" + LINE)
        assert "Queue position at arrival: 1" in request.cli_prompt
    else:
        assert request.cli_prompt == LINE + "\noriginal media/text"
    assert "[Received:" not in caplog.text
    assert ic.current_input_context("UTC") is None


async def test_matrix_actual_ingress_preserves_receipt_before_read_and_spawn(clock):
    bot = object.__new__(MatrixBot)
    bot._config = SimpleNamespace(scene=SimpleNamespace(seen_reaction=True))
    bot._preprocess_matrix_text = lambda *_: ("original", "room", "event", 100)
    bot._button_tracker = SimpleNamespace(match_input=lambda *_: None)
    bot._message_queue = Mock()
    tasks = []
    seen = []

    async def read(*_):
        clock[0] += 30

    async def dispatch(*_):
        seen.append(ic.current_input_context("Asia/Seoul"))

    def spawn(coro, **_):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    bot._set_seen_read_receipt = read
    bot._dispatch_with_lock = dispatch
    bot._spawn_task = spawn
    await bot._on_message(object(), object())
    await asyncio.gather(*tasks)
    assert seen == [LINE]


async def test_slack_actual_ingress_preserves_receipt_before_thread_lookup(clock):
    from tests.messenger.slack.test_bot import _make_bot

    bot = _make_bot()
    seen = []

    async def active(*_):
        clock[0] += 30
        return True

    async def dispatch(*args, **kwargs):
        seen.append(ic.current_input_context("Asia/Seoul"))

    bot._has_active_session_for_thread = active
    bot._dispatch_with_lock = dispatch
    await bot._on_message(
        {
            "user": "U123",
            "channel": "C123",
            "text": "original",
            "channel_type": "im",
            "ts": "2",
            "thread_ts": "1",
        }
    )
    assert seen == [LINE]


@pytest.mark.parametrize("mode", ["non_streaming", "streaming", "fallback"])
async def test_real_cli_gateway_and_preview_are_separate(monkeypatch, caplog, mode):
    caplog.set_level("DEBUG")
    request = AgentRequest(prompt="raw original", received_context=LINE)
    seen = []

    async def send(**kwargs):
        seen.append(kwargs["prompt"])
        return CLIResponse(result="reply")

    async def stream(**kwargs):
        seen.append(kwargs["prompt"])
        if mode != "fallback":
            yield ResultEvent(type="result", result="reply")

    monkeypatch.setattr(
        "ductor_bot.cli.service.create_cli",
        lambda _: Mock(send=AsyncMock(side_effect=send), send_streaming=stream),
    )
    service = _make_service()
    response = await (
        service.execute(request) if mode == "non_streaming" else service.execute_streaming(request)
    )
    assert response.result == "reply" and seen
    assert all(item == LINE + "\nraw original" for item in seen)
    for provider in ("claude", "codex", "gemini", "grok"):
        preview = format_cli_cmd([provider, request.cli_prompt], opt_prefix=None)
        assert "[Received:" not in preview and "raw original" in preview
    assert request.prompt == "raw original" and "[Received:" not in caplog.text


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("transport", ["tg", "mx", "sl"])
async def test_user_flow_keeps_records_and_system_prompt_clean(orch, transport, named, streaming):
    orch._config.user_timezone = "Asia/Seoul"
    key = SessionKey(transport, 1, 42)
    execute = AsyncMock(return_value=AgentResponse(result="reply", session_id="synthetic"))
    orch._cli_service.execute = execute
    orch._cli_service.execute_streaming = execute
    if named:
        ns = orch._named_sessions.create(1, "claude", "opus", "setup")
        orch._named_sessions.update_after_response(1, ns.name, "synthetic")
    with ic.received_at(STAMP):
        if named:
            result = await (named_session_streaming if streaming else named_session_flow)(
                orch, key, ns.name, "original"
            )
        else:
            result = await (normal_streaming if streaming else normal)(orch, key, "original")
    request = execute.await_args.args[0]
    assert request.cli_prompt.startswith(LINE + "\n")
    assert "[Received:" not in request.prompt and "[Received:" not in result.text
    assert "[Received:" not in (request.append_system_prompt or "")
    for path in orch.paths.ductor_home.glob("*.json"):
        assert "[Received:" not in path.read_text()


def envelope(kind):
    if kind.startswith("async-"):
        result = AsyncInterAgentResult(
            "synthetic",
            "worker",
            "worker",
            "preview",
            "done",
            success=kind == "async-success",
            error="synthetic error" if kind != "async-success" else None,
            failure_kind="result_lost" if kind == "async-lost" else "",
        )
        prompt = build_interagent_injection_prompt(
            result, agent_name="main", transport_label="Telegram"
        )
        return from_interagent_result(result, 12345, injection_prompt=prompt)
    if kind == "task-question":
        return from_task_question("synthetic", "Which target?", "preview", 12345)
    result = TaskResult(
        "synthetic",
        12345,
        "main",
        "check",
        "preview",
        "done",
        "done" if kind == "task-done" else "failed",
        1,
        "claude",
        "sonnet",
    )
    return from_task_result(result)


@pytest.mark.parametrize(
    "kind",
    ["async-success", "async-failure", "async-lost", "task-done", "task-failed", "task-question"],
)
async def test_actual_bus_intake_to_injection_wait_and_delivery(agents, clock, kind):  # noqa: F811
    _, main, _ = agents
    main._config.user_timezone = "Asia/Seoul"
    bus = MessageBus()
    main.wire_observers_to_bus(bus)
    transport = AsyncMock()
    transport.transport_name = "tg"
    bus.register_transport(transport)
    env = envelope(kind)
    env.created_at = STAMP
    original = env.prompt
    lock = bus.lock_pool.get(env.lock_key)
    await lock.acquire()
    task = asyncio.create_task(bus.submit(env))
    await asyncio.sleep(0)
    clock[0] += 75
    clock[1] += 75
    lock.release()
    await task
    request = main._cli_service.execute.await_args.args[0]
    assert request.cli_prompt == LINE + " [Waited: 75s]\n" + original
    assert request.prompt == env.prompt == original
    assert "[Received:" not in str(transport.mock_calls)


@pytest.mark.parametrize(
    "origin", [Origin.CRON, Origin.HEARTBEAT, Origin.WEBHOOK_WAKE, Origin.WEBHOOK_CRON]
)
async def test_automation_bus_does_not_inherit_receipt(agents, origin):  # noqa: F811
    _, main, _ = agents
    bus = MessageBus()
    main.wire_observers_to_bus(bus)
    with ic.received_at(STAMP):
        await bus.submit(
            Envelope(
                origin=origin, chat_id=12345, prompt="original automation", needs_injection=True
            )
        )
    request = main._cli_service.execute.await_args.args[0]
    assert request.received_context is None and request.cli_prompt == request.prompt


@pytest.mark.parametrize("async_send", [False, True])
async def test_actual_interagent_request_arrival_not_dispatch_or_outer_input(
    agents, clock, async_send  # noqa: F811
):  # noqa: F811
    bus, main, worker = agents
    worker._config.user_timezone = "Asia/Seoul"
    bus.set_async_result_handler("main", AsyncMock(return_value=DeliveryOutcome(True, True, True)))

    async def notify(_):
        clock[0] += 30

    bus._notify_recipient = notify
    with ic.received_at(STAMP - 500):
        if async_send:
            tid = bus.send_async("main", "worker", "original", opts=AsyncSendOptions(silent=True))
            task = bus._async_tasks[tid]
            task.timestamp = STAMP
            await task.asyncio_task
        else:
            await bus.send("main", "worker", "original")
    request = worker._cli_service.execute.await_args.args[0]
    assert request.cli_prompt.startswith(LINE + "\n[INTER-AGENT MESSAGE")
    assert "[Received:" not in request.prompt
    assert "[Received:" not in worker._named_sessions.list_active(12345)[0].prompt_preview


@pytest.mark.parametrize("fallback", [False, True])
async def test_delegation_question_uses_question_arrival_and_source_route(agents, clock, fallback):  # noqa: F811
    bus, main, _ = agents
    main._config.user_timezone = "Asia/Seoul"
    key, _ = await seed_topic(main)
    main.wire_observers_to_bus(MessageBus())
    context = DelegationContext(
        "synthetic",
        "async",
        "main",
        "worker",
        chat_id=key.chat_id,
        topic_id=key.topic_id,
        transport="tg",
        session_kind="named" if fallback else "foreground",
    )
    bus._delegations[context.request_id] = context
    lock = main._lock_pool.get(key.lock_key)
    if not fallback:
        await lock.acquire()
    task = asyncio.create_task(
        bus.send("worker", "main", "Which target?", delegation_id=context.request_id)
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    clock[0] += 75
    clock[1] += 75
    if not fallback:
        lock.release()
    response = await task
    assert response.success
    request = main._cli_service.execute.await_args.args[0]
    assert request.cli_prompt.startswith(LINE)
    assert "[Received:" not in request.prompt
    assert request.process_label.startswith("interagent:" if fallback else "interagent-question:")
    if not fallback:
        assert "[Waited: 75s]" in request.cli_prompt
        assert request.resume_session == "source-42"


async def test_telegram_button_wait_uses_callback_arrival_not_bot_message_date(
    orch, clock, monkeypatch,
):
    from aiogram.types import CallbackQuery, User

    from tests.messenger.telegram.test_app import _make_tg_bot

    monkeypatch.setattr(qc, "time", ic.time)
    orch._config.user_timezone = "Asia/Seoul"
    orch._config.streaming.enabled = False
    tg, bot = _make_tg_bot(orch._config)
    tg._orchestrator = orch
    tg._route_special_callback = AsyncMock(return_value=False)
    tg._mark_button_choice = AsyncMock()

    async def reply(msg, key, text, **kwargs):
        return await normal(orch, key, text)

    tg._handle_non_streaming = reply
    orch._cli_service.execute = AsyncMock(
        return_value=AgentResponse(result="reply", session_id="synthetic")
    )
    lock = tg._sequential.get_lock(SessionKey.telegram(100).lock_key)
    await lock.acquire()
    callback = CallbackQuery(
        id="synthetic",
        from_user=User(id=100, is_bot=False, first_name="S"),
        chat_instance="synthetic",
        message=message(),
        data="button choice",
    )
    # callback.answer uses the fake Bot, never Telegram.
    callback = callback.as_(AsyncMock())
    task = asyncio.create_task(tg._on_callback_query(callback))
    await asyncio.sleep(0)
    clock[0] += 75
    clock[1] += 75
    lock.release()
    await task
    request = orch._cli_service.execute.await_args.args[0]
    assert request.cli_prompt.startswith(
        "[Queued user message]\n" + LINE + "\nSent at: unknown\nWaited: 1m 15s\n"
    )
    assert request.queue_context is not None
    assert request.cli_prompt.count(LINE) == 1
    assert request.prompt.startswith("button choice")
    assert "[Received:" not in str(bot.mock_calls)


async def test_forwarded_command_callback_keeps_original_receipt(clock):
    mw = SequentialMiddleware()
    captured = []

    async def handle(*_):
        captured.append(ic.current_input_context("Asia/Seoul"))
        assert qc.current_queue_context("UTC") is None

    with ic.received_at(STAMP):
        clock[0] += 30
        await mw(
            handle, message("/session setup investigate"), {"command_callback_id": "synthetic"}
        )
    assert captured == [LINE]


async def test_cancelled_input_never_reaches_cli_and_scope_is_reset(clock):
    mw = SequentialMiddleware()
    lock = mw.get_lock(SessionKey.telegram(100).lock_key)
    await lock.acquire()
    handler = AsyncMock()
    task = asyncio.create_task(mw(handler, message(), {}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    lock.release()
    handler.assert_not_awaited()
    assert ic.current_input_context("UTC") is None



async def test_user_started_named_background_turn_receipt_and_clean_preview(agents):  # noqa: F811
    from ductor_bot.background.observer import BackgroundObserver
    from ductor_bot.orchestrator.core import NamedSessionRequest

    _, main, _ = agents
    main._config.user_timezone = "Asia/Seoul"
    observer = BackgroundObserver(
        main.paths, timeout_seconds=5, cli_service=main._cli_service, config=main._config
    )
    observer.set_result_handler(AsyncMock())
    main._observers.background = observer
    with ic.received_at(STAMP):
        tid, name = main.submit_named_session(
            12345, "original", NamedSessionRequest(message_id=1, thread_id=None)
        )
        await observer._tasks[tid].asyncio_task
    request = main._cli_service.execute.await_args.args[0]
    assert request.cli_prompt == LINE + "\noriginal"
    assert request.prompt == "original"
    ns = main._named_sessions.get(12345, name)
    assert "[Received:" not in ns.prompt_preview
    assert "[Received:" not in ns.last_prompt


@pytest.mark.parametrize(
    "origin", [Origin.INTERAGENT, Origin.TASK_RESULT, Origin.TASK_QUESTION]
)
async def test_wait_excludes_post_lock_wakeup_processing(agents, clock, origin):  # noqa: F811
    _, main, _ = agents
    main._config.user_timezone = "Asia/Seoul"
    bus = MessageBus()
    main.wire_observers_to_bus(bus)
    transport = AsyncMock()
    transport.transport_name = "tg"
    bus.register_transport(transport)

    async def wake(_):
        clock[0] += 20
        clock[1] += 20

    bus.set_wake_hook(wake)
    env = Envelope(
        origin=origin,
        chat_id=12345,
        prompt="raw",
        needs_injection=True,
        lock_mode=LockMode.REQUIRED,
        created_at=STAMP,
    )
    lock = bus.lock_pool.get(env.lock_key)
    await lock.acquire()
    task = asyncio.create_task(bus.submit(env))
    await asyncio.sleep(0)
    clock[0] += 5
    clock[1] += 5
    lock.release()
    await task
    request = main._cli_service.execute.await_args.args[0]
    assert request.received_context == LINE + " [Waited: 5s]", request.received_context


async def test_question_wait_excludes_post_lock_files_loading(
    agents, clock, monkeypatch  # noqa: F811
):
    ia, main, _ = agents
    main._config.user_timezone = "Asia/Seoul"
    key, _ = await seed_topic(main)
    main.wire_observers_to_bus(MessageBus())
    context = DelegationContext(
        "abcdef123456",
        "async",
        "main",
        "worker",
        chat_id=key.chat_id,
        topic_id=key.topic_id,
        transport="tg",
        session_kind="foreground",
    )
    ia._delegations[context.request_id] = context

    async def files(*_):
        clock[0] += 20
        clock[1] += 20
        return ""

    monkeypatch.setattr(injection, "build_appended_files_block", files)
    lock = main._lock_pool.get(key.lock_key)
    await lock.acquire()
    task = asyncio.create_task(
        ia.send("worker", "main", "Which target?", delegation_id=context.request_id)
    )
    for _ in range(4):
        await asyncio.sleep(0)
    clock[0] += 5
    clock[1] += 5
    lock.release()
    response = await task
    assert response.success
    request = main._cli_service.execute.await_args.args[0]
    assert request.received_context == LINE + " [Waited: 5s]", request.received_context


async def test_wait_ends_at_lock_acquisition(clock, monkeypatch):
    monkeypatch.setattr(qc, "time", ic.time)
    lock = asyncio.Lock()
    await lock.acquire()

    async def queued():
        with ic.received_at(STAMP):
            async with qc.QueuedMessageLock(lock, None):
                clock[0] += 20
                clock[1] += 20
                return qc.current_queue_context("Asia/Seoul")

    task = asyncio.create_task(queued())
    await asyncio.sleep(0)
    clock[0] += 5
    clock[1] += 5
    lock.release()
    context = await task
    assert context.count(LINE) == 1 and "Waited: 5s" in context


async def test_no_wait_when_only_preparation_is_slow(agents, clock):  # noqa: F811
    _, main, _ = agents
    main._config.user_timezone = "Asia/Seoul"
    bus = MessageBus()
    main.wire_observers_to_bus(bus)

    async def wake(_):
        clock[1] += 20

    bus.set_wake_hook(wake)
    await bus.submit(
        Envelope(
            origin=Origin.TASK_QUESTION,
            chat_id=12345,
            prompt="raw",
            needs_injection=True,
            created_at=STAMP,
        )
    )
    request = main._cli_service.execute.await_args.args[0]
    assert request.received_context == LINE


def run_tool(result, monkeypatch, capsys, async_tool=False):
    root = Path(__file__).resolve().parents[2]
    name = "ask_agent_async" if async_tool else "ask_agent"
    path = (
        root / "ductor_bot/_home_defaults/workspace/tools/agent_tools" / (name + ".py")
    )
    spec = importlib.util.spec_from_file_location("eval_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return json.dumps(result).encode()

    monkeypatch.setattr(module.sys, "argv", [str(path), "main", "synthetic question"])
    monkeypatch.setattr(
        module.urllib.request, "urlopen", lambda *_a, **_k: FakeResponse()
    )
    try:
        module.main()
    except SystemExit:
        pass
    output = capsys.readouterr()
    return output.out + output.err


@pytest.mark.parametrize(
    "kind",
    [
        "normal",
        "question-answer",
        "sync-rejected",
        "async-rejected",
        "failure",
        "timeout",
    ],
)
async def test_tool_responses_have_receipt_in_ai_output(
    agents, clock, monkeypatch, capsys, kind  # noqa: F811
):
    ia, main, worker = agents
    if kind in {"sync-rejected", "async-rejected"}:
        context = DelegationContext(
            "abcdef123456", "sync", "main", "worker", session_kind="foreground"
        )
        ia._delegations[context.request_id] = context
        if kind == "async-rejected":
            req = SimpleNamespace(
                json=AsyncMock(
                    return_value={
                        "from": "worker",
                        "to": "main",
                        "message": "synthetic question",
                        "ia_turn": True,
                        "delegation_id": context.request_id,
                    }
                )
            )
            resp = await InternalAgentAPI(ia)._handle_send_async(req)
            payload = json.loads(resp.body)
            assert (
                not payload["success"]
                and payload["error"] == SYNC_DELEGATION_QUESTION_REJECTED
            )
            text = run_tool(payload, monkeypatch, capsys, async_tool=True)
        else:
            result = await ia.send(
                "worker", "main", "synthetic", delegation_id=context.request_id
            )
            assert (
                not result.success and result.error == SYNC_DELEGATION_QUESTION_REJECTED
            )
            text = run_tool(asdict(result), monkeypatch, capsys)
        main._cli_service.execute.assert_not_awaited()
        assert not main._named_sessions.list_active(12345)
    else:
        if kind == "question-answer":
            key, _ = await seed_topic(main)
            main.wire_observers_to_bus(MessageBus())
            context = DelegationContext(
                "abcdef123456",
                "async",
                "main",
                "worker",
                chat_id=key.chat_id,
                topic_id=key.topic_id,
                transport="tg",
                session_kind="foreground",
            )
            ia._delegations[context.request_id] = context
            result = await ia.send(
                "worker", "main", "question", delegation_id=context.request_id
            )
        else:
            if kind == "failure":
                worker._cli_service.execute.side_effect = OSError("synthetic failure")
            if kind == "timeout":

                async def slow(_):
                    await asyncio.sleep(0.1)

                worker._cli_service.execute.side_effect = slow
            result = await ia.send(
                "main",
                "worker",
                "synthetic",
                send_timeout=0.01 if kind == "timeout" else 1,
            )
        text = run_tool(asdict(result), monkeypatch, capsys)
    assert "[Received:" in text, text


@pytest.mark.parametrize(
    "zone,epoch,expected",
    [
        ("", STAMP, "UTC"),
        ("invalid", STAMP, "UTC"),
        ("Asia/Seoul", STAMP, "KST"),
        (
            "America/New_York",
            datetime(2026, 11, 1, 5, 30, tzinfo=UTC).timestamp(),
            "EDT",
        ),
        (
            "America/New_York",
            datetime(2026, 11, 1, 6, 30, tzinfo=UTC).timestamp(),
            "EST",
        ),
    ],
)
def test_timezone_and_dst_controls(zone, epoch, expected):
    with ic.received_at(epoch):
        assert ic.current_input_context(zone).endswith(expected + "]")
    assert ic.current_input_context("UTC") is None


@pytest.mark.parametrize("route", ["bus", "input-lock"])
async def test_fair_lock_wait_after_release_is_still_recorded(agents, clock, route):  # noqa: F811
    _, main, _ = agents
    main._config.user_timezone = "Asia/Seoul"
    bus = MessageBus()
    main.wire_observers_to_bus(bus)
    adapter = AsyncMock()
    adapter.transport_name = "tg"
    bus.register_transport(adapter)
    lock = bus.lock_pool.get((12345, None))
    await lock.acquire()
    started = asyncio.Event()
    finish_first = asyncio.Event()
    requests = []

    async def execute(request):
        requests.append(request)
        if request.prompt == "first":
            started.set()
            await finish_first.wait()
        return AgentResponse(result="done", session_id="synthetic")

    main._cli_service.execute.side_effect = execute
    first = asyncio.create_task(
        bus.submit(
            Envelope(
                origin=Origin.TASK_QUESTION,
                chat_id=12345,
                prompt="first",
                needs_injection=True,
                lock_mode=LockMode.REQUIRED,
                created_at=STAMP,
            )
        )
    )
    await asyncio.sleep(0)
    lock.release()
    assert not lock.locked() and lock._waiters

    async def finish():
        await started.wait()
        clock[1] += 5
        finish_first.set()

    releaser = asyncio.create_task(finish())
    if route == "bus":
        await bus.submit(
            Envelope(
                origin=Origin.TASK_QUESTION,
                chat_id=12345,
                prompt="second",
                needs_injection=True,
                lock_mode=LockMode.REQUIRED,
                created_at=STAMP,
            )
        )
    else:
        with ic.received_at(STAMP):
            async with ic.input_lock(lock):
                await main.inject_prompt("second", 12345, "synthetic")
    await first
    await releaser
    assert requests[-1].received_context == LINE + " [Waited: 5s]", requests[
        -1
    ].received_context


async def test_cancelled_acquisition_freezes_wait_without_releasing_owner(clock):
    lock = asyncio.Lock()
    await lock.acquire()
    captured = []

    async def waiting():
        with ic.received_at(STAMP):
            try:
                async with ic.input_lock(lock):
                    pytest.fail("Cancelled waiter acquired the held lock")
            finally:
                captured.append(ic.current_input_context("Asia/Seoul"))

    task = asyncio.create_task(waiting())
    await asyncio.sleep(0)
    clock[1] += 5
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert captured == [LINE + " [Waited: 5s]"]
    assert lock.locked()
    lock.release()
    assert ic.current_input_context("UTC") is None


@pytest.mark.parametrize("async_tool", [False, True])
@pytest.mark.parametrize("success", [False, True])
def test_tool_preserves_server_stamp_after_delayed_http_read(
    monkeypatch, capsys, async_tool, success
):
    payload = {
        "success": success,
        "text": "unchanged answer",
        "task_id": "synthetic",
        "error": SYNC_DELEGATION_QUESTION_REJECTED,
        "received_at": STAMP,
        "received_context": LINE,
    }
    output = run_tool(payload, monkeypatch, capsys, async_tool=async_tool)
    assert output.startswith(LINE + "\n")
    assert output.count("[Received:") == 1
    if not success:
        assert output.endswith("Error: " + SYNC_DELEGATION_QUESTION_REJECTED + "\n")
    elif not async_tool:
        assert output == LINE + "\nunchanged answer\n"


async def test_sync_response_receipt_is_answer_arrival_not_request_time(agents, clock):  # noqa: F811
    ia, main, worker = agents
    main._config.user_timezone = "Asia/Seoul"

    async def delayed(_request):
        clock[0] += 65
        return AgentResponse(result="unchanged answer", session_id="synthetic")

    worker._cli_service.execute.side_effect = delayed
    response = await ia.send("main", "worker", "request")
    assert response.success and response.text == "unchanged answer"
    assert response.received_at == STAMP + 65
    assert response.received_context == "[Received: 2026-10-05 21:31:17 KST]"
    assert "[Received:" not in response.text
    assert response.error is None


@pytest.mark.parametrize("name", ["ask_agent.py", "ask_agent_async.py"])
def test_agent_workspace_zone2_copies_updated_tool(tmp_path, name):
    from ductor_bot.workspace.init import _sync_home_defaults
    from ductor_bot.workspace.paths import DuctorPaths

    for agent in ["main", "worker"]:
        paths = DuctorPaths(tmp_path / agent)
        target = paths.workspace / "tools" / "agent_tools" / name
        target.parent.mkdir(parents=True)
        target.write_text("old framework tool")
        _sync_home_defaults(paths)
        source = (
            Path(__file__).resolve().parents[2]
            / "ductor_bot/_home_defaults/workspace/tools/agent_tools"
            / name
        )
        assert target.read_bytes() == source.read_bytes()
        assert "_receipt_line" in target.read_text()


@pytest.mark.parametrize("async_tool", [False, True])
def test_legacy_response_has_safe_utc_receipt(monkeypatch, capsys, async_tool):
    output = run_tool(
        {"success": False, "error": "unchanged legacy error"},
        monkeypatch,
        capsys,
        async_tool=async_tool,
    )
    assert output.startswith("[Received:") and " UTC]\n" in output
    assert output.endswith("Error: unchanged legacy error\n")


async def test_tool_reply_uses_current_calling_agent_timezone(agents, clock):  # noqa: F811
    ia, main, _worker = agents
    main._config = main._config.model_copy(update={"user_timezone": "Asia/Seoul"})
    result = await ia.send("main", "worker", "request")
    assert result.received_context == LINE
    assert result.received_at == STAMP


async def dispatch_transport(transport, pool, key, dispatch):
    if transport == "tg":
        mw = SequentialMiddleware(lock_pool=pool)
        event = message("original")
        event = event.model_copy(
            update={"chat": event.chat.model_copy(update={"id": key.chat_id})}
        )
        await mw(dispatch, event, {})
    else:
        bot = object.__new__(MatrixBot if transport == "mx" else SlackBot)
        bot._lock_pool = pool
        bot._dispatch_message = dispatch
        with ic.received_at(STAMP):
            if transport == "mx":
                await bot._dispatch_with_lock(
                    key, "original", "synthetic", SimpleNamespace(server_timestamp=0)
                )
            else:
                await bot._dispatch_with_lock(
                    key, "original", "synthetic", None, sent_at="0"
                )


@pytest.mark.parametrize("transport", ["tg", "mx", "sl"])
async def test_user_fifo_after_bus_waiter_keeps_actual_wait(
    agents, clock, monkeypatch, transport  # noqa: F811
):
    monkeypatch.setattr(qc, "time", ic.time)
    _, main, _ = agents
    main._config.user_timezone = "Asia/Seoul"
    key = SessionKey(transport, 12345, None)
    bus = MessageBus()
    main.wire_observers_to_bus(bus)
    adapter = AsyncMock()
    adapter.transport_name = transport
    bus.register_transport(adapter)
    lock = bus.lock_pool.get(key.lock_key)
    await lock.acquire()
    started = asyncio.Event()
    finish = asyncio.Event()
    seen = []

    async def execute(req):
        if req.prompt == "preceding bus result":
            started.set()
            await finish.wait()
        return AgentResponse(result="done", session_id="synthetic")

    main._cli_service.execute.side_effect = execute
    first = asyncio.create_task(
        bus.submit(
            Envelope(
                origin=Origin.TASK_QUESTION,
                chat_id=key.chat_id,
                transport=transport,
                prompt="preceding bus result",
                needs_injection=True,
                lock_mode=LockMode.REQUIRED,
                created_at=STAMP,
            )
        )
    )
    await asyncio.sleep(0)
    lock.release()
    assert not lock.locked() and lock._waiters

    async def release_first():
        await started.wait()
        clock[1] += 5
        finish.set()

    release = asyncio.create_task(release_first())

    async def user_dispatch(*_a, **_k):
        seen.append(
            AgentRequest(
                prompt="original",
                received_context=ic.current_input_context("Asia/Seoul"),
                queue_context=qc.current_queue_context("Asia/Seoul"),
            )
        )

    await dispatch_transport(transport, bus.lock_pool, key, user_dispatch)
    await first
    await release
    req = seen[0]
    assert "Waited: 5s" in req.cli_prompt, {
        "transport": transport,
        "prompt": req.cli_prompt,
        "queue": req.queue_context,
        "receipt": req.received_context,
    }


@pytest.mark.parametrize("transport", ["tg", "mx", "sl"])
async def test_queued_named_background_preserves_acquired_wait(
    agents, clock, monkeypatch, transport  # noqa: F811
):
    from ductor_bot.background.observer import BackgroundObserver
    from ductor_bot.orchestrator.core import NamedSessionRequest

    monkeypatch.setattr(qc, "time", ic.time)
    _, main, _ = agents
    main._config.user_timezone = "Asia/Seoul"
    observer = BackgroundObserver(
        main.paths,
        timeout_seconds=5,
        cli_service=main._cli_service,
        config=main._config,
    )
    observer.set_result_handler(AsyncMock())
    main._observers.background = observer
    pool = LockPool()
    key = SessionKey(transport, 12345, None)
    lock = pool.get(key.lock_key)
    await lock.acquire()

    async def dispatch(*_a, **_k):
        tid, _ = main.submit_named_session(
            key.chat_id, "original", NamedSessionRequest(message_id=1, thread_id=None)
        )
        await observer._tasks[tid].asyncio_task

    task = asyncio.create_task(dispatch_transport(transport, pool, key, dispatch))
    await asyncio.sleep(0)
    clock[1] += 5
    lock.release()
    await task
    req = main._cli_service.execute.await_args.args[0]
    assert req.received_context == LINE + " [Waited: 5s]", {
        "transport": transport,
        "received": req.received_context,
        "cli": req.cli_prompt,
    }
    assert req.queue_context is None


@pytest.mark.parametrize("transport", ["tg", "mx", "sl"])
async def test_fifo_queue_block_is_single_and_hides_receipt_from_preview(
    agents, clock, monkeypatch, transport  # noqa: F811
):
    # Reuse the real external-bus predecessor path and inspect the single block.
    from ductor_bot.cli.base import format_cli_cmd

    monkeypatch.setattr(qc, "time", ic.time)
    pool = LockPool()
    key = SessionKey(transport, 12345, None)
    lock = pool.get(key.lock_key)
    await lock.acquire()
    entered, release = asyncio.Event(), asyncio.Event()

    async def preceding():
        async with ic.input_lock(lock):
            entered.set()
            await release.wait()

    first = asyncio.create_task(preceding())
    await asyncio.sleep(0)
    lock.release()
    assert not lock.locked() and lock._waiters

    async def finish():
        await entered.wait()
        clock[1] += 5
        release.set()

    release_task = asyncio.create_task(finish())

    async def dispatch(*args, **kwargs):
        req = AgentRequest(
            prompt="original",
            queue_context=qc.current_queue_context("Asia/Seoul"),
            received_context=ic.current_input_context("Asia/Seoul"),
        )
        assert req.queue_context and "Queue position at arrival: 1" in req.queue_context
        assert req.cli_prompt.count("[Queued user message]") == 1
        assert req.cli_prompt.count("[Received:") == 1
        assert "Waited: 5s" in req.cli_prompt and req.prompt == "original"
        preview = format_cli_cmd(["codex", "exec", req.cli_prompt], opt_prefix=None)
        assert (
            "original" in preview
            and "Queued user message" not in preview
            and "Received:" not in preview
        )

    await dispatch_transport(transport, pool, key, dispatch)
    await first
    await release_task
    assert lock not in qc._waiters


async def test_multiple_users_behind_external_waiter_keep_positions(clock, monkeypatch):
    monkeypatch.setattr(qc, "time", ic.time)
    lock = asyncio.Lock()
    await lock.acquire()
    entered, release = asyncio.Event(), asyncio.Event()

    async def preceding():
        async with ic.input_lock(lock):
            entered.set()
            await release.wait()

    first = asyncio.create_task(preceding())
    await asyncio.sleep(0)
    lock.release()
    assert not lock.locked() and lock._waiters
    observed = []

    async def user(n):
        with ic.received_at(STAMP):
            async with qc.QueuedMessageLock(lock, None):
                observed.append((n, qc.current_queue_context("Asia/Seoul")))

    one = asyncio.create_task(user(1))
    two = asyncio.create_task(user(2))
    await entered.wait()
    await asyncio.sleep(0)
    clock[1] += 5
    release.set()
    await asyncio.gather(first, one, two)
    assert [n for n, _ in observed] == [1, 2]
    for n, block in observed:
        assert f"Queue position at arrival: {n}" in block and "Waited: 5s" in block
    assert lock not in qc._waiters


async def test_queue_wait_excludes_indicator_and_post_acquisition_preparation(
    clock, monkeypatch
):
    monkeypatch.setattr(qc, "time", ic.time)
    lock = asyncio.Lock()
    await lock.acquire()
    with ic.received_at(STAMP):
        queued = qc.QueuedMessageLock(lock, None)
        clock[1] += 20  # Indicator/preparation before the actual lock acquisition.

        async def release():
            await asyncio.sleep(0)
            clock[1] += 5
            lock.release()

        task = asyncio.create_task(release())
        async with queued:
            clock[1] += 20  # Preparation after acquisition must not extend either form.
            assert ic.current_input_context("Asia/Seoul") == LINE + " [Waited: 5s]"
            assert "Waited: 5s" in qc.current_queue_context("Asia/Seoul")
        await task
    assert lock not in qc._waiters


async def test_pending_uncontended_entry_discard_is_idempotent():
    lock = asyncio.Lock()
    entry = qc.QueuedMessageLock(lock, None)
    assert qc._waiters[lock] == 1
    entry.discard()
    entry.discard()
    assert lock not in qc._waiters and not lock.locked()
