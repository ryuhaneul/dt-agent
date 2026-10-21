"""Bundled agent creation supports delegation-only sub-agents."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

AGENT_TOOL = (
    Path(__file__).resolve().parents[2]
    / "ductor_bot/_home_defaults/workspace/tools/agent_tools/create_agent.py"
)
LIST_TOOL = AGENT_TOOL.with_name("list_agents.py")
TASK_TOOL = (
    Path(__file__).resolve().parents[2]
    / "ductor_bot/_home_defaults/workspace/tools/task_tools/create_task.py"
)


def _create(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DUCTOR_HOME": str(home)}
    return subprocess.run(
        [sys.executable, str(AGENT_TOOL), "--name", "helper", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_create_headless_agent_without_credentials(tmp_path: Path) -> None:
    result = _create(
        tmp_path, "--transport", "headless", "--provider", "claude", "--model", "sonnet"
    )
    assert result.returncode == 0
    entry = json.loads((tmp_path / "agents.json").read_text())[0]
    assert entry == {
        "name": "helper",
        "transport": "headless",
        "provider": "claude",
        "model": "sonnet",
    }
    assert "Transport: headless" in result.stdout
    assert "synchronous delegation" in result.stdout
    assert "Token:" not in result.stdout


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--token", "dummy-token"),
        ("--users", "123"),
        ("--homeserver", "https://matrix.invalid"),
        ("--user-id", "@bot:invalid"),
        ("--password", "dummy-password"),
        ("--allowed-users", "@user:invalid"),
        ("--allowed-rooms", "!room:invalid"),
    ],
)
def test_headless_rejects_credential_flags(tmp_path: Path, flag: str, value: str) -> None:
    result = _create(tmp_path, "--transport", "headless", flag, value)
    assert result.returncode != 0
    assert flag in result.stderr
    assert not (tmp_path / "agents.json").exists()


def test_credential_free_auto_detection_still_errors(tmp_path: Path) -> None:
    result = _create(tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / "agents.json").exists()


def test_telegram_creation_never_prints_token_prefix(tmp_path: Path) -> None:
    token = "SECRET123456789"
    result = _create(tmp_path, "--transport", "telegram", "--token", token, "--users", "123")
    assert result.returncode == 0
    assert token[:8] not in result.stdout
    entry = json.loads((tmp_path / "agents.json").read_text())[0]
    assert entry["telegram_token"] == token


def test_list_agents_never_prints_token_prefix(tmp_path: Path) -> None:
    token = "SECRET123456789"
    (tmp_path / "agents.json").write_text(
        json.dumps([{"name": "helper", "telegram_token": token, "allowed_user_ids": [123]}])
    )
    result = subprocess.run(
        [sys.executable, str(LIST_TOOL)],
        env={**os.environ, "DUCTOR_HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert token[:8] not in result.stdout


@pytest.mark.parametrize("name", ["../x", "a/b", ".."])
def test_create_agent_rejects_unsafe_names(tmp_path: Path, name: str) -> None:
    result = _create(tmp_path, "--transport", "headless", "--name", name)
    assert result.returncode != 0
    assert not (tmp_path / "agents.json").exists()


def test_create_agent_accepts_dotted_name(tmp_path: Path) -> None:
    result = _create(tmp_path, "--transport", "headless", "--name", "alpha.v2")
    assert result.returncode == 0
    entry = json.loads((tmp_path / "agents.json").read_text())[0]
    assert entry["name"] == "alpha.v2"


def test_create_task_displays_server_rejection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = importlib.util.spec_from_file_location("create_task_headless_test", TASK_TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    error = "A delegation-only agent cannot create background tasks"
    monkeypatch.setattr(
        module,
        "_load_shared",
        lambda: (
            lambda _path: "http://127.0.0.1/tasks/create",
            lambda _url, _body, **_kwargs: {"success": False, "error": error},
            lambda: "worker",
        ),
    )
    monkeypatch.setattr(sys, "argv", ["create_task.py", "dummy prompt"])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 1
    assert error in capsys.readouterr().err


def _workspace(home: Path) -> Path:
    return home / "agents" / "helper" / "workspace"


def test_persona_and_behavior_options_write_files_before_registration(tmp_path: Path) -> None:
    persona_file = tmp_path / "persona.txt"
    persona_file.write_text("# Persona\nName: Rex\n", encoding="utf-8")
    result = _create(
        tmp_path,
        "--transport",
        "headless",
        "--persona-file",
        str(persona_file),
        "--behavior",
        "- Ask before changing config.",
    )
    assert result.returncode == 0, result.stderr
    assert (_workspace(tmp_path) / "PERSONA.md").read_text() == "# Persona\nName: Rex\n"
    assert (_workspace(tmp_path) / "BEHAVIOR.md").read_text() == "- Ask before changing config.\n"
    assert "PERSONA.md written." in result.stdout
    assert "<!-- PERSONA_UNSET -->" not in (_workspace(tmp_path) / "PERSONA.md").read_text()


def test_existing_persona_file_is_kept(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    ws.mkdir(parents=True)
    (ws / "PERSONA.md").write_text("old persona\n", encoding="utf-8")
    result = _create(tmp_path, "--transport", "headless", "--persona", "new persona")
    assert result.returncode == 0
    assert (ws / "PERSONA.md").read_text() == "old persona\n"
    assert "already exists" in result.stdout


def test_without_persona_options_nothing_is_written(tmp_path: Path) -> None:
    result = _create(tmp_path, "--transport", "headless")
    assert result.returncode == 0
    assert not _workspace(tmp_path).exists()


@pytest.mark.parametrize(
    "args",
    [
        ["--persona", "x", "--persona-file", "y"],
        ["--behavior", "   "],
        ["--persona-file", "/nonexistent/persona.txt"],
    ],
)
def test_invalid_persona_options_fail_without_registering(tmp_path: Path, args: list[str]) -> None:
    result = _create(tmp_path, "--transport", "headless", *args)
    assert result.returncode != 0
    assert not (tmp_path / "agents.json").exists()


@pytest.mark.parametrize("option", ["--persona", "--behavior"])
@pytest.mark.parametrize("where", ["agents", "agent", "workspace", "target", "broken-target"])
def test_symlinked_output_paths_are_refused_before_any_write(
    tmp_path: Path, option: str, where: str
) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    outside.mkdir()
    filename = "PERSONA.md" if option == "--persona" else "BEHAVIOR.md"
    home.mkdir()
    if where == "agents":
        (home / "agents").symlink_to(outside)
    elif where == "agent":
        (home / "agents").mkdir()
        (home / "agents" / "helper").symlink_to(outside)
    elif where == "workspace":
        (home / "agents" / "helper").mkdir(parents=True)
        (home / "agents" / "helper" / "workspace").symlink_to(outside)
    else:
        workspace = home / "agents" / "helper" / "workspace"
        workspace.mkdir(parents=True)
        goal = outside / filename if where == "broken-target" else outside / "existing.md"
        if where == "target":
            goal.write_text("outside original\n", encoding="utf-8")
        (workspace / filename).symlink_to(goal)

    result = _create(home, "--transport", "headless", option, "PRIVATE TEXT")

    assert result.returncode == 1
    assert "refusing to write persona files" in result.stderr
    assert not (home / "agents.json").exists(), "agent must not be registered"
    assert sorted(p.name for p in outside.iterdir()) in ([], ["existing.md"])
    if where == "target":
        assert (outside / "existing.md").read_text() == "outside original\n"


def test_regular_workspace_and_main_home_symlink_still_work(tmp_path: Path) -> None:
    real = tmp_path / "real-home"
    real.mkdir()
    home = tmp_path / "home-link"
    home.symlink_to(real)  # a symlinked main home is fine: it is the base of the check
    result = _create(home, "--transport", "headless", "--persona", "P", "--behavior", "B")
    assert result.returncode == 0, result.stderr
    assert (real / "agents" / "helper" / "workspace" / "PERSONA.md").read_text() == "P\n"
    assert (real / "agents" / "helper" / "workspace" / "BEHAVIOR.md").read_text() == "B\n"
    assert (real / "agents.json").exists()


@pytest.mark.parametrize("where", ["workspace", "target", "broken-target"])
def test_symlinked_join_notification_is_refused_before_any_write(
    tmp_path: Path, where: str
) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "agents" / "helper").mkdir(parents=True)
    workspace = home / "agents" / "helper" / "workspace"
    if where == "workspace":
        workspace.symlink_to(outside)
    else:
        workspace.mkdir()
        goal = outside / "JOIN_NOTIFICATION.md"
        if where == "target":
            goal.write_text("outside original\n", encoding="utf-8")
        (workspace / "JOIN_NOTIFICATION.md").symlink_to(goal)

    result = _create(home, "--transport", "headless", "--description", "PRIVATE", "--persona", "P")

    assert result.returncode == 1
    assert "refusing to write JOIN_NOTIFICATION.md" in result.stderr
    assert not (home / "agents.json").exists(), "agent must not be registered"
    assert not (workspace / "PERSONA.md").exists()
    assert sorted(p.name for p in outside.iterdir()) in ([], ["JOIN_NOTIFICATION.md"])
    if where == "target":
        assert (outside / "JOIN_NOTIFICATION.md").read_text() == "outside original\n"


def test_description_writes_join_notification_in_a_regular_workspace(tmp_path: Path) -> None:
    result = _create(tmp_path, "--transport", "headless", "--description", "Does things")
    assert result.returncode == 0, result.stderr
    assert (_workspace(tmp_path) / "JOIN_NOTIFICATION.md").read_text() == "Does things\n"
