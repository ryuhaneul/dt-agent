"""Claude slot contracts run only against temporary homes and fake CLIs."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.webchat.cli_login import CliLoginError, CliLoginManager
from ductor_bot.webchat.cli_login_slots import (
    ClaudeLoginSlots,
    PreparedClaudeSlot,
    ensure_slot_dir,
    resolve_slot_name,
    share_slot_from_default,
)
from ductor_bot.workspace.paths import DuctorPaths

ROOT = Path(__file__).resolve().parents[2]
OWNER = "fake-owner-hmac"


def bash_helper(home, command, *arguments):
    script = """source "$1"
shift
say() { :; }
say_err() { :; }
dc() { return 99; }
case "$1" in
  name) resolve_claude_slot_name "$2" ;;
  prepare) ensure_shared_projects; ensure_slot_dir "$2" ;;
  share) share_slot_from_default "$2" ;;
esac
"""
    return subprocess.run(
        [
            "bash",
            "-c",
            script,
            "slot-contract",
            str(ROOT / "scripts/login.sh"),
            command,
            *arguments,
        ],
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "DUCTOR_HOME": str(home),
            "ONBOARD_LANGUAGE": "en",
            "LC_ALL": "C",
            "TZ": "UTC",
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


@pytest.mark.parametrize(
    "name",
    [
        "",
        "default",
        "claude",
        "alt",
        "claude-alt",
        "work_2-a",
        "0",
        "claude-a_",
        "slots",
        "claude-slots",
        "UPPER",
        "-a",
        "_a",
        "../escape",
        "a/b",
        "a.b",
        "a b",
        "claude-",
        "claude--a",
        "a\n",
    ],
)
def test_name_contract_matches_login_sh(tmp_path, name):
    result = bash_helper(tmp_path, "name", name)
    if result.returncode == 0:
        assert resolve_slot_name(name) == result.stdout.strip()
    else:
        assert result.returncode == 2
        with pytest.raises(ValueError, match="invalid_slot"):
            resolve_slot_name(name)


def tree(root):
    result = {}
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in sorted(directories + files):
            path = Path(parent) / name
            relative = re.sub(r"\d{8}T\d{6}", "TIMESTAMP", str(path.relative_to(root)))
            if path.is_symlink():
                result[relative] = ("link", str(path.readlink()))
            elif path.is_dir():
                result[relative] = ("dir",)
            else:
                result[relative] = ("file", path.read_bytes())
    return result


def seed(home):
    default = home / ".cli-auth/claude"
    default.mkdir(parents=True)
    for name in (
        ".credentials.json",
        "credentials.json",
        ".claude.json",
        "settings.json",
        ".hidden",
    ):
        (default / name).write_text("default:" + name)
    for name in ("skills", "plugins", "cache"):
        (default / name).mkdir()
        (default / name / "data").write_text("shared:" + name)
    (default / "alias").symlink_to("settings.json")
    return default


@pytest.mark.parametrize(
    "scenario",
    [
        "new",
        "legacy",
        "shared_empty",
        "both_nonempty",
        "shared_file",
        "existing_symlink",
        "projects_file",
    ],
)
def test_projects_layout_matches_login_sh(tmp_path, scenario):
    homes = [tmp_path / "bash", tmp_path / "python"]
    for home in homes:
        default = seed(home)
        alternate = home / ".cli-auth/claude-work"
        alternate.mkdir()
        projects = alternate / "projects"
        shared = default.parent / "_shared-projects"
        if scenario in {"legacy", "shared_empty", "both_nonempty", "shared_file"}:
            projects.mkdir()
            (projects / "session").write_text("legacy-session")
        if scenario in {"shared_empty", "both_nonempty"}:
            shared.mkdir()
            if scenario == "both_nonempty":
                (shared / "other").write_text("shared-session")
        if scenario == "shared_file":
            shared.write_text("not-a-directory")
        if scenario == "existing_symlink":
            projects.symlink_to("../missing-target")
        if scenario == "projects_file":
            projects.write_text("leave-this-file")

    def prepare_python():
        (homes[1] / ".cli-auth/_shared-projects").mkdir(parents=True, exist_ok=True)
        ensure_slot_dir(homes[1], "claude-work")

    before_bash = tree(homes[0])
    result = bash_helper(homes[0], "prepare", "claude-work")
    if result.returncode:
        with pytest.raises((ValueError, FileExistsError)):
            prepare_python()
        assert tree(homes[0]) == before_bash
    else:
        prepare_python()
    assert tree(homes[0]) == tree(homes[1])


@pytest.mark.parametrize(
    "scenario",
    ["new", "existing_entries", "stale_links", "default", "missing_default", "missing_alternate"],
)
def test_shared_layout_matches_login_sh(tmp_path, scenario):
    homes = [tmp_path / "bash", tmp_path / "python"]
    slot = "claude" if scenario == "default" else "claude-work"
    for home in homes:
        default = seed(home)
        alternate = home / ".cli-auth/claude-work"
        alternate.mkdir()
        (alternate / "projects").symlink_to("../_shared-projects")
        for name in (".credentials.json", "credentials.json", ".claude.json", "alternate-only"):
            (alternate / name).write_text("private:" + name)
        if scenario == "existing_entries":
            (alternate / "settings.json").write_text("old-settings")
            (alternate / "skills").mkdir()
            (alternate / "skills/local").write_text("old-skill")
        if scenario == "stale_links":
            (alternate / "settings.json").symlink_to("../wrong/settings")
            (alternate / "plugins").symlink_to("../claude/plugins")
        if scenario == "missing_default":
            default.rename(default.with_name("unused-default"))
        if scenario == "missing_alternate":
            alternate.rename(alternate.with_name("unused-alternate"))
    result = bash_helper(homes[0], "share", slot)
    assert result.returncode == 0, result.stderr
    share_slot_from_default(homes[1], slot)
    assert tree(homes[0]) == tree(homes[1])


@pytest.fixture
def paths(tmp_path):
    result = DuctorPaths(tmp_path / "ductor")
    result.config_dir.mkdir(parents=True)
    result.config_path.write_text(
        json.dumps(
            {
                "claude_accounts": ["claude", "claude-existing"],
                "model": "keep",
                "nested": {"keep": True},
            }
        )
    )
    seed(result.ductor_home)
    return result


def test_prepare_finish_registers_only_success_preserving_config_and_order(paths):
    slots = ClaudeLoginSlots(paths)
    prepared = slots.prepare("work", new_slot=True)
    assert prepared.created
    assert prepared.path.name == "claude-work"
    assert (prepared.path / "projects").readlink() == Path("../_shared-projects")
    assert not (prepared.path / "settings.json").exists()
    assert "claude-work" not in json.loads(paths.config_path.read_text())["claude_accounts"]
    (prepared.path / ".credentials.json").write_text("fake-auth")
    latest = json.loads(paths.config_path.read_text())
    latest["model"] = "newer-setting"
    paths.config_path.write_text(json.dumps(latest))
    slots.finish(prepared, succeeded=True)
    expected = dict(latest, claude_accounts=["claude", "claude-existing", "claude-work"])
    assert json.loads(paths.config_path.read_text()) == expected
    assert (prepared.path / "settings.json").readlink() == Path("../claude/settings.json")
    assert (prepared.path / ".credentials.json").read_text() == "fake-auth"
    stat = paths.config_path.stat()
    slots.finish(prepared, succeeded=True)
    assert paths.config_path.stat().st_ino == stat.st_ino
    assert json.loads(paths.config_path.read_text()) == expected


@pytest.mark.parametrize("credential", [None, ".credentials.json", "credentials.json"])
def test_failure_cleanup_removes_only_new_uncredentialed_slot_and_never_link_targets(
    paths, credential
):
    slots = ClaudeLoginSlots(paths)
    prepared = slots.prepare("work", new_slot=True)
    default = paths.ductor_home / ".cli-auth/claude"
    shared = paths.ductor_home / ".cli-auth/_shared-projects"
    (shared / "session").write_text("keep-shared")
    (prepared.path / "plugins").symlink_to("../claude/plugins")
    (prepared.path / "logs").mkdir()
    (prepared.path / "logs/attempt.log").write_text("fake-attempt")
    if credential:
        (prepared.path / credential).write_text("keep-auth")
    before_config = paths.config_path.read_bytes()
    slots.finish(prepared, succeeded=False)
    assert prepared.path.exists() == bool(credential)
    assert (shared / "session").read_text() == "keep-shared"
    assert (default / "plugins/data").read_text() == "shared:plugins"
    assert paths.config_path.read_bytes() == before_config


def test_existing_relogin_skips_preparation_and_failure_never_removes_it(paths):
    slots = ClaudeLoginSlots(paths)
    path = paths.ductor_home / ".cli-auth/claude-existing"
    path.mkdir()
    (path / "projects").mkdir()
    (path / "projects/legacy").write_text("keep-legacy")
    before = tree(path)
    prepared = slots.prepare("existing", new_slot=False)
    assert not prepared.created
    slots.finish(prepared, succeeded=False)
    assert tree(path) == before
    assert not (paths.ductor_home / ".cli-auth/_shared-projects").exists()


def test_new_slot_collision_and_missing_existing_do_not_modify_anything(paths):
    slots = ClaudeLoginSlots(paths)
    before = tree(paths.ductor_home)
    with pytest.raises(ValueError, match="slot_exists"):
        slots.prepare("claude", new_slot=True)
    with pytest.raises(ValueError, match="slot_not_found"):
        slots.prepare("absent", new_slot=False)
    assert tree(paths.ductor_home) == before


def test_partial_preparation_failure_removes_own_directory(paths):
    slots = ClaudeLoginSlots(paths)
    with (
        patch(
            "ductor_bot.webchat.cli_login_slots.ensure_slot_dir",
            side_effect=OSError("fake failure"),
        ),
        pytest.raises(OSError, match="fake failure"),
    ):
        slots.prepare("work", new_slot=True)
    assert not (paths.ductor_home / ".cli-auth/claude-work").exists()
    assert (paths.ductor_home / ".cli-auth/claude/settings.json").exists()


def test_config_failure_preserves_credentials_and_original_config(paths):
    slots = ClaudeLoginSlots(paths)
    prepared = slots.prepare("work", new_slot=True)
    (prepared.path / "credentials.json").write_text("keep-auth")
    before = paths.config_path.read_bytes()
    with (
        patch(
            "ductor_bot.infra.json_store.atomic_json_save",
            side_effect=OSError("fake atomic failure"),
        ),
        pytest.raises(OSError, match="fake atomic failure"),
    ):
        slots.finish(prepared, succeeded=True)
    assert paths.config_path.read_bytes() == before
    assert (prepared.path / "credentials.json").read_text() == "keep-auth"


def test_replaced_root_symlink_cleanup_unlinks_only_the_link(paths):
    path = paths.ductor_home / ".cli-auth/claude-work"
    target = paths.ductor_home / "keep-target"
    target.mkdir()
    (target / "data").write_text("keep")
    path.symlink_to(target)
    ClaudeLoginSlots.cleanup(PreparedClaudeSlot("claude-work", path, created=True))
    assert not path.is_symlink()
    assert (target / "data").read_text() == "keep"


async def until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("slot CLI condition timed out")


@pytest.fixture
async def manager_factory(paths, tmp_path, monkeypatch):
    binary = tmp_path / "bin"
    binary.mkdir()
    executable = binary / "claude"
    executable.write_text(
        "#!"
        + sys.executable
        + """
import os,sys,time,json
from pathlib import Path
slot=Path(os.environ["CLAUDE_CONFIG_DIR"])
mode=os.environ["FAKE_MODE"]
record=Path(os.environ["FAKE_RECORD"])
record.write_text(json.dumps({"projects":str((slot/"projects").readlink()) if (slot/"projects").is_symlink() else None,"settings_link":(slot/"settings.json").is_symlink()}))
if mode in {"success","credential_failure"}:
    (slot/".credentials.json").write_text("fake-new-auth")
if mode == "success":
    (slot/"settings.json").write_text("login-created-setting")
    print("Login successful",flush=True)
    sys.exit(0)
if mode == "credential_failure":
    sys.exit(1)
(slot/"attempt-cache").write_text("fake-cache")
while True:
    time.sleep(.05)
"""
    )
    executable.chmod(0o700)
    monkeypatch.setattr(
        "ductor_bot.webchat.cli_login.container_slot_config_dir",
        lambda slot: str(paths.ductor_home / ".cli-auth" / slot),
    )
    managers = []

    def make(mode="success", slots=None):
        record = tmp_path / f"record-{len(managers)}.json"
        manager = CliLoginManager(
            startup_providers=frozenset({"claude"}),
            env={
                "PATH": str(binary),
                "HOME": str(tmp_path),
                "FAKE_MODE": mode,
                "FAKE_RECORD": str(record),
            },
            auth_checker=lambda p: AuthResult(p, AuthStatus.AUTHENTICATED),
            slots=slots or ClaudeLoginSlots(paths),
        )
        managers.append(manager)
        return manager, record

    yield make
    for manager in managers:
        await manager.close()


async def settled(manager, job):
    await until(manager._jobs[job].task.done)
    return manager.get(job, OWNER)


async def test_manager_prepares_before_cli_then_shares_and_registers_after_success(
    paths, manager_factory
):
    manager, record = manager_factory()
    job = manager.start("claude", OWNER, slot="work", new_slot=True)
    result = await settled(manager, job)
    assert result["state"] == "succeeded"
    assert json.loads(record.read_text()) == {
        "projects": "../_shared-projects",
        "settings_link": False,
    }
    path = paths.ductor_home / ".cli-auth/claude-work"
    assert (path / "settings.json").readlink() == Path("../claude/settings.json")
    backups = list((path / ".pre-share-backup").glob("*/settings.json"))
    assert len(backups) == 1
    assert backups[0].read_text() == "login-created-setting"
    assert json.loads(paths.config_path.read_text())["claude_accounts"][-1] == "claude-work"
    assert manager.status(OWNER)["running"] is False


@pytest.mark.parametrize("action", ["cancel", "timeout", "close", "credential_failure"])
async def test_manager_failure_cleanup_and_config_unchanged(paths, manager_factory, action):
    manager, record = manager_factory(
        "credential_failure" if action == "credential_failure" else "wait"
    )
    now = [10.0]
    manager._clock = lambda: now[0]
    before = paths.config_path.read_bytes()
    job = manager.start("claude", OWNER, slot="work", new_slot=True)
    await until(record.exists)
    if action == "cancel":
        manager.cancel(job, OWNER)
    elif action == "timeout":
        now[0] += 601
    elif action == "close":
        await manager.close()
    if action != "close":
        result = await settled(manager, job)
        assert (
            result["state"]
            == {"cancel": "cancelled", "timeout": "timed_out", "credential_failure": "failed"}[
                action
            ]
        )
    path = paths.ductor_home / ".cli-auth/claude-work"
    assert path.exists() == (action == "credential_failure")
    assert paths.config_path.read_bytes() == before
    assert (
        paths.ductor_home / ".cli-auth/claude/settings.json"
    ).read_text() == "default:settings.json"


async def test_manager_relogin_leaves_preexisting_projects_as_they_were(paths, manager_factory):
    existing = paths.ductor_home / ".cli-auth/claude-existing"
    existing.mkdir()
    (existing / "projects").mkdir()
    manager, record = manager_factory()
    job = manager.start("claude", OWNER, slot="existing")
    assert (await settled(manager, job))["state"] == "succeeded"
    assert json.loads(record.read_text())["projects"] is None
    assert (existing / "projects").is_dir()
    assert not (existing / "projects").is_symlink()
    assert json.loads(paths.config_path.read_text())["claude_accounts"] == [
        "claude",
        "claude-existing",
    ]


async def test_finalization_keeps_singleton_and_pending_state_without_blocking_requests(
    paths, manager_factory
):
    slots = ClaudeLoginSlots(paths)
    original = slots.finish
    ready = threading.Event()
    release = threading.Event()

    def delayed_finish(*args, **kwargs):
        ready.set()
        assert release.wait(5)
        original(*args, **kwargs)

    with patch.object(slots, "finish", side_effect=delayed_finish):
        manager, _ = manager_factory(slots=slots)
        job = manager.start("claude", OWNER, slot="work", new_slot=True)
        try:
            await until(ready.is_set)
            began = time.monotonic()
            assert manager.get(job, OWNER)["state"] == "running"
            assert manager.status(OWNER)["running"]
            with pytest.raises(CliLoginError) as error:
                manager.start("claude", OWNER, slot="another", new_slot=True)
            assert error.value.code == "login_running"
            assert time.monotonic() - began < 0.1
            assert not (paths.ductor_home / ".cli-auth/claude-another").exists()
        finally:
            release.set()
        assert (await settled(manager, job))["state"] == "succeeded"
    assert manager.status(OWNER)["running"] is False


async def test_postprocess_failure_reports_failure_and_preserves_new_auth(paths, manager_factory):
    manager, _ = manager_factory()
    before = paths.config_path.read_bytes()
    with patch(
        "ductor_bot.webchat.cli_login_slots.transform_config_file",
        side_effect=OSError("fake failure"),
    ):
        job = manager.start("claude", OWNER, slot="work", new_slot=True)
        result = await settled(manager, job)
    assert result["state"] == "failed"
    assert result["reason"] == "slot_finalize_failed"
    assert paths.config_path.read_bytes() == before
    assert (
        paths.ductor_home / ".cli-auth/claude-work/.credentials.json"
    ).read_text() == "fake-new-auth"


@pytest.mark.parametrize("action", ["cancel", "timeout"])
async def test_interruption_during_preparation_cleans_new_slot_without_starting_cli(
    paths, manager_factory, action
):
    slots = ClaudeLoginSlots(paths)
    prepare = slots.prepare
    ready = threading.Event()
    release = threading.Event()

    def delayed_prepare(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        ready.set()
        assert release.wait(5)
        return prepared

    with patch.object(slots, "prepare", side_effect=delayed_prepare):
        manager, record = manager_factory("wait", slots=slots)
        now = [10.0]
        manager._clock = lambda: now[0]
        job = manager.start("claude", OWNER, slot="work", new_slot=True)
        try:
            await until(ready.is_set)
            if action == "cancel":
                manager.cancel(job, OWNER)
            else:
                now[0] += 601
        finally:
            release.set()
        assert (await settled(manager, job))["state"] == (
            "cancelled" if action == "cancel" else "timed_out"
        )
    assert not record.exists()
    assert not (paths.ductor_home / ".cli-auth/claude-work").exists()
    assert json.loads(paths.config_path.read_text())["claude_accounts"] == [
        "claude",
        "claude-existing",
    ]


@pytest.mark.parametrize("configured", [None, []])
def test_implicit_default_remains_first_after_adding_slot(paths, configured):
    config = {"model": "keep", "nested": {"keep": True}}
    if configured is not None:
        config["claude_accounts"] = configured
    paths.config_path.write_text(json.dumps(config))
    default = paths.ductor_home / ".cli-auth/claude"
    (default / ".credentials.json").write_text("fake-default-auth")
    slots = ClaudeLoginSlots(paths)
    prepared = slots.prepare("web", new_slot=True)
    (prepared.path / ".credentials.json").write_text("fake-web-auth")
    slots.finish(prepared, succeeded=True)
    assert json.loads(paths.config_path.read_text()) == dict(
        config, claude_accounts=["claude", "claude-web"]
    )
    assert (default / ".credentials.json").read_text() == "fake-default-auth"


@pytest.mark.parametrize("slot", ["claude", "claude-unlisted"])
@pytest.mark.parametrize("accounts", [None, [], ["claude-existing", "claude"]])
def test_successful_existing_login_does_not_register_or_reorder_slots(paths, slot, accounts):
    directory = paths.ductor_home / ".cli-auth" / slot
    directory.mkdir(exist_ok=True)
    config = {"model": "keep"}
    if accounts is not None:
        config["claude_accounts"] = accounts
    paths.config_path.write_text(json.dumps(config))
    before = paths.config_path.read_bytes()
    inode = paths.config_path.stat().st_ino
    slots = ClaudeLoginSlots(paths)
    slots.finish(slots.prepare(slot, new_slot=False), succeeded=True)
    assert paths.config_path.read_bytes() == before
    assert paths.config_path.stat().st_ino == inode
