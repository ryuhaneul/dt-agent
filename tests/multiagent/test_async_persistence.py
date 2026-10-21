"""Durable intake and delivery stages for asynchronous inter-agent results."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.bus.adapters import build_interagent_injection_prompt, from_interagent_result
from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import IA_CHUNK_SCHEMES, DeliveryOutcome
from ductor_bot.multiagent.bus import (
    AsyncInterAgentResult,
    AsyncInterAgentTask,
    AsyncRecordError,
    AsyncSendOptions,
    InterAgentBus,
)
from ductor_bot.multiagent.persistence import AsyncRecordStore, CorruptAsyncRecordError, target_key

if TYPE_CHECKING:
    from ductor_bot.multiagent.supervisor import AgentSupervisor


def _task(**kwargs: object) -> AsyncInterAgentTask:
    fields = {
        "task_id": "abcdef123456",
        "sender": "main",
        "recipient": "alpha.v2",
        "message": "Original request",
        "chat_id": 123,
        "topic_id": 8,
        "transport": "tg",
        "result_origin": "user-task",
        "broadcast_ref": "ref-1",
    }
    fields.update(kwargs)
    return AsyncInterAgentTask(**fields)


def _result(task: AsyncInterAgentTask, **kwargs: object) -> AsyncInterAgentResult:
    fields = {
        "task_id": task.task_id,
        "sender": task.sender,
        "recipient": task.recipient,
        "message_preview": "Original request",
        "result_text": "Completed",
        "success": True,
        "error": None,
        "elapsed_seconds": 2.5,
        "session_name": "ia.main",
        "provider_switch_notice": "switched",
        "original_message": task.message,
        "chat_id": task.chat_id,
        "topic_id": task.topic_id,
        "transport": task.transport,
        "reply_to": task.reply_to,
        "result_origin": task.result_origin,
        "broadcast_ref": task.broadcast_ref,
        "failure_count": 0,
    }
    fields.update(kwargs)
    return AsyncInterAgentResult(**fields)


def _setup_bus(home: Path, *, response: str = "Final report") -> tuple[InterAgentBus, MessageBus, AsyncMock]:
    ia = InterAgentBus(home)
    message_bus = MessageBus()
    injector = AsyncMock()
    injector.inject_prompt = AsyncMock(return_value=response)
    message_bus.set_injector(injector)
    transport = AsyncMock()
    transport.transport_name = "tg"
    message_bus.register_transport(transport)
    main_bot = SimpleNamespace(_bus=message_bus, orchestrator=SimpleNamespace())
    sub_bot = SimpleNamespace(orchestrator=SimpleNamespace(
        handle_interagent_message=AsyncMock(return_value=("Completed", "ia.main", ""))
    ))
    ia.register("main", SimpleNamespace(config=SimpleNamespace(transport="telegram"), bot=main_bot))
    ia.register("alpha.v2", SimpleNamespace(config=SimpleNamespace(transport="telegram"), bot=sub_bot))

    async def deliver(result: AsyncInterAgentResult) -> DeliveryOutcome:
        prompt = build_interagent_injection_prompt(
            result, agent_name="main", transport_label="Telegram chat"
        )
        return await message_bus.submit(
            from_interagent_result(result, 0, injection_prompt=prompt)
        )

    ia.set_async_result_handler("main", deliver)
    return ia, message_bus, transport


def test_record_schema_permissions_and_roundtrip(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    path = store.path_for("main", task.task_id)
    assert path == tmp_path / "interagent" / "inflight" / target_key("main") / "abcdef123456.json"
    assert target_key("main") == hashlib.sha256(b"main").hexdigest()[:32]
    assert path.stat().st_mode & 0o777 == 0o600
    for directory in (path.parent, path.parent.parent, store.root):
        assert directory.stat().st_mode & 0o777 == 0o700
    record = store.load("main", task.task_id)
    assert (record["target"], record["recipient"], record["stage"], record["status"]) == (
        "main", "alpha.v2", "received", "inflight"
    )
    assert (record["result_origin"], record["broadcast_ref"]) == ("user-task", "ref-1")
    result = _result(task)
    store.update("main", task.task_id, result=result)
    record = store.load("main", task.task_id)
    restored = store.result_from_record(record)
    assert restored == result
    assert record["status"] == "completed"
    assert build_interagent_injection_prompt(
        restored, agent_name="main", transport_label="Telegram chat"
    ) == build_interagent_injection_prompt(
        result, agent_name="main", transport_label="Telegram chat"
    )
    assert path.stat().st_mode & 0o777 == 0o600


def test_defaults_failed_result_and_original_target(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task(reply_to="alpha.v2", result_origin="", broadcast_ref="")
    store.create(task)
    path = store.path_for("alpha.v2", task.task_id)
    assert path.exists()
    assert path.parent.name == hashlib.sha256(b"alpha.v2").hexdigest()[:32]
    assert not store.path_for("main", task.task_id).exists()
    result = _result(task, success=False, error="interrupted by restart")
    store.update("alpha.v2", task.task_id, result=result)
    record = store.load("alpha.v2", task.task_id)
    assert record["target"] == "alpha.v2"
    assert (record["status"], record["result_origin"], record["broadcast_ref"]) == (
        "failed", "", ""
    )
    assert store.result_from_record(record) == result


def test_invalid_id_and_symlink_root_create_no_file(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    with pytest.raises(ValueError, match="task_id"):
        store.create(_task(task_id="../bad"))
    assert not store.root.exists()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    store.root.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        store.create(_task())
    assert list(elsewhere.iterdir()) == []


async def test_intake_persists_before_task_and_rejects_bad_reply_to(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ia, _message_bus, _transport = _setup_bus(tmp_path)
    with pytest.raises(Exception, match="receiver"):
        ia.send_async("main", "alpha.v2", "Original request", opts=AsyncSendOptions(reply_to="../ghost"))
    assert not (tmp_path / "interagent").exists()
    monkeypatch.setattr("ductor_bot.multiagent.bus.secrets.token_hex", lambda _n: "../bad")
    with pytest.raises(AsyncRecordError, match="persist"):
        ia.send_async("main", "alpha.v2", "Original request")
    assert not (tmp_path / "interagent").exists()


async def test_completed_result_and_injection_saved_before_delivery(tmp_path: Path) -> None:
    ia, _message_bus, transport = _setup_bus(tmp_path)
    store = AsyncRecordStore(tmp_path)
    observed: list[dict] = []

    async def inspect(_env: object) -> None:
        path = next((tmp_path / "interagent" / "inflight").glob("*/*.json"))
        observed.append(json.loads(path.read_text()))

    transport.deliver.side_effect = inspect
    task_id = ia.send_async(
        "main", "alpha.v2", "Original request",
        opts=AsyncSendOptions(chat_id=123, topic_id=8, transport="tg", result_origin="user-task", broadcast_ref="ref-1"),
    )
    assert task_id is not None
    assert store.load("main", task_id)["status"] == "inflight"
    await ia._async_tasks[task_id].asyncio_task
    assert not store.path_for("main", task_id).exists()
    assert observed[0]["status"] == "completed"
    assert observed[0]["result"]["result_origin"] == "user-task"
    assert observed[0]["result"]["broadcast_ref"] == "ref-1"
    assert observed[0]["stage"] == "injected"
    assert observed[0]["main_response"] == "Final report"
    assert observed[0]["injected_at"] is not None


async def test_blank_injection_does_not_advance_stage(tmp_path: Path) -> None:
    ia, _message_bus, transport = _setup_bus(tmp_path, response=" \n ")
    ia._retry_delays = ()
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task
    path = AsyncRecordStore(tmp_path).path_for("main", task_id, area="undelivered")
    record = json.loads(path.read_text())
    assert (record["status"], record["stage"], record["main_response"]) == ("completed", "received", "")
    transport.deliver.assert_not_awaited()


async def test_storage_failure_rejects_before_task_creation(tmp_path: Path) -> None:
    ia, _message_bus, _transport = _setup_bus(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "interagent").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(AsyncRecordError, match="persist"):
        ia.send_async("main", "alpha.v2", "Original request")
    assert not ia._async_tasks
    assert list(elsewhere.iterdir()) == []


async def test_failed_delivery_retains_injected_response(tmp_path: Path) -> None:
    ia, _message_bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = ()
    async def fail_delivery(env: object) -> None:
        env.delivered = False
        env.delivery_error = "send failed"
    transport.deliver.side_effect = fail_delivery
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task
    path = AsyncRecordStore(tmp_path).path_for("main", task_id, area="undelivered")
    record = json.loads(path.read_text())
    assert record["stage"] == "injected"
    assert record["main_response"] == "Final report"


async def test_update_io_failure_logs_but_delivery_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ia, _message_bus, transport = _setup_bus(tmp_path)
    store = ia._record_store
    assert store is not None
    monkeypatch.setattr(store, "update", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task
    transport.deliver.assert_awaited_once()
    assert "Async record update failed" in caplog.text


async def test_retry_delivery_only_after_saved_injection(tmp_path: Path) -> None:
    ia, message_bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (0,)
    store = ia._record_store
    assert store is not None
    original_delete = store.delete
    deleted_stages: list[str] = []

    def inspect_delete(target: str, task_id: str) -> None:
        deleted_stages.append(store.load(target, task_id)["stage"])
        original_delete(target, task_id)

    store.delete = inspect_delete
    attempts = 0

    async def send(env: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            env.delivered = False
            env.delivery_error = "send failed"

    transport.deliver.side_effect = send
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task
    assert attempts == 2
    assert message_bus._injector.inject_prompt.await_count == 1
    assert deleted_stages == ["delivered"]
    assert not AsyncRecordStore(tmp_path).path_for("main", task_id).exists()


async def test_five_retries_preserve_record_and_log_routing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ia, message_bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (0,) * 5

    async def fail(env: object) -> None:
        env.delivered = False
        env.delivery_error = "send failed"

    transport.deliver.side_effect = fail
    task_id = ia.send_async(
        "main", "alpha.v2", "Original request",
        opts=AsyncSendOptions(chat_id=123, topic_id=8, transport="tg"),
    )
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task
    store = AsyncRecordStore(tmp_path)
    path = store.path_for("main", task_id, area="undelivered")
    assert path.exists()
    assert not store.path_for("main", task_id).exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(path.read_text())["stage"] == "injected"
    assert transport.deliver.await_count == 6
    assert message_bus._injector.inject_prompt.await_count == 1
    assert all(part in caplog.text for part in (task_id, "target=main", "chat_id=123", "topic_id=8", "transport=tg"))


async def test_failed_agent_result_still_injects_and_reports(tmp_path: Path) -> None:
    ia, message_bus, transport = _setup_bus(tmp_path)
    ia._agents["alpha.v2"].bot.orchestrator.handle_interagent_message = AsyncMock(
        side_effect=RuntimeError("agent failed")
    )
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task
    assert message_bus._injector.inject_prompt.await_count == 1
    prompt = message_bus._injector.inject_prompt.await_args.args[0]
    assert "agent failed" in prompt
    assert "Original request" in prompt
    transport.deliver.assert_awaited_once()
    assert not AsyncRecordStore(tmp_path).path_for("main", task_id).exists()


async def test_receiver_stopped_before_completion_is_preserved(tmp_path: Path) -> None:
    import asyncio

    ia, _message_bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (0,) * 5
    release = asyncio.Event()

    async def finish(*_args: object, **_kwargs: object) -> tuple[str, str, str]:
        await release.wait()
        return "Completed", "ia.main", ""

    ia._agents["alpha.v2"].bot.orchestrator.handle_interagent_message = AsyncMock(side_effect=finish)
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    ia.unregister("main")
    release.set()
    await ia._async_tasks[task_id].asyncio_task
    assert AsyncRecordStore(tmp_path).path_for("main", task_id, area="undelivered").exists()
    transport.deliver.assert_not_awaited()


async def test_cancel_during_retry_wait_moves_completed_record(tmp_path: Path) -> None:
    import asyncio
    import contextlib

    ia, _message_bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (3600,)

    async def fail(env: object) -> None:
        env.delivered = False
        env.delivery_error = "send failed"

    transport.deliver.side_effect = fail
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    running = ia._async_tasks[task_id].asyncio_task
    for _ in range(100):
        if task_id in ia._retry_waiting:
            break
        await asyncio.sleep(0)
    assert task_id in ia._retry_waiting
    assert await ia.cancel_all_async() == 1
    with contextlib.suppress(asyncio.CancelledError):
        await running
    store = AsyncRecordStore(tmp_path)
    assert not store.path_for("main", task_id).exists()
    assert store.path_for("main", task_id, area="undelivered").exists()


async def test_blank_injection_retries_injection(tmp_path: Path) -> None:
    ia, message_bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (0,)
    message_bus._injector.inject_prompt.side_effect = ["  ", "Final report"]
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task
    assert message_bus._injector.inject_prompt.await_count == 2
    transport.deliver.assert_awaited_once()
    assert not AsyncRecordStore(tmp_path).path_for("main", task_id).exists()


async def test_headless_receiver_after_intake_is_preserved(tmp_path: Path) -> None:
    ia, _message_bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (0,) * 5
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    ia._agents["main"].config.transport = "headless"
    await ia._async_tasks[task_id].asyncio_task
    assert AsyncRecordStore(tmp_path).path_for("main", task_id, area="undelivered").exists()
    transport.deliver.assert_not_awaited()


async def test_retry_does_not_increment_failure_ledger(tmp_path: Path) -> None:
    ia, message_bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (0, 0)
    ia._agents["alpha.v2"].bot.orchestrator.handle_interagent_message = AsyncMock(
        side_effect=RuntimeError("agent failed")
    )

    async def fail(env: object) -> None:
        env.delivered = False
        env.delivery_error = "send failed"

    transport.deliver.side_effect = fail
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task
    entries = json.loads((tmp_path / "interagent" / "failures.json").read_text())
    assert len(entries) == 1
    assert next(iter(entries.values()))["count"] == 1
    assert message_bus._injector.inject_prompt.await_count == 1


async def test_handler_exception_retries_with_current_handler(tmp_path: Path) -> None:
    ia, _message_bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (0,)
    original = ia._async_result_handlers["main"]
    calls = 0

    async def first(_result: AsyncInterAgentResult) -> DeliveryOutcome:
        nonlocal calls
        calls += 1
        ia.set_async_result_handler("main", original)
        raise RuntimeError("temporary handler failure")

    ia.set_async_result_handler("main", first)
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task
    assert calls == 1
    transport.deliver.assert_awaited_once()
    assert not AsyncRecordStore(tmp_path).path_for("main", task_id).exists()


async def test_cancel_running_request_keeps_inflight_record(tmp_path: Path) -> None:
    import asyncio
    import contextlib

    ia, _message_bus, _transport = _setup_bus(tmp_path)
    entered = asyncio.Event()

    async def never_finish(*_args: object, **_kwargs: object) -> tuple[str, str, str]:
        entered.set()
        await asyncio.Event().wait()
        return "Completed", "", ""

    ia._agents["alpha.v2"].bot.orchestrator.handle_interagent_message = AsyncMock(
        side_effect=never_finish
    )
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    running = ia._async_tasks[task_id].asyncio_task
    await entered.wait()
    assert await ia.cancel_all_async() == 1
    with contextlib.suppress(asyncio.CancelledError):
        await running
    store = AsyncRecordStore(tmp_path)
    assert store.load("main", task_id)["status"] == "inflight"
    assert not store.path_for("main", task_id, area="undelivered").exists()


async def test_injected_record_without_response_falls_back_to_memory(tmp_path: Path) -> None:
    ia, message_bus, transport = _setup_bus(tmp_path)
    task = _task()
    result = _result(task)
    store = AsyncRecordStore(tmp_path)
    store.create(task)
    store.update("main", task.task_id, result=result, stage="injected")
    await ia._deliver_async_result(result)
    assert not store.path_for("main", task.task_id).exists()
    assert len(list((store.root / "corrupt" / target_key("main")).glob("*.json"))) == 1
    message_bus._injector.inject_prompt.assert_awaited_once()
    transport.deliver.assert_awaited_once()


@pytest.mark.parametrize(
    "payload",
    ["null", "[]", '"text"', "123", '{"schema": 1}', '{"schema": 2}'],
)
async def test_corrupt_record_does_not_block_result_delivery(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, payload: str
) -> None:
    ia, message_bus, transport = _setup_bus(tmp_path)
    task = _task()
    result = _result(task)
    store = AsyncRecordStore(tmp_path)
    store.create(task)
    path = store.path_for("main", task.task_id)
    if payload == '{"schema": 2}':
        record = json.loads(path.read_text())
        record["schema"] = 2
        payload = json.dumps(record)
    path.write_text(payload)
    await ia._deliver_async_result(result)
    assert message_bus._injector.inject_prompt.await_count == 1
    transport.deliver.assert_awaited_once()
    assert not path.exists()
    archives = list((store.root / "corrupt" / target_key("main")).glob("*.json"))
    assert len(archives) == 1
    assert archives[0].read_text() == payload
    assert "Async record update failed path=" in caplog.text
    assert "Async corrupt record quarantined path=" in caplog.text
    assert "CorruptAsyncRecordError" in caplog.text


async def test_record_corrupted_between_retries_still_reports(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ia, message_bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (0,)
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    path = AsyncRecordStore(tmp_path).path_for("main", task_id)
    attempts = 0

    async def send(env: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            path.write_text("null")
            env.delivered = False
            env.delivery_error = "send failed"

    transport.deliver.side_effect = send
    await ia._async_tasks[task_id].asyncio_task
    assert attempts == 2
    assert message_bus._injector.inject_prompt.await_count == 1
    assert not path.exists()
    assert len(list((AsyncRecordStore(tmp_path).root / "corrupt" / target_key("main")).glob("*.json"))) == 1
    assert "Async corrupt record quarantined" in caplog.text


async def test_type_error_during_update_logs_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ia, message_bus, transport = _setup_bus(tmp_path)
    store = ia._record_store
    assert store is not None
    task = _task()
    store.create(task)
    monkeypatch.setattr(
        store, "update", lambda *_args, **_kwargs: (_ for _ in ()).throw(TypeError())
    )
    await ia._deliver_async_result(_result(task))
    assert message_bus._injector.inject_prompt.await_count == 1
    transport.deliver.assert_awaited_once()
    assert store.path_for("main", task.task_id).exists()
    assert "TypeError" in caplog.text


async def test_missing_required_result_field_still_delivers(tmp_path: Path) -> None:
    ia, message_bus, transport = _setup_bus(tmp_path)
    task = _task()
    store = AsyncRecordStore(tmp_path)
    store.create(task)
    store.update("main", task.task_id, result=_result(task))
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    del record["result"]["result_text"]
    path.write_text(json.dumps(record))
    await ia._deliver_async_result(_result(task))
    message_bus._injector.inject_prompt.assert_awaited_once()
    transport.deliver.assert_awaited_once()
    assert not path.exists()
    assert len(list((store.root / "corrupt" / target_key("main")).glob("*.json"))) == 1


async def test_failed_chunk_progress_persists_and_retry_starts_after_it(tmp_path: Path) -> None:
    ia, message_bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (0, 0)
    store = AsyncRecordStore(tmp_path)
    ia._agents["alpha.v2"].bot.orchestrator.handle_interagent_message = AsyncMock(
        return_value=("Completed", "ia.main", "switched")
    )
    seen: list[tuple[int, int, str, bool]] = []

    async def send(env: object) -> None:
        task_id = env.metadata["ia_task_id"]
        record = store.load("main", task_id)
        seen.append((
            env.start_chunk,
            record["delivered_chunks"],
            env.metadata["provider_switch_notice"],
            record["stage"] == "injected",
        ))
        if len(seen) <= 2:
            # The second report is smaller; persisted progress cannot regress.
            env.chunk_scheme = IA_CHUNK_SCHEMES["tg"]
            env.delivered_chunks = 2 if len(seen) == 1 else 1
            env.delivered = False
            env.delivery_error = "send failed"

    transport.deliver.side_effect = send
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task

    assert seen == [
        (0, 0, "switched", True),
        (2, 2, "", True),
        (2, 2, "", True),
    ]
    assert message_bus._injector.inject_prompt.await_count == 1
    assert not store.path_for("main", task_id).exists()


def test_legacy_record_without_chunk_progress_defaults_to_zero(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    record.pop("delivered_chunks")
    path.write_text(json.dumps(record))
    assert store.load("main", task.task_id)["delivered_chunks"] == 0
    store.update("main", task.task_id, result=_result(task))
    store.update("main", task.task_id, stage="injected", main_response="Final report")
    store.update(
        "main", task.task_id, delivered_chunks=2,
        delivered_text_sha256=hashlib.sha256(b"Final report").hexdigest(),
        chunk_scheme=IA_CHUNK_SCHEMES["tg"],
    )
    assert store.load("main", task.task_id)["delivered_chunks"] == 2
    assert store.result_from_record(store.load("main", task.task_id)).delivered_chunks == 2


@pytest.mark.parametrize("success", [True, False])
def test_retry_envelope_omits_provider_notice_for_all_results(success: bool) -> None:
    task = _task()
    result = _result(
        task, success=success, error=None if success else "failed",
        delivered_chunks=2, main_response="Final report",
    )
    first = from_interagent_result(result, 0, injection_prompt="prompt")
    assert first.metadata["provider_switch_notice"] == "switched"
    assert first.start_chunk == 2
    result.delivery_retry = True
    retried = from_interagent_result(result, 0, injection_prompt="prompt")
    assert retried.metadata["provider_switch_notice"] == ""
    assert retried.start_chunk == 2
    assert retried.delivered_chunks == 2


@pytest.mark.parametrize("damaged", [False, True])
async def test_unreadable_retry_resets_chunk_offset_and_logs_duplicate_risk(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, damaged: bool
) -> None:
    ia, _message_bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (0,)
    store = AsyncRecordStore(tmp_path)
    offsets: list[int] = []

    async def send(env: object) -> None:
        offsets.append(env.start_chunk)
        if len(offsets) == 1:
            path = store.path_for("main", env.metadata["ia_task_id"])
            if damaged:
                path.write_text("null")
            else:
                path.unlink()
            env.chunk_scheme = IA_CHUNK_SCHEMES["tg"]
            env.delivered_chunks = 1
            env.delivered = False
            env.delivery_error = "send failed"

    transport.deliver.side_effect = send
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task
    assert offsets == ([0, 1] if damaged else [0, 0])
    if damaged:
        assert "Async corrupt record quarantined" in caplog.text
    else:
        assert "duplicate chunk delivery is possible" in caplog.text


@pytest.mark.parametrize("field", ["chat_id", "topic_id"])
def test_completed_result_missing_routing_field_is_corrupt(tmp_path: Path, field: str) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task(chat_id=-100123, topic_id=8)
    store.create(task)
    store.update("main", task.task_id, result=_result(task))
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    del record["result"][field]
    path.write_text(json.dumps(record))
    with pytest.raises(CorruptAsyncRecordError):
        store.load("main", task.task_id)


@pytest.mark.parametrize("status", ["completed", "failed"])
@pytest.mark.parametrize("remove", [False, True])
def test_finished_record_without_result_is_corrupt(
    tmp_path: Path, status: str, remove: bool
) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    record["status"] = status
    if remove:
        del record["result"]
    path.write_text(json.dumps(record))
    with pytest.raises(CorruptAsyncRecordError):
        store.load("main", task.task_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [("chat_id", 0), ("topic_id", None), ("transport", "mx"),
     ("reply_to", "other"), ("sender", "other"), ("recipient", "other"),
     ("result_origin", "other"), ("broadcast_ref", "other")],
)
def test_result_routing_must_match_intake(
    tmp_path: Path, field: str, value: object
) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task(chat_id=-100123, topic_id=8)
    store.create(task)
    store.update("main", task.task_id, result=_result(task))
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    record["result"][field] = value
    path.write_text(json.dumps(record))
    with pytest.raises(CorruptAsyncRecordError):
        store.load("main", task.task_id)


def test_reconstructed_result_uses_intake_routing(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task(chat_id=-100123, topic_id=8)
    store.create(task)
    store.update("main", task.task_id, result=_result(task))
    record = store.load("main", task.task_id)
    restored = store.result_from_record(record)
    assert (restored.chat_id, restored.topic_id) == (-100123, 8)
    # The reconstruction API takes routing from intake even if its caller
    # passes an unchecked record.
    record["result"]["chat_id"] = 0
    record["result"]["topic_id"] = None
    restored = store.result_from_record(record)
    assert (restored.chat_id, restored.topic_id) == (-100123, 8)


@pytest.mark.parametrize("remove", [False, True])
def test_inflight_record_allows_no_result(tmp_path: Path, remove: bool) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    if remove:
        del record["result"]
    path.write_text(json.dumps(record))
    assert store.load("main", task.task_id)["status"] == "inflight"


@pytest.mark.parametrize("value", [-1, "1", True])
def test_invalid_chunk_progress_is_corrupt(tmp_path: Path, value: object) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    record["delivered_chunks"] = value
    path.write_text(json.dumps(record))
    with pytest.raises(CorruptAsyncRecordError):
        store.load("main", task.task_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [("success", "yes"), ("error", 7), ("elapsed_seconds", "1"),
     ("failure_count", True), ("delivery_retry", 0),
     ("message_preview", None), ("delivered_chunks", -1)],
)
def test_completed_result_field_types_are_validated(
    tmp_path: Path, field: str, value: object
) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    store.update("main", task.task_id, result=_result(task))
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    record["result"][field] = value
    path.write_text(json.dumps(record))
    with pytest.raises(CorruptAsyncRecordError):
        store.load("main", task.task_id)


@pytest.mark.parametrize("damage", ["missing_chat", "missing_topic", "missing_result"])
async def test_corrupt_routing_or_result_keeps_memory_delivery(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, damage: str
) -> None:
    ia, _message_bus, transport = _setup_bus(tmp_path)
    task = _task(chat_id=-100123, topic_id=8)
    store = AsyncRecordStore(tmp_path)
    store.create(task)
    store.update("main", task.task_id, result=_result(task))
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    if damage == "missing_result":
        record["result"] = None
    else:
        del record["result"]["chat_id" if damage == "missing_chat" else "topic_id"]
    path.write_text(json.dumps(record))
    ia._retry_delays = ()
    await ia._deliver_async_result(_result(task))
    transport.deliver.assert_awaited_once()
    assert not path.exists()
    assert len(list((store.root / "corrupt" / target_key("main")).glob("*.json"))) == 1
    assert "Async corrupt record quarantined" in caplog.text
    assert "CorruptAsyncRecordError" in caplog.text


def test_legacy_completed_result_without_chunk_fields_reconstructs(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task(chat_id=-100123, topic_id=8)
    store.create(task)
    store.update("main", task.task_id, result=_result(task))
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    del record["delivered_chunks"]
    del record["result"]["delivered_chunks"]
    del record["result"]["delivery_retry"]
    path.write_text(json.dumps(record))
    restored = store.result_from_record(store.load("main", task.task_id))
    assert (restored.chat_id, restored.topic_id, restored.delivered_chunks) == (-100123, 8, 0)


def test_completed_result_unknown_field_is_corrupt(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    store.update("main", task.task_id, result=_result(task))
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    record["result"]["unexpected"] = "value"
    path.write_text(json.dumps(record))
    with pytest.raises(CorruptAsyncRecordError):
        store.load("main", task.task_id)


def test_intake_target_must_match_sender_or_reply_to(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    record["sender"] = "other"
    path.write_text(json.dumps(record))
    with pytest.raises(CorruptAsyncRecordError):
        store.load("main", task.task_id)


@pytest.mark.parametrize("transport_name", ["mx", "sl"])
async def test_matrix_slack_chunk_retry_uses_saved_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport_name: str
) -> None:
    from unittest.mock import MagicMock

    from ductor_bot.messenger.matrix.transport import MatrixTransport
    from ductor_bot.messenger.slack.transport import SlackTransport

    ia, message_bus, _mock_transport = _setup_bus(tmp_path, response="three chunks")
    ia._retry_delays = (0,)
    message_bus._transports.clear()
    bot = MagicMock()
    bot.client = AsyncMock()
    bot.file_roots.return_value = []
    bot.id_map.int_to_room.return_value = "!room:test"
    bot.id_map.int_to_channel.return_value = "C123"
    if transport_name == "mx":
        from ductor_bot.messenger.matrix import sender

        monkeypatch.setattr(sender, "_MAX_EVENT_SIZE", 1)
        monkeypatch.setattr(
            sender, "_split_text",
            lambda _plain, _html: [
                ("first", "first"), ("second", "second"), ("third", "third")
            ],
        )
        message_bus.register_transport(MatrixTransport(bot))
        responses = iter([
            SimpleNamespace(event_id="$1"), object(),
            SimpleNamespace(event_id="$2"), SimpleNamespace(event_id="$3"),
        ])

        async def send_matrix(_room: str, _event: str, _content: dict) -> object:
            if bot.client.room_send.await_count == 3:
                record = AsyncRecordStore(tmp_path).load("main", task_id)
                assert record["delivered_chunks"] == 1
                assert record["chunk_scheme"] == IA_CHUNK_SCHEMES[transport_name]
            return next(responses)

        bot.client.room_send.side_effect = send_matrix
    else:
        from ductor_bot.messenger.slack import sender

        monkeypatch.setattr(sender, "_split_text", lambda _text: [
            "first", "second", "third"
        ])
        message_bus.register_transport(SlackTransport(bot))
        responses = iter([
            {"ok": True, "ts": "1"}, {"ok": False},
            {"ok": True, "ts": "2"}, {"ok": True, "ts": "3"},
        ])

        async def send_slack(**_kwargs: object) -> dict:
            if bot.client.chat_postMessage.await_count == 3:
                record = AsyncRecordStore(tmp_path).load("main", task_id)
                assert record["delivered_chunks"] == 1
                assert record["chunk_scheme"] == IA_CHUNK_SCHEMES[transport_name]
            return next(responses)

        bot.client.chat_postMessage.side_effect = send_slack

    task_id = ia.send_async(
        "main", "alpha.v2", "Original request",
        opts=AsyncSendOptions(chat_id=123, transport=transport_name, silent=True),
    )
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task

    if transport_name == "mx":
        sent = [call.args[2]["body"] for call in bot.client.room_send.await_args_list]
    else:
        sent = [call.kwargs["text"] for call in bot.client.chat_postMessage.await_args_list]
    assert sent == ["first", "second", "second", "third"]
    assert message_bus._injector.inject_prompt.await_count == 1
    assert not AsyncRecordStore(tmp_path).path_for("main", task_id).exists()


def test_old_completed_record_without_new_optional_fields_loads(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    store.update("main", task.task_id, result=_result(task))
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    for key in ("delivered_chunks", "delivered_text_sha256", "chunk_scheme"):
        record.pop(key)
    for key in ("main_response", "delivered_chunks", "delivery_retry"):
        record["result"].pop(key)
    path.write_text(json.dumps(record))
    loaded = store.load("main", task.task_id)
    restored = store.result_from_record(loaded)
    assert (restored.main_response, restored.delivered_chunks, restored.delivery_retry) == (
        "", 0, False,
    )
    assert loaded["delivered_text_sha256"] == ""
    assert loaded["chunk_scheme"] == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("delivered_text_sha256", "bad"),
        ("delivered_text_sha256", 7),
        ("delivered_chunks", True),
        ("chunk_scheme", 7),
        ("chunk_scheme", "bad scheme"),
    ],
)
def test_invalid_optional_progress_fields_are_corrupt(
    tmp_path: Path, field: str, value: object
) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))
    with pytest.raises(CorruptAsyncRecordError):
        store.load("main", task.task_id)


@pytest.mark.parametrize(
    ("stage", "text_hash", "expected"),
    [
        ("received", "matching", 0),
        ("injected", "different", 0),
        ("injected", "", 0),
        ("injected", "matching", 1),
    ],
)
def test_chunk_offset_requires_injected_stage_and_matching_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    stage: str, text_hash: str, expected: int,
) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    store.update("main", task.task_id, result=_result(task))
    if stage == "injected":
        store.update("main", task.task_id, stage="injected", main_response="Final report")
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    record["delivered_chunks"] = 1
    record["chunk_scheme"] = IA_CHUNK_SCHEMES["tg"]
    record["delivered_text_sha256"] = (
        hashlib.sha256(b"Final report").hexdigest() if text_hash == "matching"
        else hashlib.sha256(b"Other report").hexdigest() if text_hash == "different"
        else ""
    )
    path.write_text(json.dumps(record))
    loaded = store.load("main", task.task_id)
    restored = store.result_from_record(loaded)
    assert restored.delivered_chunks == expected
    if not expected:
        assert "duplicate delivery possible" in caplog.text


@pytest.mark.parametrize("damage", ["received", "mismatch", "missing_hash"])
def test_delivery_result_resets_stale_progress(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, damage: str,
) -> None:
    ia, _message_bus, _transport = _setup_bus(tmp_path)
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    result = _result(task, delivered_chunks=1)
    store.update("main", task.task_id, result=result)
    if damage != "received":
        store.update("main", task.task_id, stage="injected", main_response="Final report")
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    record["delivered_chunks"] = 1
    record["chunk_scheme"] = IA_CHUNK_SCHEMES["tg"]
    record["delivered_text_sha256"] = (
        "" if damage == "missing_hash"
        else hashlib.sha256(b"Other report" if damage == "mismatch" else b"Final report").hexdigest()
    )
    path.write_text(json.dumps(record))
    resumed = ia._delivery_result(result, "main")
    assert resumed.delivered_chunks == 0
    assert store.load("main", task.task_id)["delivered_chunks"] == 0
    assert "duplicate delivery possible" in caplog.text


async def test_changed_injection_response_cannot_skip_first_chunk(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    ia, message_bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (0,)
    message_bus._injector.inject_prompt = AsyncMock(side_effect=["First report", "Second report"])
    store = AsyncRecordStore(tmp_path)
    original_update = ia._record_store.update
    injected_save_attempts = 0

    def fail_first_injected_save(*args: object, **kwargs: object) -> None:
        nonlocal injected_save_attempts
        if kwargs.get("stage") == "injected":
            injected_save_attempts += 1
            if injected_save_attempts == 1:
                raise OSError("dummy disk failure")
        original_update(*args, **kwargs)

    ia._record_store.update = fail_first_injected_save
    offsets: list[int] = []

    async def send(env: object) -> None:
        offsets.append(env.start_chunk)
        if len(offsets) == 1:
            env.chunk_scheme = IA_CHUNK_SCHEMES["tg"]
            env.delivered_chunks = 1
            env.delivered = False
            env.delivery_error = "dummy send failure"

    transport.deliver.side_effect = send
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task
    assert offsets == [0, 0]
    assert message_bus._injector.inject_prompt.await_count == 2
    assert "duplicate delivery possible" in caplog.text
    assert not store.path_for("main", task_id).exists()


def test_hashless_zero_failure_does_not_erase_saved_progress(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    store.update("main", task.task_id, result=_result(task))
    store.update("main", task.task_id, stage="injected", main_response="Final report")
    expected_hash = hashlib.sha256(b"Final report").hexdigest()
    store.update(
        "main", task.task_id, delivered_chunks=1,
        delivered_text_sha256=expected_hash,
        chunk_scheme=IA_CHUNK_SCHEMES["tg"],
    )
    store.update("main", task.task_id, delivered_chunks=0)
    record = store.load("main", task.task_id)
    assert (record["delivered_chunks"], record["delivered_text_sha256"]) == (1, expected_hash)


async def test_matrix_file_without_event_id_keeps_undelivered_record(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from ductor_bot.messenger.matrix.transport import MatrixTransport

    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")
    ia, message_bus, _mock_transport = _setup_bus(
        tmp_path, response=f"<file:{file_path}>",
    )
    ia._retry_delays = ()
    message_bus._transports.clear()
    bot = MagicMock()
    bot.client = AsyncMock()
    bot.client.upload.return_value = (
        SimpleNamespace(content_uri="mxc://test/report"), None,
    )
    bot.client.room_send.return_value = SimpleNamespace(event_id=None)
    bot.id_map.int_to_room.return_value = "!room:test"
    bot.file_roots.return_value = [tmp_path]
    message_bus.register_transport(MatrixTransport(bot))

    task_id = ia.send_async(
        "main", "alpha.v2", "Original request",
        opts=AsyncSendOptions(chat_id=123, transport="mx", silent=True),
    )
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task

    store = AsyncRecordStore(tmp_path)
    assert not store.path_for("main", task_id).exists()
    record_path = store.path_for("main", task_id, area="undelivered")
    assert record_path.exists()
    assert json.loads(record_path.read_text())["delivered_chunks"] == 0


async def test_slack_file_without_files_list_keeps_undelivered_record(
    tmp_path: Path,
) -> None:
    from unittest.mock import MagicMock

    from ductor_bot.messenger.slack.transport import SlackTransport

    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")
    ia, message_bus, _mock_transport = _setup_bus(
        tmp_path, response=f"<file:{file_path}>",
    )
    ia._retry_delays = ()
    message_bus._transports.clear()
    bot = MagicMock()
    bot.client = AsyncMock()
    bot.client.files_upload_v2.return_value = {
        "ok": True, "file": {"id": "F123"},
    }
    bot.id_map.int_to_channel.return_value = "C123"
    bot.file_roots.return_value = [tmp_path]
    message_bus.register_transport(SlackTransport(bot))

    task_id = ia.send_async(
        "main", "alpha.v2", "Original request",
        opts=AsyncSendOptions(chat_id=123, transport="sl", silent=True),
    )
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task

    store = AsyncRecordStore(tmp_path)
    assert not store.path_for("main", task_id).exists()
    record_path = store.path_for("main", task_id, area="undelivered")
    assert record_path.exists()
    assert json.loads(record_path.read_text())["delivered_chunks"] == 0
    bot.client.files_upload_v2.assert_awaited_once()


async def test_telegram_chunk_retry_uses_saved_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from aiogram.exceptions import TelegramBadRequest

    from ductor_bot.messenger.telegram import sender
    from ductor_bot.messenger.telegram.transport import TelegramTransport

    ia, message_bus, _mock_transport = _setup_bus(tmp_path, response="three chunks")
    ia._retry_delays = (0,)
    message_bus._transports.clear()
    monkeypatch.setattr(sender, "_ia_text_chunks", lambda _text: ["first", "second", "third"])
    bot = MagicMock()
    bot.bot_instance = MagicMock()
    bot.file_roots.return_value = []
    responses = iter([
        SimpleNamespace(message_id=1),
        TelegramBadRequest(MagicMock(), "bad HTML"),
        TelegramBadRequest(MagicMock(), "bad plain"),
        SimpleNamespace(message_id=2), SimpleNamespace(message_id=3),
    ])
    sent: list[tuple[str, object]] = []

    async def send_message(**kwargs: object) -> object:
        sent.append((kwargs["text"], kwargs["parse_mode"]))
        if len(sent) == 4:
            record = AsyncRecordStore(tmp_path).load("main", task_id)
            assert record["delivered_chunks"] == 1
            assert record["chunk_scheme"] == IA_CHUNK_SCHEMES["tg"]
            assert record["delivered_text_sha256"] == hashlib.sha256(
                b"three chunks"
            ).hexdigest()
        return next(responses)

    bot.bot_instance.send_message = AsyncMock(side_effect=send_message)
    message_bus.register_transport(TelegramTransport(bot))
    task_id = ia.send_async(
        "main", "alpha.v2", "Original request",
        opts=AsyncSendOptions(chat_id=123, transport="tg", silent=True),
    )
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task

    assert [text for text, _mode in sent] == [
        "first", "second", "second", "second", "third",
    ]
    assert sent[2][1] is None
    assert message_bus._injector.inject_prompt.await_count == 1
    assert not AsyncRecordStore(tmp_path).path_for("main", task_id).exists()


async def test_telegram_file_without_message_id_keeps_undelivered_record(
    tmp_path: Path,
) -> None:
    from unittest.mock import MagicMock

    from ductor_bot.messenger.telegram.transport import TelegramTransport

    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")
    ia, message_bus, _mock_transport = _setup_bus(
        tmp_path, response=f"<file:{file_path}>",
    )
    ia._retry_delays = ()
    message_bus._transports.clear()
    bot = MagicMock()
    bot.bot_instance = MagicMock()
    bot.bot_instance.send_document = AsyncMock(
        return_value=SimpleNamespace(message_id=None)
    )
    bot.file_roots.return_value = [tmp_path]
    message_bus.register_transport(TelegramTransport(bot))

    task_id = ia.send_async(
        "main", "alpha.v2", "Original request",
        opts=AsyncSendOptions(chat_id=123, transport="tg", silent=True),
    )
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task

    store = AsyncRecordStore(tmp_path)
    assert not store.path_for("main", task_id).exists()
    record_path = store.path_for("main", task_id, area="undelivered")
    assert record_path.exists()
    assert json.loads(record_path.read_text())["delivered_chunks"] == 0


async def test_receiver_not_ready_parks_without_retry_or_handler(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    ia, message_bus, transport = _setup_bus(tmp_path)
    ia.set_delivery_ready_checker(lambda _target: False)
    ia._retry_delays = (3600,)
    task_id = ia.send_async(
        "main", "alpha.v2", "Original request",
        opts=AsyncSendOptions(chat_id=123, topic_id=8, transport="tg"),
    )
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task
    record_path = AsyncRecordStore(tmp_path).path_for(
        "main", task_id, area="undelivered",
    )
    record = json.loads(record_path.read_text())
    assert record["status"] == "completed"
    assert record["stage"] == "received"
    assert (record["chat_id"], record["topic_id"]) == (123, 8)
    message_bus._injector.inject_prompt.assert_not_awaited()
    transport.deliver.assert_not_awaited()
    assert "parked before receiver ready" in caplog.text


async def test_fatal_main_result_is_preserved_without_handler(
    tmp_path: Path,
) -> None:
    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    supervisor._main_generation = 1
    supervisor._main_ready_gen = 1
    supervisor._main_fatal = True
    ia, message_bus, _transport = _setup_bus(tmp_path)
    ia.set_delivery_ready_checker(supervisor.is_delivery_ready)
    ia._retry_delays = (3600,)
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task
    path = AsyncRecordStore(tmp_path).path_for("main", task_id, area="undelivered")
    assert path.exists()
    assert json.loads(path.read_text())["result"]["result_text"] == "Completed"
    message_bus._injector.inject_prompt.assert_not_awaited()

async def test_corrupt_record_before_ready_is_rebuilt_and_survives_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ia, _message_bus, _transport = _setup_bus(tmp_path)
    ia.set_delivery_ready_checker(lambda _target: False)
    task_id = ia.send_async(
        "main", "alpha.v2", "Original request",
        opts=AsyncSendOptions(
            chat_id=-100123, topic_id=8, transport="tg",
            result_origin="user-task", broadcast_ref="ref-1",
        ),
    )
    assert task_id is not None
    store = AsyncRecordStore(tmp_path)
    inflight = store.path_for("main", task_id)
    inflight.write_text("null")
    await ia._async_tasks[task_id].asyncio_task

    parked = store.path_for("main", task_id, area="undelivered")
    assert parked.exists()
    assert not inflight.exists()
    archive_dir = store.root / "corrupt" / target_key("main")
    archives = list(archive_dir.glob(f"{task_id}.*.json"))
    assert len(archives) == 1
    assert archives[0].read_text() == "null"
    assert archives[0].stat().st_mode & 0o777 == 0o600
    assert archive_dir.stat().st_mode & 0o777 == 0o700
    assert parked.stat().st_mode & 0o777 == 0o600

    restarted = InterAgentBus(tmp_path)
    fresh_store = restarted._record_store
    assert fresh_store is not None
    record = fresh_store.load("main", task_id, area="undelivered")
    restored = fresh_store.result_from_record(record)
    assert record["stage"] == "received"
    assert (restored.chat_id, restored.topic_id, restored.transport) == (-100123, 8, "tg")
    assert (restored.result_text, restored.original_message) == ("Completed", "Original request")
    assert "Async corrupt record quarantined" in caplog.text


async def test_corrupt_record_ready_is_valid_before_transport_and_delivered(
    tmp_path: Path,
) -> None:
    ia, _message_bus, transport = _setup_bus(tmp_path)
    store = AsyncRecordStore(tmp_path)
    task_id = ia.send_async(
        "main", "alpha.v2", "Original request",
        opts=AsyncSendOptions(chat_id=-100123, topic_id=8, transport="tg"),
    )
    assert task_id is not None
    path = store.path_for("main", task_id)
    path.write_text("[]")
    observed: list[dict[str, object]] = []

    async def inspect(_envelope: object) -> None:
        observed.append(store.load("main", task_id))

    transport.deliver.side_effect = inspect
    await ia._async_tasks[task_id].asyncio_task
    assert len(observed) == 1
    assert observed[0]["stage"] == "injected"
    assert (observed[0]["chat_id"], observed[0]["topic_id"]) == (-100123, 8)
    assert observed[0]["result"]["result_text"] == "Completed"
    assert not path.exists()
    assert len(list((store.root / "corrupt" / target_key("main")).glob("*.json"))) == 1


async def test_corrupt_rebuild_write_failure_keeps_memory_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ia, _message_bus, transport = _setup_bus(tmp_path)
    ia.set_delivery_ready_checker(lambda _target: False)
    task_id = ia.send_async(
        "main", "alpha.v2", "Original request",
        opts=AsyncSendOptions(chat_id=-100123, topic_id=8, transport="tg"),
    )
    assert task_id is not None
    store = ia._record_store
    assert store is not None
    store.path_for("main", task_id).write_text("null")
    monkeypatch.setattr(
        store, "_write", lambda *_args: (_ for _ in ()).throw(OSError())
    )
    await ia._async_tasks[task_id].asyncio_task
    assert task_id in ia._unpersisted_results
    assert ia._unpersisted_results[task_id].chat_id == -100123
    assert len(list((store.root / "corrupt" / target_key("main")).glob("*.json"))) == 1
    assert "Async record recovery failed path=" in caplog.text
    assert "error=OSError" in caplog.text
    transport.deliver.assert_not_awaited()

async def test_corrupt_record_after_failed_delivery_parks_valid_injected_result(
    tmp_path: Path,
) -> None:
    ia, _message_bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = ()
    store = AsyncRecordStore(tmp_path)
    task_id = ia.send_async(
        "main", "alpha.v2", "Original request",
        opts=AsyncSendOptions(chat_id=-100123, topic_id=8, transport="tg"),
    )
    assert task_id is not None
    source = store.path_for("main", task_id)

    async def fail_after_corruption(envelope: object) -> None:
        source.write_text("null")
        envelope.delivered = False
        envelope.delivery_error = "send failed"

    transport.deliver.side_effect = fail_after_corruption
    await ia._async_tasks[task_id].asyncio_task
    assert not source.exists()
    record = InterAgentBus(tmp_path)._record_store.load("main", task_id, area="undelivered")
    restored = store.result_from_record(record)
    assert record["stage"] == "injected"
    assert record["main_response"] == "Final report"
    assert (restored.chat_id, restored.topic_id, restored.result_text) == (
        -100123, 8, "Completed",
    )
    assert len(list((store.root / "corrupt" / target_key("main")).glob("*.json"))) == 1

def test_corrupt_archive_rejects_symlinked_directory(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.root / "corrupt").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        store.recover_corrupt(task, _result(task))
    assert store.path_for("main", task.task_id).exists()
    assert list(outside.iterdir()) == []

async def test_corrupt_record_recovers_injected_stage_from_memory_result(
    tmp_path: Path,
) -> None:
    ia, message_bus, _transport = _setup_bus(tmp_path)
    ia.set_delivery_ready_checker(lambda _target: False)
    task = _task(chat_id=-100123, topic_id=8)
    store = AsyncRecordStore(tmp_path)
    store.create(task)
    store.path_for("main", task.task_id).write_text("null")
    await ia._deliver_async_result(_result(task, main_response="Saved final report"))
    record = store.load("main", task.task_id, area="undelivered")
    assert record["stage"] == "injected"
    assert record["main_response"] == "Saved final report"
    message_bus._injector.inject_prompt.assert_not_awaited()

@pytest.mark.parametrize(
    ("scheme", "expected"),
    [(None, 0), ("tg-ia-v2", 0), ("tg-ia-v3", 0), (IA_CHUNK_SCHEMES["mx"], 0), (IA_CHUNK_SCHEMES["tg"], 1)],
)
def test_chunk_scheme_must_match_to_resume(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, scheme: str | None, expected: int,
) -> None:
    ia, _message_bus, _transport = _setup_bus(tmp_path)
    task = _task(transport="tg")
    result = _result(task)
    store = AsyncRecordStore(tmp_path)
    store.create(task)
    store.update("main", task.task_id, result=result)
    store.update("main", task.task_id, stage="injected", main_response="Final report")
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    record["delivered_chunks"] = 1
    record["delivered_text_sha256"] = hashlib.sha256(b"Final report").hexdigest()
    if scheme is None:
        record.pop("chunk_scheme")
    else:
        record["chunk_scheme"] = scheme
    path.write_text(json.dumps(record))

    resumed = ia._delivery_result(result, "main")
    assert resumed.delivered_chunks == expected
    assert store.load("main", task.task_id)["delivered_chunks"] == expected
    if expected == 0:
        assert "duplicate delivery possible" in caplog.text
    else:
        assert "chunk progress reset" not in caplog.text

async def test_telegram_file_button_result_deletes_record_only_after_file_with_keyboard(
    tmp_path: Path,
) -> None:
    from unittest.mock import MagicMock

    from ductor_bot.messenger.telegram.transport import TelegramTransport

    report = tmp_path / "report.txt"
    report.write_text("report")
    ia, message_bus, _mock_transport = _setup_bus(
        tmp_path, response=f"[button:Go] <file:{report}>",
    )
    message_bus._transports.clear()
    bot = MagicMock()
    bot.bot_instance = MagicMock()
    bot.bot_instance.send_message = AsyncMock()
    bot.bot_instance.send_document = AsyncMock(return_value=SimpleNamespace(message_id=71))
    bot.file_roots.return_value = [tmp_path]
    message_bus.register_transport(TelegramTransport(bot))

    task_id = ia.send_async(
        "main", "alpha.v2", "Original request",
        opts=AsyncSendOptions(chat_id=123, transport="tg", silent=True),
    )
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task
    bot.bot_instance.send_message.assert_not_awaited()
    bot.bot_instance.send_document.assert_awaited_once()
    assert bot.bot_instance.send_document.await_args.kwargs[
        "reply_markup"
    ].inline_keyboard[0][0].text == "Go"
    assert not AsyncRecordStore(tmp_path).path_for("main", task_id).exists()


def _park_test_record(
    store: AsyncRecordStore, task_id: str, *, result: bool = True,
    injected: bool = False, success: bool = True,
) -> AsyncInterAgentTask:
    task = _task(task_id=task_id, chat_id=-100123, topic_id=8)
    store.create(task)
    if result:
        store.update("main", task_id, result=_result(
            task, success=success, error=None if success else "subtask failed",
            failure_count=0 if success else 3,
        ))
    if injected:
        store.update("main", task_id, stage="injected", main_response="Final report")
        store.update(
            "main", task_id, delivered_chunks=1,
            delivered_text_sha256=hashlib.sha256(b"Final report").hexdigest(),
            chunk_scheme=IA_CHUNK_SCHEMES["tg"],
        )
    store.move_to_undelivered("main", task_id)
    return task


async def test_redeliver_parked_result_and_readiness(tmp_path: Path) -> None:
    ia, _message_bus, transport = _setup_bus(tmp_path)
    store = AsyncRecordStore(tmp_path)
    task = _park_test_record(store, "abc000000001")
    ia.set_delivery_ready_checker(lambda _target: False)
    assert await ia.redeliver_undelivered("main") == 0
    assert store.path_for("main", task.task_id, area="undelivered").exists()
    transport.deliver.assert_not_awaited()
    ia.set_delivery_ready_checker(lambda _target: True)
    assert await ia.redeliver_undelivered("main") == 1
    assert not store.path_for("main", task.task_id, area="undelivered").exists()
    assert not store.path_for("main", task.task_id).exists()
    assert transport.deliver.await_args.args[0].chat_id == -100123
    assert transport.deliver.await_args.args[0].topic_id == 8


async def test_redeliver_injected_resumes_only_matching_progress(tmp_path: Path) -> None:
    ia, bus, transport = _setup_bus(tmp_path)
    store = AsyncRecordStore(tmp_path)
    _park_test_record(store, "abc000000002", injected=True)
    offsets: list[int] = []

    async def send(envelope: object) -> None:
        offsets.append(envelope.start_chunk)

    transport.deliver.side_effect = send
    assert await ia.redeliver_undelivered("main") == 1
    assert offsets == [1]
    bus._injector.inject_prompt.assert_not_awaited()

    for task_id, key in (("abc000000003", "delivered_text_sha256"),
                         ("abc000000004", "chunk_scheme")):
        _park_test_record(store, task_id, injected=True)
        path = store.path_for("main", task_id, area="undelivered")
        record = json.loads(path.read_text())
        record[key] = "0" * 64 if key == "delivered_text_sha256" else "tg-old"
        path.write_text(json.dumps(record))
    assert await ia.redeliver_undelivered("main") == 2
    assert offsets == [1, 0, 0]
    bus._injector.inject_prompt.assert_not_awaited()


async def test_redeliver_quarantines_corrupt_and_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ia, _bus, transport = _setup_bus(tmp_path)
    store = AsyncRecordStore(tmp_path)
    _park_test_record(store, "abc000000005")
    _park_test_record(store, "abc000000006")
    corrupt = store.path_for("main", "abc000000005", area="undelivered")
    corrupt.write_text("null")
    assert await ia.redeliver_undelivered("main") == 1
    assert not corrupt.exists()
    archived = list((store.root / "corrupt" / target_key("main")).glob("abc000000005.*.json"))
    assert len(archived) == 1
    assert archived[0].read_text() == "null"
    assert archived[0].stat().st_mode & 0o777 == 0o600
    assert "corrupt parked record quarantined" in caplog.text
    transport.deliver.assert_awaited_once()


async def test_corrupt_parked_memory_result_survives_failed_replay_and_restart(
    tmp_path: Path,
) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _park_test_record(store, "abc000000020")
    result = _result(task, result_text="Actual final result")
    parked = store.path_for("main", task.task_id, area="undelivered")
    parked.write_text("null")
    ia, _bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = ()
    ia._parked_results[task.task_id] = result
    observed: list[str] = []

    async def fail(envelope: object) -> None:
        observed.append(store.load("main", task.task_id)["result"]["result_text"])
        envelope.delivered = False
        envelope.delivery_error = "temporary"

    transport.deliver.side_effect = fail
    assert await ia.redeliver_undelivered("main") == 0
    assert observed == ["Actual final result"]
    record = store.load("main", task.task_id, area="undelivered")
    assert record["result"]["result_text"] == "Actual final result"
    assert (record["chat_id"], record["topic_id"]) == (-100123, 8)
    archived = list((store.root / "corrupt" / target_key("main")).glob(f"{task.task_id}.*.json"))
    assert len(archived) == 1
    assert archived[0].read_text() == "null"
    assert task.task_id not in ia._parked_results

    restarted, restarted_bus, restarted_transport = _setup_bus(tmp_path)
    assert await restarted.redeliver_undelivered("main") == 1
    restarted_bus._injector.inject_prompt.assert_not_awaited()
    assert restarted_transport.deliver.await_args.args[0].result_text == "Final report"
    assert not parked.exists()
    restarted_transport.deliver.assert_awaited_once()


async def test_persisted_parked_results_do_not_accumulate_in_memory(tmp_path: Path) -> None:
    ia, _bus, _transport = _setup_bus(tmp_path)
    ia.set_delivery_ready_checker(lambda _target: False)
    store = AsyncRecordStore(tmp_path)
    for index in range(5):
        task = _task(task_id=f"abc00000002{index}")
        store.create(task)
        result = _result(task, result_text="x" * 500_000 if index == 0 else "done")
        await ia._deliver_async_result(result)
        record = store.load("main", task.task_id, area="undelivered")
        assert record["result"]["result_text"] == result.result_text
    assert ia._parked_results == {}
    assert ia._unpersisted_results == {}


async def test_persisted_result_parks_without_a_second_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ia, _bus, _transport = _setup_bus(tmp_path)
    ia.set_delivery_ready_checker(lambda _target: False)
    store = ia._record_store
    assert store is not None
    task = _task(task_id="abc000000027")
    store.create(task)
    original_update = store.update
    updates = 0

    def update_once(*args: object, **kwargs: object) -> None:
        nonlocal updates
        updates += 1
        if updates > 1:
            raise OSError("later write unavailable")
        original_update(*args, **kwargs)

    monkeypatch.setattr(store, "update", update_once)
    result = _result(task)
    await ia._deliver_async_result(result)
    assert updates == 1
    assert store.load("main", task.task_id, area="undelivered")["result"]["result_text"] == (
        result.result_text
    )
    assert ia._parked_results == {}


async def test_unpersisted_parked_result_stays_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ia, _bus, _transport = _setup_bus(tmp_path)
    ia.set_delivery_ready_checker(lambda _target: False)
    store = ia._record_store
    assert store is not None
    task = _task(task_id="abc000000025")
    store.create(task)
    monkeypatch.setattr(store, "update", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))
    result = _result(task, result_text="memory only")
    await ia._deliver_async_result(result)
    assert ia._parked_results[task.task_id] is result
    assert ia._unpersisted_results[task.task_id] is result


async def test_redeliver_resultless_record_reports_loss_to_origin(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _park_test_record(store, "abc000000007", result=False)
    # A fresh bus has no in-memory result from the pre-restart process.
    ia, bus, transport = _setup_bus(tmp_path)
    ia._failure_ledger = SimpleNamespace(
        record_failure=AsyncMock(), clear_success=AsyncMock(),
    )
    observed: list[dict] = []

    async def inspect(_envelope: object) -> None:
        observed.append(store.load("main", task.task_id))

    transport.deliver.side_effect = inspect
    assert await ia.redeliver_undelivered("main") == 1
    assert observed[0]["status"] == "failed"
    assert observed[0]["result"]["error"] == "result lost: not persisted before restart"
    assert observed[0]["result"]["failure_kind"] == "result_lost"
    ia._failure_ledger.record_failure.assert_not_awaited()
    ia._failure_ledger.clear_success.assert_not_awaited()
    assert not store.path_for("main", task.task_id, area="undelivered").exists()
    assert not store.path_for("main", task.task_id).exists()
    prompt = bus._injector.inject_prompt.await_args.args[0]
    assert "result lost: not persisted before restart" in prompt
    assert "The original work may already be done and only its result was lost" in prompt
    assert "Do not delegate the same request again" in prompt
    assert "tell the user in the original chat/topic that the result was lost" in prompt
    assert "Original request: Original request" in prompt
    assert "Original chat ID: -100123, original topic ID: 8" in prompt
    assert "Decide whether to retry" not in prompt
    envelope = transport.deliver.await_args.args[0]
    assert (envelope.chat_id, envelope.topic_id) == (-100123, 8)


async def test_redeliver_failure_retries_and_keeps_file_without_ledger_count(
    tmp_path: Path
) -> None:
    ia, bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (0, 0)
    store = AsyncRecordStore(tmp_path)
    task = _park_test_record(store, "abc000000008", success=False)
    ia._failure_ledger = SimpleNamespace(
        record_failure=AsyncMock(), clear_success=AsyncMock()
    )

    async def fail(envelope: object) -> None:
        envelope.delivered = False
        envelope.delivery_error = "send failed"

    transport.deliver.side_effect = fail
    assert await ia.redeliver_undelivered("main") == 0
    assert transport.deliver.await_count == 3
    assert bus._injector.inject_prompt.await_count == 1
    ia._failure_ledger.record_failure.assert_not_awaited()
    ia._failure_ledger.clear_success.assert_not_awaited()
    record = store.load("main", task.task_id, area="undelivered")
    assert record["stage"] == "injected"
    assert record["result"]["failure_count"] == 3


def test_failure_kind_record_roundtrip_and_old_record_default(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    result = _result(
        task, success=False, error="result lost: not persisted before restart",
        failure_kind="result_lost",
    )
    store.create(task)
    store.update("main", task.task_id, result=result)
    record = store.load("main", task.task_id)
    assert record["result"]["failure_kind"] == "result_lost"
    assert store.result_from_record(record) == result

    path = store.path_for("main", task.task_id)
    old_record = json.loads(path.read_text())
    old_record["result"].pop("failure_kind")
    path.write_text(json.dumps(old_record))
    loaded = store.load("main", task.task_id)
    assert loaded["result"]["failure_kind"] == ""
    assert store.result_from_record(loaded).failure_kind == ""

    old_record["result"]["failure_kind"] = 7
    path.write_text(json.dumps(old_record))
    with pytest.raises(CorruptAsyncRecordError):
        store.load("main", task.task_id)


async def test_main_ready_event_replays_result_from_transient_restart(
    tmp_path: Path,
) -> None:
    from unittest.mock import MagicMock

    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    ia, message_bus, transport = _setup_bus(tmp_path)
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    supervisor._bus = ia
    supervisor._main_generation = 2
    supervisor._main_ready_gen = None
    ia.set_delivery_ready_checker(supervisor.is_delivery_ready)
    task_id = ia.send_async(
        "main", "alpha.v2", "Original request",
        opts=AsyncSendOptions(chat_id=-100123, topic_id=8, transport="tg"),
    )
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task
    store = AsyncRecordStore(tmp_path)
    parked = store.path_for("main", task_id, area="undelivered")
    assert parked.exists()
    transport.deliver.assert_not_awaited()

    stack = MagicMock()
    stack.name = "main"
    stack.is_main = True
    stack.bot.orchestrator = MagicMock()
    stack.bot.orchestrator.inject_prompt = AsyncMock(return_value="Final report")
    stack.bot._bus = message_bus
    message_bus.set_injector(stack.bot.orchestrator)
    supervisor._stacks["main"] = stack
    supervisor._inject_supervisor_hook(stack)
    hook = stack.bot.register_startup_hook.call_args.args[0]
    await hook()
    assert supervisor.is_delivery_ready("main")
    await supervisor._redelivery_tasks["main"]
    assert not parked.exists()
    transport.deliver.assert_awaited_once()
    assert (transport.deliver.await_args.args[0].chat_id,
            transport.deliver.await_args.args[0].topic_id) == (-100123, 8)

    await hook()  # a repeated readiness event cannot deliver the same task twice
    await supervisor._redelivery_tasks["main"]
    transport.deliver.assert_awaited_once()


async def test_first_start_ready_event_replays_old_parked_file(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    store = AsyncRecordStore(tmp_path)
    task = _park_test_record(store, "abc000000009")
    ia, message_bus, transport = _setup_bus(tmp_path)  # new process, no memory queue
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    supervisor._bus = ia
    supervisor._main_generation = 1
    ia.set_delivery_ready_checker(supervisor.is_delivery_ready)
    stack = MagicMock()
    stack.name = "main"
    stack.is_main = True
    stack.bot.orchestrator = MagicMock()
    stack.bot.orchestrator.inject_prompt = AsyncMock(return_value="Final report")
    stack.bot._bus = message_bus
    message_bus.set_injector(stack.bot.orchestrator)
    supervisor._stacks["main"] = stack
    supervisor._inject_supervisor_hook(stack)
    await stack.bot.register_startup_hook.call_args.args[0]()
    await supervisor._redelivery_tasks["main"]
    assert not store.path_for("main", task.task_id, area="undelivered").exists()
    transport.deliver.assert_awaited_once()


async def test_sub_ready_event_after_headless_and_reregistration(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.health import AgentHealth
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    target = "alpha.v2"
    store = AsyncRecordStore(tmp_path)
    first = _task(task_id="abc00000000a", reply_to=target)
    store.create(first)
    store.update(target, first.task_id, result=_result(first))
    store.move_to_undelivered(target, first.task_id)
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    ia = InterAgentBus(tmp_path)
    supervisor._bus = ia
    ia.set_delivery_ready_checker(supervisor.is_delivery_ready)
    received: list[str] = []

    async def deliver(result: AsyncInterAgentResult) -> DeliveryOutcome:
        received.append(result.task_id)
        return DeliveryOutcome(ok=True, injected=True, delivered=True)

    ia.set_async_result_handler(target, deliver)
    stack = MagicMock()
    stack.name = target
    stack.is_main = False
    stack.config.transport = "headless"
    stack.bot.orchestrator = MagicMock()
    supervisor._stacks[target] = stack
    supervisor._health[target] = AgentHealth(name=target, status="running")
    ia.register(target, stack)
    supervisor._inject_supervisor_hook(stack)
    hook = stack.bot.register_startup_hook.call_args.args[0]
    await hook()
    assert received == []
    assert store.path_for(target, first.task_id, area="undelivered").exists()

    stack.config.transport = "telegram"
    await hook()
    await supervisor._redelivery_tasks[target]
    assert received == [first.task_id]
    assert not store.path_for(target, first.task_id, area="undelivered").exists()

    await supervisor.stop_agent(target)
    assert not ia.has_async_result_handler(target)
    second = _task(task_id="abc00000000b", reply_to=target)
    store.create(second)
    store.update(target, second.task_id, result=_result(second))
    store.move_to_undelivered(target, second.task_id)
    replacement = MagicMock()
    replacement.name = target
    replacement.is_main = False
    replacement.config.transport = "telegram"
    replacement.bot.orchestrator = MagicMock()
    supervisor._stacks[target] = replacement
    supervisor._health[target] = AgentHealth(name=target, status="running")
    ia.register(target, replacement)
    ia.set_async_result_handler(target, deliver)
    supervisor._inject_supervisor_hook(replacement)
    await replacement.bot.register_startup_hook.call_args.args[0]()
    await supervisor._redelivery_tasks[target]
    assert received == [first.task_id, second.task_id]
    assert not store.path_for(target, second.task_id, area="undelivered").exists()


async def test_ready_hook_schedules_one_replay_and_shutdown_restores_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    store = AsyncRecordStore(tmp_path)
    task = _park_test_record(store, "abc000000026")
    ia, message_bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (3600,)
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    supervisor._bus = ia
    supervisor._main_generation = 1
    ia.set_delivery_ready_checker(supervisor.is_delivery_ready)
    stack = MagicMock()
    stack.name = "main"
    stack.is_main = True
    stack.bot.orchestrator = MagicMock()
    stack.bot.orchestrator.inject_prompt = AsyncMock(return_value="Final report")
    stack.bot._bus = message_bus
    stack.shutdown = AsyncMock()
    message_bus.set_injector(stack.bot.orchestrator)
    supervisor._stacks["main"] = stack
    supervisor._watcher.stop = AsyncMock()
    supervisor._inject_supervisor_hook(stack)
    hook = stack.bot.register_startup_hook.call_args.args[0]

    async def fail(envelope: object) -> None:
        envelope.delivered = False
        envelope.delivery_error = "temporary"

    transport.deliver.side_effect = fail
    sleeping = asyncio.Event()
    never = asyncio.Event()

    async def wait_retry(_delay: float) -> None:
        sleeping.set()
        await never.wait()

    monkeypatch.setattr("ductor_bot.multiagent.bus.asyncio.sleep", wait_retry)
    started = asyncio.get_running_loop().time()
    await hook()
    assert asyncio.get_running_loop().time() - started < 0.2
    replay = supervisor._redelivery_tasks["main"]
    await hook()
    assert supervisor._redelivery_tasks["main"] is replay
    await asyncio.wait_for(sleeping.wait(), timeout=1)
    assert transport.deliver.await_count == 1

    await supervisor.stop_all()
    assert replay.cancelled()
    assert supervisor._redelivery_tasks == {}
    assert store.path_for("main", task.task_id, area="undelivered").exists()
    assert not store.path_for("main", task.task_id).exists()


async def test_redelivery_task_exception_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    bus = InterAgentBus(tmp_path)
    bus.redeliver_undelivered = AsyncMock(side_effect=RuntimeError("hidden"))
    supervisor._bus = bus
    supervisor._start_redelivery("main")
    await supervisor._redelivery_tasks["main"]
    assert "Parked async redelivery failed target=main error=RuntimeError" in caplog.text
    assert "hidden" not in caplog.text
    assert supervisor._redelivery_tasks == {}


async def test_memory_result_wins_when_parked_file_has_no_result(
    tmp_path: Path,
) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _park_test_record(store, "abc00000000c", result=False)
    ia, bus, transport = _setup_bus(tmp_path)
    ia._parked_results[task.task_id] = _result(task, result_text="Actual completed result")
    assert await ia.redeliver_undelivered("main") == 1
    prompt = bus._injector.inject_prompt.await_args.args[0]
    assert "Actual completed result" in prompt
    assert "RESULT LOST" not in prompt
    transport.deliver.assert_awaited_once()
    assert task.task_id not in ia._parked_results
    assert not store.path_for("main", task.task_id, area="undelivered").exists()


async def test_memory_result_delivers_after_record_update_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ia, bus, transport = _setup_bus(tmp_path)
    store = ia._record_store
    assert store is not None
    task = _task(task_id="abc00000000d", chat_id=-100123, topic_id=8)
    store.create(task)
    original_update = store.update
    calls = 0

    def fail_twice(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise OSError("temporary write failure")
        original_update(*args, **kwargs)

    monkeypatch.setattr(store, "update", fail_twice)
    ia.set_delivery_ready_checker(lambda _target: False)
    result = _result(task, result_text="Real result")
    await ia._deliver_async_result(result)
    assert ia._parked_results[task.task_id] is result
    assert store.load("main", task.task_id)["result"] is None
    assert not store.path_for("main", task.task_id, area="undelivered").exists()

    ia.set_delivery_ready_checker(lambda _target: True)
    assert await ia.redeliver_undelivered("main") == 1
    assert "Real result" in bus._injector.inject_prompt.await_args.args[0]
    assert "RESULT LOST" not in bus._injector.inject_prompt.await_args.args[0]
    transport.deliver.assert_awaited_once()
    assert task.task_id not in ia._parked_results
    assert not store.path_for("main", task.task_id).exists()


async def test_redelivery_claim_released_after_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ia, _bus, _transport = _setup_bus(tmp_path)
    store = AsyncRecordStore(tmp_path)
    task = _park_test_record(store, "abc00000000e")

    async def fail(_result: AsyncInterAgentResult, _target: str, **_kwargs: object) -> bool:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(ia, "_attempt_delivery", fail)
    with pytest.raises(RuntimeError, match="unexpected"):
        await ia.redeliver_undelivered("main")
    assert task.task_id not in ia._delivery_claims
    assert store.path_for("main", task.task_id, area="undelivered").exists()


async def test_two_ready_events_do_not_duplicate_active_redelivery(tmp_path: Path) -> None:
    ia, bus, transport = _setup_bus(tmp_path)
    store = AsyncRecordStore(tmp_path)
    task = _park_test_record(store, "abc000000010")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(_envelope: object) -> None:
        entered.set()
        await release.wait()

    transport.deliver.side_effect = send
    first = asyncio.create_task(ia.redeliver_undelivered("main"))
    await entered.wait()
    assert task.task_id in ia._delivery_claims
    assert await ia.redeliver_undelivered("main") == 0
    release.set()
    assert await first == 1
    transport.deliver.assert_awaited_once()
    bus._injector.inject_prompt.assert_awaited_once()
    assert task.task_id not in ia._delivery_claims


async def test_active_d2b_retry_claim_blocks_ready_replay(tmp_path: Path) -> None:
    ia, bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (0.05,)
    task = _task(task_id="abc000000011")
    store = AsyncRecordStore(tmp_path)
    store.create(task)
    result = _result(task)
    sends = 0

    async def send(envelope: object) -> None:
        nonlocal sends
        sends += 1
        if sends == 1:
            envelope.delivered = False
            envelope.delivery_error = "temporary"

    transport.deliver.side_effect = send
    active = asyncio.create_task(ia._deliver_async_result(result))
    for _ in range(100):
        if task.task_id in ia._retry_waiting:
            break
        await asyncio.sleep(0)
    assert task.task_id in ia._retry_waiting
    ia._parked_results[task.task_id] = result
    assert await ia.redeliver_undelivered("main") == 0
    await active
    assert sends == 2
    bus._injector.inject_prompt.assert_awaited_once()
    assert task.task_id not in ia._delivery_claims
    assert task.task_id not in ia._parked_results

async def test_restart_recovers_unfinished_main_in_original_topic(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task(task_id="abc000000041", chat_id=-100123, topic_id=8)
    store.create(task)
    ia, bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = ()
    observed: list[dict] = []

    async def inspect(_envelope: object) -> None:
        observed.append(store.load("main", task.task_id))

    transport.deliver.side_effect = inspect
    assert await ia.recover_inflight("main") == 1
    assert observed[0]["status"] == "failed"
    assert observed[0]["result"]["error"] == "interrupted by restart"
    prompt = bus._injector.inject_prompt.await_args.args[0]
    assert "interrupted by restart" in prompt
    assert "Original request" in prompt
    envelope = transport.deliver.await_args.args[0]
    assert (envelope.chat_id, envelope.topic_id, envelope.transport) == (-100123, 8, "tg")
    assert not store.path_for("main", task.task_id).exists()
    assert await ia.recover_inflight("main") == 0
    transport.deliver.assert_awaited_once()


async def test_restart_keeps_unready_inflight_record(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task(task_id="abc000000042")
    store.create(task)
    ia, bus, transport = _setup_bus(tmp_path)
    ia.set_delivery_ready_checker(lambda _target: False)
    assert await ia.recover_inflight("main") == 0
    assert store.load("main", task.task_id)["status"] == "inflight"
    bus._injector.inject_prompt.assert_not_awaited()
    transport.deliver.assert_not_awaited()


async def test_restart_replays_completed_received_and_injected(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    received = _task(task_id="abc000000043")
    injected = _task(task_id="abc000000044")
    for task in (received, injected):
        store.create(task)
        store.update("main", task.task_id, result=_result(task, result_text="Original result"))
    store.update("main", injected.task_id, stage="injected", main_response="Saved final report")
    store.update(
        "main", injected.task_id, delivered_chunks=1,
        delivered_text_sha256=hashlib.sha256(b"Saved final report").hexdigest(),
        chunk_scheme=IA_CHUNK_SCHEMES["tg"],
    )
    ia, bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = ()
    sent: list[tuple[str, str, int]] = []

    async def capture(envelope: object) -> None:
        sent.append((envelope.metadata["ia_task_id"], envelope.result_text, envelope.start_chunk))

    transport.deliver.side_effect = capture
    assert await ia.recover_inflight("main") == 2
    assert sent == [
        (received.task_id, "Final report", 0),
        (injected.task_id, "Saved final report", 1),
    ]
    assert bus._injector.inject_prompt.await_count == 1
    assert not store.path_for("main", received.task_id).exists()
    assert not store.path_for("main", injected.task_id).exists()


async def test_restart_distinguishes_interrupted_from_parked_result_loss(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    interrupted = _task(task_id="abc000000045")
    missing = _task(task_id="abc000000046")
    store.create(interrupted)
    store.create(missing)
    store.move_to_undelivered("main", missing.task_id)
    ia, bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = ()
    assert await ia.redeliver_undelivered("main") == 1
    assert await ia.recover_inflight("main") == 1
    assert transport.deliver.await_count == 2
    prompts = [call.args[0] for call in bus._injector.inject_prompt.await_args_list]
    assert sum("result lost: not persisted before restart" in prompt for prompt in prompts) == 1
    assert sum("interrupted by restart" in prompt for prompt in prompts) == 1
    assert not store.path_for("main", interrupted.task_id).exists()
    assert not store.path_for("main", missing.task_id, area="undelivered").exists()


async def test_restart_quarantines_corrupt_inflight_and_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    store = AsyncRecordStore(tmp_path)
    bad = _task(task_id="abc000000047")
    good = _task(task_id="abc000000048")
    store.create(bad)
    store.create(good)
    store.path_for("main", bad.task_id).write_text("null")
    ia, _bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = ()
    assert await ia.recover_inflight("main") == 1
    archived = list((store.root / "corrupt" / target_key("main")).glob(f"{bad.task_id}.*.json"))
    assert len(archived) == 1
    assert archived[0].read_text() == "null"
    assert "corrupt inflight record quarantined" in caplog.text
    assert not store.path_for("main", bad.task_id).exists()
    transport.deliver.assert_awaited_once()


async def test_restart_failure_parks_interrupted_result(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task(task_id="abc000000049")
    store.create(task)
    ia, _bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = ()

    async def fail(envelope: object) -> None:
        envelope.delivered = False
        envelope.delivery_error = "temporary"

    transport.deliver.side_effect = fail
    assert await ia.recover_inflight("main") == 0
    parked = store.load("main", task.task_id, area="undelivered")
    assert parked["status"] == "failed"
    assert parked["result"]["error"] == "interrupted by restart"
    assert not store.path_for("main", task.task_id).exists()

async def test_restart_routes_unfinished_sub_result_to_sub_chat(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task(
        task_id="abc00000004a", sender="alpha.v2", recipient="main",
        chat_id=-100456, topic_id=19, reply_to="",
    )
    store.create(task)
    ia, _main_bus, _main_transport = _setup_bus(tmp_path)
    sub_bus = MessageBus()
    injector = AsyncMock()
    injector.inject_prompt = AsyncMock(return_value="Sub final report")
    sub_bus.set_injector(injector)
    sub_transport = AsyncMock()
    sub_transport.transport_name = "tg"
    sub_bus.register_transport(sub_transport)
    ia._agents["alpha.v2"].bot._bus = sub_bus

    async def deliver(result: AsyncInterAgentResult) -> DeliveryOutcome:
        prompt = build_interagent_injection_prompt(
            result, agent_name="alpha.v2", transport_label="Telegram chat",
        )
        return await sub_bus.submit(from_interagent_result(result, 0, injection_prompt=prompt))

    ia.set_async_result_handler("alpha.v2", deliver)
    assert await ia.recover_inflight("alpha.v2") == 1
    assert "interrupted by restart" in injector.inject_prompt.await_args.args[0]
    envelope = sub_transport.deliver.await_args.args[0]
    assert (envelope.chat_id, envelope.topic_id, envelope.result_text) == (
        -100456, 19, "Sub final report",
    )
    assert not store.path_for("alpha.v2", task.task_id).exists()


async def test_restart_scan_skips_live_async_task_and_claim(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task(task_id="abc00000004b")
    store.create(task)
    ia, bus, transport = _setup_bus(tmp_path)
    blocker = asyncio.Event()
    live = asyncio.create_task(blocker.wait())
    ia._async_tasks[task.task_id] = SimpleNamespace(asyncio_task=live)
    try:
        assert await ia.recover_inflight("main") == 0
        ia._delivery_claims.add(task.task_id)
        blocker.set()
        await live
        assert await ia.recover_inflight("main") == 0
    finally:
        ia._delivery_claims.discard(task.task_id)
    bus._injector.inject_prompt.assert_not_awaited()
    transport.deliver.assert_not_awaited()
    assert store.load("main", task.task_id)["status"] == "inflight"


async def test_restart_ready_hook_does_not_wait_for_inflight_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    store = AsyncRecordStore(tmp_path)
    task = _task(task_id="abc00000004c")
    store.create(task)
    ia, message_bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (3600,)
    sleep_started = asyncio.Event()

    async def fail(envelope: object) -> None:
        envelope.delivered = False
        envelope.delivery_error = "temporary"

    transport.deliver.side_effect = fail
    original_sleep = asyncio.sleep

    async def slow_sleep(_seconds: float) -> None:
        sleep_started.set()
        await original_sleep(3600)

    monkeypatch.setattr("ductor_bot.multiagent.bus.asyncio.sleep", slow_sleep)
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    supervisor._bus = ia
    supervisor._main_generation = 1
    ia.set_delivery_ready_checker(supervisor.is_delivery_ready)
    stack = MagicMock()
    stack.name = "main"
    stack.is_main = True
    stack.bot.orchestrator = MagicMock()
    stack.bot.orchestrator.inject_prompt = AsyncMock(return_value="Final report")
    stack.bot._bus = message_bus
    message_bus.set_injector(stack.bot.orchestrator)
    supervisor._stacks["main"] = stack
    supervisor._inject_supervisor_hook(stack)
    hook = stack.bot.register_startup_hook.call_args.args[0]
    import time

    started = time.monotonic()
    await hook()
    assert time.monotonic() - started < 0.5
    task = supervisor._redelivery_tasks["main"]
    try:
        await asyncio.wait_for(sleep_started.wait(), timeout=1)
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert store.load("main", "abc00000004c", area="undelivered")["result"]["error"] == (
        "interrupted by restart"
    )

async def test_restart_preserves_failed_result_and_cleans_delivered_stage(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    store = AsyncRecordStore(tmp_path)
    failed = _task(task_id="abc00000004d")
    done = _task(task_id="abc00000004e")
    for task in (failed, done):
        store.create(task)
    store.update(
        "main", failed.task_id,
        result=_result(failed, success=False, error="subtask failed", failure_count=2),
    )
    store.update("main", done.task_id, result=_result(done))
    store.update("main", done.task_id, stage="injected", main_response="Saved final report")
    store.update("main", done.task_id, stage="delivered")
    assert store.load("main", done.task_id)["stage"] == "delivered"
    ia, bus, transport = _setup_bus(tmp_path)
    with caplog.at_level("INFO"):
        assert await ia.recover_inflight("main") == 1
    prompt = bus._injector.inject_prompt.await_args.args[0]
    assert "subtask failed" in prompt
    assert "interrupted by restart" not in prompt
    transport.deliver.assert_awaited_once()
    assert not store.path_for("main", failed.task_id).exists()
    assert not store.path_for("main", done.task_id).exists()
    assert "Async delivered record cleaned" in caplog.text
    assert not list((store.root / "corrupt" / target_key("main")).glob(f"{done.task_id}.*.json"))

async def test_ready_event_rescans_new_parked_record_after_active_replay(
    tmp_path: Path,
) -> None:
    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    store = AsyncRecordStore(tmp_path)
    first = _park_test_record(store, "abc000000051")
    ia, _bus, transport = _setup_bus(tmp_path)
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    supervisor._bus = ia
    original_replay = ia.redeliver_undelivered
    ia.redeliver_undelivered = AsyncMock(wraps=original_replay)
    entered = asyncio.Event()
    release = asyncio.Event()
    sent: list[str] = []

    async def send(envelope: object) -> None:
        sent.append(envelope.metadata["ia_task_id"])
        if len(sent) == 1:
            entered.set()
            await release.wait()

    transport.deliver.side_effect = send
    supervisor._start_redelivery("main")
    replay = supervisor._redelivery_tasks["main"]
    await asyncio.wait_for(entered.wait(), timeout=1)
    second = _park_test_record(store, "abc000000052")
    for _ in range(3):
        supervisor._start_redelivery("main")
        assert supervisor._redelivery_tasks["main"] is replay
    assert supervisor._redelivery_pending == {"main": True}
    release.set()
    await asyncio.wait_for(replay, timeout=1)
    assert sent == [first.task_id, second.task_id]
    assert ia.redeliver_undelivered.await_count == 2
    assert supervisor._redelivery_tasks == {}
    assert supervisor._redelivery_pending == {}
    for task in (first, second):
        assert not store.path_for("main", task.task_id, area="undelivered").exists()


async def test_ready_event_rescans_new_inflight_record_after_active_recovery(
    tmp_path: Path,
) -> None:
    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    store = AsyncRecordStore(tmp_path)
    first = _task(task_id="abc000000053")
    store.create(first)
    ia, _bus, transport = _setup_bus(tmp_path)
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    supervisor._bus = ia
    original_recovery = ia.recover_inflight
    ia.recover_inflight = AsyncMock(wraps=original_recovery)
    entered = asyncio.Event()
    release = asyncio.Event()
    sent: list[str] = []

    async def send(envelope: object) -> None:
        sent.append(envelope.metadata["ia_task_id"])
        if len(sent) == 1:
            entered.set()
            await release.wait()

    transport.deliver.side_effect = send
    supervisor._start_redelivery("main")
    replay = supervisor._redelivery_tasks["main"]
    await asyncio.wait_for(entered.wait(), timeout=1)
    second = _task(task_id="abc000000054")
    store.create(second)
    supervisor._start_redelivery("main")
    release.set()
    await asyncio.wait_for(replay, timeout=1)
    assert sent == [first.task_id, second.task_id]
    assert ia.recover_inflight.await_count == 2
    assert not store.path_for("main", first.task_id).exists()
    assert not store.path_for("main", second.task_id).exists()


async def test_redelivery_cancellation_discards_pending_rescan(tmp_path: Path) -> None:
    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    store = AsyncRecordStore(tmp_path)
    first = _park_test_record(store, "abc000000055")
    ia, _bus, transport = _setup_bus(tmp_path)
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    supervisor._bus = ia
    original_replay = ia.redeliver_undelivered
    ia.redeliver_undelivered = AsyncMock(wraps=original_replay)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_send(_envelope: object) -> None:
        entered.set()
        await release.wait()

    transport.deliver.side_effect = wait_for_send
    supervisor._start_redelivery("main")
    await asyncio.wait_for(entered.wait(), timeout=1)
    second = _park_test_record(store, "abc000000056")
    supervisor._start_redelivery("main")
    await supervisor._cancel_redeliveries("main")
    assert ia.redeliver_undelivered.await_count == 1
    assert supervisor._redelivery_tasks == {}
    assert supervisor._redelivery_pending == {}
    for task in (first, second):
        assert store.path_for("main", task.task_id, area="undelivered").exists()


async def test_single_telegram_api_result_is_parked_without_wrong_delivery(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    from unittest.mock import MagicMock

    from ductor_bot.messenger.telegram.app import TelegramBot

    task = _task(task_id="abc000000061", transport="api")
    store = AsyncRecordStore(tmp_path)
    store.create(task)
    ia, bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = ()
    telegram = MagicMock()

    async def deliver(result: AsyncInterAgentResult) -> DeliveryOutcome:
        return await TelegramBot.on_async_interagent_result(telegram, result)

    ia.set_async_result_handler("main", deliver)
    with caplog.at_level("ERROR"):
        await ia._deliver_async_result(_result(task, result_text="SENSITIVE RESULT"))
    parked = store.load("main", task.task_id, area="undelivered")
    assert parked["result"]["transport"] == "api"
    assert parked["result"]["result_text"] == "SENSITIVE RESULT"
    assert "Async result undelivered" in caplog.text
    assert "SENSITIVE RESULT" not in caplog.text
    assert not store.path_for("main", task.task_id).exists()
    bus._injector.inject_prompt.assert_not_awaited()
    transport.deliver.assert_not_awaited()
    telegram._bus.submit.assert_not_called()


async def test_multibot_unknown_transport_is_parked_without_broadcast(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    from unittest.mock import MagicMock

    from ductor_bot.messenger.multi import MultiBotAdapter

    task = _task(task_id="abc000000062", transport="unknown")
    store = AsyncRecordStore(tmp_path)
    store.create(task)
    ia, bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = ()
    telegram = MagicMock()
    matrix = MagicMock()
    telegram.on_async_interagent_result = AsyncMock()
    matrix.on_async_interagent_result = AsyncMock()
    multi = MultiBotAdapter.__new__(MultiBotAdapter)
    multi._transports = ["telegram", "matrix"]
    multi._all = [telegram, matrix]
    ia.set_async_result_handler("main", multi.on_async_interagent_result)
    with caplog.at_level("ERROR"):
        await ia._deliver_async_result(_result(task))
    assert store.load("main", task.task_id, area="undelivered")["result"]["transport"] == (
        "unknown"
    )
    assert "Async result undelivered" in caplog.text
    bus._injector.inject_prompt.assert_not_awaited()
    transport.deliver.assert_not_awaited()
    telegram.on_async_interagent_result.assert_not_awaited()
    matrix.on_async_interagent_result.assert_not_awaited()


async def test_receiver_turning_headless_retries_then_parks_with_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    task = _task(task_id="abc000000063", reply_to="alpha.v2")
    store = AsyncRecordStore(tmp_path)
    store.create(task)
    ia, bus, transport = _setup_bus(tmp_path)
    ia._agents["alpha.v2"].config.transport = "headless"
    ia.set_delivery_ready_checker(lambda target: target != "alpha.v2")
    ia._retry_delays = (0, 0)
    attempt = AsyncMock(wraps=ia._attempt_delivery)
    ia._attempt_delivery = attempt
    with caplog.at_level("ERROR"):
        await ia._deliver_async_result(_result(task))
    assert attempt.await_count == 3
    assert store.load("alpha.v2", task.task_id, area="undelivered")["result"]["result_text"] == (
        "Completed"
    )
    assert "Async result undelivered" in caplog.text
    assert not store.path_for("alpha.v2", task.task_id).exists()
    bus._injector.inject_prompt.assert_not_awaited()
    transport.deliver.assert_not_awaited()


async def test_inflight_recovery_runs_before_parked_retry_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    store = AsyncRecordStore(tmp_path)
    active = _task(task_id="abc000000071")
    store.create(active)
    parked = _park_test_record(store, "abc000000072")
    ia, _bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (3600,)
    retry_waiting = asyncio.Event()
    wait_forever = asyncio.Event()

    async def pause_retry(_delay: float) -> None:
        retry_waiting.set()
        await wait_forever.wait()

    async def send(envelope: object) -> None:
        if envelope.metadata["ia_task_id"] == parked.task_id:
            envelope.delivered = False
            envelope.delivery_error = "temporary"

    monkeypatch.setattr("ductor_bot.multiagent.bus.asyncio.sleep", pause_retry)
    transport.deliver.side_effect = send
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    supervisor._bus = ia
    supervisor._start_redelivery("main")
    replay = supervisor._redelivery_tasks["main"]
    try:
        await asyncio.wait_for(retry_waiting.wait(), timeout=1)
        assert not store.path_for("main", active.task_id).exists()
        assert transport.deliver.await_args_list[0].args[0].metadata["ia_task_id"] == (
            active.task_id
        )
        assert not replay.done()
    finally:
        await supervisor._cancel_redeliveries("main")
    assert store.path_for("main", parked.task_id, area="undelivered").exists()


async def test_recovery_scans_survive_each_others_exception(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    store = AsyncRecordStore(tmp_path)
    active = _task(task_id="abc000000073")
    store.create(active)
    ia, _bus, transport = _setup_bus(tmp_path)
    ia.redeliver_undelivered = AsyncMock(side_effect=RuntimeError("hidden"))
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    supervisor._bus = ia
    supervisor._start_redelivery("main")
    await supervisor._redelivery_tasks["main"]
    assert not store.path_for("main", active.task_id).exists()
    transport.deliver.assert_awaited_once()
    assert "Parked async redelivery failed target=main error=RuntimeError" in caplog.text
    assert "hidden" not in caplog.text

    parked = _park_test_record(store, "abc000000074")
    ia.recover_inflight = AsyncMock(side_effect=RuntimeError("hidden"))
    ia.redeliver_undelivered = InterAgentBus.redeliver_undelivered.__get__(ia)
    supervisor._start_redelivery("main")
    await supervisor._redelivery_tasks["main"]
    assert not store.path_for("main", parked.task_id, area="undelivered").exists()
    assert transport.deliver.await_count == 2
    assert "Inflight async recovery failed target=main error=RuntimeError" in caplog.text


async def test_restart_cleans_acknowledged_record_after_result_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    task = _task(task_id="abc000000075")
    ia, _bus, transport = _setup_bus(tmp_path)
    store = ia._record_store
    assert store is not None
    store.create(task)
    original_update = store.update
    failed_once = False

    def fail_first_result(*args: object, **kwargs: object) -> None:
        nonlocal failed_once
        if kwargs.get("result") is not None and not failed_once:
            failed_once = True
            raise OSError("temporary write failure")
        original_update(*args, **kwargs)

    monkeypatch.setattr(store, "update", fail_first_result)
    monkeypatch.setattr(
        store, "delete", lambda *_args: (_ for _ in ()).throw(OSError("delete failed")),
    )
    await ia._deliver_async_result(_result(task))
    stale = store.load("main", task.task_id)
    assert (stale["status"], stale["stage"], stale["result"]) == (
        "inflight", "delivered", None,
    )
    transport.deliver.assert_awaited_once()

    restarted, restarted_bus, restarted_transport = _setup_bus(tmp_path)
    with caplog.at_level("INFO"):
        assert await restarted.recover_inflight("main") == 0
    assert not store.path_for("main", task.task_id).exists()
    assert "Async delivered record cleaned" in caplog.text
    restarted_bus._injector.inject_prompt.assert_not_awaited()
    restarted_transport.deliver.assert_not_awaited()


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_restart_cleans_delivered_record_without_result_or_saved_response(
    tmp_path: Path, status: str,
) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task(task_id="abc000000076")
    store.create(task)
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    record.update(status=status, stage="delivered", result=None, main_response="")
    path.write_text(json.dumps(record))
    assert store.load("main", task.task_id)["stage"] == "delivered"
    ia, bus, transport = _setup_bus(tmp_path)
    assert await ia.recover_inflight("main") == 0
    assert not path.exists()
    bus._injector.inject_prompt.assert_not_awaited()
    transport.deliver.assert_not_awaited()



async def test_ready_cycle_does_not_retry_newly_parked_inflight_result(
    tmp_path: Path,
) -> None:
    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    store = AsyncRecordStore(tmp_path)
    active = _task(task_id="abc000000081")
    store.create(active)
    already_parked = _park_test_record(store, "abc000000082")
    ia, bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = (0, 0)
    attempts: list[str] = []

    async def send(envelope: object) -> None:
        task_id = envelope.metadata["ia_task_id"]
        attempts.append(task_id)
        if task_id == active.task_id:
            envelope.delivered = False
            envelope.delivery_error = "temporary"

    transport.deliver.side_effect = send
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    supervisor._bus = ia
    supervisor._start_redelivery("main")
    await supervisor._redelivery_tasks["main"]

    assert attempts.count(active.task_id) == 3
    assert attempts.count(already_parked.task_id) == 1
    assert store.path_for("main", active.task_id, area="undelivered").exists()
    assert not store.path_for("main", already_parked.task_id, area="undelivered").exists()
    assert bus._injector.inject_prompt.await_count == 2

    transport.deliver.side_effect = None
    supervisor._start_redelivery("main")
    await supervisor._redelivery_tasks["main"]
    assert transport.deliver.await_count == 5
    assert bus._injector.inject_prompt.await_count == 2
    assert not store.path_for("main", active.task_id, area="undelivered").exists()


@pytest.mark.parametrize("scenario", ["none", "partial", "all"])
async def test_cron_broadcast_recipient_outcome_controls_record(
    tmp_path: Path, scenario: str,
) -> None:
    from ductor_bot.messenger.telegram.app import TelegramBot
    from ductor_bot.messenger.telegram.transport import TelegramTransport

    ia, _old_bus, _old_transport = _setup_bus(tmp_path)
    ia._retry_delays = (0,)
    bot = MagicMock()
    bot._agent_name = "main"
    bot._config.allowed_user_ids = [] if scenario == "none" else [101, 102, 103]
    bot.file_roots.return_value = []
    message_bus = MessageBus()
    injector = AsyncMock()
    injector.inject_prompt.return_value = "Final report"
    message_bus.set_injector(injector)
    message_bus.register_transport(TelegramTransport(bot))
    bot._bus = message_bus
    ia.register("main", SimpleNamespace(
        config=SimpleNamespace(transport="telegram"), bot=bot,
    ))

    async def deliver(result: AsyncInterAgentResult) -> DeliveryOutcome:
        return await TelegramBot.on_async_interagent_result(bot, result)

    ia.set_async_result_handler("main", deliver)
    values = (
        [(True, 1), (False, 0), (True, 1)] * 2
        if scenario == "partial" else [(True, 1)] * 3
    )
    with patch(
        "ductor_bot.messenger.telegram.transport.send_rich_checked_progress",
        new=AsyncMock(side_effect=values),
    ) as send:
        task_id = ia.send_async(
            "main", "alpha.v2", "Original request",
            opts=AsyncSendOptions(
                chat_id=0, transport="tg", result_origin="cron-broadcast",
                broadcast_ref="job-1",
            ),
        )
        assert task_id is not None
        await ia._async_tasks[task_id].asyncio_task

    store = AsyncRecordStore(tmp_path)
    parked = store.path_for("main", task_id, area="undelivered")
    assert injector.inject_prompt.await_count == 1
    assert send.await_count == {
        "none": 0, "partial": 6, "all": 3,
    }[scenario]
    assert parked.exists() is (scenario != "all")
    assert not store.path_for("main", task_id).exists()
    if scenario != "all":
        record = store.load("main", task_id, area="undelivered")
        assert record["stage"] == "injected"
        assert record["main_response"] == "Final report"
        assert record["delivered_chunks"] == 0
        assert (record["result_origin"], record["broadcast_ref"]) == (
            "cron-broadcast", "job-1",
        )


def _broadcast_recovery_bus(
    home: Path,
) -> tuple[InterAgentBus, MessageBus, AsyncMock, list[AsyncMock], MagicMock]:
    from ductor_bot.messenger.telegram.app import TelegramBot

    ia = InterAgentBus(home)
    ia._retry_delays = ()
    message_bus = MessageBus()
    injector = AsyncMock()
    injector.inject_prompt.return_value = "Public final report"
    message_bus.set_injector(injector)
    transports = []
    for name in ("tg", "mx", "sl"):
        transport = AsyncMock()
        transport.transport_name = name
        message_bus.register_transport(transport)
        transports.append(transport)
    bot = MagicMock()
    bot._agent_name = "main"
    bot._config.allowed_user_ids = [999]
    bot._bus = message_bus
    ia.register("main", SimpleNamespace(
        config=SimpleNamespace(transport="telegram"), bot=bot,
    ))
    ia.set_async_result_handler(
        "main", lambda result: TelegramBot.on_async_interagent_result(bot, result),
    )
    return ia, message_bus, injector, transports, bot


@pytest.mark.parametrize("area", ["inflight", "undelivered"])
@pytest.mark.parametrize("status", ["completed", "failed"])
@pytest.mark.parametrize("injected", [False, True])
async def test_cron_broadcast_inflight_restart_keeps_isolated_route(
    tmp_path: Path, area: str, status: str, injected: bool,
) -> None:
    from ductor_bot.bus.envelope import DeliveryMode

    task = _task(
        task_id="abc000000091", chat_id=0, topic_id=None,
        result_origin="cron-broadcast", broadcast_ref="job-91",
    )
    store = AsyncRecordStore(tmp_path)
    store.create(task)
    store.update(
        "main", task.task_id,
        result=_result(
            task, success=status == "completed",
            error=None if status == "completed" else "subtask failed",
        ),
    )
    if injected:
        store.update(
            "main", task.task_id, stage="injected",
            main_response="Saved public report",
        )
    if area == "undelivered":
        store.move_to_undelivered("main", task.task_id)
    ia, _bus, injector, transports, bot = _broadcast_recovery_bus(tmp_path)

    delivered = (
        await ia.recover_inflight("main") if area == "inflight"
        else await ia.redeliver_undelivered("main")
    )
    assert delivered == 1
    assert not store.path_for("main", task.task_id, area=area).exists()
    assert injector.inject_prompt.await_count == (0 if injected else 1)
    if not injected:
        assert injector.inject_prompt.await_args.kwargs["isolated"] is True
        assert injector.inject_prompt.await_args.args[1] == 0
    for transport in transports:
        transport.deliver.assert_not_awaited()
        transport.deliver_broadcast.assert_awaited_once()
        envelope = transport.deliver_broadcast.await_args.args[0]
        assert envelope.delivery is DeliveryMode.BROADCAST
        assert envelope.chat_id == 0
        assert envelope.metadata["result_origin"] == "cron-broadcast"
        assert envelope.metadata["broadcast_ref"] == "job-91"
        assert envelope.result_text == (
            "Saved public report" if injected else "Public final report"
        )
    bot._default_chat_id.assert_not_called()
    assert (
        await ia.recover_inflight("main") if area == "inflight"
        else await ia.redeliver_undelivered("main")
    ) == 0


@pytest.mark.parametrize("area", ["inflight", "undelivered"])
async def test_cron_broadcast_restart_failure_keeps_isolated_route(
    tmp_path: Path, area: str,
) -> None:
    from ductor_bot.bus.envelope import DeliveryMode

    task = _task(
        task_id="abc000000092", chat_id=0, topic_id=None,
        result_origin="cron-broadcast", broadcast_ref="job-92",
    )
    store = AsyncRecordStore(tmp_path)
    store.create(task)
    if area == "undelivered":
        store.move_to_undelivered("main", task.task_id)
    ia, _bus, injector, transports, bot = _broadcast_recovery_bus(tmp_path)

    delivered = (
        await ia.recover_inflight("main") if area == "inflight"
        else await ia.redeliver_undelivered("main")
    )
    assert delivered == 1
    assert not store.path_for("main", task.task_id, area=area).exists()
    injector.inject_prompt.assert_awaited_once()
    prompt = injector.inject_prompt.await_args.args[0]
    assert ("interrupted by restart" if area == "inflight" else "result lost") in prompt
    assert injector.inject_prompt.await_args.kwargs["isolated"] is True
    assert injector.inject_prompt.await_args.args[1] == 0
    for transport in transports:
        transport.deliver.assert_not_awaited()
        transport.deliver_broadcast.assert_awaited_once()
        assert transport.deliver_broadcast.await_args.args[0].delivery is DeliveryMode.BROADCAST
    bot._default_chat_id.assert_not_called()


async def test_cron_broadcast_partial_retry_keeps_current_scope(
    tmp_path: Path,
) -> None:
    task = _task(
        task_id="abc000000093", chat_id=0, topic_id=None,
        result_origin="cron-broadcast", broadcast_ref="job-93",
    )
    store = AsyncRecordStore(tmp_path)
    store.create(task)
    store.update("main", task.task_id, result=_result(task))
    ia, _bus, injector, transports, _bot = _broadcast_recovery_bus(tmp_path)
    ia._retry_delays = (0,)

    async def fail_one(envelope: object) -> None:
        envelope.delivered = False
        envelope.delivery_error = "temporary"

    transports[1].deliver_broadcast.side_effect = fail_one
    assert await ia.recover_inflight("main") == 0
    injector.inject_prompt.assert_awaited_once()
    assert store.path_for("main", task.task_id, area="undelivered").exists()
    # Pending user decision: current retries repeat all transports, including successes.
    assert [t.deliver_broadcast.await_count for t in transports] == [2, 2, 2]


def test_redelivery_metadata_is_top_level_and_result_unchanged(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    result = _result(task)
    store.create(task)
    store.update("main", task.task_id, result=result, redelivery_state=(2, 2000.0))
    record = store.load("main", task.task_id)
    assert (record["redelivery_attempts"], record["next_redelivery_at"]) == (2, 2000.0)
    assert "redelivery_attempts" not in record["result"]
    assert "next_redelivery_at" not in record["result"]
    assert store.result_from_record(record) == result
    store.update("main", task.task_id, redelivery_state=(1, 3000.0))
    assert store.load("main", task.task_id)["redelivery_attempts"] == 2
    store.update("main", task.task_id, redelivery_state=(2, 2500.0))
    assert store.load("main", task.task_id)["next_redelivery_at"] == 2500.0


@pytest.mark.parametrize(
    ("attempts", "next_at"),
    [
        ("bad", None), (True, None), (-1, None),
        (0, float("nan")), (0, float("inf")), (0, False),
        (0, ["wrong"]), (0, -1.0), ("bad", "wrong"),
    ],
)
def test_invalid_redelivery_metadata_warns_and_repairs_on_update(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    attempts: object, next_at: object,
) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    record["redelivery_attempts"] = attempts
    record["next_redelivery_at"] = next_at
    path.write_text(json.dumps(record))
    loaded = store.load("main", task.task_id)
    assert (loaded["redelivery_attempts"], loaded["next_redelivery_at"]) == (0, None)
    assert store.load("main", task.task_id)["redelivery_attempts"] == 0
    warnings = [r for r in caplog.records if "Invalid async redelivery metadata" in r.message]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert "bad" not in warnings[0].message
    store.update("main", task.task_id, redelivery_state=(1, 1000.0))
    repaired = json.loads(path.read_text())
    assert (repaired["redelivery_attempts"], repaired["next_redelivery_at"]) == (1, 1000.0)


def test_failed_parking_increments_backoff_once_and_caps(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    ia, _bus, _transport = _setup_bus(tmp_path)
    store = AsyncRecordStore(tmp_path)
    task = _task()
    result = _result(task, result_text="hidden result body")
    store.create(task)
    store.update("main", task.task_id, result=result)
    delays = (300, 600, 1200, 2400, 4800, 9600, 19200, 21600, 21600)
    for attempt, expected in enumerate(delays, 1):
        before = time.time()
        ia._park_record(result, "main")
        record = store.load("main", task.task_id, area="undelivered")
        assert record["redelivery_attempts"] == attempt
        assert before + expected <= record["next_redelivery_at"] <= time.time() + expected
        if attempt < 9:
            store.move_to_inflight("main", task.task_id)
    summaries = [r for r in caplog.records if "Async result redelivery deferred" in r.message]
    assert len(summaries) == 9
    assert all(record.levelno == logging.ERROR for record in summaries)
    assert all("hidden result body" not in record.message for record in summaries)
    assert "task=abcdef123456 target=main attempts=9 next_redelivery_at=" in summaries[-1].message


def test_not_ready_parking_preserves_backoff(tmp_path: Path) -> None:
    ia, _bus, _transport = _setup_bus(tmp_path)
    store = AsyncRecordStore(tmp_path)
    task = _task()
    result = _result(task)
    store.create(task)
    store.update("main", task.task_id, result=result, redelivery_state=(3, 9000.0))
    ia._park_record(result, "main", not_ready=True)
    record = store.load("main", task.task_id, area="undelivered")
    assert (record["redelivery_attempts"], record["next_redelivery_at"]) == (3, 9000.0)


async def test_respect_backoff_skips_future_then_due_and_default_bypasses(
    tmp_path: Path,
) -> None:
    ia, _bus, transport = _setup_bus(tmp_path)
    store = AsyncRecordStore(tmp_path)
    task = _park_test_record(store, "abc0000000e1")
    path = store.path_for("main", task.task_id, area="undelivered")
    record = json.loads(path.read_text())
    record["redelivery_attempts"] = 1
    record["next_redelivery_at"] = time.time() + 3600
    path.write_text(json.dumps(record))
    assert await ia.redeliver_undelivered("main", respect_backoff=True) == 0
    transport.deliver.assert_not_awaited()
    assert await ia.redeliver_undelivered("main") == 1
    task = _park_test_record(store, "abc0000000e2")
    path = store.path_for("main", task.task_id, area="undelivered")
    record = json.loads(path.read_text())
    record["redelivery_attempts"] = 1
    record["next_redelivery_at"] = time.time() - 1
    path.write_text(json.dumps(record))
    assert await ia.redeliver_undelivered("main", respect_backoff=True) == 1


async def test_memory_backoff_respects_deadline_then_persists_and_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ia, _bus, transport = _setup_bus(tmp_path)
    store = ia._record_store
    assert store is not None
    task = _task()
    result = _result(task)
    store.create(task)
    original_update = store.update
    monkeypatch.setattr(store, "update", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    ia._park_record(result, "main")
    assert ia._unpersisted_redelivery[task.task_id][0] == 1
    assert ia._unpersisted_results[task.task_id] is result
    assert await ia.redeliver_undelivered("main", respect_backoff=True) == 0
    transport.deliver.assert_not_awaited()
    monkeypatch.setattr(store, "update", original_update)
    ia._park_record(result, "main", not_ready=True)
    assert task.task_id not in ia._unpersisted_redelivery
    record = store.load("main", task.task_id, area="undelivered")
    assert record["redelivery_attempts"] == 1
    assert record["next_redelivery_at"] > time.time()
    restarted, _bus2, transport2 = _setup_bus(tmp_path)
    assert await restarted.redeliver_undelivered("main", respect_backoff=True) == 0
    transport2.deliver.assert_not_awaited()
    assert await restarted.redeliver_undelivered("main") == 1


async def test_delivery_success_clears_memory_backoff_even_if_store_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ia, _bus, _transport = _setup_bus(tmp_path)
    store = ia._record_store
    assert store is not None
    task = _task()
    result = _result(task)
    store.create(task)
    monkeypatch.setattr(store, "update", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    ia._park_record(result, "main")
    assert task.task_id in ia._unpersisted_results
    assert task.task_id in ia._unpersisted_redelivery
    assert await ia._attempt_delivery(result, "main")
    assert task.task_id not in ia._unpersisted_results
    assert task.task_id not in ia._unpersisted_redelivery


@pytest.mark.parametrize(
    ("stored", "memory", "expected"),
    [
        ((2, 2000.0), (3, 1500.0), (3, 1500.0)),
        ((2, 2000.0), (2, 3000.0), (2, 3000.0)),
        ((4, 1000.0), (2, 9000.0), (4, 1000.0)),
        ((0, None), (0, 0.0), (0, 0.0)),
    ],
)
def test_memory_redelivery_state_merges_into_file_on_update(
    tmp_path: Path,
    stored: tuple[int, float | None],
    memory: tuple[int, float | None],
    expected: tuple[int, float | None],
) -> None:
    ia, _bus, _transport = _setup_bus(tmp_path)
    store = ia._record_store
    assert store is not None
    task = _task()
    result = _result(task)
    store.create(task)
    store.update("main", task.task_id, result=result, redelivery_state=stored)
    ia._unpersisted_redelivery[task.task_id] = memory
    assert ia._update_record("main", task.task_id)
    record = store.load("main", task.task_id)
    assert (record["redelivery_attempts"], record["next_redelivery_at"]) == expected
    assert task.task_id not in ia._unpersisted_redelivery


def test_memory_result_and_backoff_persist_together_on_rewrite(tmp_path: Path) -> None:
    ia, _bus, _transport = _setup_bus(tmp_path)
    store = ia._record_store
    assert store is not None
    task = _task()
    result = _result(task)
    store.create(task)
    ia._unpersisted_results[task.task_id] = result
    ia._unpersisted_redelivery[task.task_id] = (1, 2000.0)
    assert ia._update_record("main", task.task_id)
    record = store.load("main", task.task_id)
    assert record["result"]["result_text"] == result.result_text
    assert (record["redelivery_attempts"], record["next_redelivery_at"]) == (1, 2000.0)
    assert task.task_id not in ia._unpersisted_redelivery


async def test_initial_and_replay_failures_each_advance_backoff_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    ia, _bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = ()

    async def fail(envelope: object) -> None:
        envelope.delivered = False
        envelope.delivery_error = "temporary"

    transport.deliver.side_effect = fail
    task_id = ia.send_async("main", "alpha.v2", "Original request")
    assert task_id is not None
    await ia._async_tasks[task_id].asyncio_task
    store = AsyncRecordStore(tmp_path)
    assert store.load("main", task_id, area="undelivered")["redelivery_attempts"] == 1
    assert await ia.redeliver_undelivered("main") == 0
    assert store.load("main", task_id, area="undelivered")["redelivery_attempts"] == 2
    summaries = [r.message for r in caplog.records if "Async result redelivery deferred" in r.message]
    assert len(summaries) == 2
    assert "attempts=1" in summaries[0]
    assert "attempts=2" in summaries[1]


async def test_inflight_recovery_failure_advances_backoff(tmp_path: Path) -> None:
    ia, _bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = ()
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    store.update("main", task.task_id, result=_result(task))

    async def fail(envelope: object) -> None:
        envelope.delivered = False
        envelope.delivery_error = "temporary"

    transport.deliver.side_effect = fail
    assert await ia.recover_inflight("main") == 0
    record = store.load("main", task.task_id, area="undelivered")
    assert record["redelivery_attempts"] == 1
    assert record["next_redelivery_at"] > time.time()


async def test_respect_backoff_skips_memory_copy_of_future_file(tmp_path: Path) -> None:
    ia, _bus, transport = _setup_bus(tmp_path)
    store = AsyncRecordStore(tmp_path)
    task = _park_test_record(store, "abc0000000e3")
    path = store.path_for("main", task.task_id, area="undelivered")
    record = json.loads(path.read_text())
    record["redelivery_attempts"] = 1
    record["next_redelivery_at"] = time.time() + 3600
    path.write_text(json.dumps(record))
    ia._parked_results[task.task_id] = _result(task)
    assert await ia.redeliver_undelivered("main", respect_backoff=True) == 0
    transport.deliver.assert_not_awaited()


class _RedeliveryClock:
    """Release individual scan ticks without waiting for wall-clock time."""

    def __init__(self) -> None:
        self.yield_control = asyncio.sleep
        self.sleeps: asyncio.Queue[tuple[float, asyncio.Future[None]]] = asyncio.Queue()
        self.delays: list[float] = []
        self.cancelled = False

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        future = asyncio.get_running_loop().create_future()
        self.sleeps.put_nowait((delay, future))
        try:
            await future
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def tick(self) -> None:
        delay, future = await self.sleeps.get()
        assert delay == 7
        future.set_result(None)
        await self.yield_control(0)


@pytest.fixture
def redelivery_clock(monkeypatch: pytest.MonkeyPatch) -> _RedeliveryClock:
    from ductor_bot.multiagent import supervisor as module

    clock = _RedeliveryClock()
    monkeypatch.setattr(module, "REDELIVERY_SCAN_INTERVAL", 7)
    monkeypatch.setattr(module.asyncio, "sleep", clock.sleep)
    return clock


def _periodic_supervisor(home: Path, ia: InterAgentBus) -> AgentSupervisor:
    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(home)))
    supervisor._bus = ia
    supervisor._running = True
    supervisor._main_generation = 1
    supervisor._main_ready_gen = 1
    ia._retry_delays = ()
    ia.set_delivery_ready_checker(supervisor.is_delivery_ready)
    return supervisor


async def _finish_scan(supervisor: AgentSupervisor) -> None:
    supervisor._running = False
    assert supervisor._redelivery_scan_task is not None
    supervisor._redelivery_scan_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await supervisor._redelivery_scan_task
    await supervisor._cancel_redeliveries()


async def _scan_tick(supervisor: AgentSupervisor, clock: _RedeliveryClock) -> None:
    await clock.tick()
    await asyncio.gather(*list(supervisor._redelivery_tasks.values()))


async def test_periodic_tick_obeys_backoff_then_delivers_and_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redelivery_clock: _RedeliveryClock,
) -> None:
    ia, bus, transport = _setup_bus(tmp_path)
    supervisor = _periodic_supervisor(tmp_path, ia)
    store = AsyncRecordStore(tmp_path)
    task = _park_test_record(store, "abc000000101")
    store.move_to_inflight("main", task.task_id)
    store.update("main", task.task_id, redelivery_state=(1, 1300.0))
    store.move_to_undelivered("main", task.task_id)
    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    supervisor._redelivery_scan_task = asyncio.create_task(supervisor._scan_redeliveries())
    try:
        await _scan_tick(supervisor, redelivery_clock)
        transport.deliver.assert_not_awaited()
        now[0] = 1300.0
        await _scan_tick(supervisor, redelivery_clock)
        transport.deliver.assert_awaited_once()
        bus._injector.inject_prompt.assert_awaited_once()
        assert not store.path_for("main", task.task_id, area="undelivered").exists()
        assert not store.path_for("main", task.task_id).exists()
    finally:
        await _finish_scan(supervisor)


@pytest.mark.parametrize("source", ["inflight", "memory"])
async def test_periodic_tick_without_parked_files_recovers_results(
    tmp_path: Path, redelivery_clock: _RedeliveryClock, source: str,
) -> None:
    ia, bus, transport = _setup_bus(tmp_path)
    supervisor = _periodic_supervisor(tmp_path, ia)
    store = AsyncRecordStore(tmp_path)
    task = _task(task_id="abc000000102")
    if source == "inflight":
        store.create(task)
    else:
        result = _result(task)
        ia._parked_results[task.task_id] = result
        ia._unpersisted_results[task.task_id] = result
    assert not store.path_for("main", task.task_id, area="undelivered").exists()
    supervisor._redelivery_scan_task = asyncio.create_task(supervisor._scan_redeliveries())
    try:
        await _scan_tick(supervisor, redelivery_clock)
        transport.deliver.assert_awaited_once()
        bus._injector.inject_prompt.assert_awaited_once()
        assert not store.path_for("main", task.task_id).exists()
        assert task.task_id not in ia._parked_results
        assert task.task_id not in ia._unpersisted_results
        if source == "inflight":
            assert "interrupted" in bus._injector.inject_prompt.await_args.args[0].lower()
        else:
            assert "Completed" in bus._injector.inject_prompt.await_args.args[0]
    finally:
        await _finish_scan(supervisor)


@pytest.mark.parametrize("state", ["live", "claimed"])
async def test_periodic_tick_does_not_report_live_or_claimed_work(
    tmp_path: Path, redelivery_clock: _RedeliveryClock, state: str,
) -> None:
    ia, bus, transport = _setup_bus(tmp_path)
    supervisor = _periodic_supervisor(tmp_path, ia)
    store = AsyncRecordStore(tmp_path)
    task = _task(task_id="abc000000103")
    store.create(task)
    blocker = asyncio.Event()
    live = asyncio.create_task(blocker.wait())
    if state == "live":
        ia._async_tasks[task.task_id] = SimpleNamespace(asyncio_task=live)
    else:
        ia._delivery_claims.add(task.task_id)
    supervisor._redelivery_scan_task = asyncio.create_task(supervisor._scan_redeliveries())
    try:
        await _scan_tick(supervisor, redelivery_clock)
        bus._injector.inject_prompt.assert_not_awaited()
        transport.deliver.assert_not_awaited()
        assert store.load("main", task.task_id)["status"] == "inflight"
    finally:
        await _finish_scan(supervisor)
        ia._delivery_claims.discard(task.task_id)
        blocker.set()
        await live


async def test_periodic_tick_scans_ready_known_targets_including_headless(
    tmp_path: Path, redelivery_clock: _RedeliveryClock,
) -> None:
    ia, _bus, _transport = _setup_bus(tmp_path)
    supervisor = _periodic_supervisor(tmp_path, ia)
    store = AsyncRecordStore(tmp_path)
    removed = _task(task_id="abc000000108", sender="removed")
    store.create(removed)
    store.update("removed", removed.task_id, result=_result(removed))
    store.move_to_undelivered("removed", removed.task_id)
    supervisor._stacks = {"headless": MagicMock(), "unready": MagicMock()}
    supervisor.is_delivery_ready = MagicMock(side_effect=lambda target: target != "unready")
    ia.recover_inflight = AsyncMock(return_value=0)
    ia.redeliver_undelivered = AsyncMock(return_value=0)
    supervisor._redelivery_scan_task = asyncio.create_task(supervisor._scan_redeliveries())
    try:
        await _scan_tick(supervisor, redelivery_clock)
        assert {call.args[0] for call in ia.recover_inflight.await_args_list} == {"main", "headless"}
        assert {call.args[0] for call in supervisor.is_delivery_ready.call_args_list} == {
            "main", "headless", "unready",
        }
        assert all(call.kwargs["respect_backoff"] for call in ia.redeliver_undelivered.await_args_list)
        assert "removed" not in supervisor._redelivery_tasks
        assert store.path_for("removed", removed.task_id, area="undelivered").exists()
    finally:
        await _finish_scan(supervisor)


@pytest.mark.parametrize(
    ("initial_force", "requests", "expected"),
    [
        (False, [True], [True, False]),
        (True, [False], [False, True]),
        (False, [False, True, False], [True, False]),
        (True, [True, False, True], [False, False]),
        (False, [False, False], [True, True]),
    ],
)
async def test_redelivery_requests_merge_force_with_one_target_task(
    tmp_path: Path, initial_force: bool, requests: list[bool], expected: list[bool],
) -> None:
    ia, _bus, _transport = _setup_bus(tmp_path)
    supervisor = _periodic_supervisor(tmp_path, ia)
    entered, release = asyncio.Event(), asyncio.Event()
    modes: list[bool] = []

    async def replay(_target: str, **kwargs: object) -> int:
        modes.append(kwargs["respect_backoff"])
        if len(modes) == 1:
            entered.set()
            await release.wait()
        return 0

    ia.recover_inflight = AsyncMock(return_value=0)
    ia.redeliver_undelivered = AsyncMock(side_effect=replay)
    supervisor._start_redelivery("main", force=initial_force)
    task = supervisor._redelivery_tasks["main"]
    await entered.wait()
    for force in requests:
        supervisor._start_redelivery("main", force=force)
        assert supervisor._redelivery_tasks["main"] is task
    assert supervisor._redelivery_pending == {"main": any(requests)}
    release.set()
    await task
    assert modes == expected
    assert ia.recover_inflight.await_count == 2
    assert supervisor._redelivery_pending == {}
    assert supervisor._redelivery_tasks == {}


@pytest.mark.parametrize("force", [False, True], ids=["periodic", "ready-event"])
async def test_inflight_failure_is_excluded_once_then_merged_mode_controls_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    force: bool,
) -> None:
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    ia, bus, transport = _setup_bus(tmp_path)
    supervisor = _periodic_supervisor(tmp_path, ia)
    store = AsyncRecordStore(tmp_path)
    task = _task(task_id="abc000000104")
    store.create(task)
    attempts = []

    async def send(envelope: object) -> None:
        attempts.append(envelope.metadata["ia_task_id"])
        if len(attempts) == 1:
            envelope.delivered = False
            envelope.delivery_error = "synthetic failure"
            supervisor._start_redelivery("main", force=force)

    transport.deliver.side_effect = send
    supervisor._start_redelivery("main", force=False)
    await supervisor._redelivery_tasks["main"]
    assert attempts == [task.task_id] * (2 if force else 1)
    bus._injector.inject_prompt.assert_awaited_once()
    assert store.path_for("main", task.task_id, area="undelivered").exists() is (not force)
    summaries = [r.message for r in caplog.records if "Async result redelivery deferred" in r.message]
    assert len(summaries) == 1
    assert "attempts=1 next_redelivery_at=1300.0" in summaries[0]
    if not force:
        assert store.load("main", task.task_id, area="undelivered")["redelivery_attempts"] == 1


async def test_periodic_and_ready_events_do_not_duplicate_delivery_or_injection(
    tmp_path: Path, redelivery_clock: _RedeliveryClock,
) -> None:
    ia, bus, transport = _setup_bus(tmp_path)
    supervisor = _periodic_supervisor(tmp_path, ia)
    task = _park_test_record(AsyncRecordStore(tmp_path), "abc000000105")
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(_envelope: object) -> None:
        entered.set()
        await release.wait()

    transport.deliver.side_effect = send
    supervisor._redelivery_scan_task = asyncio.create_task(supervisor._scan_redeliveries())
    try:
        await redelivery_clock.tick()
        await entered.wait()
        target_task = supervisor._redelivery_tasks["main"]
        for _ in range(3):
            await redelivery_clock.tick()
            supervisor._start_redelivery("main")
            assert supervisor._redelivery_tasks["main"] is target_task
        release.set()
        await target_task
        transport.deliver.assert_awaited_once()
        bus._injector.inject_prompt.assert_awaited_once()
        assert ia._delivery_claims == set()
        assert task.task_id not in ia._parked_results
    finally:
        release.set()
        await _finish_scan(supervisor)


@pytest.mark.parametrize("queued", [False, True], ids=["active-replay", "tick-race"])
async def test_shutdown_cancels_scan_before_targets_and_leaves_no_scheduled_replay(
    tmp_path: Path, redelivery_clock: _RedeliveryClock, queued: bool,
) -> None:
    ia, _bus, transport = _setup_bus(tmp_path)
    supervisor = _periodic_supervisor(tmp_path, ia)
    task = _park_test_record(AsyncRecordStore(tmp_path), "abc000000106")
    supervisor._watcher.stop = AsyncMock()
    entered, blocker = asyncio.Event(), asyncio.Event()
    order: list[str] = []

    async def send(_envelope: object) -> None:
        entered.set()
        try:
            await blocker.wait()
        finally:
            assert redelivery_clock.cancelled
            order.append("target-cancelled")

    transport.deliver.side_effect = send
    scan = asyncio.create_task(supervisor._scan_redeliveries())
    supervisor._redelivery_scan_task = scan
    if queued:
        _delay, future = await redelivery_clock.sleeps.get()
        future.set_result(None)
        # Queue shutdown behind the awakened tick so it races with a newly scheduled replay.
        await asyncio.create_task(supervisor.stop_all())
    else:
        await redelivery_clock.tick()
        await entered.wait()
        await supervisor.stop_all()
        assert order == ["target-cancelled"]
    assert scan.cancelled()
    assert supervisor._redelivery_scan_task is None
    assert supervisor._redelivery_tasks == {}
    assert supervisor._redelivery_pending == {}
    supervisor._start_redelivery("main")
    assert supervisor._redelivery_tasks == {}
    assert AsyncRecordStore(tmp_path).path_for("main", task.task_id, area="undelivered").exists()


async def test_periodic_failed_parking_emits_one_safe_error_summary_per_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, redelivery_clock: _RedeliveryClock,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    ia, bus, transport = _setup_bus(tmp_path, response="SYNTHETIC_MAIN_RESPONSE")
    supervisor = _periodic_supervisor(tmp_path, ia)
    store = AsyncRecordStore(tmp_path)
    task = _task(task_id="abc000000107", message="SYNTHETIC_REQUEST_BODY")
    store.create(task)
    store.update("main", task.task_id, result=_result(task, result_text="SYNTHETIC_RESULT_BODY"))
    store.move_to_undelivered("main", task.task_id)

    async def fail(envelope: object) -> None:
        envelope.delivered = False
        envelope.delivery_error = "synthetic failure"

    transport.deliver.side_effect = fail
    supervisor._redelivery_scan_task = asyncio.create_task(supervisor._scan_redeliveries())
    try:
        await _scan_tick(supervisor, redelivery_clock)
        await _scan_tick(supervisor, redelivery_clock)
        transport.deliver.assert_awaited_once()
        now[0] = 1300.0
        await _scan_tick(supervisor, redelivery_clock)
        assert transport.deliver.await_count == 2
        bus._injector.inject_prompt.assert_awaited_once()
        summaries = [r for r in caplog.records if "Async result redelivery deferred" in r.message]
        assert len(summaries) == 2
        for attempt, record in enumerate(summaries, 1):
            assert record.levelname == "ERROR"
            assert f"task={task.task_id} target=main attempts={attempt} next_redelivery_at=" in record.message
            assert "SYNTHETIC_REQUEST_BODY" not in record.message
            assert "SYNTHETIC_RESULT_BODY" not in record.message
            assert "SYNTHETIC_MAIN_RESPONSE" not in record.message
    finally:
        await _finish_scan(supervisor)


async def test_cancel_redelivery_before_coroutine_start_clears_pending_mode(tmp_path: Path) -> None:
    ia, _bus, transport = _setup_bus(tmp_path)
    supervisor = _periodic_supervisor(tmp_path, ia)
    supervisor._start_redelivery("main", force=False)
    task = supervisor._redelivery_tasks["main"]
    await supervisor._cancel_redeliveries()
    assert task.cancelled()
    assert supervisor._redelivery_tasks == {}
    assert supervisor._redelivery_pending == {}
    transport.deliver.assert_not_awaited()


async def test_supervisor_start_creates_one_periodic_scan_and_stop_joins_it(
    tmp_path: Path, redelivery_clock: _RedeliveryClock,
) -> None:
    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path), tasks={"enabled": False}))
    stack = MagicMock()
    stack.shutdown = AsyncMock()
    api = MagicMock(start=AsyncMock(return_value=True), stop=AsyncMock())
    shared = MagicMock(start=AsyncMock(), stop=AsyncMock())
    supervisor._watcher.start = AsyncMock()
    supervisor._watcher.stop = AsyncMock()

    async def run(_name: str, _stack: object) -> int:
        supervisor._main_ready.set()
        await supervisor._main_done.wait()
        return 0

    with (
        patch("ductor_bot.multiagent.internal_api.InternalAgentAPI", return_value=api),
        patch("ductor_bot.multiagent.shared_knowledge.SharedKnowledgeSync", return_value=shared),
        patch.object(supervisor, "_sync_sub_agents", new_callable=AsyncMock),
        patch.object(supervisor, "_supervised_run", side_effect=run),
        patch("ductor_bot.multiagent.supervisor.AgentStack.create", AsyncMock(return_value=stack)),
    ):
        startup = asyncio.create_task(supervisor.start())
        try:
            await redelivery_clock.tick()
            scan = supervisor._redelivery_scan_task
            assert scan is not None
            assert scan.get_name() == "ia-redelivery-scan"
            for _ in range(2):
                await redelivery_clock.tick()
                assert supervisor._redelivery_scan_task is scan
            assert len([t for t in asyncio.all_tasks() if t.get_name() == "ia-redelivery-scan"]) == 1
            supervisor._main_done.set()
            assert await startup == 0
        finally:
            supervisor._main_done.set()
            await supervisor.stop_all()
            await startup
    assert scan.cancelled()
    assert supervisor._redelivery_scan_task is None
    api.stop.assert_awaited_once()
    shared.stop.assert_awaited_once()


@pytest.mark.parametrize("failure", ["move", "read-after-move"])
@pytest.mark.parametrize("prior_attempts", [0, 3])
@pytest.mark.parametrize("retry_mode", ["deadline", "ready-event"])
async def test_partial_parking_failure_preserves_backoff_for_memory_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    failure: str, prior_attempts: int, retry_mode: str,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    ia, bus, transport = _setup_bus(tmp_path)
    ia._retry_delays = ()
    store = ia._record_store
    assert store is not None
    task = _task(task_id="abc000000109")
    result = _result(task)
    store.create(task)
    store.update("main", task.task_id, result=result, redelivery_state=(prior_attempts, 900.0))
    original_load = store.load

    def fail_move(_target: str, _task_id: str) -> None:
        raise OSError("synthetic move failure")

    def fail_read(target: str, task_id: str, *, area: str = "inflight") -> dict:
        if area == "undelivered":
            raise OSError("synthetic read failure")
        return original_load(target, task_id, area=area)

    if failure == "move":
        monkeypatch.setattr(store, "move_to_undelivered", fail_move)
    else:
        monkeypatch.setattr(store, "load", fail_read)
    ia._park_record(result, "main")
    area = "inflight" if failure == "move" else "undelivered"
    record = original_load("main", task.task_id, area=area)
    deadline = 1000.0 + (300 if prior_attempts == 0 else 2400)
    expected = (prior_attempts + 1, deadline)
    assert (record["redelivery_attempts"], record["next_redelivery_at"]) == expected
    assert ia._parked_results[task.task_id] is result

    assert await ia.redeliver_undelivered("main", respect_backoff=True) == 0
    transport.deliver.assert_not_awaited()
    bus._injector.inject_prompt.assert_not_awaited()
    assert ia._unpersisted_redelivery[task.task_id] == expected
    if retry_mode == "deadline":
        now[0] = deadline
    assert await ia.redeliver_undelivered(
        "main", respect_backoff=retry_mode == "deadline",
    ) == 1
    transport.deliver.assert_awaited_once()
    bus._injector.inject_prompt.assert_awaited_once()
    assert task.task_id not in ia._unpersisted_redelivery
    assert task.task_id not in ia._unpersisted_results
    assert task.task_id not in ia._parked_results


def test_default_redelivery_scan_interval_is_five_minutes() -> None:
    from ductor_bot.multiagent.supervisor import REDELIVERY_SCAN_INTERVAL

    assert REDELIVERY_SCAN_INTERVAL == 300


@pytest.mark.parametrize("force", [False, True], ids=["periodic", "ready-event"])
@pytest.mark.parametrize("cleanup_failure", ["read", "move", "delete"])
async def test_memory_delivery_read_recovery_never_reinjects_or_resends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool, cleanup_failure: str,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    ia, bus, transport = _setup_bus(tmp_path)
    supervisor = _periodic_supervisor(tmp_path, ia)
    store = ia._record_store
    assert store is not None
    task = _task(task_id="abc000000110")
    result = _result(task, result_text="SYNTHETIC_ACTUAL_RESULT")
    store.create(task)
    store.update("main", task.task_id, result=result)
    original_load = store.load

    def unreadable(target: str, task_id: str, *, area: str = "inflight") -> dict:
        if area == "undelivered":
            raise OSError("synthetic unreadable parked record")
        return original_load(target, task_id, area=area)

    monkeypatch.setattr(store, "load", unreadable)
    ia._park_record(result, "main")
    supervisor._start_redelivery("main", force=False)
    await supervisor._redelivery_tasks["main"]
    transport.deliver.assert_not_awaited()
    if not force:
        now[0] = 1300.0
    supervisor._start_redelivery("main", force=force)
    await supervisor._redelivery_tasks["main"]
    transport.deliver.assert_awaited_once()
    bus._injector.inject_prompt.assert_awaited_once()
    assert "SYNTHETIC_ACTUAL_RESULT" in bus._injector.inject_prompt.await_args.args[0]
    assert store.path_for("main", task.task_id, area="undelivered").exists()

    monkeypatch.setattr(store, "load", original_load)
    if cleanup_failure != "read":
        operation = "move_to_inflight" if cleanup_failure == "move" else "delete"
        original_operation = getattr(store, operation)

        def fail_cleanup(_target: str, _task_id: str) -> None:
            raise OSError("synthetic cleanup failure")

        monkeypatch.setattr(store, operation, fail_cleanup)
        for mode in (False, True):
            supervisor._start_redelivery("main", force=mode)
            await supervisor._redelivery_tasks["main"]
        transport.deliver.assert_awaited_once()
        bus._injector.inject_prompt.assert_awaited_once()
        monkeypatch.setattr(store, operation, original_operation)
    for mode in (False, True, False):
        supervisor._start_redelivery("main", force=mode)
        await supervisor._redelivery_tasks["main"]
    transport.deliver.assert_awaited_once()
    bus._injector.inject_prompt.assert_awaited_once()
    assert not store.path_for("main", task.task_id).exists()
    assert not store.path_for("main", task.task_id, area="undelivered").exists()
    assert task.task_id not in ia._parked_results
    assert task.task_id not in ia._unpersisted_results


@pytest.mark.parametrize("force", [False, True], ids=["periodic", "ready-event"])
@pytest.mark.parametrize("failure", ["update", "move"])
async def test_inflight_memory_result_survives_storage_recovery_and_obeys_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool, failure: str,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    ia, bus, transport = _setup_bus(tmp_path)
    supervisor = _periodic_supervisor(tmp_path, ia)
    store = ia._record_store
    assert store is not None
    task = _task(task_id="abc000000111")
    result = _result(task, result_text="SYNTHETIC_ACTUAL_RESULT")
    store.create(task)
    if failure == "move":
        store.update("main", task.task_id, result=result, redelivery_state=(3, 900.0))
    operation = "update" if failure == "update" else "move_to_undelivered"
    original = getattr(store, operation)

    def fail_storage(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(store, operation, fail_storage)
    ia._park_record(result, "main", not_ready=failure == "update")
    monkeypatch.setattr(store, operation, original)
    assert store.path_for("main", task.task_id).exists()
    if failure == "update":
        assert store.load("main", task.task_id)["result"] is None
    now[0] = 1300.0
    supervisor._start_redelivery("main", force=force)
    await supervisor._redelivery_tasks["main"]
    if failure == "move" and not force:
        transport.deliver.assert_not_awaited()
        bus._injector.inject_prompt.assert_not_awaited()
        assert ia._unpersisted_redelivery[task.task_id] == (4, 3400.0)
        now[0] = 3400.0
        supervisor._start_redelivery("main", force=False)
        await supervisor._redelivery_tasks["main"]
    transport.deliver.assert_awaited_once()
    bus._injector.inject_prompt.assert_awaited_once()
    prompt = bus._injector.inject_prompt.await_args.args[0]
    assert "SYNTHETIC_ACTUAL_RESULT" in prompt
    assert "interrupted by restart" not in prompt
    for mode in (False, True, False):
        supervisor._start_redelivery("main", force=mode)
        await supervisor._redelivery_tasks["main"]
    transport.deliver.assert_awaited_once()
    bus._injector.inject_prompt.assert_awaited_once()
    assert not store.path_for("main", task.task_id).exists()
    assert not store.path_for("main", task.task_id, area="undelivered").exists()
    assert task.task_id not in ia._unpersisted_redelivery


@pytest.mark.parametrize("force", [False, True], ids=["periodic", "ready-event"])
async def test_periodic_inflight_backoff_applies_to_completed_result_but_not_true_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool,
) -> None:
    now = [1300.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    ia, bus, transport = _setup_bus(tmp_path)
    supervisor = _periodic_supervisor(tmp_path, ia)
    store = AsyncRecordStore(tmp_path)
    completed = _task(task_id="abc000000112")
    orphan = _task(task_id="abc000000113")
    store.create(completed)
    store.update("main", completed.task_id, result=_result(completed), redelivery_state=(4, 3400.0))
    store.create(orphan)
    store.update("main", orphan.task_id, redelivery_state=(4, 3400.0))
    supervisor._start_redelivery("main", force=force)
    await supervisor._redelivery_tasks["main"]
    assert transport.deliver.await_count == (2 if force else 1)
    assert bus._injector.inject_prompt.await_count == (2 if force else 1)
    prompts = [call.args[0] for call in bus._injector.inject_prompt.await_args_list]
    assert sum("interrupted by restart" in prompt for prompt in prompts) == 1
    assert store.path_for("main", completed.task_id).exists() is (not force)
    now[0] = 3400.0
    supervisor._start_redelivery("main", force=False)
    await supervisor._redelivery_tasks["main"]
    assert transport.deliver.await_count == 2
    assert bus._injector.inject_prompt.await_count == 2
    assert not store.path_for("main", completed.task_id).exists()


async def test_inflight_memory_replay_merges_disk_deadline_before_periodic_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1300.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    ia, bus, transport = _setup_bus(tmp_path)
    supervisor = _periodic_supervisor(tmp_path, ia)
    store = AsyncRecordStore(tmp_path)
    task = _task(task_id="abc000000114")
    result = _result(task, result_text="SYNTHETIC_ACTUAL_RESULT")
    store.create(task)
    store.update("main", task.task_id, redelivery_state=(4, 3400.0))
    ia._unpersisted_results[task.task_id] = result
    supervisor._start_redelivery("main", force=False)
    await supervisor._redelivery_tasks["main"]
    transport.deliver.assert_not_awaited()
    bus._injector.inject_prompt.assert_not_awaited()
    assert ia._unpersisted_redelivery[task.task_id] == (4, 3400.0)
    supervisor._start_redelivery("main", force=True)
    await supervisor._redelivery_tasks["main"]
    transport.deliver.assert_awaited_once()
    bus._injector.inject_prompt.assert_awaited_once()
    assert "SYNTHETIC_ACTUAL_RESULT" in bus._injector.inject_prompt.await_args.args[0]
    assert "interrupted by restart" not in bus._injector.inject_prompt.await_args.args[0]
    assert not store.path_for("main", task.task_id).exists()


async def test_delivered_parked_record_after_restart_is_cleanup_only(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task(task_id="abc000000115")
    store.create(task)
    store.update("main", task.task_id, result=_result(task), stage="delivered")
    store.move_to_undelivered("main", task.task_id)
    ia, bus, transport = _setup_bus(tmp_path)
    assert await ia.redeliver_undelivered("main") == 0
    transport.deliver.assert_not_awaited()
    bus._injector.inject_prompt.assert_not_awaited()
    assert not store.path_for("main", task.task_id).exists()
    assert not store.path_for("main", task.task_id, area="undelivered").exists()
