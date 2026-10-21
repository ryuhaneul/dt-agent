"""Task instructions carry durable intake metadata only in CLI input."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.cli.types import AgentResponse
from ductor_bot.infra.input_context import current_input_context, received_at
from ductor_bot.tasks import hub as module
from ductor_bot.tasks.hub import TaskHub
from ductor_bot.tasks.models import TaskEntry, TaskSubmit
from ductor_bot.tasks.registry import TaskRegistry

STAMP = 1500000000.0


def setup(tmp_path, monkeypatch, zone="Asia/Seoul"):
    clock = SimpleNamespace(wall=STAMP, mono=100.0)
    monkeypatch.setattr(
        module, "time", SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.mono)
    )
    cli = MagicMock()
    # The real foreground CLI config does not receive AgentConfig.user_timezone.
    cli._config = SimpleNamespace(user_timezone="")
    cli.resolve_provider.return_value = ("claude", "sonnet")
    cli.execute = AsyncMock(
        return_value=AgentResponse(result="synthetic result", session_id="session", num_turns=1)
    )
    registry = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
    hub = TaskHub(
        registry,
        MagicMock(workspace=tmp_path),
        cli_service=cli,
        user_timezone=zone,
        config=SimpleNamespace(enabled=True, max_parallel=2, timeout_seconds=60),
    )
    return hub, registry, cli, clock


def submit(hub, parent="main"):
    return hub.submit(TaskSubmit(42, "Inspect the synthetic system", 1, 9, parent))


async def finish(hub, task_id):
    await hub._in_flight[task_id].asyncio_task
    # Let the existing done callback remove the handle before resuming.
    await asyncio.sleep(0)


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize(
    ("zone", "stamp"),
    [
        ("Asia/Seoul", "2017-07-14 11:40:00 KST"),
        ("UTC", "2017-07-14 02:40:00 UTC"),
        ("invalid/zone", "2017-07-14 02:40:00 UTC"),
    ],
)
async def test_instruction_intake_raw_text_timezone_and_no_wait(
    tmp_path, monkeypatch, caplog, resume, zone, stamp
):
    hub, registry, cli, clock = setup(tmp_path, monkeypatch, zone)
    task_id = submit(hub)
    await finish(hub, task_id)
    original = registry.get(task_id).original_prompt
    if resume:
        hub.resume(task_id, "Use the answer: synthetic target")
        await finish(hub, task_id)
    request = cli.execute.await_args.args[0]
    assert request.received_context == f"[Received: {stamp}]"
    assert request.cli_prompt == request.received_context + "\n" + request.prompt
    assert "[Received:" not in request.prompt
    assert "[Waited:" not in request.cli_prompt
    assert registry.get(task_id).original_prompt == original
    assert registry.get(task_id).prompt_preview == original[:80]
    assert "[Received:" not in (tmp_path / "tasks.json").read_text()
    assert "[Received:" not in caplog.text
    for path in registry.task_folder(task_id).iterdir():
        if path.is_file():
            assert "[Received:" not in path.read_text()
    assert request.resume_session == ("session" if resume else None)
    await hub.shutdown()


@pytest.mark.parametrize("resume", [False, True])
async def test_wait_is_frozen_at_execution_not_file_preparation(tmp_path, monkeypatch, resume):
    hub, registry, cli, clock = setup(tmp_path, monkeypatch)
    if resume:
        task_id = submit(hub)
        await finish(hub, task_id)
        clock.wall += 60
        hub.resume(task_id, "Parent answer")
    else:
        task_id = submit(hub)
    intake = clock.wall
    clock.wall += 5
    clock.mono += 5

    async def prepare(*_):
        clock.wall += 20
        clock.mono += 20
        return "static system context"

    monkeypatch.setattr(module, "build_appended_files_block", prepare)
    await finish(hub, task_id)
    request = cli.execute.await_args.args[0]
    assert request.received_context.endswith(" [Waited: 5s]")
    assert registry.get(task_id).instruction_received_at == intake
    assert registry.get(task_id).instruction_waited_seconds == 5
    assert request.append_system_prompt == "static system context"
    assert "[Received:" not in request.append_system_prompt
    assert request.cli_prompt.count("[Received:") == 1
    await hub.shutdown()


async def test_per_agent_timezone_does_not_inherit_parent_receipt_or_wait(tmp_path, monkeypatch):
    hub, _registry, cli, _clock = setup(tmp_path, monkeypatch)
    sub = MagicMock()
    sub._config = SimpleNamespace(user_timezone="UTC")
    sub.resolve_provider.return_value = ("codex", "synthetic")
    sub.execute = AsyncMock(
        return_value=AgentResponse(result="ok", session_id="sub-session", num_turns=1)
    )
    hub.set_cli_service("sub", sub, user_timezone="UTC")
    with received_at(STAMP - 99, waited_seconds=90):
        task_id = submit(hub, "sub")
        await finish(hub, task_id)
        assert current_input_context("UTC").endswith("[Waited: 90s]")
    assert sub.execute.await_args.args[0].received_context == "[Received: 2017-07-14 02:40:00 UTC]"
    cli.execute.assert_not_called()
    await hub.shutdown()


async def test_restart_replay_keeps_receipt_and_wait_new_resume_has_new_intake(
    tmp_path, monkeypatch
):
    hub, _registry, cli, clock = setup(tmp_path, monkeypatch)
    task_id = submit(hub)
    clock.mono += 5
    await finish(hub, task_id)
    first = cli.execute.await_args.args[0].received_context
    restored = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
    entry = restored.get(task_id)
    other = TaskHub(
        restored,
        MagicMock(workspace=tmp_path),
        cli_service=cli,
        config=hub._config,
        user_timezone=hub._user_timezone,
    )
    clock.wall += 999
    request = await other._prepare_request(entry, entry.original_prompt, cli, entry.session_id)
    assert request.received_context == first
    saved = json.loads((tmp_path / "tasks.json").read_text())["tasks"][0]
    assert saved["instruction_received_at"] == STAMP
    assert saved["instruction_waited_seconds"] == 5
    other.resume(task_id, "A new instruction after restart")
    await finish(other, task_id)
    assert cli.execute.await_args.args[0].received_context != first
    assert entry.instruction_received_at == STAMP + 999
    assert entry.instruction_waited_seconds is None
    assert entry.original_prompt == "Inspect the synthetic system"
    await hub.shutdown()
    await other.shutdown()


@pytest.mark.parametrize(
    "created", [STAMP, 0, None, True, -1, "old", float("nan"), float("inf"), 1e100]
)
async def test_legacy_receipt_fallback_is_frozen_without_retaining_text_annotation(
    tmp_path, monkeypatch, created
):
    hub, _registry, cli, clock = setup(tmp_path, monkeypatch)
    task_id = submit(hub)
    await finish(hub, task_id)
    path = tmp_path / "tasks.json"
    saved = json.loads(path.read_text())
    raw = saved["tasks"][0]
    raw.pop("instruction_received_at")
    raw.pop("instruction_waited_seconds")
    raw["created_at"] = created
    path.write_text(json.dumps(saved))
    restored = TaskRegistry(path, tmp_path / "tasks")
    other = TaskHub(
        restored,
        MagicMock(workspace=tmp_path),
        cli_service=cli,
        config=hub._config,
        user_timezone=hub._user_timezone,
    )
    entry = restored.get(task_id)
    assert entry.instruction_received_at == 0
    clock.wall += 99
    first = await other._prepare_request(entry, "raw replay", cli, "session")
    clock.wall += 99
    second = await other._prepare_request(entry, "raw replay", cli, "session")
    assert first.received_context == second.received_context
    assert entry.instruction_received_at == STAMP + 99
    assert first.received_context
    assert "Waited" not in first.received_context
    assert "[Received:" not in path.read_text()
    await hub.shutdown()


@pytest.mark.parametrize("invalid", [None, True, "old", -1, float("nan"), float("inf"), 1e100])
def test_invalid_durable_receipt_defaults_to_legacy_unknown(invalid):
    entry = TaskEntry("abc", 1, "main", "test", "raw", "claude", "sonnet", "done")
    raw = entry.to_dict()
    raw["instruction_received_at"] = invalid
    raw["instruction_waited_seconds"] = invalid
    parsed = TaskEntry.from_dict(raw)
    assert parsed.instruction_received_at == 0
    # A huge finite wait is not an invalid timestamp; duration has no epoch ceiling.
    assert parsed.instruction_waited_seconds == (invalid if invalid == 1e100 else None)


async def test_rejected_cap_keeps_existing_rejection_not_a_queue(tmp_path, monkeypatch):
    hub, registry, cli, _clock = setup(tmp_path, monkeypatch)
    hub._config.max_parallel = 1
    gate = asyncio.Event()

    async def hold(_request):
        await gate.wait()
        return AgentResponse(result="ok", session_id="s", num_turns=1)

    cli.execute.side_effect = hold
    first = submit(hub)
    with pytest.raises(ValueError, match="Too many background tasks"):
        submit(hub)
    assert len(registry.list_all()) == 1
    gate.set()
    await finish(hub, first)
    await hub.shutdown()


async def test_parent_answer_resumes_waiting_task_with_answer_intake(tmp_path, monkeypatch):
    hub, registry, cli, clock = setup(tmp_path, monkeypatch)
    task_id = submit(hub)
    await finish(hub, task_id)
    registry.update_status(
        task_id, "waiting", last_question="Which target?", last_question_received_at=STAMP
    )
    clock.wall += 60
    hub.resume(task_id, "Synthetic target A")
    await finish(hub, task_id)
    request = cli.execute.await_args.args[0]
    assert request.received_context == "[Received: 2017-07-14 11:41:00 KST]"
    assert request.prompt.startswith("Synthetic target A\n")
    assert registry.get(task_id).last_question == ""
    assert registry.get(task_id).last_question_received_at == STAMP
    await hub.shutdown()


async def test_pending_instruction_receipt_survives_restart_before_first_execution(
    tmp_path, monkeypatch
):
    hub, registry, cli, clock = setup(tmp_path, monkeypatch)
    task_id = submit(hub)
    await hub.shutdown()
    cli.execute.assert_not_called()
    restored = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
    entry = restored.get(task_id)
    assert entry.instruction_received_at == STAMP
    other = TaskHub(
        restored,
        MagicMock(workspace=tmp_path),
        cli_service=cli,
        config=hub._config,
        user_timezone=hub._user_timezone,
    )
    clock.wall += 120
    request = await other._prepare_request(entry, entry.original_prompt, cli, None)
    assert request.received_context == "[Received: 2017-07-14 11:40:00 KST]"
    assert entry.status == "running"  # Provider pre-resolution retains existing behavior.
    await other.shutdown()
