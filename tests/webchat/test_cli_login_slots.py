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
from tests.webchat.test_bridge_api import _PREFIX, bridge, cli_bridge, cli_headers  # noqa: F401
from tests.webchat.test_web_app import CLIENT, login, site  # noqa: F401

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
        "api",
        "claude-api",
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
        api_key = name in {"api", "claude-api"}
        assert result.returncode == (1 if api_key else 2)
        with pytest.raises(ValueError, match="api_key_slot" if api_key else "invalid_slot"):
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


@pytest.mark.parametrize("name", ["api", "claude-api", "claude-manual"])
@pytest.mark.parametrize("new_slot", [False, True])
def test_api_key_oauth_preparation_rejected_without_filesystem_changes(paths, name, new_slot):
    slot = paths.ductor_home / ".cli-auth/claude-manual"
    slot.mkdir()
    (slot / "settings.json").write_text('{"apiKeyHelper": "helper.sh"}')
    before = tree(paths.ductor_home)
    with pytest.raises(ValueError, match="api_key_slot"):
        ClaudeLoginSlots(paths).prepare(name, new_slot=new_slot)
    assert tree(paths.ductor_home) == before


@pytest.mark.parametrize("settings", ['{"permissions": {"allow": ["Read"]}}', "invalid", "[]", None])
@pytest.mark.parametrize("slot", ["claude-api", "claude-manual"])
def test_api_shared_settings_match_login_sh_and_preserve_default(tmp_path, settings, slot):
    homes = [tmp_path / "bash", tmp_path / "python"]
    for home in homes:
        default = seed(home)
        source = default / "settings.json"
        if settings is None:
            source.unlink()
        else:
            source.write_text(settings)
        alternate = home / ".cli-auth" / slot
        alternate.mkdir()
        (alternate / "settings.json").write_text(
            '{"apiKeyHelper": "/ductor/.cli-auth/claude-api/api-key-helper.sh", "old": true}'
        )
    before = (homes[1] / ".cli-auth/claude/settings.json").read_bytes() if settings else None
    result = bash_helper(homes[0], "share", slot)
    assert result.returncode == 0, result.stderr
    share_slot_from_default(homes[1], slot)
    assert tree(homes[0]) == tree(homes[1])
    path = homes[1] / ".cli-auth" / slot / "settings.json"
    assert not path.is_symlink()
    assert path.stat().st_mode & 0o777 == 0o644
    data = json.loads(path.read_text())
    assert data["apiKeyHelper"] == "/ductor/.cli-auth/claude-api/api-key-helper.sh"
    assert "old" not in data
    source = homes[1] / ".cli-auth/claude/settings.json"
    assert (source.read_bytes() if source.exists() else None) == before


def test_api_shared_settings_recreated_when_default_directory_is_missing(tmp_path):
    alternate = tmp_path / ".cli-auth/claude-api"
    alternate.mkdir(parents=True)
    (alternate / "settings.json").write_text('{"apiKeyHelper": "helper.sh", "old": true}')
    share_slot_from_default(tmp_path, "claude-api")
    assert json.loads((alternate / "settings.json").read_text()) == {"apiKeyHelper": "helper.sh"}


@pytest.mark.parametrize("operation", ["share", "resolve"])
@pytest.mark.parametrize(
    "name", ["..", "../escape", "../.cli-auth-sibling/claude", "/escape", "claude/../../escape"]
)
def test_web_slot_paths_reject_escape_before_api_probe(paths, operation, name):
    before = tree(paths.ductor_home)
    # Exercise the path boundary independently of the slot-name regex.
    classifier = "slot_is_api_key" if operation == "share" else "slot_kind"
    with (
        patch("ductor_bot.webchat.cli_login_slots.resolve_slot_name", return_value=name),
        patch(f"ductor_bot.webchat.cli_login_slots.{classifier}") as probe,
        pytest.raises(ValueError, match="invalid_slot"),
    ):
        if operation == "share":
            share_slot_from_default(paths.ductor_home, name)
        else:
            ClaudeLoginSlots(paths).resolve_oauth_slot(name)
    probe.assert_not_called()
    assert tree(paths.ductor_home) == before


async def test_active_login_and_turn_share_slot_but_refuse_deletion(paths, manager_factory):
    from ductor_bot.cli.claude_accounts import SlotRegistry, state_file_path
    from ductor_bot.cli.claude_slots import remove
    from ductor_bot.cli.slot_rotation import reserve_slot, slot_use_lock

    directory = paths.ductor_home / ".cli-auth" / "claude-existing"
    directory.mkdir()
    manager, record = manager_factory("wait")
    with slot_use_lock(directory.parent, directory.name, deleting=True) as deleting:
        assert deleting
        with pytest.raises(CliLoginError, match="slot_busy"):
            manager.start("claude", OWNER, slot=directory.name)
    registry = SlotRegistry(state_file_path(paths.ductor_home))
    with reserve_slot(registry, directory.name) as turning:
        assert turning
        job = manager.start("claude", OWNER, slot=directory.name)
        # Check immediately, before the new task can even start preparing its directory.
        with pytest.raises(ValueError, match="slot_busy"):
            remove(paths.ductor_home, directory.name)
    await until(record.exists)
    with reserve_slot(registry, directory.name) as turning, reserve_slot(
        registry, directory.name
    ) as second_turn:
        assert turning and second_turn
    with pytest.raises(ValueError, match="slot_busy"):
        remove(paths.ductor_home, directory.name)
    manager.cancel(job, OWNER)
    assert (await settled(manager, job))["state"] == "cancelled"
    remove(paths.ductor_home, directory.name)
    assert not directory.exists()


async def slot_mutation_payload(client, slot):
    listing = await client.get(f"{_PREFIX}/cli-login/status", headers=cli_headers())
    assert listing.status == 200
    return {"expected": (await listing.json())["claude_slot_identities"][slot]}


async def test_delete_route_enforces_bridge_auth_and_live_inventory(cli_bridge, monkeypatch):
    from ductor_bot.cli import claude_slots
    from ductor_bot.cli.claude_accounts import SlotRegistry, SlotSnapshot, state_file_path
    from ductor_bot.cli.slot_rotation import reserve_slot, slot_use_lock

    client, bot, _, paths, _ = cli_bridge
    target = f"{_PREFIX}/cli-login/slots"
    route = f"{target}/claude-work"
    folder = paths.ductor_home / ".cli-auth" / "claude-work"
    paths.config_path.write_text('{"claude_accounts":["claude","claude-work"]}')
    bot.config.claude_accounts = ["claude", "claude-work"]
    folder.mkdir()
    registry = SlotRegistry(state_file_path(paths.ductor_home))
    registry.save(SlotSnapshot(cooldowns={"claude-work": 100, "claude": 200}))

    for headers in ({"X-Dtw-Owner": OWNER}, {
        key: value for key, value in cli_headers().items() if key != "X-Dtw-Owner"
    }):
        response = await client.delete(route, headers=headers)
        assert response.status == 403
        assert "WWW-Authenticate" not in response.headers
    default_payload = await slot_mutation_payload(client, "claude")
    response = await client.delete(f"{target}/claude", headers=cli_headers(), json=default_payload)
    payload = await slot_mutation_payload(client, folder.name)
    assert (response.status, await response.json()) == (400, {"error": "slot_default"})
    with slot_use_lock(folder.parent, folder.name):
        response = await client.delete(route, headers=cli_headers(), json=payload)
        assert (response.status, await response.json()) == (409, {"error": "slot_busy"})

    def refuse(directory):
        assert directory == folder
        raise PermissionError(13, "Denied", folder)

    with monkeypatch.context() as patcher:
        patcher.setattr(claude_slots.shutil, "rmtree", refuse)
        response = await client.delete(route, headers=cli_headers(), json=payload)
    assert response.status == 500
    assert await response.json() == {
        "error": "slot_delete_failed", "path": ".cli-auth/claude-work", "reason": "Denied",
    }
    assert registry.load().cooldowns == {"claude": 200}
    assert folder.is_dir()
    with reserve_slot(registry, folder.name) as reserved:
        assert not reserved
    listing = await client.get(f"{_PREFIX}/cli-login/status", headers=cli_headers())
    assert (await listing.json())["claude_slots"][-1]["priority"] is None
    assert bot.config.claude_accounts == ["claude", "claude-work"]

    response = await client.delete(route, headers=cli_headers(), json=payload)
    assert (response.status, await response.json()) == (200, {"ok": True})
    assert not folder.exists()
    listing = await client.get(f"{_PREFIX}/cli-login/status", headers=cli_headers())
    assert [slot["name"] for slot in (await listing.json())["claude_slots"]] == ["claude"]
    response = await client.delete(route, headers=cli_headers(), json=payload)
    assert (response.status, await response.json()) == (404, {"error": "slot_not_found"})


@pytest.mark.parametrize("action", ["delete", "logout", "disconnect"])
async def test_slot_mutation_rejects_recreated_slot(cli_bridge, monkeypatch, action):
    from ductor_bot.cli import claude_slots

    client, _, _, paths, _ = cli_bridge
    home = paths.ductor_home
    name = "claude-work"
    folder = home / ".cli-auth" / name
    folder.mkdir()
    retained = folder.with_name("replacement")
    retained.mkdir()
    credential = retained / (".credentials.json" if action != "disconnect" else "settings.json")
    credential.write_text('{"apiKeyHelper":"helper.sh"}' if action == "disconnect" else "fixture")
    method = "DELETE" if action == "delete" else "POST"
    route = f"{_PREFIX}/cli-login/slots/{name}"
    if action != "delete":
        route += f"/{action}"

    listing = await client.get(f"{_PREFIX}/cli-login/status", headers=cli_headers())
    old_status = await listing.json()
    old_identity = ":".join(map(str, claude_slots.slot_identity(home, name)))
    claude_slots.remove(home, name)
    retained.rename(folder)
    credential = folder / credential.name
    original = tree(folder)
    configured = paths.config_path.read_bytes()
    logout_calls = []

    def logout_cli(*_args, **_kwargs):
        logout_calls.append(name)
        credential.unlink()
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(claude_slots.subprocess, "run", logout_cli)
    response = await client.request(
        method, route, headers=cli_headers(), json={"expected": old_identity}
    )
    assert (response.status, await response.json()) == (409, {"error": "slot_stale"})
    assert tree(folder) == original
    assert paths.config_path.read_bytes() == configured
    assert not logout_calls
    assert old_status["claude_slot_identities"][name] == old_identity

    listing = await client.get(f"{_PREFIX}/cli-login/status", headers=cli_headers())
    current = (await listing.json())["claude_slot_identities"][name]
    assert isinstance(current, str)
    assert current != old_identity
    response = await client.request(method, route, headers=cli_headers(), json={})
    assert (response.status, await response.json()) == (400, {"error": "bad_request"})
    response = await client.request(
        method, route, headers=cli_headers(), json={"expected": current}
    )
    assert (response.status, await response.json()) == (200, {"ok": True})
    if action == "delete":
        assert not folder.exists()
    elif action == "logout":
        assert logout_calls == [name]
        assert not credential.exists()
    else:
        assert "apiKeyHelper" not in json.loads(credential.read_bytes())


async def test_browser_delete_requires_session_and_csrf_before_relay(site):
    client, fake = site
    route = "/api/cli-login/slots/claude-work"
    response = await client.delete(route, headers=CLIENT)
    assert response.status == 403
    assert (await response.json())["error"] == "auth_required"
    authorized, _ = await login(client)
    for missing in ["X-Dtw-Client", "X-Dtw-Csrf"]:
        headers = {key: value for key, value in authorized.items() if key != missing}
        response = await client.delete(route, headers=headers)
        assert response.status == 403
        assert "WWW-Authenticate" not in response.headers
    assert not any(call[1].startswith("/cli-login/") for call in fake.calls)
    await client.delete(route, headers={**authorized, "X-Dtw-Owner": "forged-owner"})
    assert fake.calls[-1][:2] == ("DELETE", "/cli-login/slots/claude-work")
    path, owner = fake.owner_headers[-1]
    assert path == "/cli-login/slots/claude-work"
    assert owner and owner != "forged-owner"


@pytest.mark.parametrize("has_directory", [True, False])
async def test_delete_preserves_unreadable_cooldowns_and_allows_retry(
    cli_bridge, monkeypatch, has_directory
):
    from ductor_bot.cli.claude_accounts import SlotRegistry, SlotSnapshot, state_file_path

    client, _, _, paths, _ = cli_bridge
    route = f"{_PREFIX}/cli-login/slots/claude-work"
    paths.config_path.write_text('{"claude_accounts":["claude-work","claude-other"]}')
    folder = paths.ductor_home / ".cli-auth" / "claude-work"
    if has_directory:
        folder.mkdir()
    payload = await slot_mutation_payload(client, folder.name)
    config_before = paths.config_path.read_bytes()
    config_inode = paths.config_path.stat().st_ino
    state = state_file_path(paths.ductor_home)
    registry = SlotRegistry(state)
    registry.save(SlotSnapshot(cooldowns={"claude-work": 100, "claude-other": 200}))
    before = state.stat()
    contents = state.read_bytes()

    with monkeypatch.context() as patcher:
        if os.geteuid() == 0:
            read_text = Path.read_text

            def deny_read(path, *args, **kwargs):
                if path == state:
                    raise PermissionError(13, "Permission denied", path)
                return read_text(path, *args, **kwargs)

            patcher.setattr(Path, "read_text", deny_read)
        state.chmod(0)
        try:
            response = await client.delete(route, headers=cli_headers(), json=payload)
            assert (response.status, await response.json()) == (
                500, {"error": "slot_delete_error"}
            )
            assert state.stat().st_ino == before.st_ino
        finally:
            state.chmod(before.st_mode)

    assert state.read_bytes() == contents
    assert registry.load().cooldowns == {"claude-work": 100, "claude-other": 200}
    assert paths.config_path.read_bytes() == config_before
    assert paths.config_path.stat().st_ino == config_inode
    assert folder.is_dir() == has_directory

    response = await client.delete(route, headers=cli_headers(), json=payload)
    assert (response.status, await response.json()) == (200, {"ok": True})
    assert registry.load().cooldowns == {"claude-other": 200}
    assert json.loads(paths.config_path.read_text())["claude_accounts"] == ["claude-other"]
    assert not folder.exists()


@pytest.mark.parametrize("contents", ["{not json", "[]", ""])
async def test_delete_recovers_corrupt_cooldown_state(cli_bridge, contents):
    from ductor_bot.cli.claude_accounts import state_file_path

    client, _, _, paths, _ = cli_bridge
    slot = "claude-work"
    other = "claude-other"
    paths.config_path.write_text(json.dumps({"claude_accounts": [slot, other]}))
    folder = paths.ductor_home / ".cli-auth" / slot
    folder.mkdir()
    state = state_file_path(paths.ductor_home)
    state.write_text(contents)

    payload = await slot_mutation_payload(client, slot)
    response = await client.delete(
        f"{_PREFIX}/cli-login/slots/{slot}", headers=cli_headers(), json=payload
    )
    assert (response.status, await response.json()) == (200, {"ok": True})
    assert json.loads(state.read_text()) == {"cooldowns": {}}
    assert json.loads(paths.config_path.read_text())["claude_accounts"] == [other]
    assert not folder.exists()


@pytest.mark.parametrize("mode", ["cancel", "timeout", "managed-timeout"])
@pytest.mark.parametrize(
    ("leader_exits", "output", "sent_result"),
    [
        (False, "both", False),
        (True, "both", False),
        (True, "stdout", False),
        (True, "stderr", False),
        (True, "stderr", True),
        (False, "none", False),
    ],
)
async def test_interrupted_slot_can_be_deleted_with_detached_child(
    cli_bridge, monkeypatch, mode, leader_exits, output, sent_result
):
    import contextlib
    import signal

    from ductor_bot.cli import executor
    from ductor_bot.cli.base import CLIConfig
    from ductor_bot.cli.claude_provider import ClaudeCodeCLI
    from ductor_bot.cli.timeout_controller import TimeoutConfig, TimeoutController
    from ductor_bot.infra.process_tree import force_kill_process_tree

    client, _, _, paths, _ = cli_bridge
    slot = "claude-work"
    paths.config_path.write_text(json.dumps({"claude_accounts": [slot]}))
    folder = paths.ductor_home / ".cli-auth" / slot
    folder.mkdir()
    pids = paths.ductor_home / "pids.json"

    def kill_test_tree(pid):
        assert pid == json.loads(pids.read_text())[0]
        force_kill_process_tree(pid)

    monkeypatch.setattr(executor, "force_kill_process_tree", kill_test_tree)
    write_slot = paths.ductor_home / "write-slot"
    marker = folder / "child-wrote"
    child_script = """import os, sys, time
from pathlib import Path
trigger = Path(sys.argv[1])
deadline = time.monotonic() + 10
while not trigger.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
if trigger.exists():
    folder = Path(os.environ["CLAUDE_CONFIG_DIR"])
    folder.mkdir(exist_ok=True)
    (folder / "child-wrote").touch()
"""
    fake = paths.ductor_home / "fake-claude"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"stdout = None if {output in {'stdout', 'both'}!r} else subprocess.DEVNULL\n"
        f"stderr = None if {output in {'stderr', 'both'}!r} else subprocess.DEVNULL\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_script!r}, {str(write_slot)!r}], "
        "start_new_session=True, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)\n"
        f"Path({str(pids)!r}).write_text(json.dumps([os.getpid(), child.pid]))\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':'fixture'}), flush=True)\n"
        f"if {sent_result!r}: "
        "print(json.dumps({'type':'result','result':'OK'}), flush=True)\n"
        f"if not {leader_exits!r}: time.sleep(10)\n"
    )
    fake.chmod(0o700)
    monkeypatch.setattr(ClaudeCodeCLI, "_find_cli", staticmethod(lambda: str(fake)))
    cli = ClaudeCodeCLI(CLIConfig(
        working_dir=paths.ductor_home, ductor_home=paths.ductor_home, claude_accounts=[slot]
    ))

    def alive(pid):
        try:
            status = (Path("/proc") / str(pid) / "stat").read_text().rpartition(") ")[2]
        except (FileNotFoundError, ProcessLookupError):
            return False
        return not status.startswith("Z ")

    controller = (
        TimeoutController(TimeoutConfig(timeout_seconds=1, extend_on_activity=output == "stderr"))
        if mode == "managed-timeout"
        else None
    )
    stream = cli.send_streaming(
        "fixture", timeout_seconds=1 if mode == "timeout" else None, timeout_controller=controller
    )
    initialized = asyncio.Event()

    async def consume():
        events = []
        async for event in stream:
            events.append(event)
            if event.type == "system":
                initialized.set()
        return events

    loop = asyncio.get_running_loop()
    reader = asyncio.create_task(consume())
    route = f"{_PREFIX}/cli-login/slots/{slot}"
    try:
        await asyncio.wait_for(initialized.wait(), 5)
        deadline = loop.time() + 1.25
        leader, child = json.loads(pids.read_text())
        if leader_exits:
            await until(lambda: not alive(leader))
        else:
            assert alive(leader)
        assert os.getpgid(child) == child
        payload = await slot_mutation_payload(client, slot)
        response = await client.delete(route, headers=cli_headers(), json=payload)
        assert (response.status, await response.json()) == (409, {"error": "slot_busy"})

        if mode == "cancel":
            reader.cancel()
            done, _ = await asyncio.wait({reader}, timeout=0.25)
            assert reader in done, "cancellation waited for a detached writer"
            with pytest.raises(asyncio.CancelledError):
                await reader
        else:
            done, _ = await asyncio.wait({reader}, timeout=max(0, deadline - loop.time()))
            assert reader in done, "timeout waited for a detached writer"
            result = (await reader)[-1]
            if sent_result:
                assert not result.is_error and result.result == "OK"
            else:
                assert result.is_error and result.result == "__TIMEOUT__1"

        payload = await slot_mutation_payload(client, slot)
        response = await client.delete(route, headers=cli_headers(), json=payload)
        assert (response.status, await response.json()) == (200, {"ok": True})
        assert not folder.exists()

        if not leader_exits:
            assert not alive(child)
            return

        assert alive(child)
        write_slot.touch()
        await until(marker.exists)
        listing = await client.get(f"{_PREFIX}/cli-login/status", headers=cli_headers())
        slots = (await listing.json())["claude_slots"]
        assert next(item for item in slots if item["name"] == slot)["priority"] is None
        payload = await slot_mutation_payload(client, slot)
        response = await client.delete(route, headers=cli_headers(), json=payload)
        assert (response.status, await response.json()) == (200, {"ok": True})
        assert not folder.exists()
    finally:
        if pids.exists():
            for pid in reversed(json.loads(pids.read_text())):
                if alive(pid):
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
        if not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await stream.aclose()


@pytest.mark.parametrize("new_slot", [False, True])
async def test_manager_rejects_detached_api_key_oauth(paths, manager_factory, new_slot):
    from ductor_bot.cli.claude_slots import disconnect_api_key

    home = paths.ductor_home
    directory = home / ".cli-auth" / "claude-manual"
    directory.mkdir()
    (directory / "settings.json").write_text('{"apiKeyHelper": "helper.sh"}')
    disconnect_api_key(home, directory.name)
    manager, record = manager_factory()
    before = tree(home)
    with pytest.raises(CliLoginError, match=r"^api_key_slot$") as exc:
        manager.start("claude", OWNER, slot=directory.name, new_slot=new_slot)
    assert exc.value.status == 400
    assert not record.exists()
    assert tree(home) == before


@pytest.mark.parametrize("slot", ["claude", "claude-existing"])
async def test_logout_refuses_slots_reserved_by_web_login(paths, manager_factory, slot):
    from ductor_bot.cli import claude_slots

    directory = paths.ductor_home / ".cli-auth" / slot
    directory.mkdir(exist_ok=True)
    manager, record = manager_factory("wait")
    job = manager.start("claude", OWNER, slot=slot)
    with (
        patch.object(claude_slots.subprocess, "run") as logout_cli,
        pytest.raises(claude_slots.SlotDeleteError, match=r"^slot_busy$"),
    ):
        claude_slots.logout(paths.ductor_home, slot)
    logout_cli.assert_not_called()
    assert not record.exists()
    await until(record.exists)
    manager.cancel(job, OWNER)
    assert (await settled(manager, job))["state"] == "cancelled"


@pytest.mark.parametrize(
    ("action", "operation", "wrong_kind", "failure"),
    [
        ("logout", "logout", "api_key_slot", "logout_failed"),
        ("disconnect", "disconnect_api_key", "not_api_key_slot", "disconnect_failed"),
    ],
)
async def test_slot_auth_mutations_enforce_bridge_auth_and_map_errors(
    cli_bridge, monkeypatch, action, operation, wrong_kind, failure
):
    from ductor_bot.cli import claude_slots

    client, _, _, paths, _ = cli_bridge
    name = "claude" if action == "logout" else "claude-work"
    folder = paths.ductor_home / ".cli-auth" / name
    folder.mkdir(exist_ok=True)
    payload = await slot_mutation_payload(client, name)
    info = folder.lstat()
    route = f"{_PREFIX}/cli-login/slots/{name}/{action}"
    count = 0
    expected_error = None
    event_loop_thread = threading.get_ident()

    def mutate(home, slot, *, expected):
        nonlocal count
        assert threading.get_ident() != event_loop_thread
        assert home == paths.ductor_home
        assert slot == name
        assert expected == (info.st_dev, info.st_ino, info.st_ctime_ns)
        count += 1
        if expected_error is not None:
            raise claude_slots.SlotDeleteError(expected_error)

    monkeypatch.setattr(claude_slots, operation, mutate)
    for headers in ({"X-Dtw-Owner": OWNER}, {
        key: value for key, value in cli_headers().items() if key != "X-Dtw-Owner"
    }):
        response = await client.post(route, headers=headers)
        assert response.status == 403
        assert "WWW-Authenticate" not in response.headers
    assert count == 0
    for expected_error, status in [
        ("slot_not_found", 404), ("slot_busy", 409), ("invalid_slot", 400),
        (wrong_kind, 400), (failure, 500),
    ]:
        response = await client.post(route, headers=cli_headers(), json=payload)
        assert response.status == status
        assert await response.json() == {"error": expected_error}
    expected_error = None
    response = await client.post(route, headers=cli_headers(), json=payload)
    assert response.status == 200
    assert await response.json() == {"ok": True}
    assert count == 6


@pytest.mark.parametrize("action", ["logout", "disconnect"])
async def test_web_slot_auth_mutations_require_session_and_csrf(site, action):
    client, fake = site
    route = f"/cli-login/slots/claude-work/{action}"
    response = await client.post(f"/api{route}", headers=CLIENT)
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers
    authorized, _ = await login(client)
    headers = {key: value for key, value in authorized.items() if key != "X-Dtw-Csrf"}
    response = await client.post(f"/api{route}", headers=headers)
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers
    assert not any(call[1] == route for call in fake.calls)

    response = await client.post(f"/api{route}", headers={**authorized, "X-Dtw-Owner": "forged"})
    assert response.status == 200
    assert fake.calls[-1][:2] == ("POST", route)
    path, owner = fake.owner_headers[-1]
    assert path == route
    assert owner
    assert owner != "forged"


async def test_disconnect_updates_kind_in_slot_status(cli_bridge):
    client, _, _, paths, _ = cli_bridge
    target = paths.ductor_home / ".cli-auth" / "claude-manual"
    target.mkdir()
    (target / "settings.json").write_text('{"apiKeyHelper":"helper.sh"}')
    base = f"{_PREFIX}/cli-login"
    headers = cli_headers()

    async def inventory():
        response = await client.get(f"{base}/status", headers=headers)
        return {slot["name"]: slot for slot in (await response.json())["claude_slots"]}

    assert (await inventory())["claude-manual"]["kind"] == "api_key"
    payload = await slot_mutation_payload(client, target.name)
    response = await client.post(
        f"{base}/slots/claude-manual/disconnect", headers=headers, json=payload
    )
    assert response.status == 200
    slot = (await inventory())["claude-manual"]
    assert slot["kind"] == "api_key_detached"
    assert slot["authenticated"] is False
