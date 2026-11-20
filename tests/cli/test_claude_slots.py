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
def test_remove_refuses_default_missing_and_invalid_slots(home, slot, code):
    from ductor_bot.cli.claude_slots import remove

    before = _config(home)
    with pytest.raises(ValueError, match=code):
        remove(home, slot)
    assert _config(home) == before


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


def test_remove_reports_partial_failure_and_allows_retry(home, monkeypatch):
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
    claude_slots.remove(home, "claude-a")
    assert not folder.exists()


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
    home, monkeypatch, slot
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
def test_logout_failure_preserves_state(home, monkeypatch, case, code):
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


@pytest.mark.parametrize("helper", ["./api-key-helper.sh", "helper.sh"])
def test_disconnect_api_key_preserves_settings_and_can_be_restored(home, helper):
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
def test_disconnect_write_failure_preserves_settings(home, monkeypatch, failure):
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
    assert (settings.read_bytes(), registry.state_path.read_bytes(), usage.load()) == before
    assert slot_kind(folder) == "api_key"


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
