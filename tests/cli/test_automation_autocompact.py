"""Automation values survive storage and take precedence throughout real request paths."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from ductor_bot._home_defaults.workspace.tools._tool_shared import (
    normalize_autocompact_records,
    parse_autocompact_option,
)
from ductor_bot.cli.autocompact import stored_autocompact, validate_autocompact
from ductor_bot.cli.param_resolver import TaskExecutionConfig, TaskOverrides, resolve_cli_config
from ductor_bot.cli.slot_rotation import RotationResult
from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig
from ductor_bot.cron.execution import OneShotExecutionResult, build_cmd, execute_one_shot
from ductor_bot.cron.manager import CronJob
from ductor_bot.infra.task_runner import TaskRunOptions, run_oneshot_task
from ductor_bot.orchestrator.injection import _inject_prompt
from ductor_bot.session import SessionKey
from ductor_bot.tasks.hub import TaskHub
from ductor_bot.tasks.models import TaskEntry, TaskSubmit
from ductor_bot.tasks.registry import TaskRegistry
from ductor_bot.webhook.models import WebhookEntry
from ductor_bot.workspace.paths import DuctorPaths
from tests.cron.test_delivery_modes import World, _job
from tests.orchestrator.conftest import (
    orch as orch,  # noqa: PLC0414 -- shared pytest fixture
)
from tests.orchestrator.conftest import (
    workspace as workspace,  # noqa: PLC0414 -- shared pytest fixture
)
from tests.tasks.test_hub import _make_cli_service, _make_config
from tests.webhook.test_observer import _make_hook, _make_manager, _make_observer, _make_paths


def _payload(kind):
    return (
        {
            "id": "record-id",
            "title": "keep",
            "description": "keep",
            "schedule": "0 9 * * *",
            "task_folder": "job",
            "agent_instruction": "keep",
            "mode": "wake",
            "prompt_template": "keep",
        }
        if kind != "task"
        else {"task_id": "record-id", "chat_id": 1, "original_prompt": "keep"}
    )


@pytest.mark.parametrize("kind", ["cron", "webhook", "task"])
@pytest.mark.parametrize("value", ["default", "off", 100000, 1000000])
def test_stored_roundtrip_and_legacy_defaults(kind, value):
    cls = {"cron": CronJob, "webhook": WebhookEntry, "task": TaskEntry}[kind]
    payload = _payload(kind)
    legacy = cls.from_dict(payload)
    assert legacy.autocompact == (None if kind == "webhook" else "default")
    payload["autocompact"] = value
    entry = cls.from_dict(payload)
    assert cls.from_dict(entry.to_dict()).autocompact == value


@pytest.mark.parametrize("kind", ["cron", "webhook", "task"])
@pytest.mark.parametrize(
    "value", [True, False, 99999, 1000001, 100000.0, "100000", "inherit", {}, []]
)
def test_bad_stored_value_repairs_only_the_setting(kind, value, caplog):
    cls = {"cron": CronJob, "webhook": WebhookEntry, "task": TaskEntry}[kind]
    entry = cls.from_dict({**_payload(kind), "autocompact": value})
    assert entry.autocompact == (None if kind == "webhook" else "default")
    assert entry.original_prompt == "keep" if kind == "task" else entry.description == "keep"
    assert "record-id" in caplog.text
    assert entry.to_dict()["autocompact"] == entry.autocompact


@pytest.mark.parametrize("value", ["default", "off", 100000, 300000, 1000000])
def test_tool_and_backend_validators_agree_on_cli_representation(value):
    parsed = parse_autocompact_option(str(value))
    assert parsed == validate_autocompact(value)


@pytest.mark.parametrize(
    "value", [True, False, 0, 99999, 1000001, 100000.0, "auto", "inherit", "1e6"]
)
def test_tool_and_backend_reject_invalid_values(value):
    with pytest.raises(ValueError, match="autocompact"):
        parse_autocompact_option(str(value))
    with pytest.raises(ValidationError):
        validate_autocompact(value)


@pytest.mark.parametrize("key", ["jobs", "hooks"])
@pytest.mark.parametrize("value", [None, True, 100000.0, "100000", {}, 300000, "off"])
def test_tool_storage_repair_matches_backend(key, value):
    data = {key: [{"id": "record-id", "autocompact": value, "description": "keep"}]}
    record = normalize_autocompact_records(data, key)[key][0]
    assert record["autocompact"] == stored_autocompact(value, "record-id", inherit=key == "hooks")
    assert record["description"] == "keep"


def test_environment_snapshot_is_creation_only_and_webhook_inherit_is_explicit(monkeypatch):
    monkeypatch.setenv("DUCTOR_AUTOCOMPACT", "300000")
    assert parse_autocompact_option(None, use_env=True) == 300000
    assert parse_autocompact_option(None) is None
    assert parse_autocompact_option("inherit", allow_inherit=True) is None
    monkeypatch.delenv("DUCTOR_AUTOCOMPACT")
    assert parse_autocompact_option(None, use_env=True) == "default"


def _config(tmp_path, provider="claude", value=300000, **extra):
    return TaskExecutionConfig(
        provider=provider,
        model="opus",
        reasoning_effort="",
        cli_parameters=[],
        permission_mode="bypassPermissions",
        working_dir=str(tmp_path),
        file_access="all",
        autocompact=value,
        **extra,
    )


@pytest.mark.parametrize("provider", ["claude", "codex", "grok", "gemini"])
@pytest.mark.parametrize("value", ["default", "off", 300000])
def test_oneshot_cli_uses_shared_conversion(tmp_path, monkeypatch, provider, value):
    monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: "/fake/cli")
    monkeypatch.setattr("ductor_bot.cron.execution.find_gemini_cli", lambda: "/fake/gemini")
    monkeypatch.setattr(
        "ductor_bot.cron.execution.claude_autocompact_flag_supported", lambda _: True
    )
    command = build_cmd(_config(tmp_path, provider, value), "hello")
    try:
        assert command.env_overrides["DUCTOR_AUTOCOMPACT"] == str(value)
        assert ("--autocompact" in command.cmd) == (provider == "claude" and isinstance(value, int))
        assert ("model_auto_compact_token_limit=300000" in command.cmd) == (
            provider == "codex" and isinstance(value, int)
        )
        if provider == "claude" and isinstance(value, int):
            settings = json.loads(command.cmd[command.cmd.index("--settings") + 1])
            assert settings["autoCompactEnabled"] is True
            assert settings["env"]["DISABLE_AUTO_COMPACT"] == "0"
            assert command.env_overrides["DISABLE_AUTO_COMPACT"] is None
            assert command.env_overrides["DISABLE_COMPACT"] is None
        elif provider == "claude" and value == "off":
            assert command.env_overrides["DISABLE_AUTO_COMPACT"] == "1"
        elif provider != "claude":
            assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in command.env_overrides
    finally:
        for path in command.cleanup_paths:
            path.unlink(missing_ok=True)


async def test_private_file_settings_use_actual_task_cwd_and_are_cleaned(tmp_path, monkeypatch):
    monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: "/fake/claude")
    monkeypatch.setattr(
        "ductor_bot.cron.execution.claude_autocompact_flag_supported", lambda _: True
    )
    folder = tmp_path / "task"
    folder.mkdir()
    (folder / "settings.json").write_text(
        json.dumps({"env": {"SYNTHETIC_SECRET": "private-value"}})
    )
    config = replace(_config(tmp_path), cli_parameters=["--settings", "settings.json"])
    command = build_cmd(config, "hello", working_dir=folder)
    private = Path(command.cmd[command.cmd.index("--settings") + 1])
    assert (await asyncio.to_thread(private.stat)).st_mode & 0o777 == 0o600
    assert (await asyncio.to_thread(private.parent.stat)).st_mode & 0o777 == 0o700
    assert "private-value" not in " ".join(command.cmd)
    assert (
        json.loads(await asyncio.to_thread(private.read_text))["env"][
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
        ]
        == "300000"
    )
    proc = AsyncMock(returncode=0)
    proc.communicate.return_value = (b'{"result":"ok"}', b"")
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    await execute_one_shot(
        command, cwd=folder, provider="claude", timeout_seconds=1, timeout_label="test"
    )
    assert not await asyncio.to_thread(private.exists)
    assert not await asyncio.to_thread(private.parent.exists)
    assert (folder / "settings.json").exists()


async def test_rotated_slots_and_caller_env_cannot_change_saved_autocompact(tmp_path, monkeypatch):
    monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: "/fake/claude")
    monkeypatch.setattr(
        "ductor_bot.cron.execution.claude_autocompact_flag_supported", lambda _: True
    )
    monkeypatch.setattr("ductor_bot.cli.slot_rotation.make_registry", lambda **_: MagicMock())
    monkeypatch.setattr(
        "ductor_bot.cli.slot_rotation.slot_extra_env",
        lambda slot, **_: {"CLAUDE_CONFIG_DIR": slot, "DISABLE_AUTO_COMPACT": "1"},
    )
    captured = []
    execution = OneShotExecutionResult("success", "ok", b'{"result":"ok"}', b"", 0, False)

    async def execute(command, **_):
        captured.append((list(command.cmd), dict(command.env_overrides)))
        return execution

    async def rotate(**kwargs):
        for slot in ["claude", "claude-sub", "claude-api"]:
            await kwargs["attempt"](slot)
        return RotationResult(payload=execution, slot="claude-api")

    monkeypatch.setattr("ductor_bot.cron.execution.execute_one_shot", execute)
    monkeypatch.setattr("ductor_bot.cli.slot_rotation.run_with_rotation", rotate)
    config = _config(tmp_path, claude_accounts=["claude", "claude-sub", "claude-api"])
    await run_oneshot_task(
        config,
        "hello",
        TaskRunOptions(
            cwd=tmp_path,
            timeout_seconds=1,
            timeout_label="test",
            ductor_home=tmp_path,
            env_overrides={
                "DISABLE_AUTO_COMPACT": "1",
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "999999",
            },
        ),
    )
    assert len(captured) == 3
    assert captured[0][0] == captured[1][0] == captured[2][0]
    for _, env in captured:
        assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "300000"
        assert env["DISABLE_AUTO_COMPACT"] is env["DISABLE_COMPACT"] is None
        assert env["DUCTOR_AUTOCOMPACT"] == "300000"


@pytest.mark.parametrize("value", ["default", "off", 300000])
async def test_cron_in_session_carries_saved_override_through_bus(tmp_path, value):
    world = World(tmp_path, autocompact=900000)
    job = world.add(_job("daily", delivery_mode="in_session", autocompact=value))
    spawned = await world.run(job)
    spawned.assert_not_called()
    assert world.injector.calls[0]["autocompact"] == value


async def test_webhook_wake_inherits_current_global_but_explicit_default_stops_it(tmp_path):
    paths = _make_paths(tmp_path)
    manager = _make_manager(paths)
    hook = _make_hook()
    manager.add_hook(hook)
    observer = _make_observer(paths, manager, autocompact=200000, allowed_user_ids=[1])
    wake = AsyncMock(return_value="reply")
    observer.set_wake_handler(wake)
    await observer._dispatch(hook.id, {"msg": "hello"})
    assert wake.await_args.kwargs["autocompact"] == 200000
    observer._config.autocompact = 400000
    await observer._dispatch(hook.id, {"msg": "hello"})
    assert wake.await_args.kwargs["autocompact"] == 400000
    hook.autocompact = "default"
    await observer._dispatch(hook.id, {"msg": "hello"})
    assert wake.await_args.kwargs["autocompact"] == "default"


async def test_override_does_not_change_topic_and_survives_recovery(orch):
    key = SessionKey(chat_id=1, topic_id=7)
    await orch._sessions.set_autocompact(key, 600000)
    orch._cli_service.execute = AsyncMock(
        side_effect=[
            AgentResponse(result="No conversation found with session ID: old", is_error=True),
            AgentResponse(result="ok", session_id="new"),
        ]
    )
    await orch.handle_message(key, "hello", autocompact=300000)
    assert [call.args[0].autocompact for call in orch._cli_service.execute.await_args_list] == [
        300000,
        300000,
    ]
    assert (await orch._sessions.get_active(key)).autocompact == 600000
    orch._cli_service.execute = AsyncMock(return_value=AgentResponse(result="ok"))
    await orch.handle_message(key, "hello")
    assert orch._cli_service.execute.await_args.args[0].autocompact == 600000
    await _inject_prompt(orch, "injected", 1, "cron", topic_id=7, autocompact="default")
    assert orch._cli_service.execute.await_args.args[0].autocompact == "default"


async def test_tasks_keep_snapshot_on_resume_until_explicitly_changed(tmp_path):
    registry = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
    cli = _make_cli_service()
    hub = TaskHub(
        registry, DuctorPaths(ductor_home=tmp_path), cli_service=cli, config=_make_config()
    )
    submit = TaskSubmit(
        chat_id=1,
        prompt="first",
        message_id=0,
        thread_id=None,
        parent_agent="main",
        autocompact=300000,
    )
    task_id = hub.submit(submit)
    await hub._in_flight[task_id].asyncio_task
    assert cli.execute.await_args.args[0].autocompact == 300000
    hub.resume(task_id, "second")
    await hub._in_flight[task_id].asyncio_task
    assert cli.execute.await_args.args[0].autocompact == 300000
    hub.resume(task_id, "third", autocompact="off")
    await hub._in_flight[task_id].asyncio_task
    assert cli.execute.await_args.args[0].autocompact == "off"
    assert (
        TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks").get(task_id).autocompact == "off"
    )
    if hub._pending_deliveries:
        await asyncio.gather(*hub._pending_deliveries)


@pytest.mark.parametrize("value", [None, "default", "off", 300000])
def test_task_config_resolves_only_webhook_inheritance(value):
    config = AgentConfig(autocompact=800000)
    assert resolve_cli_config(
        config, None, task_overrides=TaskOverrides(autocompact=value)
    ).autocompact == (800000 if value is None else value)


def test_cron_and_webhook_tools_snapshot_edit_and_inheritance(tmp_path, monkeypatch):
    from tests.cron.test_cron_add_tool import _full_args, _run_tool
    from tests.cron.test_cron_edit_tool import TOOL_EDIT, _run

    tools = Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults/workspace/tools"
    monkeypatch.setenv("DUCTOR_AUTOCOMPACT", "300000")
    added = _run_tool(tmp_path, _full_args("job"))
    assert added.returncode == 0, added.stderr
    jobs_path = tmp_path / "cron_jobs.json"
    assert json.loads(jobs_path.read_text())["jobs"][0]["autocompact"] == 300000
    monkeypatch.setenv("DUCTOR_AUTOCOMPACT", "900000")
    edited = _run(tmp_path, TOOL_EDIT, ["job", "--title", "renamed"])
    assert edited.returncode == 0, edited.stderr
    assert json.loads(jobs_path.read_text())["jobs"][0]["autocompact"] == 300000
    changed = _run(tmp_path, TOOL_EDIT, ["job", "--autocompact", "off"])
    assert changed.returncode == 0, changed.stderr
    assert json.loads(jobs_path.read_text())["jobs"][0]["autocompact"] == "off"
    before = jobs_path.read_bytes()
    invalid = _run(tmp_path, TOOL_EDIT, ["job", "--autocompact", "inherit"])
    assert invalid.returncode != 0
    assert jobs_path.read_bytes() == before
    added = _run(
        tmp_path,
        tools / "webhook_tools/webhook_add.py",
        [
            "--name",
            "hook",
            "--title",
            "Hook",
            "--description",
            "keep",
            "--mode",
            "wake",
            "--prompt-template",
            "hello",
        ],
    )
    assert added.returncode == 0, added.stderr
    hooks_path = tmp_path / "webhooks.json"
    assert json.loads(hooks_path.read_text())["hooks"][0]["autocompact"] is None
    edited = _run(
        tmp_path, tools / "webhook_tools/webhook_edit.py", ["hook", "--autocompact", "400000"]
    )
    assert edited.returncode == 0, edited.stderr
    assert json.loads(hooks_path.read_text())["hooks"][0]["autocompact"] == 400000
    edited = _run(
        tmp_path, tools / "webhook_tools/webhook_edit.py", ["hook", "--autocompact", "inherit"]
    )
    assert edited.returncode == 0, edited.stderr
    assert json.loads(hooks_path.read_text())["hooks"][0]["autocompact"] is None


@pytest.mark.parametrize("slots", [[], ["claude"]])
async def test_oneshot_probe_runs_outside_event_loop(tmp_path, monkeypatch, slots):
    loop_thread = threading.get_ident()
    probe_threads = []

    def probe(_cli):
        probe_threads.append(threading.get_ident())
        return True

    monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: "/fake/claude")
    monkeypatch.setattr("ductor_bot.cron.execution.claude_autocompact_flag_supported", probe)
    monkeypatch.setattr("ductor_bot.cli.slot_rotation.make_registry", lambda **_: MagicMock())
    execution = OneShotExecutionResult("success", "ok", b"", b"", 0, False)
    monkeypatch.setattr(
        "ductor_bot.cron.execution.execute_one_shot", AsyncMock(return_value=execution)
    )

    async def rotate(**kwargs):
        _, result = await kwargs["attempt"]("claude")
        return RotationResult(payload=result, slot="claude")

    monkeypatch.setattr("ductor_bot.cli.slot_rotation.run_with_rotation", rotate)
    await run_oneshot_task(
        _config(tmp_path, claude_accounts=slots),
        "hello",
        TaskRunOptions(cwd=tmp_path, timeout_seconds=1, timeout_label="test", ductor_home=tmp_path),
    )
    assert len(probe_threads) == 1
    assert probe_threads[0] != loop_thread
