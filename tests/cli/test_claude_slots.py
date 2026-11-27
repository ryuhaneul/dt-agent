"""Slot management and cross-process read-modify-write transactions."""

from __future__ import annotations

import asyncio
import json
import logging
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


@pytest.fixture
def slot_logs(caplog):
    logger_name = "ductor_bot.cli.claude_slots"
    caplog.set_level(logging.INFO, logger=logger_name)

    def messages():
        return [
            message
            for name, level, message in caplog.record_tuples
            if name == logger_name and level == logging.INFO
        ]

    return messages


def test_priority_operations_keep_other_fields_and_lock_inode(home, slot_logs):
    from ductor_bot.cli.claude_slots import move, prefer, set_priority

    prefer(home, "claude-c")
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
    set_priority(home, ["claude-b", "claude-a", "claude-c"])
    assert _config(home) == {
        "claude_accounts": ["claude-b", "claude-a", "claude-c"],
        "model": "opus",
    }
    assert lock.stat().st_ino == inode
    assert slot_logs() == [
        "Claude slot preferred: claude-c",
        "Claude slot moved: claude-a (+1)",
        "Claude slot moved: claude-a (-1)",
        "Claude slot priority set: claude-b, claude-a, claude-c",
    ]


@pytest.mark.parametrize(
    "first,second,expected",
    [
        ("claude-a", "claude-b", ["claude-b", "claude-a", "claude-c"]),
        ("claude-a", "claude-c", ["claude-c", "claude-b", "claude-a"]),
    ],
)
def test_swap_slot_priority(home, first, second, expected):
    from ductor_bot.cli import claude_slots

    claude_slots.swap(home, first, second)
    assert _config(home) == {"claude_accounts": expected, "model": "opus"}


@pytest.mark.parametrize(
    "first,second,error",
    [
        ("claude-a", "absent", "unknown_slot"),
        ("absent", "claude-a", "unknown_slot"),
        ("claude-a", "claude-a", "invalid_swap"),
    ],
)
def test_swap_slot_priority_rejects_invalid_pairs(home, first, second, error):
    from ductor_bot.cli import claude_slots

    original = _config(home)
    with pytest.raises(ValueError, match=error):
        claude_slots.swap(home, first, second)
    assert _config(home) == original


def test_swap_slot_priority_logs_changed_slots(home, slot_logs):
    from ductor_bot.cli.claude_slots import swap

    swap(home, "claude-a", "claude-c")

    assert slot_logs() == ["Claude slot swapped: claude-a, claude-c"]


def test_priority_log_hides_invalid_configured_names(home, slot_logs):
    from ductor_bot.cli.claude_slots import prefer, set_priority

    invalid_name = "unexpected/path"
    update_config_file(home / "config" / "config.json", claude_accounts=["claude-a", invalid_name])

    prefer(home, invalid_name)
    set_priority(home, ["claude-a", invalid_name])

    assert slot_logs() == [
        "Claude slot preferred: <invalid slot name>",
        "Claude slot priority set: claude-a, <invalid slot name>",
    ]


@pytest.mark.parametrize("operation", ["prefer", "move", "clear", "missing", "duplicate", "delta"])
def test_invalid_operations_do_not_write_data(home, operation, slot_logs):
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
    assert slot_logs() == []


def test_clear_preserves_other_entries_and_overview_contract(home, slot_logs):
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
            {
                "name": "claude-a",
                "authenticated": True,
                "auth_expired": False,
                "cooldown_until": reset,
            },
            {
                "name": "claude-b",
                "authenticated": False,
                "auth_expired": False,
                "cooldown_until": reset,
            },
            {
                "name": "claude-c",
                "authenticated": False,
                "auth_expired": False,
                "cooldown_until": None,
            },
        ]
    }
    clear_cooldown(home, "claude-a")
    assert registry.load().cooldowns == {"claude-b": reset, "claude-c": 1}
    assert registry.load().last_used == "claude-b"
    expected_logs = ["Claude slot cooldown cleared: claude-a"]
    assert slot_logs() == expected_logs
    clear_cooldown(home, "claude-a")
    assert slot_logs() == expected_logs

    clear_cooldown(home, None)
    assert registry.load().cooldowns == {}
    assert overview(home, AgentConfig()) == {"slots": []}
    expected_logs.append("All Claude slot cooldowns cleared")
    assert slot_logs() == expected_logs
    clear_cooldown(home, None)
    assert slot_logs() == expected_logs


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


async def test_auth_expiry_overview_and_clear_cooldown_preserve_block(home):
    from ductor_bot.cli.claude_slots import clear_cooldown, overview
    from ductor_bot.cli.slot_rotation import record_auth_failure

    auth = home / ".cli-auth" / "claude-a"
    auth.mkdir(parents=True)
    cred = auth / ".credentials.json"
    cred.write_text("{}")
    registry = SlotRegistry(state_file_path(home))
    registry.save(SlotSnapshot(cooldowns={"claude-a": time.time() + 3600}))
    await record_auth_failure(registry, "claude-a", registry.credential_mtimes("claude-a"))
    config = AgentConfig(claude_accounts=["claude-a"])
    slot = overview(home, config)["slots"][0]
    assert slot["auth_expired"] is True
    assert slot["authenticated"] is False
    clear_cooldown(home, "claude-a")
    assert overview(home, config)["slots"][0]["auth_expired"] is True
    old = cred.stat().st_mtime_ns
    os.utime(cred, ns=(old + 1000000, old + 1000000))
    slot = overview(home, config)["slots"][0]
    assert slot["auth_expired"] is False
    assert slot["authenticated"] is True


def test_overview_ignores_stored_api_key_auth_failure(home):
    from ductor_bot.cli.claude_slots import overview

    slot = home / ".cli-auth" / "claude-api"
    slot.mkdir(parents=True)
    (slot / "settings.json").write_text('{"apiKeyHelper": "helper.sh"}')
    registry = SlotRegistry(state_file_path(home))
    registry.state_path.write_text(
        json.dumps(
            {
                "auth_failures": {"claude-api": registry.credential_mtimes("claude-api")},
            }
        )
    )
    assert overview(home, AgentConfig(claude_accounts=["claude-api"]))["slots"] == [
        {
            "name": "claude-api",
            "authenticated": True,
            "auth_expired": False,
            "cooldown_until": None,
        }
    ]


@pytest.mark.parametrize(
    ("slot", "code"),
    [
        ("claude", "slot_default"),
        ("claude-missing", "slot_not_found"),
        ("../claude-a", "invalid_slot"),
    ],
)
def test_remove_refuses_default_missing_and_invalid_slots(home, slot, code, slot_logs):
    from ductor_bot.cli.claude_slots import remove

    before = _config(home)
    with pytest.raises(ValueError, match=code):
        remove(home, slot)
    assert _config(home) == before
    assert slot_logs() == []


@pytest.mark.parametrize("slot", ["claude-a", "claude-unregistered"])
def test_remove_preserves_other_state_and_shared_targets(home, slot):
    from ductor_bot.cli.claude_accounts import AuthFailureDetails
    from ductor_bot.cli.claude_slots import remove

    registry = SlotRegistry(state_file_path(home))
    root = registry.state_path.parent
    shared = root / "_shared-projects"
    shared.mkdir(parents=True)
    (shared / "history").write_text("keep")
    folder = root / slot
    folder.mkdir()
    (folder / "projects").symlink_to(shared, target_is_directory=True)
    (folder / ".credentials.json").write_text("fixture")
    registry.save(SlotSnapshot(
        cooldowns={slot: 100, "claude-b": 200},
        last_used=slot,
        auth_failures={slot: registry.credential_mtimes(slot)},
        auth_failure_details={slot: AuthFailureDetails()},
    ))
    config_path = home / "config/config.json"
    inode = config_path.stat().st_ino

    remove(home, slot)

    assert not folder.exists()
    assert (shared / "history").read_text() == "keep"
    assert registry.load() == SlotSnapshot(cooldowns={"claude-b": 200})
    assert _config(home)["claude_accounts"] == [
        name for name in ("claude-a", "claude-b", "claude-c") if name != slot
    ]
    if slot == "claude-unregistered":
        assert config_path.stat().st_ino == inode


def test_remove_reports_partial_failure_and_allows_retry(home, monkeypatch, caplog, slot_logs):
    from ductor_bot.cli import claude_slots

    registry = SlotRegistry(state_file_path(home))
    registry.save(SlotSnapshot(cooldowns={"claude-a": 100, "claude-b": 200}))
    folder = registry.state_path.parent / "claude-a"
    folder.mkdir()

    def fail(path):
        assert path == folder
        raise PermissionError(13, "Permission denied", path)

    with monkeypatch.context() as patch:
        patch.setattr(claude_slots.shutil, "rmtree", fail)
        with pytest.raises(claude_slots.SlotDirectoryError) as error:
            claude_slots.remove(home, "claude-a")

    assert error.value.path == folder
    assert error.value.reason == "Permission denied"
    assert folder.is_dir()
    assert "claude-a" not in _config(home)["claude_accounts"]
    assert registry.load().cooldowns == {"claude-b": 200}
    warnings = [
        record
        for record in caplog.records
        if record.name == claude_slots.__name__ and record.levelno == logging.WARNING
    ]
    (warning,) = warnings
    assert warning.getMessage() == (
        "Claude slot unregistered but its directory could not be removed: claude-a"
    )
    assert warning.exc_info is None
    assert slot_logs() == []
    claude_slots.remove(home, "claude-a")
    assert not folder.exists()
    assert slot_logs() == ["Claude slot deleted: claude-a"]


def _seed_auth_change(home, slot):
    from ductor_bot.cli.claude_accounts import host_slot_config_dir
    from ductor_bot.cli.claude_usage import UsageStore, usage_file_path

    folder = host_slot_config_dir(home, slot)
    folder.mkdir(parents=True)
    registry = SlotRegistry(state_file_path(home))
    registry.save(SlotSnapshot(cooldowns={slot: time.time() + 100, "claude-c": 200}))
    usage = UsageStore(usage_file_path(home))
    event = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}
    usage.record(slot, event)
    usage.record("claude-c", event)
    return folder, registry, usage


@pytest.mark.parametrize("slot", ["claude", "claude-a"])
async def test_logout_preserves_slot_and_quarantines_until_credentials_change(
    home, monkeypatch, slot, slot_logs
):
    from ductor_bot.cli import claude_slots
    from ductor_bot.cli.claude_accounts import container_slot_config_dir
    from ductor_bot.cli.slot_rotation import hot_slots, slot_use_lock

    folder, registry, usage = _seed_auth_change(home, slot)
    credentials = folder / ".credentials.json"
    credentials.write_text("fixture")
    original_config = _config(home)

    def fake_logout(argv, **kwargs):
        assert argv == ("claude", "auth", "logout")
        assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == container_slot_config_dir(slot)
        assert kwargs["timeout"] == 30
        with slot_use_lock(folder.parent, slot, include_default=True) as reserved:
            assert not reserved
        if slot == "claude":
            with slot_use_lock(folder.parent, slot) as turn_reserved:
                assert turn_reserved
        credentials.unlink()
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(claude_slots.subprocess, "run", fake_logout)
    claude_slots.logout(home, slot)

    assert slot_logs() == [f"Claude slot logged out: {slot}"]
    assert folder.is_dir()
    assert _config(home) == original_config
    snapshot = registry.load()
    assert snapshot.cooldowns == {"claude-c": 200}
    assert snapshot.auth_failures == {slot: registry.credential_mtimes(slot)}
    assert snapshot.auth_failure_details[slot].kind == "login_expired"
    assert await hot_slots(registry, [slot, "claude-b"]) == ["claude-b"]
    assert set(usage.load()) == {"claude-c"}
    credentials.write_text("new fixture")
    assert await hot_slots(registry, [slot, "claude-b"]) == [slot, "claude-b"]


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("busy", "slot_busy"),
        ("active_api", "api_key_slot"),
        ("detached_api", "api_key_slot"),
        ("nonzero", "logout_failed"),
        ("still_authenticated", "logout_failed"),
        ("timeout", "logout_failed"),
        ("missing_cli", "logout_failed"),
    ],
)
def test_logout_failure_preserves_state(home, monkeypatch, case, code, slot_logs):
    from ductor_bot.cli import claude_slots
    from ductor_bot.cli.slot_rotation import slot_use_lock

    slot = "claude-a"
    folder, registry, usage = _seed_auth_change(home, slot)
    credentials = folder / ".credentials.json"
    credentials.write_text("fixture")
    if case == "active_api":
        (folder / "settings.json").write_text('{"apiKeyHelper": "helper.sh"}')
    if case == "detached_api":
        (folder / "api-key-helper.detached").write_text("helper.sh\n")
    before = (registry.state_path.read_bytes(), usage.load(), _config(home))

    def fake_logout(argv, **kwargs):
        if case == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if case == "missing_cli":
            raise FileNotFoundError
        assert case in {"nonzero", "still_authenticated"}
        return subprocess.CompletedProcess(argv, int(case == "nonzero"))

    monkeypatch.setattr(claude_slots.subprocess, "run", fake_logout)
    if case == "busy":
        with slot_use_lock(folder.parent, slot) as held:
            assert held
            with pytest.raises(claude_slots.SlotDeleteError, match=r"^slot_busy$"):
                claude_slots.logout(home, slot)
    else:
        with pytest.raises(claude_slots.SlotDeleteError, match=f"^{code}$"):
            claude_slots.logout(home, slot)
    assert (registry.state_path.read_bytes(), usage.load(), _config(home)) == before
    assert credentials.read_text() == "fixture"
    assert slot_logs() == []


@pytest.mark.parametrize("helper", ["./api-key-helper.sh", "helper.sh"])
def test_disconnect_api_key_preserves_settings_and_can_be_restored(home, helper, slot_logs):
    from ductor_bot.cli.claude_accounts import slot_kind
    from ductor_bot.cli.claude_slots import disconnect_api_key

    slot = "claude-a"
    folder, registry, usage = _seed_auth_change(home, slot)
    helper_script = folder / "api-key-helper.sh"
    if helper.startswith("./"):
        helper_script.write_text("fixture")
    settings = folder / "settings.json"
    other = {"permissions": {"allow": ["Read"]}}
    settings.write_text(json.dumps({**other, "apiKeyHelper": helper}))
    before = _config(home)
    assert slot_kind(folder) == "api_key"

    disconnect_api_key(home, slot)

    assert slot_logs() == ["Claude slot API key disconnected: claude-a"]
    assert _config(home) == before
    assert json.loads(settings.read_text()) == other
    assert slot_kind(folder) == "api_key_detached"
    marker = folder / "api-key-helper.detached"
    assert marker.read_text() == f"{helper}\n"
    assert marker.stat().st_mode & 0o777 == 0o600
    if helper.startswith("./"):
        assert helper_script.read_text() == "fixture"
    else:
        assert not helper_script.exists()
    snapshot = registry.load()
    assert snapshot.cooldowns == {"claude-c": 200}
    assert snapshot.auth_failures == {slot: registry.credential_mtimes(slot)}
    assert snapshot.auth_failure_details[slot].kind == "login_expired"
    assert set(usage.load()) == {"claude-c"}

    settings.write_text(
        json.dumps({**other, "apiKeyHelper": marker.read_text().removesuffix("\n")})
    )
    assert marker.is_file()
    assert slot_kind(folder) == "api_key"
    assert slot not in registry.load().auth_failures


@pytest.mark.parametrize("failure", ["marker", "settings", "usage"])
def test_disconnect_write_failure_preserves_settings(home, monkeypatch, failure, slot_logs):
    from ductor_bot.cli import claude_slots
    from ductor_bot.cli.claude_accounts import slot_kind

    slot = "claude-a"
    folder, registry, usage = _seed_auth_change(home, slot)
    settings = folder / "settings.json"
    settings.write_text('{"apiKeyHelper": "helper.sh", "keep": true}\n')
    before = (settings.read_bytes(), registry.state_path.read_bytes(), usage.load())

    def fail(*_args):
        raise OSError("write failed")

    if failure == "marker":
        monkeypatch.setattr(claude_slots, "atomic_text_save", fail)
    elif failure == "settings":
        monkeypatch.setattr(claude_slots, "atomic_json_save", fail)
    else:
        monkeypatch.setattr(claude_slots.UsageStore, "remove", fail)
    with pytest.raises(claude_slots.SlotDeleteError, match=r"^disconnect_failed$"):
        claude_slots.disconnect_api_key(home, slot)
    assert settings.read_bytes() == before[0]
    assert usage.load() == before[2]
    if failure == "usage":
        assert registry.load() == SlotSnapshot(cooldowns={"claude-c": 200})
    else:
        assert registry.state_path.read_bytes() == before[1]
    assert slot_kind(folder) == "api_key"
    assert slot_logs() == []


def test_disconnect_refuses_oauth_and_busy_slots(home):
    from ductor_bot.cli import claude_slots
    from ductor_bot.cli.slot_rotation import slot_use_lock

    slot = "claude-a"
    folder, _, _ = _seed_auth_change(home, slot)
    settings = folder / "settings.json"
    settings.write_text("{}")
    with pytest.raises(claude_slots.SlotDeleteError, match=r"^not_api_key_slot$"):
        claude_slots.disconnect_api_key(home, slot)
    settings.write_text('{"apiKeyHelper": "helper.sh"}')
    with slot_use_lock(folder.parent, slot) as held:
        assert held
        with pytest.raises(claude_slots.SlotDeleteError, match=r"^slot_busy$"):
            claude_slots.disconnect_api_key(home, slot)
    assert not (folder / "api-key-helper.detached").exists()


def test_remove_clears_only_deleted_slots_usage(home):
    from ductor_bot.cli.claude_slots import remove

    _, _, usage = _seed_auth_change(home, "claude-a")
    retained = usage.load()["claude-c"]
    remove(home, "claude-a")
    assert usage.load() == {"claude-c": retained}


@pytest.mark.parametrize("operation", ["logout", "disconnect_api_key"])
def test_auth_change_rejects_linked_path(home, monkeypatch, operation):
    from ductor_bot.cli import claude_slots

    slot = "claude-a"
    folder, registry, usage = _seed_auth_change(home, slot)
    credential = folder / (".credentials.json" if operation == "logout" else "settings.json")
    credential.write_text('{"apiKeyHelper":"helper.sh"}' if operation != "logout" else "{}")
    original = folder.rename(folder.with_name("claude-original"))
    folder.symlink_to(original, target_is_directory=True)
    before = {path.relative_to(original): path.read_bytes() for path in original.rglob("*")
              if path.is_file()}
    config = _config(home)

    def unexpected(*_args, **_kwargs):
        pytest.fail("A linked slot must be rejected before any auth mutation")

    monkeypatch.setattr(claude_slots.subprocess, "run", unexpected)
    monkeypatch.setattr(claude_slots, "atomic_text_save", unexpected)
    with pytest.raises(claude_slots.SlotDeleteError, match=r"^invalid_slot$"):
        getattr(claude_slots, operation)(home, slot)
    assert {path.relative_to(original): path.read_bytes() for path in original.rglob("*")
            if path.is_file()} == before
    assert _config(home) == config
    assert slot in registry.load().cooldowns
    assert slot in usage.load()


def test_disconnect_keeps_settings_symlink_out_of_api_key_slots(home):
    from ductor_bot.cli import claude_slots

    slot = "claude-a"
    folder, _, _ = _seed_auth_change(home, slot)
    shared = home / "shared-settings.json"
    shared.write_text('{"apiKeyHelper":"helper.sh"}')
    settings = folder / "settings.json"
    settings.symlink_to(shared)
    before = shared.read_bytes()

    with pytest.raises(claude_slots.SlotDeleteError, match=r"^not_api_key_slot$"):
        claude_slots.disconnect_api_key(home, slot)

    assert settings.is_symlink()
    assert shared.read_bytes() == before
    assert not (folder / "api-key-helper.detached").exists()


@pytest.mark.parametrize("operation", ["logout", "disconnect_api_key", "remove"])
def test_slot_usage_stays_when_state_update_fails(home, monkeypatch, operation):
    from ductor_bot.cli import claude_slots

    slot = "claude-a"
    folder, registry, usage = _seed_auth_change(home, slot)
    before_usage = usage.load()
    before_state = registry.state_path.read_bytes()
    before_accounts = _config(home)
    credentials = folder / ".credentials.json"
    settings = folder / "settings.json"
    if operation == "logout":
        credentials.write_text("{}")
    elif operation == "disconnect_api_key":
        settings.write_text('{"apiKeyHelper":"helper.sh"}')

    def fake_logout(argv, **_kwargs):
        credentials.unlink()
        return subprocess.CompletedProcess(argv, 0)

    def refuse_state_write(*_args, **_kwargs):
        raise OSError("State write failed")

    monkeypatch.setattr(claude_slots.subprocess, "run", fake_logout)
    monkeypatch.setattr(claude_slots.SlotRegistry, "update", refuse_state_write)
    if operation == "remove":
        with pytest.raises(OSError, match="State write failed"):
            claude_slots.remove(home, slot)
    else:
        code = "logout_failed" if operation == "logout" else "disconnect_failed"
        with pytest.raises(claude_slots.SlotDeleteError, match=f"^{code}$"):
            getattr(claude_slots, operation)(home, slot)

    assert registry.state_path.read_bytes() == before_state
    assert usage.load() == before_usage
    assert _config(home) == before_accounts
    assert folder.is_dir()
    assert not credentials.exists()
    if operation == "disconnect_api_key":
        assert settings.read_text() == '{"apiKeyHelper":"helper.sh"}'


def test_delete_can_retry_after_usage_cleanup_fails(home, monkeypatch):
    from ductor_bot.cli import claude_slots

    slot = "claude-a"
    folder, registry, usage = _seed_auth_change(home, slot)
    before = usage.load()

    def refuse_usage_write(*_args):
        raise OSError("Usage write failed")

    with monkeypatch.context() as patch:
        patch.setattr(claude_slots.UsageStore, "remove", refuse_usage_write)
        with pytest.raises(OSError, match="Usage write failed"):
            claude_slots.remove(home, slot)

    assert registry.load().cooldowns == {"claude-c": 200}
    assert usage.load() == before
    assert folder.is_dir()
    assert slot in _config(home)["claude_accounts"]

    claude_slots.remove(home, slot)
    assert not folder.exists()
    assert slot not in _config(home)["claude_accounts"]
    assert usage.load() == {"claude-c": before["claude-c"]}


def test_auth_changes_allow_a_linked_auth_root(home, monkeypatch):
    from ductor_bot.cli import claude_slots
    from ductor_bot.cli.claude_accounts import host_cli_auth_dir, slot_kind

    root = host_cli_auth_dir(home)
    storage = home / "auth-storage"
    storage.mkdir()
    root.symlink_to(storage, target_is_directory=True)
    oauth = storage / "claude-a"
    oauth.mkdir()
    credentials = oauth / ".credentials.json"
    credentials.write_text("{}")
    api = storage / "claude-b"
    api.mkdir()
    settings = api / "settings.json"
    settings.write_text('{"apiKeyHelper":"helper.sh","keep":true}')

    def fake_logout(argv, **_kwargs):
        credentials.unlink()
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(claude_slots.subprocess, "run", fake_logout)

    claude_slots.logout(home, oauth.name)
    claude_slots.disconnect_api_key(home, api.name)

    assert root.is_symlink()
    assert not credentials.exists()
    assert slot_kind(api) == "api_key_detached"
    assert json.loads(settings.read_text()) == {"keep": True}


def test_concurrent_first_slot_identity_reads_publish_one_id(home, monkeypatch):
    import stat
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from ductor_bot.cli import claude_slots

    folder = home / ".cli-auth" / "claude-a"
    folder.mkdir(parents=True)
    assert claude_slots.current_slot_identity(home, folder.name) is None
    assert list(folder.parent.iterdir()) == [folder]
    lock = claude_slots.exclusive_file_lock
    both_need_id = Barrier(2)

    def enter_lock(path):
        both_need_id.wait(timeout=3)
        return lock(path)

    with monkeypatch.context() as patcher:
        patcher.setattr(claude_slots, "exclusive_file_lock", enter_lock)
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(claude_slots.slot_identity, home, folder.name)
            second = workers.submit(claude_slots.slot_identity, home, folder.name)
            identity = first.result(timeout=10)
            assert second.result(timeout=10) == identity

    (identity_file,) = folder.parent.glob(".slot-*.id")
    info = folder.lstat()
    assert identity_file.read_text() == f"{identity.removeprefix('id:')} {info.st_dev} {info.st_ino}\n"
    assert stat.S_IMODE(identity_file.stat().st_mode) == 0o600
    assert claude_slots.slot_identity(home, folder.name) == identity
    assert claude_slots.current_slot_identity(home, folder.name) == identity
