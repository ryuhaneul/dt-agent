"""One-shot account rotation uses the bot home independently of task cwd."""

from __future__ import annotations

import json
import time

import pytest

from ductor_bot.cli.claude_accounts import SlotRegistry, SlotSnapshot
from ductor_bot.cli.param_resolver import resolve_cli_config
from ductor_bot.config import AgentConfig
from ductor_bot.infra.task_runner import TaskRunOptions, run_oneshot_task
from tests.cli.test_claude_provider import _install_slot_cli, _read_slot_calls


@pytest.mark.parametrize("suffix", ["", "agents/dev", "agents/workspace"])
async def test_oneshot_slot_paths_and_shared_cooldowns(tmp_path, monkeypatch, suffix):
    home, control, calls = _install_slot_cli(tmp_path, monkeypatch)
    bot_home = home / suffix
    bot_home.mkdir(parents=True, exist_ok=True)
    config = AgentConfig(
        ductor_home=str(bot_home),
        provider="claude",
        model="opus",
        claude_accounts=["claude-a", "claude-b"],
        user_timezone="UTC",
    )
    exec_config = resolve_cli_config(config, None)
    cwd = tmp_path / "project"
    cwd.mkdir()
    options = TaskRunOptions(cwd=cwd, timeout_seconds=10, timeout_label="test")
    registry = SlotRegistry(home / ".cli-auth" / "_claude-slots.json")
    expected = [["claude-a", "claude-b"], ["claude-b"], ["claude-a"], ["claude-a"]]
    for stage, slots in enumerate(expected):
        if stage >= 2:
            expiry = time.time() + 3600 if stage == 2 else time.time() - 1
            registry.save(SlotSnapshot(cooldowns=dict.fromkeys(config.claude_accounts, expiry)))
            control.write_text(json.dumps({"limited": []}))
        calls.write_text("")
        result = await run_oneshot_task(exec_config, "hello", options)
        assert result.execution is not None
        assert result.execution.returncode == 0
        assert [row["auth"] for row in _read_slot_calls(calls)] == [
            str(home / ".cli-auth" / slot) for slot in slots
        ]
    assert list(tmp_path.rglob("_claude-slots.json")) == [home / ".cli-auth" / "_claude-slots.json"]
    assert not (home / "agents" / ".cli-auth").exists()


@pytest.mark.parametrize("suffix", ["", "agents/dev", "agents/workspace"])
async def test_empty_accounts_pin_default_oneshot_slot(tmp_path, monkeypatch, suffix):
    home, control, calls = _install_slot_cli(tmp_path, monkeypatch)
    auth = home / ".cli-auth" / "claude"
    auth.mkdir()
    (auth / ".credentials.json").write_text("{}")
    control.write_text(json.dumps({"limited": []}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "wrong-slot"))
    config = AgentConfig(
        ductor_home=str(home / suffix),
        provider="claude",
        model="opus",
        claude_accounts=[],
    )
    result = await run_oneshot_task(
        resolve_cli_config(config, None),
        "hello",
        TaskRunOptions(cwd=tmp_path, timeout_seconds=10, timeout_label="test"),
    )
    assert result.result_text == "ok"
    assert [row["auth"] for row in _read_slot_calls(calls)] == [str(auth)]
    assert not list(home.rglob("_claude-slots.json"))


@pytest.mark.parametrize("estimated", [False, True])
async def test_oneshot_notice_distinguishes_estimated_reset(tmp_path, monkeypatch, estimated):
    from ductor_bot.i18n import init, t

    init("en")
    home, control, calls = _install_slot_cli(tmp_path, monkeypatch)
    error = "Usage limit" if estimated else "Usage limit; resets at 18:30"
    control.write_text(json.dumps({"limited": ["claude-a"], "error": error}))
    config = AgentConfig(ductor_home=str(home), provider="claude", model="opus",
                         claude_accounts=["claude-a", "claude-b"], user_timezone="UTC")
    result = await run_oneshot_task(
        resolve_cli_config(config, None), "hello",
        TaskRunOptions(cwd=tmp_path, timeout_seconds=10, timeout_label="test"))
    assert result.result_text.startswith("[Claude: claude-a → claude-b ")
    assert (t("slot.switched_estimated") in result.result_text) is estimated
    assert ("18:30" in result.result_text) is (not estimated)
    assert [row["auth"] for row in _read_slot_calls(calls)] == [
        str(home / ".cli-auth" / slot) for slot in ["claude-a", "claude-b"]]


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("explicit_verbose", [False, True])
@pytest.mark.parametrize("save_fails", [False, True])
async def test_oneshot_collects_usage_without_changing_result(
    tmp_path, monkeypatch, named, explicit_verbose, save_fails, caplog
):
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    from ductor_bot.cli import claude_usage
    from ductor_bot.infra import task_runner
    from ductor_bot.cli.claude_usage import UsageStore, usage_file_path
    from ductor_bot.cron.execution import parse_claude_result

    home = tmp_path / "home"
    home.mkdir()
    calls = tmp_path / "calls"
    script = tmp_path / "fake-claude"
    event = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "rateLimitType": "five_hour", "unifiedWindows": {
            "five_hour": {"utilization": 0.8, "resetsAt": 4000000000},
            "seven_day": {"utilization": 0.2, "resetsAt": 4000604800}}}}
    result_event = {"type": "result", "result": "unchanged one-shot result", "is_error": False}
    script.write_text("#!" + sys.executable + "\n" +
        "import json,sys,os\nfrom pathlib import Path\nsys.stdin.read()\n" +
        f"Path({str(calls)!r}).write_text(json.dumps(sys.argv[1:]))\n" +
        f"event={event!r}\nresult={result_event!r}\n" +
        "print(json.dumps([event,result] if '--verbose' in sys.argv else result))\n")
    script.chmod(0o700)
    monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: str(script))
    monkeypatch.setattr(claude_usage, "time", SimpleNamespace(time=lambda: 1900000000))
    monkeypatch.setattr(task_runner, "time", SimpleNamespace(time=lambda: 1900000000))
    if save_fails:
        def fail(*args, **kwargs):
            raise OSError("synthetic-private-value")
        monkeypatch.setattr(UsageStore, "record", fail)
    config = AgentConfig(ductor_home=str(home), provider="claude", model="opus",
                         claude_accounts=["claude-a", "claude-b"] if named else [],
                         cli_parameters={"claude": ["--verbose"] if explicit_verbose else []})
    exec_config = resolve_cli_config(config, None)
    result = await run_oneshot_task(exec_config, "hello",
        TaskRunOptions(cwd=tmp_path, timeout_seconds=5, timeout_label="test"))
    assert result.status == "success"
    assert result.result_text == "unchanged one-shot result"
    assert json.loads(calls.read_text()).count("--verbose") == 1
    assert parse_claude_result(json.dumps(result_event).encode()) == result.result_text
    assert parse_claude_result(json.dumps([event, result_event]).encode()) == result.result_text
    path = usage_file_path(Path(exec_config.working_dir) / "workspace")
    assert path == usage_file_path(home / "workspace")
    assert "synthetic-private-value" not in caplog.text
    if save_fails:
        assert UsageStore(path).load() == {}
        assert "OSError" in caplog.text
    else:
        usage = UsageStore(path).load()["claude-a" if named else "claude"]
        assert (usage.status, usage.rate_limit_type, usage.captured_at) == (
            "allowed", "five_hour", 1900000000)
        assert [(w.utilization, w.resets_at, w.captured_at) for w in usage.windows.values()] == [
            (0.8, 4000000000, 1900000000), (0.2, 4000604800, 1900000000)]


@pytest.mark.parametrize("save_fails", [False, True])
async def test_oneshot_rotation_collects_each_attempt_and_survives_save_error(
    tmp_path, monkeypatch, save_fails
):
    from ductor_bot.cli.claude_usage import UsageStore, usage_file_path

    home, control, calls = _install_slot_cli(tmp_path, monkeypatch)
    script = tmp_path / "fake-claude"
    text = script.read_text()
    original = "print(json.dumps({'type': 'result', 'is_error': error, 'result': text}))"
    assert original in text
    text = text.replace(original,
        "event = {'type':'rate_limit_event','rate_limit_info':{'status': 'rejected' if error else 'allowed',"
        "'rateLimitType':'five_hour','unifiedWindows': {'five_hour': {'utilization':1.0 if error else 0.2,"
        "'resetsAt':4000000000}, 'seven_day':{'utilization':0.3,'resetsAt':4000604800}}}}\n"
        "print(json.dumps([event, {'type':'result','is_error':error,'result':text}]))")
    script.write_text(text)
    if save_fails:
        def fail(*args, **kwargs):
            raise OSError("synthetic")
        monkeypatch.setattr(UsageStore, "record", fail)
    config = AgentConfig(ductor_home=str(home), provider="claude", model="opus",
                         claude_accounts=["claude-a", "claude-b"], user_timezone="UTC")
    result = await run_oneshot_task(resolve_cli_config(config, None), "hello",
        TaskRunOptions(cwd=tmp_path, timeout_seconds=5, timeout_label="test"))
    assert result.status == "success"
    assert result.result_text.startswith("[Claude: claude-a → claude-b ")
    assert result.result_text.endswith("ok")
    assert [row["auth"] for row in _read_slot_calls(calls)] == [
        str(home / ".cli-auth" / slot) for slot in ["claude-a", "claude-b"]]
    slots = UsageStore(usage_file_path(home / "workspace")).load()
    if save_fails:
        assert slots == {}
    else:
        assert slots["claude-a"].status == "rejected"
        assert slots["claude-a"].windows["five_hour"].utilization == 1.0
        assert slots["claude-b"].status == "allowed"
        assert slots["claude-b"].windows["five_hour"].utilization == 0.2
