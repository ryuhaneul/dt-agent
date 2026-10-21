"""Original input receipt through durable IA retry and task-question delivery."""

from __future__ import annotations
import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ductor_bot.bus.adapters import from_task_question, from_interagent_result
from ductor_bot.bus.bus import MessageBus
from ductor_bot.cli.types import AgentResponse
from ductor_bot.tasks.hub import TaskHub
from ductor_bot.tasks.models import TaskEntry
from tests.multiagent.test_async_persistence import _setup_bus, _task, _result
from tests.multiagent.test_delegation_context import agents as agents


@pytest.mark.parametrize("kind", ["success", "failure", "lost"])
@pytest.mark.parametrize("transport", ["tg", "mx", "sl"])
@pytest.mark.parametrize("restart", [False, True])
async def test_original_receipt_survives_actual_result_retry_and_restart(
    tmp_path, agents, kind, transport, restart
):
    _, main, _ = agents
    main._config.user_timezone = "UTC"
    home = tmp_path / "durable"
    home.mkdir()
    ia, bus, adapter = _setup_bus(home)
    main.wire_observers_to_bus(bus)
    adapter.transport_name = transport
    task = _task(transport=transport)
    ia._record_store.create(task)
    result = _result(
        task,
        success=kind == "success",
        error=None if kind == "success" else "synthetic failure",
        failure_kind="result_lost" if kind == "lost" else "",
    )
    captures = []

    async def execute(request):
        captures.append(request.received_context)
        if len(captures) == 1:
            raise OSError("synthetic injection outage")
        return AgentResponse(result="final report", session_id="synthetic")

    main._cli_service.execute.side_effect = execute
    ia._retry_delays = () if restart else (1.05,)
    await ia._deliver_async_result(result)
    if restart:
        assert ia._record_store.path_for("main", task.task_id, area="undelivered").exists()
        await asyncio.sleep(1.05)
        restored, restored_bus, restored_adapter = _setup_bus(home)
        restored_adapter.transport_name = transport
        main.wire_observers_to_bus(restored_bus)
        assert await restored.redeliver_undelivered("main") == 1
    assert len(captures) == 2 and captures[0]
    assert captures[1] == captures[0], captures


def _asked(prompt: str) -> str:
    """The question inside the prompt the parent session gets (header and advice removed)."""
    assert prompt.startswith("Background task ")
    return prompt.split("\n", 1)[1].rsplit("\n\nThe task is waiting", 1)[0]


async def test_task_question_receipt_is_not_forwarded_task_dispatch_time(agents, tmp_path):
    _, main, _ = agents
    main._config.user_timezone = "UTC"
    bus = MessageBus()
    main.wire_observers_to_bus(bus)
    adapter = AsyncMock()
    adapter.transport_name = "tg"
    bus.register_transport(adapter)
    entry = TaskEntry(
        "abcdef123456", 12345, "main", "synthetic", "raw", "claude", "sonnet", "running"
    )
    registry = SimpleNamespace(get=lambda _: entry, update_status=lambda *_a, **_kw: None)
    hub = TaskHub(registry, main.paths, config=main._config.tasks)
    done = asyncio.Event()

    async def receive(task_id, question, preview, chat_id, thread, _transport=""):
        await bus.submit(from_task_question(task_id, question, preview, chat_id, topic_id=thread))
        done.set()

    hub.set_question_handler("main", receive)
    before = time.time()
    await hub.forward_question(entry.task_id, "Which target?")
    # Simulate a busy event loop between API intake and fire-and-forget delivery.
    time.sleep(1.05)
    await done.wait()
    request = main._cli_service.execute.await_args.args[0]
    stamp = datetime.fromtimestamp(before, UTC).strftime("%Y-%m-%d %H:%M:%S %Z")
    assert request.received_context == f"[Received: {stamp}]", request.received_context


def test_result_timestamp_is_carried_only_as_metadata(monkeypatch):
    result = _result(_task(), received_at=1760000000.0)
    first = from_interagent_result(result, 123, injection_prompt="original prompt")
    monkeypatch.setattr(time, "time", lambda: 1760000090.0)
    second = from_interagent_result(result, 123, injection_prompt="original prompt")
    assert first.created_at == second.created_at == result.received_at
    assert first.prompt == second.prompt == "original prompt"
    assert "[Received:" not in first.prompt_preview
    assert "[Received:" not in first.result_text


def test_saved_receipt_survives_record_and_legacy_compatibility(tmp_path, monkeypatch):
    import json
    from ductor_bot.multiagent.persistence import AsyncRecordStore

    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    result = _result(task, received_at=1760000000.0)
    store.update("main", task.task_id, result=result)
    path = store.path_for("main", task.task_id)
    raw = json.loads(path.read_text())
    assert raw["result"]["received_at"] == result.received_at
    assert (
        store.result_from_record(store.load("main", task.task_id)).received_at == result.received_at
    )
    assert path.stat().st_mode & 0o777 == 0o600

    del raw["result"]["received_at"]
    raw["updated_at"] = 1760000005.0
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(time, "time", lambda: 1760000100.0)
    loaded = store.load("main", task.task_id)
    restored = store.result_from_record(loaded)
    assert restored.received_at == 1760000005.0
    store.update("main", task.task_id, stage="received")
    monkeypatch.setattr(time, "time", lambda: 1760000200.0)
    assert (
        store.result_from_record(store.load("main", task.task_id)).received_at
        == restored.received_at
    )
    assert store.result_from_record(raw).received_at == restored.received_at


@pytest.mark.parametrize(
    "invalid", [None, True, "yesterday", -1, float("nan"), float("inf"), 1e100]
)
def test_invalid_result_receipt_is_rejected_without_discarding_legacy_files(tmp_path, invalid):
    import json
    from ductor_bot.multiagent.persistence import AsyncRecordStore, CorruptAsyncRecordError

    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    store.update("main", task.task_id, result=_result(task))
    path = store.path_for("main", task.task_id)
    raw = json.loads(path.read_text())
    raw["result"]["received_at"] = invalid
    path.write_text(json.dumps(raw))
    with pytest.raises(CorruptAsyncRecordError):
        store.load("main", task.task_id)


@pytest.mark.parametrize("value", [None, True, "yesterday", -1, float("nan"), float("inf"), 1e100])
def test_task_question_legacy_or_invalid_timestamp_defaults_to_unknown(value):
    raw = TaskEntry("task", 123, "main", "check", "raw", "claude", "sonnet", "waiting").to_dict()
    raw.pop("last_question_received_at")
    assert TaskEntry.from_dict(raw).last_question_received_at == 0.0
    raw["last_question_received_at"] = value
    assert TaskEntry.from_dict(raw).last_question_received_at == 0.0


async def test_actual_question_registry_restart_reuses_saved_receipt(tmp_path, agents, monkeypatch):
    from ductor_bot.infra.input_context import current_input_timestamp
    from ductor_bot.tasks.models import TaskSubmit
    from ductor_bot.tasks.registry import TaskRegistry

    _, main, _ = agents
    registry = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
    entry = registry.create(TaskSubmit(123, "original", 1, 42, "main"), "claude", "sonnet")
    hub = TaskHub(registry, main.paths, config=main._config.tasks)
    captures, done = [], asyncio.Event()

    async def receive(task_id, question, preview, chat_id, thread, _transport=""):
        captures.append((_asked(question), current_input_timestamp()))
        done.set()

    hub.set_question_handler("main", receive)
    monkeypatch.setattr(time, "time", lambda: 1760000000.0)
    await hub.forward_question(entry.task_id, "Which target?")
    await done.wait()
    saved = registry.get(entry.task_id)
    assert saved.last_question_received_at == 1760000000.0
    loaded = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
    restored = loaded.get(entry.task_id)
    assert restored.last_question_received_at == saved.last_question_received_at
    monkeypatch.setattr(time, "time", lambda: 1760000200.0)
    restored_hub = TaskHub(loaded, main.paths, config=main._config.tasks)
    await restored_hub._deliver_question(receive, restored, restored.last_question)
    assert captures == [("Which target?", 1760000000.0)] * 2
    assert "[Received:" not in restored.last_question
    assert "[Received:" not in restored.prompt_preview


async def test_concurrent_questions_keep_distinct_receipts(agents, monkeypatch):
    from ductor_bot.infra.input_context import current_input_timestamp

    _, main, _ = agents
    entry = TaskEntry("task", 123, "main", "check", "raw", "claude", "sonnet", "running")

    def update(_task_id, _status, **changes):
        for key, value in changes.items():
            setattr(entry, key, value)

    registry = SimpleNamespace(get=lambda _: entry, update_status=update)
    hub = TaskHub(registry, main.paths, config=main._config.tasks)
    seen, done = [], asyncio.Event()

    async def receive(task_id, question, preview, chat_id, thread, _transport=""):
        await asyncio.sleep(0)
        seen.append((_asked(question), current_input_timestamp()))
        if len(seen) == 2:
            done.set()

    hub.set_question_handler("main", receive)
    monkeypatch.setattr(time, "time", lambda: 1760000000.0)
    await hub.forward_question("task", "first")
    monkeypatch.setattr(time, "time", lambda: 1760000010.0)
    await hub.forward_question("task", "second")
    await done.wait()
    assert seen == [("first", 1760000000.0), ("second", 1760000010.0)]
    assert entry.last_question_received_at == 1760000010.0


def test_default_result_receipt_is_captured_at_result_creation(monkeypatch):
    from ductor_bot.multiagent.bus import _make_result

    monkeypatch.setattr(time, "time", lambda: 1760000000.0)
    task = _task()
    result = _make_result(task, 1759999990.0, result_text="answer")
    assert result.received_at == 1760000000.0
    monkeypatch.setattr(time, "time", lambda: 1760000100.0)
    assert from_interagent_result(result, 123).created_at == 1760000000.0


async def test_legacy_question_first_recovery_receipt_is_persisted_once(
    tmp_path, agents, monkeypatch
):
    import json
    from ductor_bot.infra.input_context import current_input_timestamp
    from ductor_bot.tasks.models import TaskSubmit
    from ductor_bot.tasks.registry import TaskRegistry

    _, main, _ = agents
    path = tmp_path / "tasks.json"
    registry = TaskRegistry(path, tmp_path / "tasks")
    entry = registry.create(TaskSubmit(123, "original", 1, None, "main"), "claude", "sonnet")
    registry.update_status(entry.task_id, "waiting", last_question="Legacy question")
    old = json.loads(path.read_text())
    old["tasks"][0].pop("last_question_received_at")
    path.write_text(json.dumps(old))
    restored_registry = TaskRegistry(path, tmp_path / "tasks")
    restored = restored_registry.get(entry.task_id)
    assert restored.last_question_received_at == 0.0
    hub = TaskHub(restored_registry, main.paths, config=main._config.tasks)
    receipts = []

    async def receive(*args):
        receipts.append(current_input_timestamp())

    monkeypatch.setattr(time, "time", lambda: 1760000100.0)
    await hub._deliver_question(receive, restored, restored.last_question)
    again = TaskRegistry(path, tmp_path / "tasks")
    recovered = again.get(entry.task_id)
    assert recovered.last_question_received_at == 1760000100.0
    monkeypatch.setattr(time, "time", lambda: 1760000200.0)
    replay_hub = TaskHub(again, main.paths, config=main._config.tasks)
    await replay_hub._deliver_question(receive, recovered, recovered.last_question)
    assert receipts == [1760000100.0, 1760000100.0]
