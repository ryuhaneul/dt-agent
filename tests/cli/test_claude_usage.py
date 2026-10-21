"""Passive Claude usage events and process-safe snapshot persistence."""
from __future__ import annotations

import json
import logging
import multiprocessing
from pathlib import Path

import pytest

from ductor_bot.cli.claude_accounts import state_file_path
from ductor_bot.cli.claude_usage import UsageStore, parse_usage_event, usage_file_path

CAPTURED = 1900000000
REJECTED = {'type': 'rate_limit_event',
 'rate_limit_info': {'status': 'rejected',
                     'resetsAt': 1792557600,
                     'rateLimitType': 'five_hour',
                     'overageStatus': 'rejected',
                     'overageDisabledReason': 'org_level_disabled',
                     'isUsingOverage': False,
                     'unifiedWindows': {'five_hour': {'utilization': 1, 'resetsAt': 1792557600},
                                        'seven_day': {'utilization': 0.36,
                                                      'resetsAt': 1793041200}}}}
ALLOWED = {'type': 'rate_limit_event',
 'rate_limit_info': {'status': 'allowed',
                     'resetsAt': 1792560600,
                     'rateLimitType': 'five_hour',
                     'overageStatus': 'rejected',
                     'overageDisabledReason': 'org_level_disabled',
                     'isUsingOverage': False,
                     'unifiedWindows': {'five_hour': {'utilization': 0.11, 'resetsAt': 1792560600},
                                        'seven_day': {'utilization': 0.02,
                                                      'resetsAt': 1793134800}}}}


def window_event(name, value, *, status="allowed"):
    return {"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "rateLimitType": name, "utilization": value, "resetsAt": CAPTURED + 3600,
    }}


@pytest.mark.parametrize(("event", "expected"), [(REJECTED, [1.0, 0.36]), (ALLOWED, [0.11, 0.02])])
def test_measured_event_shapes_keep_both_windows(event, expected, caplog):
    usage = parse_usage_event(event, captured_at=CAPTURED)
    assert usage.status == event["rate_limit_info"]["status"]
    assert usage.rate_limit_type == "five_hour"
    assert usage.captured_at == CAPTURED
    assert [usage.windows[name].utilization for name in ("five_hour", "seven_day")] == expected
    assert all(window.captured_at == CAPTURED for window in usage.windows.values())
    assert [usage.windows[name].resets_at for name in ("five_hour", "seven_day")] == [
        event["rate_limit_info"]["unifiedWindows"][name]["resetsAt"]
        for name in ("five_hour", "seven_day")
    ]
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_unified_windows_override_top_level_and_allow_above_one():
    event = json.loads(json.dumps(ALLOWED))
    event["rate_limit_info"]["utilization"] = 0.99
    event["rate_limit_info"]["unifiedWindows"]["five_hour"]["utilization"] = 1.25
    usage = parse_usage_event(event, captured_at=CAPTURED)
    assert usage.windows["five_hour"].utilization == 1.25


def test_partial_and_status_only_events_preserve_other_window(tmp_path):
    store = UsageStore(usage_file_path(tmp_path))
    store.record("claude-a", ALLOWED, captured_at=CAPTURED)
    store.record("claude-a", window_event("five_hour", 1.2), captured_at=CAPTURED + 10)
    usage = store.load()["claude-a"]
    assert usage.windows["five_hour"].utilization == 1.2
    assert usage.windows["five_hour"].captured_at == CAPTURED + 10
    assert usage.windows["seven_day"].utilization == 0.02
    assert usage.windows["seven_day"].captured_at == CAPTURED
    event = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning"}}
    store.record("claude-a", event, captured_at=CAPTURED + 20)
    current = store.load()["claude-a"]
    assert current.status == "allowed_warning"
    assert current.rate_limit_type is None
    assert current.captured_at == CAPTURED + 20
    assert current.windows == usage.windows


def test_one_unified_window_does_not_replace_other_window(tmp_path):
    store = UsageStore(usage_file_path(tmp_path))
    store.record("claude-a", REJECTED, captured_at=CAPTURED)
    event = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "unifiedWindows": {
            "seven_day": {"utilization": 0.4, "resetsAt": CAPTURED + 5000}}}}
    store.record("claude-a", event, captured_at=CAPTURED + 1)
    usage = store.load()["claude-a"]
    assert usage.windows["five_hour"].utilization == 1.0
    assert usage.windows["seven_day"].utilization == 0.4


@pytest.mark.parametrize("bad", [None, [], "synthetic-private-value", {"status": "new-status"}])
def test_malformed_event_info_is_ignored_with_one_safe_log(bad, caplog):
    event = {"type": "rate_limit_event", "rate_limit_info": bad}
    assert parse_usage_event(event, captured_at=CAPTURED) is None
    assert len(caplog.records) == 1
    assert "synthetic-private-value" not in caplog.text


@pytest.mark.parametrize("value", [-0.1, True, "0.2", float("nan"), float("inf")])
def test_invalid_window_values_are_ignored_with_one_log(value, caplog):
    event = window_event("five_hour", value)
    usage = parse_usage_event(event, captured_at=CAPTURED)
    assert usage.windows == {}
    assert usage.status == "allowed"
    assert len(caplog.records) == 1


@pytest.mark.parametrize("root_field", [False, True])
def test_unknown_fields_are_not_persisted_or_logged(tmp_path, caplog, root_field):
    caplog.set_level(logging.DEBUG, logger="ductor_bot.cli.claude_usage")
    event = json.loads(json.dumps(ALLOWED))
    target = event if root_field else event["rate_limit_info"]
    target["new_field"] = "synthetic-private-value"
    path = usage_file_path(tmp_path)
    UsageStore(path).record("claude-a", event, captured_at=CAPTURED)
    assert "synthetic-private-value" not in path.read_text()
    assert "new_field" not in path.read_text()
    assert "synthetic-private-value" not in caplog.text
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.DEBUG


@pytest.mark.parametrize("suffix", ["workspace", "agents/helper/workspace", "agents/workspace/workspace"])
def test_usage_and_cooldown_resolve_to_same_shared_directory(tmp_path, suffix):
    path = usage_file_path(tmp_path / suffix)
    assert path == state_file_path(tmp_path).with_name("_claude-usage.json")


def test_snapshot_schema_permissions_and_cooldown_unchanged(tmp_path):
    cooldown = state_file_path(tmp_path)
    cooldown.parent.mkdir(parents=True)
    original = b'{"cooldowns":{"claude-a":1900000010},"last_used":"claude-a"}'
    cooldown.write_bytes(original)
    path = usage_file_path(tmp_path)
    store = UsageStore(path)
    store.record("claude-a", ALLOWED, captured_at=CAPTURED)
    path.chmod(0o644)
    store.record("claude-b", REJECTED, captured_at=CAPTURED + 1)
    raw = json.loads(path.read_text())
    assert set(raw) == {"version", "slots"}
    assert raw["version"] == 1
    assert set(raw["slots"]["claude-a"]) == {"status", "rate_limit_type", "captured_at", "windows"}
    assert raw["slots"]["claude-a"]["windows"]["five_hour"] == {
        "utilization": 0.11, "resets_at": 1792560600, "captured_at": CAPTURED}
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.with_name(path.name + ".lock").stat().st_mode & 0o777 == 0o600
    assert cooldown.read_bytes() == original
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert len(store.load()) == 2
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def _write_usage_process(path, barrier, index, same_slot):
    store = UsageStore(Path(path))
    window = ("five_hour", "seven_day")[index]
    for number in range(20):
        barrier.wait(timeout=10)
        slot = "claude-a" if same_slot else f"claude-{index}-{number}"
        store.record(slot, window_event(window, number / 100), captured_at=CAPTURED + number)


@pytest.mark.parametrize("same_slot", [False, True])
def test_two_processes_merge_without_losing_slots_or_windows(tmp_path, same_slot):
    context = multiprocessing.get_context("spawn")
    path = usage_file_path(tmp_path)
    cooldown = state_file_path(tmp_path)
    cooldown.parent.mkdir(parents=True)
    cooldown.write_text('{"cooldowns":{}}')
    before = cooldown.read_bytes()
    barrier = context.Barrier(2)
    processes = [context.Process(target=_write_usage_process,
                  args=(str(path), barrier, index, same_slot)) for index in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    slots = UsageStore(path).load()
    if same_slot:
        assert set(slots["claude-a"].windows) == {"five_hour", "seven_day"}
        assert all(window.utilization == 0.19 for window in slots["claude-a"].windows.values())
    else:
        assert len(slots) == 40
        for index in range(2):
            for number in range(20):
                window = ("five_hour", "seven_day")[index]
                assert slots[f"claude-{index}-{number}"].windows[window].utilization == number / 100
    assert path.stat().st_mode & 0o777 == 0o600
    assert cooldown.read_bytes() == before


@pytest.mark.parametrize("windows", [None, [], {"five_hour": {"utilization": 0.2}}])
def test_malformed_unified_windows_do_not_use_top_level_fallback(windows, caplog):
    event = json.loads(json.dumps(ALLOWED))
    event["rate_limit_info"]["unifiedWindows"] = windows
    usage = parse_usage_event(event, captured_at=CAPTURED)
    assert usage.windows == {}
    assert len(caplog.records) == 1


def test_reading_missing_snapshot_creates_no_files(tmp_path):
    path = usage_file_path(tmp_path)
    assert UsageStore(path).load() == {}
    assert not path.parent.exists()


def test_failed_atomic_replace_preserves_previous_snapshot(tmp_path, monkeypatch):
    path = usage_file_path(tmp_path)
    store = UsageStore(path)
    store.record("claude-a", ALLOWED, captured_at=CAPTURED)
    before = path.read_bytes()
    monkeypatch.setattr(Path, "replace", lambda *args: (_ for _ in ()).throw(OSError("synthetic")))
    with pytest.raises(OSError):
        store.record("claude-b", REJECTED, captured_at=CAPTURED)
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


def test_top_level_numbers_without_window_type_only_update_status(caplog):
    event = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "utilization": 0.3, "resetsAt": CAPTURED + 60}}
    usage = parse_usage_event(event, captured_at=CAPTURED)
    assert usage.windows == {}
    assert usage.status == "allowed"
    assert usage.captured_at == CAPTURED
    assert len(caplog.records) == 1



def test_unknown_window_is_ignored_with_debug_only(caplog):
    caplog.set_level(logging.DEBUG, logger="ductor_bot.cli.claude_usage")
    event = json.loads(json.dumps(ALLOWED))
    event["rate_limit_info"]["unifiedWindows"]["seven_day_opus"] = {
        "utilization": "synthetic-private-value", "resetsAt": None}
    usage = parse_usage_event(event, captured_at=CAPTURED)
    assert set(usage.windows) == {"five_hour", "seven_day"}
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.DEBUG
    assert "synthetic-private-value" not in caplog.text


@pytest.mark.parametrize("rate_type", ["new-window", 1, []])
def test_invalid_rate_limit_type_warns(rate_type, caplog):
    event = json.loads(json.dumps(ALLOWED))
    event["rate_limit_info"]["rateLimitType"] = rate_type
    usage = parse_usage_event(event, captured_at=CAPTURED)
    assert usage.rate_limit_type is None
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING


@pytest.mark.parametrize("same_time", [False, True])
def test_window_and_slot_capture_times_merge_independently(tmp_path, same_time):
    store = UsageStore(usage_file_path(tmp_path))
    store.record("claude-a", window_event("five_hour", 1.0, status="rejected"),
                 captured_at=CAPTURED + 100)
    store.record("claude-a", window_event("seven_day", 0.2), captured_at=CAPTURED)
    usage = store.load()["claude-a"]
    assert usage.status == "rejected"
    assert usage.rate_limit_type == "five_hour"
    assert usage.captured_at == CAPTURED + 100
    assert usage.windows["seven_day"].captured_at == CAPTURED
    when = CAPTURED + (100 if same_time else 50)
    store.record("claude-a", window_event("five_hour", 0.3), captured_at=when)
    current = store.load()["claude-a"]
    assert current.windows["seven_day"] == usage.windows["seven_day"]
    assert current.windows["five_hour"].utilization == (0.3 if same_time else 1.0)
    assert current.status == ("allowed" if same_time else "rejected")


async def test_late_stream_completion_keeps_latest_observation(tmp_path, monkeypatch):
    import asyncio
    from contextvars import ContextVar
    from types import SimpleNamespace

    from ductor_bot.cli import claude_provider as provider
    from ductor_bot.cli.base import CLIConfig

    clock = ContextVar("usage_clock", default=CAPTURED)
    received, finish = asyncio.Event(), asyncio.Event()
    old = window_event("five_hour", 0.1)
    new = window_event("five_hour", 1.0, status="rejected")
    for event, value in [(old, 0.05), (new, 0.5)]:
        info = event["rate_limit_info"]
        info["unifiedWindows"] = {
            "five_hour": {"utilization": info["utilization"], "resetsAt": CAPTURED + 3600},
            "seven_day": {"utilization": value, "resetsAt": CAPTURED + 604800}}

    async def fake_stream(*, spec, line_handler, **kwargs):
        event = old if spec.stdin_text == "slow" else new
        async for parsed in line_handler(json.dumps(event)):
            yield parsed
        if spec.stdin_text == "slow":
            received.set()
            await finish.wait()
        final = {"type": "result", "result": "unchanged", "is_error": False}
        async for parsed in line_handler(json.dumps(final)):
            yield parsed

    monkeypatch.setattr(provider.ClaudeCodeCLI, "_find_cli", lambda _: "fake-unused")
    monkeypatch.setattr(provider, "time", SimpleNamespace(time=lambda: clock.get()))
    monkeypatch.setattr(provider, "run_streaming_subprocess", fake_stream)
    config = CLIConfig(ductor_home=str(tmp_path), working_dir=str(tmp_path), claude_accounts=[])

    async def run(prompt, when):
        clock.set(when)
        return [event async for event in provider.ClaudeCodeCLI(config).send_streaming(prompt)]

    task = asyncio.create_task(run("slow", CAPTURED))
    try:
        await asyncio.wait_for(received.wait(), 2)
        await run("fast", CAPTURED + 100)
        store = UsageStore(usage_file_path(tmp_path / "workspace"))
        before = store.load()["claude"]
    finally:
        finish.set()
        await task
    assert before.status == "rejected"
    assert before.captured_at == CAPTURED + 100
    assert store.load()["claude"] == before



def test_invalid_utf8_snapshot_recovers_on_next_record(tmp_path, caplog):
    path = usage_file_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe")
    store = UsageStore(path)
    assert store.load() == {}
    assert len(caplog.records) == 1
    store.record("claude-a", ALLOWED, captured_at=CAPTURED)
    assert store.load()["claude-a"].captured_at == CAPTURED
