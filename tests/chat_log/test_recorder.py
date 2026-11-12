# ruff: noqa: INP001
"""Record public content while keeping internal prompts and inactive paths isolated."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramNetworkError

from ductor_bot.bus.adapters import from_heartbeat
from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import DeliveryMode, Envelope, Origin
from ductor_bot.chat_log.events import EventHub
from ductor_bot.chat_log.history import HistoryStore
from ductor_bot.chat_log.outbox import Outbox
from ductor_bot.chat_log.recorder import HistoryRecorder, chat_log_source, chat_log_turn
from ductor_bot.config import AgentConfig
from ductor_bot.heartbeat.models import HeartbeatOutcome, HeartbeatStatus
from ductor_bot.messenger.telegram.transport import TelegramTransport
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.session.key import SessionKey
from ductor_bot.workspace.paths import DuctorPaths


@pytest.fixture
def orch(tmp_path: Path, monkeypatch) -> Orchestrator:
    result = Orchestrator(AgentConfig(), DuctorPaths(ductor_home=tmp_path))
    monkeypatch.setattr(
        result, "_route_message", AsyncMock(return_value=OrchestratorResult(text="reply"))
    )
    monkeypatch.setattr(result._observers.heartbeat, "notify_activity", MagicMock())
    return result


def storage(orch: Orchestrator):
    hub = EventHub()
    store = HistoryStore(orch.paths.chat_log_dir / "history", hub)
    recorder = HistoryRecorder(store, hub, allowed_user_ids=[123, 456])
    return store, hub, recorder


def events(queue):
    return [queue.get_nowait() for _ in range(queue.qsize())]


async def test_uninstalled_and_removed_recorder_leave_no_chat_files(orch: Orchestrator) -> None:
    with patch.object(HistoryStore, "append", new_callable=AsyncMock) as append:
        result = await orch.handle_message(SessionKey.telegram(1), "hello")
        assert result.text == "reply"
        append.assert_not_awaited()
        _, _, recorder = storage(orch)
        orch.set_history_recorder(recorder)
        orch.set_history_recorder(None)
        await orch.handle_message(SessionKey.telegram(1), "hello again")
        append.assert_not_awaited()
    assert not orch.paths.chat_log_dir.exists()
    assert not orch.paths.webchat_dir.exists()


@pytest.mark.parametrize(
    ("source", "override", "expected"),
    [(None, None, "telegram"), (None, "api", "api"), ("web", "api", "web")],
)
async def test_conversation_sources_ids_and_events(
    orch: Orchestrator, source, override, expected
) -> None:
    store, hub, recorder = storage(orch)
    queue = hub.subscribe()
    orch.set_history_recorder(recorder)
    source_token = chat_log_source.set(source)
    turn_token = chat_log_turn.set(
        {"turn_id": "turn", "user_entry_id": "123456abcdef", "reply_entry_id": "abcdef123456"}
    )
    try:
        await orch.handle_message_streaming(
            SessionKey.telegram(-123, 7), "hello", request_transport=override
        )
    finally:
        chat_log_turn.reset(turn_token)
        chat_log_source.reset(source_token)
    rows = await store.read("-123.7")
    assert [(row.role, row.source, row.text) for row in rows] == [
        ("user", expected, "hello"),
        ("bot", expected, "reply"),
    ]
    if expected == "web":
        assert [row.id for row in rows] == ["123456abcdef", "abcdef123456"]
        assert [row.turn_id for row in rows] == ["turn", "turn"]
        assert rows[1].status == "ok"
    else:
        assert all(row.turn_id is None and row.status is None for row in rows)
    recorded = events(queue)
    assert [event["type"] for event in recorded] == ["busy", "entry", "entry", "busy"]
    assert recorded[0] == {"type": "busy", "tid": "-123.7", "busy": True, "source": expected}
    assert recorded[-1] == {**recorded[0], "busy": False}


async def test_non_telegram_key_is_ignored(orch: Orchestrator) -> None:
    store, hub, recorder = storage(orch)
    queue = hub.subscribe()
    orch.set_history_recorder(recorder)
    await orch.handle_message(SessionKey.matrix(1), "private input")
    assert not store.directory.exists()
    assert queue.empty()


async def test_internal_memory_prompt_hidden_but_reply_and_busy_retained(
    orch: Orchestrator,
) -> None:
    store, hub, recorder = storage(orch)
    queue = hub.subscribe()
    orch.set_history_recorder(recorder)
    await orch.handle_message(
        SessionKey.telegram(1), "INTERNAL_MEMORY_PROMPT", counts_as_activity=False
    )
    assert [(row.role, row.text) for row in await store.read("1")] == [("bot", "reply")]
    assert [event["type"] for event in events(queue)] == ["busy", "entry", "busy"]
    orch._observers.heartbeat.notify_activity.assert_not_called()


@pytest.mark.parametrize("error", [ValueError("route failed"), asyncio.CancelledError()])
async def test_error_and_cancel_always_finish_busy(orch: Orchestrator, error) -> None:
    store, hub, recorder = storage(orch)
    queue = hub.subscribe()
    orch.set_history_recorder(recorder)
    orch._route_message.side_effect = error
    token = chat_log_source.set("web")
    try:
        if isinstance(error, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await orch.handle_message(SessionKey.telegram(1), "hello")
        else:
            result = await orch.handle_message(SessionKey.telegram(1), "hello")
            assert result.text
            assert (await store.read("1"))[-1].status == "error"
    finally:
        chat_log_source.reset(token)
    assert events(queue)[-1]["busy"] is False
    if isinstance(error, asyncio.CancelledError):
        assert [row.role for row in await store.read("1")] == ["user"]


@pytest.mark.parametrize("topic_abort", [False, True])
async def test_web_aborted_status_uses_process_flags(orch: Orchestrator, topic_abort) -> None:
    store, _, recorder = storage(orch)
    orch.set_history_recorder(recorder)

    async def route(_dispatch):
        if topic_abort:
            orch._process_registry._aborted_topics.add((1, 2))
        else:
            orch._process_registry._aborted.add(1)
        return OrchestratorResult(text="partial reply")

    orch._route_message.side_effect = route
    token = chat_log_source.set("web")
    try:
        await orch.handle_message(SessionKey.telegram(1, 2), "hello")
    finally:
        chat_log_source.reset(token)
    assert (await store.read("1.2"))[-1].status == "aborted"


async def test_empty_reply_is_not_appended(orch: Orchestrator) -> None:
    store, hub, recorder = storage(orch)
    queue = hub.subscribe()
    orch.set_history_recorder(recorder)
    orch._route_message.return_value = OrchestratorResult(text="")
    await orch.handle_message(SessionKey.telegram(1), "hello")
    assert [row.role for row in await store.read("1")] == ["user"]
    assert events(queue)[-1]["busy"] is False


async def test_disk_failures_do_not_replace_reply_and_busy_finishes(
    orch: Orchestrator, caplog
) -> None:
    store, hub, recorder = storage(orch)
    queue = hub.subscribe()
    orch.set_history_recorder(recorder)
    expected = orch._route_message.return_value
    with patch.object(
        store, "append", new_callable=AsyncMock, side_effect=OSError("disk unavailable")
    ) as append:
        result = await orch.handle_message(SessionKey.telegram(1), "hello")
    assert result is expected
    assert append.await_count == 2
    assert [event["busy"] for event in events(queue)] == [True, False]
    assert "History begin failed: session=tg:1" in caplog.text
    assert "History finish failed: session=tg:1" in caplog.text


async def test_finish_failure_does_not_replace_uncaught_exception(orch: Orchestrator) -> None:
    _, _, recorder = storage(orch)
    orch.set_history_recorder(recorder)
    original = LookupError("uncaught route failure")
    orch._route_message.side_effect = original
    with (
        patch.object(
            recorder, "finish", new_callable=AsyncMock, side_effect=OSError("disk unavailable")
        ),
        pytest.raises(LookupError) as caught,
    ):
        await orch.handle_message(SessionKey.telegram(1), "hello")
    assert caught.value is original


@pytest.mark.parametrize(
    ("origin", "kind"),
    [
        (Origin.HEARTBEAT, "heartbeat"),
        (Origin.INTERAGENT, "interagent"),
        (Origin.CRON, "cron"),
        (Origin.WEBHOOK_WAKE, "webhook"),
        (Origin.WEBHOOK_CRON, "webhook"),
        (Origin.BACKGROUND, "background"),
    ],
)
async def test_bus_origin_mapping_and_internal_prompt_exclusion(
    orch: Orchestrator, origin, kind
) -> None:
    store, _, recorder = storage(orch)
    body = "visible result"
    if origin == Origin.CRON:
        body += "\nMessage sent successfully. Delivered to Telegram."
    envelope = Envelope(
        origin=origin,
        chat_id=1,
        topic_id=2,
        prompt="FORBIDDEN_INJECTION_PAYLOAD",
        result_text=body,
        status="success",
        is_error=origin == Origin.INTERAGENT,
        metadata={"title": "Job title", "chat_log_entry_id": "123456abcdef"},
    )
    await recorder.on_delivered(envelope)
    rows = await store.read("1.2")
    assert len(rows) == 1
    assert (rows[0].role, rows[0].source, rows[0].kind) == ("event", "system", kind)
    assert (rows[0].text, rows[0].title, rows[0].id) == (
        "visible result",
        "Job title",
        "123456abcdef",
    )
    assert rows[0].status == ("error" if origin == Origin.INTERAGENT else "success")
    assert "FORBIDDEN_INJECTION_PAYLOAD" not in (store.directory / "tg_1_2.jsonl").read_text()


@pytest.mark.parametrize(
    ("status", "inject", "expected"),
    [("done", True, "visible reply"), ("cancelled", False, ""), ("failed", False, "")],
)
async def test_task_result_only_records_injected_response(
    orch: Orchestrator, status, inject, expected
) -> None:
    store, _, recorder = storage(orch)
    envelope = Envelope(
        origin=Origin.TASK_RESULT,
        chat_id=1,
        prompt="FORBIDDEN_TASK_INJECTION",
        result_text="visible reply",
        status=status,
        needs_injection=inject,
        metadata={"name": "Task name"},
    )
    await recorder.on_delivered(envelope)
    row = (await store.read("1"))[0]
    assert (row.role, row.kind, row.text, row.title, row.status) == (
        "event",
        "task_result",
        expected,
        "Task name",
        status,
    )
    assert "FORBIDDEN_TASK_INJECTION" not in (store.directory / "tg_1.jsonl").read_text()


@pytest.mark.parametrize("failure_part", [None, "question", "answer"])
async def test_question_and_reply_order_and_outbox_join(orch: Orchestrator, failure_part) -> None:
    store, _, recorder = storage(orch)
    outbox = Outbox(orch.paths.chat_log_dir / "outbox")
    bot = MagicMock()
    bot._orch = orch
    bot.file_roots.return_value = [orch.paths.ductor_home]
    sent = SimpleNamespace(message_id=1)
    error = TelegramNetworkError(MagicMock(), "Synthetic network outage")
    bot.bot_instance.send_message = AsyncMock(
        side_effect={None: [sent, sent], "question": [error], "answer": [sent, error]}[failure_part]
    )
    transport = TelegramTransport(bot)
    transport.set_outbox(outbox)
    bus = MessageBus()
    bus.register_transport(transport)
    bus.set_delivery_observer(recorder.on_delivered)
    envelope = Envelope(
        origin=Origin.TASK_QUESTION,
        chat_id=1,
        prompt="Visible question?",
        result_text="Visible answer.",
        status="waiting",
        metadata={"task_id": "task-example"},
    )
    await bus.submit(envelope)
    rows = await store.read("1")
    assert [(row.seq, row.role, row.text, row.kind, row.title) for row in rows] == [
        (1, "event", "Visible question?", "task_question", "task-example"),
        (2, "bot", "Visible answer.", "task_question", "task-example"),
    ]
    assert rows[0].id != rows[1].id
    items = await outbox.pending()
    expected = rows if failure_part == "question" else rows[1:] if failure_part else []
    assert [item.history_id for item in items] == [row.id for row in expected]
    for row in expected:
        assert await outbox.mirror_state(row.id) == "pending"
    # Exercise the actual browser merge, which previously replaced the question with its answer.
    code = """
const fs = require('fs');
const src = fs.readFileSync('ductor_bot/webchat/web/static/js/chat.js', 'utf8');
const start = src.indexOf('export function mergeEntries(');
const end = src.indexOf('function sorted(', start);
eval(src.slice(start, end).replace('export ', ''));
const incoming = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(mergeEntries(new Map(), incoming)));
"""
    result = await asyncio.to_thread(
        subprocess.run,
        ["node", "-e", code],
        input=json.dumps([asdict(row) for row in rows]),
        text=True,
        capture_output=True,
        check=True,
    )
    merged = json.loads(result.stdout)
    assert [row["role"] for row in merged] == ["event", "bot"]


async def test_broadcast_records_once_for_first_allowed_user(orch: Orchestrator) -> None:
    store, _, recorder = storage(orch)
    await recorder.on_delivered(
        Envelope(
            origin=Origin.CRON,
            chat_id=0,
            topic_id=7,
            delivery=DeliveryMode.BROADCAST,
            result_text="Visible report",
            status="success",
        )
    )
    assert store.tids() == ["123"]
    assert await store.count("123") == 1


@pytest.mark.parametrize(
    ("transport", "origin"),
    [("mx", Origin.CRON), ("api", Origin.BACKGROUND), ("tg", Origin.USER), ("tg", Origin.API)],
)
async def test_non_telegram_and_non_bus_origins_are_ignored(
    orch: Orchestrator, transport, origin
) -> None:
    store, _, recorder = storage(orch)
    await recorder.on_delivered(
        Envelope(origin=origin, chat_id=1, transport=transport, result_text="ignored")
    )
    assert not store.directory.exists()


async def test_bus_observer_does_not_record_generated_transport_fallback(
    orch: Orchestrator,
) -> None:
    store, _, recorder = storage(orch)
    transport = MagicMock(transport_name="tg")
    transport.deliver_broadcast = AsyncMock()
    bus = MessageBus()
    bus.register_transport(transport)
    bus.set_delivery_observer(recorder.on_delivered)
    await bus.submit(
        Envelope(origin=Origin.CRON, chat_id=1, transport="mx", result_text="other transport")
    )
    transport.deliver_broadcast.assert_awaited_once()
    assert not store.directory.exists()


async def test_heartbeat_ack_path_never_calls_bus_or_history(orch: Orchestrator) -> None:
    store, _, recorder = storage(orch)
    bus = MessageBus()
    bus.set_delivery_observer(recorder.on_delivered)
    orch.wire_observers_to_bus(bus)
    observer = orch._observers.heartbeat
    orch.config.allowed_user_ids = [1]
    handler = AsyncMock(return_value=HeartbeatOutcome(HeartbeatStatus.ACK))
    observer.set_heartbeat_handler(handler)
    with patch.object(bus, "submit", wraps=bus.submit) as submit:
        await observer._run_for_chat(1)
    await observer.stop()
    handler.assert_awaited_once()
    submit.assert_not_awaited()
    assert not store.directory.exists()
    await recorder.on_delivered(from_heartbeat(1, "visible alert"))
    assert (await store.read("1"))[0].kind == "heartbeat"


async def test_cancel_during_begin_still_finishes_busy(orch: Orchestrator) -> None:
    store, hub, recorder = storage(orch)
    queue = hub.subscribe()
    orch.set_history_recorder(recorder)
    with (
        patch.object(store, "append", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await orch.handle_message(SessionKey.telegram(1), "hello")
    assert [event["busy"] for event in events(queue)] == [True, False]
    orch._route_message.assert_not_awaited()
