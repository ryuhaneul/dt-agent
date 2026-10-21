"""Slot management and cross-process read-modify-write transactions."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ductor_bot.cli.claude_accounts import SlotRegistry, SlotSnapshot, state_file_path
from ductor_bot.config import AgentConfig, update_config_file

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path):
    config = tmp_path / "config" / "config.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps({"claude_accounts": ["claude-a", "claude-b", "claude-c"], "model": "opus"})
    )
    return tmp_path


def _config(home):
    return json.loads((home / "config" / "config.json").read_text())


def test_priority_operations_keep_other_fields_and_lock_inode(home):
    from ductor_bot.cli.claude_slots import move, prefer, set_priority

    prefer(home, "claude-c")
    lock = home / "config" / ".config.json.lock"
    inode = lock.stat().st_ino
    assert _config(home)["claude_accounts"] == ["claude-c", "claude-a", "claude-b"]
    move(home, "claude-a", 1)
    assert _config(home)["claude_accounts"] == ["claude-c", "claude-b", "claude-a"]
    move(home, "claude-a", 1)
    move(home, "claude-c", -1)
    move(home, "claude-a", -1)
    assert _config(home)["claude_accounts"] == ["claude-c", "claude-a", "claude-b"]
    set_priority(home, ["claude-b", "claude-a", "claude-c"])
    assert _config(home) == {
        "claude_accounts": ["claude-b", "claude-a", "claude-c"],
        "model": "opus",
    }
    assert lock.stat().st_ino == inode


@pytest.mark.parametrize("operation", ["prefer", "move", "clear", "missing", "duplicate", "delta"])
def test_invalid_operations_do_not_write_data(home, operation):
    from ductor_bot.cli.claude_slots import clear_cooldown, move, prefer, set_priority

    path = home / "config" / "config.json"
    registry = SlotRegistry(state_file_path(home))
    registry.save(SlotSnapshot(cooldowns={"claude-a": time.time() + 1000}))
    before = (path.read_bytes(), registry.state_path.read_bytes())
    operations = {
        "prefer": lambda: prefer(home, "absent"),
        "move": lambda: move(home, "absent", 1),
        "clear": lambda: clear_cooldown(home, "absent"),
        "missing": lambda: set_priority(home, ["claude-a"]),
        "duplicate": lambda: set_priority(home, ["claude-a", "claude-a", "claude-c"]),
        "delta": lambda: move(home, "claude-a", 2),
    }
    expected = (
        "unknown_slot"
        if operation in {"prefer", "move", "clear"}
        else ("invalid_delta" if operation == "delta" else "invalid_priority")
    )
    with pytest.raises(ValueError, match=expected):
        operations[operation]()
    assert before == (path.read_bytes(), registry.state_path.read_bytes())


def test_clear_preserves_other_entries_and_overview_contract(home):
    from ductor_bot.cli.claude_slots import clear_cooldown, overview

    reset = time.time() + 3600
    registry = SlotRegistry(state_file_path(home))
    registry.save(
        SlotSnapshot(
            cooldowns={"claude-a": reset, "claude-b": reset, "claude-c": 1}, last_used="claude-b"
        )
    )
    auth = home / ".cli-auth" / "claude-a"
    auth.mkdir()
    (auth / ".credentials.json").write_text("{}")
    config = AgentConfig(claude_accounts=["claude-a", "claude-b", "claude-c"])
    assert overview(home, config) == {
        "slots": [
            {"name": "claude-a", "authenticated": True, "cooldown_until": reset},
            {"name": "claude-b", "authenticated": False, "cooldown_until": reset},
            {"name": "claude-c", "authenticated": False, "cooldown_until": None},
        ]
    }
    clear_cooldown(home, "claude-a")
    assert registry.load().cooldowns == {"claude-b": reset, "claude-c": 1}
    assert registry.load().last_used == "claude-b"
    clear_cooldown(home, None)
    assert registry.load().cooldowns == {}
    assert overview(home, AgentConfig()) == {"slots": []}


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["show"], None),
        (["prefer", "claude-c"], None),
        (["priority", "claude-c,claude-a,claude-b"], None),
        (["clear-cooldown", "claude-a"], None),
        (["clear-cooldown", "all"], None),
        (["prefer", "absent"], "unknown_slot"),
        (["priority", "claude-a"], "invalid_priority"),
        ([], "invalid_arguments"),
        (["move", "claude-a", "2"], "invalid_arguments"),
    ],
)
def test_module_json_protocol(home, args, error):
    result = subprocess.run(
        [sys.executable, "-m", "ductor_bot.cli.claude_slots", *args],
        env=dict(os.environ, DUCTOR_HOME=str(home)),
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == (2 if error else 0), result.stderr
    assert len(result.stdout.splitlines()) == 1
    data = json.loads(result.stdout)
    if error:
        assert data == {"error": error}
    else:
        assert set(data) == {"slots"}
        assert len(data["slots"]) == 3
        if args[0] in {"prefer", "priority"}:
            assert data["slots"][0]["name"] == "claude-c"


def _cooldown_worker(home, operation, loaded, release):
    from ductor_bot.cli.slot_rotation import record_cooldown

    original = SlotRegistry.load

    def gated_load(registry):
        snapshot = original(registry)
        loaded.set()
        if release is not None:
            assert release.wait(5)
        return snapshot

    SlotRegistry.load = gated_load
    registry = SlotRegistry(state_file_path(home))
    if operation == "clear":
        from ductor_bot.cli.claude_slots import clear_cooldown

        clear_cooldown(home, "claude-a")
    else:
        asyncio.run(record_cooldown(registry, operation, 9999999999.0))


def _config_worker(path, operation, loaded, release):
    original = Path.read_text

    def gated_read(self, *args, **kwargs):
        text = original(self, *args, **kwargs)
        if self == path:
            loaded.set()
            if release is not None:
                assert release.wait(5)
        return text

    Path.read_text = gated_read
    if operation == "priority":
        update_config_file(path, claude_accounts=["claude-b", "claude-a", "claude-c"])
    else:
        update_config_file(path, model="sonnet")


def _overlap(first_worker, first_args, second_worker, second_args):
    ctx = multiprocessing.get_context("fork")
    loaded, second_loaded, release = ctx.Event(), ctx.Event(), ctx.Event()
    one = ctx.Process(target=first_worker, args=(*first_args, loaded, release))
    two = ctx.Process(target=second_worker, args=(*second_args, second_loaded, None))
    one.start()
    try:
        assert loaded.wait(5)
        two.start()
        # The old writer reads a stale snapshot while the first writer is
        # paused. With locking, this read waits until the first commit.
        second_loaded.wait(0.5)
        release.set()
        one.join(10)
        two.join(10)
        assert one.exitcode == 0
        assert two.exitcode == 0
    finally:
        release.set()
        for worker in (one, two):
            if worker.is_alive():
                worker.kill()
                worker.join()


@pytest.mark.parametrize("operation", ["claude-b", "clear"])
def test_two_process_cooldown_record_and_clear_preserve_other_slots(home, operation):
    registry = SlotRegistry(state_file_path(home))
    initial = {"claude-a": 9999999999.0}
    if operation == "clear":
        initial["claude-b"] = 9999999999.0
    registry.save(SlotSnapshot(cooldowns=initial))
    _overlap(_cooldown_worker, (home, "claude-c"), _cooldown_worker, (home, operation))
    snapshot = registry.load()
    assert snapshot.cooldowns["claude-b"] == 9999999999.0
    assert snapshot.cooldowns["claude-c"] == 9999999999.0
    assert ("claude-a" in snapshot.cooldowns) is (operation != "clear")
    assert not list(registry.state_path.parent.glob("*.tmp"))


def test_two_process_config_updates_preserve_both_keys(home):
    path = home / "config" / "config.json"
    _overlap(_config_worker, (path, "priority"), _config_worker, (path, "model"))
    assert _config(home) == {
        "claude_accounts": ["claude-b", "claude-a", "claude-c"],
        "model": "sonnet",
    }


def test_transform_rejection_preserves_file(home):
    from ductor_bot.config import transform_config_file

    path = home / "config" / "config.json"
    before = path.read_bytes()

    def reject(data):
        data["model"] = "changed"
        raise ValueError("invalid_priority")

    with pytest.raises(ValueError):
        transform_config_file(path, reject)
    assert path.read_bytes() == before


def _snapshot_writer(path, gate, name):
    gate.wait(5)
    for _ in range(20):
        SlotRegistry(path).save(SlotSnapshot(cooldowns={name: 9999999999.0}))


def test_two_process_snapshot_saves_use_distinct_temp_files(home):
    ctx = multiprocessing.get_context("fork")
    gate = ctx.Event()
    path = state_file_path(home)
    workers = [
        ctx.Process(target=_snapshot_writer, args=(path, gate, name))
        for name in ("claude-a", "claude-b")
    ]
    try:
        for worker in workers:
            worker.start()
        gate.set()
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0
        assert len(SlotRegistry(path).load().cooldowns) == 1
        assert not list(path.parent.glob("*.tmp"))
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.kill()
                worker.join()


def _disable_worker(path, operation, loaded, release):
    subprocess.run(
        ["bash", str(ROOT / "scripts" / "logout.sh"), "claude", "--disable", "claude-a"],
        env=dict(os.environ, DUCTOR_HOME=str(path.parent.parent), ONBOARD_LANGUAGE="en"),
        capture_output=True,
        text=True,
        check=True,
        timeout=8,
    )
    loaded.set()


def test_shell_disable_and_priority_preserve_latest_config(home):
    path = home / "config" / "config.json"
    _overlap(_config_worker, (path, "priority"), _disable_worker, (path, "disable"))
    assert _config(home) == {"claude_accounts": ["claude-b", "claude-c"], "model": "opus"}


def test_config_update_preserves_missing_none_noop(home):
    path = home / "config" / "config.json"
    before = path.read_bytes()
    update_config_file(path, gemini_api_key=None)
    assert path.read_bytes() == before
