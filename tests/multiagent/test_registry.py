"""Tests for multiagent/registry.py: AgentRegistry load."""

from __future__ import annotations

import json
import os
import runpy
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
import questionary

from ductor_bot.__main__ import main as cli_main
from ductor_bot.cli_commands import agents as agents_cli
from ductor_bot.infra import file_lock
from ductor_bot.multiagent import registry
from ductor_bot.multiagent.registry import AgentRegistry


@pytest.fixture
def agents_path(tmp_path: Path) -> Path:
    return tmp_path / "agents.json"


class TestRegistryLoad:
    """Test AgentRegistry.load() behavior."""

    def test_missing_file_returns_empty(self, agents_path: Path) -> None:
        reg = AgentRegistry(agents_path)
        assert reg.load() == []

    def test_valid_json_array(self, agents_path: Path) -> None:
        data = [
            {"name": "sub1", "telegram_token": "tok:1"},
            {"name": "sub2", "telegram_token": "tok:2", "provider": "codex"},
        ]
        agents_path.write_text(json.dumps(data))
        reg = AgentRegistry(agents_path)
        agents = reg.load()
        assert len(agents) == 2
        assert agents[0].name == "sub1"
        assert agents[1].provider == "codex"

    def test_corrupt_json_returns_empty(self, agents_path: Path) -> None:
        agents_path.write_text("{not valid json")
        reg = AgentRegistry(agents_path)
        assert reg.load() == []

    def test_non_array_json_returns_empty(self, agents_path: Path) -> None:
        agents_path.write_text('{"name": "sub1"}')
        reg = AgentRegistry(agents_path)
        assert reg.load() == []

    def test_empty_array_returns_empty(self, agents_path: Path) -> None:
        agents_path.write_text("[]")
        reg = AgentRegistry(agents_path)
        assert reg.load() == []

    def test_invalid_entry_is_skipped(self, agents_path: Path) -> None:
        data = [
            {"name": "sub1", "telegram_token": "tok:1"},
            {"invalid": "missing name and token"},
            {"name": "sub3", "telegram_token": "tok:3"},
        ]
        agents_path.write_text(json.dumps(data))
        reg = AgentRegistry(agents_path)
        agents = reg.load()
        assert len(agents) == 2
        assert agents[0].name == "sub1"
        assert agents[1].name == "sub3"


@pytest.mark.skipif(file_lock.fcntl is None, reason="flock unavailable")
def test_update_agent_fields_respects_registry_lock(agents_path, monkeypatch):
    original = '[{"name": "worker", "model": "opus"}]'
    agents_path.write_text(original)
    lock = file_lock.exclusive_file_lock
    monkeypatch.setattr(registry, "exclusive_file_lock", partial(lock, timeout=0))
    with lock(agents_path), pytest.raises(file_lock.LockTimeoutError):
        registry.update_agent_fields(agents_path, "worker", model="sonnet")

    assert agents_path.read_text() == original


@pytest.mark.skipif(file_lock.fcntl is None, reason="flock unavailable")
@pytest.mark.parametrize("command", ["remove_script", "remove_cli", "create_script"])
def test_registry_writes_preserve_concurrent_changes(agents_path, monkeypatch, command):
    agents_path.write_text(json.dumps([{"name": "sub1", "model": "opus"}, {"name": "sub2"}]))
    monkeypatch.setenv("DUCTOR_HOME", str(agents_path.parent))
    tools = (
        Path(__file__).resolve().parents[2]
        / "ductor_bot/_home_defaults/workspace/tools/agent_tools"
    )

    ready_to_save = Event()
    continue_save = Event()
    atomic_json_save = registry.atomic_json_save
    flock = file_lock.fcntl.flock

    def pause_update(path, data):
        ready_to_save.set()
        assert continue_save.wait(5), "Timed out waiting for the competing writer"
        atomic_json_save(path, data)

    def signal_contention(fd, operation):
        try:
            flock(fd, operation)
        except BlockingIOError:
            continue_save.set()
            raise

    monkeypatch.setattr(registry, "atomic_json_save", pause_update)
    monkeypatch.setattr(file_lock.fcntl, "flock", signal_contention)
    with ThreadPoolExecutor(max_workers=1) as executor:
        update = executor.submit(registry.update_agent_fields, agents_path, "sub1", model="sonnet")
        try:
            assert ready_to_save.wait(5), "Timed out waiting for the model update to read"
            if command == "remove_cli":
                monkeypatch.setattr(
                    questionary, "confirm", Mock(return_value=Mock(ask=lambda: True))
                )
                monkeypatch.setattr(sys, "argv", ["ductor", "agents", "remove", "sub2"])
                cli_main()
            else:
                if command == "remove_script":
                    script, args = "remove_agent.py", ["sub2"]
                else:
                    script, args = "create_agent.py", ["--name", "sub3", "--transport", "headless"]
                monkeypatch.setattr(sys, "argv", [str(tools / script), *args])
                runpy.run_path(str(tools / script), run_name="__main__")
        finally:
            # An unlocked command completes before the paused update can overwrite it.
            continue_save.set()
        update.result(timeout=5)

    agents = {agent["name"]: agent for agent in json.loads(agents_path.read_text())}
    assert agents["sub1"]["model"] == "sonnet"
    if command == "create_script":
        assert set(agents) == {"sub1", "sub2", "sub3"}
    else:
        assert set(agents) == {"sub1"}


def test_create_script_keeps_registry_it_cannot_read(agents_path, monkeypatch):
    original = json.dumps([{"name": "sub1"}, {"name": "sub2"}])
    agents_path.write_text(original)
    monkeypatch.setenv("DUCTOR_HOME", str(agents_path.parent))
    script = (
        Path(__file__).resolve().parents[2]
        / "ductor_bot/_home_defaults/workspace/tools/agent_tools/create_agent.py"
    )
    read_text = Path.read_text

    def fail_registry_read(self, *args, **kwargs):
        if self.name == "agents.json":
            raise PermissionError("unreadable")
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_registry_read)
    monkeypatch.setattr(sys, "argv", [str(script), "--name", "sub3", "--transport", "headless"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(script), run_name="__main__")

    assert exit_info.value.code == 1
    assert read_text(agents_path) == original
    assert not (agents_path.parent / "agents" / "sub3").exists()


@pytest.mark.parametrize(
    "original",
    [
        '[{"name":"sub1"},{"name":"sub2"',
        '{"name":"sub1"}',
        pytest.param("[" * 100_000 + "]" * 100_000, id="deeply-nested"),
    ],
)
def test_create_script_keeps_invalid_registry(agents_path, monkeypatch, capsys, original):
    agents_path.write_text(original)
    monkeypatch.setenv("DUCTOR_HOME", str(agents_path.parent))
    script = (
        Path(__file__).resolve().parents[2]
        / "ductor_bot/_home_defaults/workspace/tools/agent_tools/create_agent.py"
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [str(script), "--name", "sub3", "--transport", "headless", "--persona", "New persona"],
    )
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(script), run_name="__main__")

    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "Error: Failed to read agents.json\n"
    assert agents_path.read_text() == original
    assert not (agents_path.parent / "agents" / "sub3").exists()


@pytest.mark.parametrize(
    "original",
    [
        '[{"name":"sub1"},{"name":"sub2"',
        pytest.param("[" * 100_000 + "]" * 100_000, id="deeply-nested"),
    ],
)
def test_cli_add_keeps_invalid_registry(agents_path, monkeypatch, capsys, original):
    agents_path.write_text(original)
    monkeypatch.setenv("DUCTOR_HOME", str(agents_path.parent))
    monkeypatch.setattr(sys, "argv", ["ductor", "agents", "add", "sub3"])
    answers = iter(["test-token", "1", "", "sonnet"])
    monkeypatch.setattr(questionary, "text", Mock(return_value=Mock(ask=lambda: next(answers))))
    monkeypatch.setattr(questionary, "select", Mock(return_value=Mock(ask=lambda: "claude")))

    with pytest.raises(SystemExit) as exit_info:
        cli_main()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out.strip().startswith("Could not update agents.json:")
    assert captured.err == ""
    assert agents_path.read_text() == original


@pytest.mark.parametrize(
    ("command", "original"),
    [
        pytest.param("add", b'[{"name":"sub1"', id="v1-add-malformed"),
        pytest.param("add", b'{"name":"sub1"}', id="v1-add-object"),
        pytest.param("remove", b'[{"name":"sub1"', id="v2-remove-malformed"),
        pytest.param("remove", b'{"name":"sub1"}', id="v2-remove-object"),
        pytest.param("remove", b"null", id="v2-remove-null"),
        pytest.param("add", b"\xff", id="v3-add-utf8"),
        pytest.param("remove", b"\xff", id="v3-remove-utf8"),
        pytest.param("add", b'[{"name":"sub1"}, "oops"]', id="v4-add-mixed"),
        pytest.param("remove", b'[{"name":"sub1"}, "oops"]', id="v4-remove-mixed"),
    ],
)
def test_cli_rejects_invalid_registry_before_prompts(
    agents_path, monkeypatch, capsys, command, original
):
    agents_path.write_bytes(original)
    monkeypatch.setenv("DUCTOR_HOME", str(agents_path.parent))
    name = "sub3" if command == "add" else "sub1"
    monkeypatch.setattr(sys, "argv", ["ductor", "agents", command, name])
    answers = iter(["test-token", "1", "", "sonnet"])
    text = Mock(return_value=Mock(ask=lambda: next(answers)))
    select = Mock(return_value=Mock(ask=lambda: "claude"))
    confirm = Mock(return_value=Mock(ask=lambda: True))
    monkeypatch.setattr(questionary, "text", text)
    monkeypatch.setattr(questionary, "select", select)
    monkeypatch.setattr(questionary, "confirm", confirm)

    with pytest.raises(SystemExit) as exit_info:
        cli_main()

    assert exit_info.value.code == 1
    text.assert_not_called()
    select.assert_not_called()
    confirm.assert_not_called()
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("Could not update agents.json:")
    assert captured.err == ""
    assert agents_path.read_bytes() == original


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param([], "Usage: ductor agents add <name>", id="missing-name"),
        pytest.param(["main"], "Name 'main' is reserved.", id="reserved-name"),
    ],
)
def test_cli_add_name_guidance_ignores_invalid_registry(
    agents_path, monkeypatch, capsys, args, expected
):
    original = b'[{"name":"sub1"}, "oops"]'
    agents_path.write_bytes(original)
    monkeypatch.setenv("DUCTOR_HOME", str(agents_path.parent))
    monkeypatch.setattr(sys, "argv", ["ductor", "agents", "add", *args])

    cli_main()

    assert capsys.readouterr().out.strip() == expected
    assert agents_path.read_bytes() == original


@pytest.mark.parametrize(
    ("command", "original"),
    [
        pytest.param("create", b'[{"name":"sub1"}, "oops"]', id="v4-create-mixed"),
        pytest.param("remove", b'{"name":"sub1"}', id="v5-remove-object"),
        pytest.param("remove", b"null", id="v5-remove-null"),
        pytest.param("remove", b'[{"name":"sub1"}, "oops"]', id="v5-remove-mixed"),
        pytest.param("remove", b"\xff", id="v5-remove-utf8"),
        pytest.param("remove", b"[" * 100_000 + b"]" * 100_000, id="v5-remove-deeply-nested"),
    ],
)
def test_tool_rejects_invalid_registry(agents_path, monkeypatch, capsys, command, original):
    agents_path.write_bytes(original)
    monkeypatch.setenv("DUCTOR_HOME", str(agents_path.parent))
    script = (
        Path(__file__).resolve().parents[2]
        / f"ductor_bot/_home_defaults/workspace/tools/agent_tools/{command}_agent.py"
    )
    if command == "create":
        args = ["--name", "sub3", "--transport", "headless", "--persona", "New persona"]
    else:
        args = ["sub1"]
    monkeypatch.setattr(sys, "argv", [str(script), *args])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(script), run_name="__main__")

    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "Error: Failed to read agents.json\n"
    assert agents_path.read_bytes() == original
    assert not (agents_path.parent / "agents" / "sub3").exists()


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        pytest.param(b'[{"name":"sub1"}, "oops"]', "Sub-Agents (1)", id="mixed"),
        pytest.param(b"\xff", "No sub-agents configured.", id="utf8"),
        pytest.param(
            b"[" * 100_000 + b"]" * 100_000, "No sub-agents configured.", id="deeply-nested"
        ),
    ],
)
def test_cli_list_shows_usable_entries_of_invalid_registry(
    agents_path, monkeypatch, capsys, original, expected
):
    agents_path.write_bytes(original)
    monkeypatch.setenv("DUCTOR_HOME", str(agents_path.parent))
    monkeypatch.setattr(sys, "argv", ["ductor", "agents", "list"])

    cli_main()

    assert expected in capsys.readouterr().out
    assert agents_path.read_bytes() == original


@pytest.mark.parametrize(
    "original",
    [
        pytest.param(b'{"name":"sub1"}', id="object"),
        pytest.param(b'[{"name":"sub1"}, "oops"]', id="mixed"),
        pytest.param(b"\xff", id="utf8"),
        pytest.param(b"[" * 100_000 + b"]" * 100_000, id="deeply-nested"),
    ],
)
def test_list_tool_rejects_invalid_registry(agents_path, monkeypatch, capsys, original):
    agents_path.write_bytes(original)
    monkeypatch.setenv("DUCTOR_HOME", str(agents_path.parent))
    script = (
        Path(__file__).resolve().parents[2]
        / "ductor_bot/_home_defaults/workspace/tools/agent_tools/list_agents.py"
    )
    monkeypatch.setattr(sys, "argv", [str(script)])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(script), run_name="__main__")

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Error: Failed to read agents.json\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
@pytest.mark.parametrize("kind", ["directory", "fifo-link"])
def test_remove_tool_reports_unreadable_registry_path(
    agents_path, tmp_path, monkeypatch, capsys, kind
):
    if kind == "directory":
        agents_path.mkdir()
    else:
        fifo = tmp_path / "registry.fifo"
        os.mkfifo(fifo)
        agents_path.symlink_to(fifo)
    monkeypatch.setenv("DUCTOR_HOME", str(agents_path.parent))
    script = (
        Path(__file__).resolve().parents[2]
        / "ductor_bot/_home_defaults/workspace/tools/agent_tools/remove_agent.py"
    )
    monkeypatch.setattr(sys, "argv", [str(script), "sub1"])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(script), run_name="__main__")

    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "Error: Failed to read agents.json\n"


@pytest.mark.parametrize("command", ["add", "remove"])
def test_cli_rejects_registry_invalidated_during_prompts(agents_path, monkeypatch, capsys, command):
    agents_path.write_bytes(b'[{"name":"sub1"}]')
    invalid = b'[{"name":"sub1"}, "oops"]'
    monkeypatch.setenv("DUCTOR_HOME", str(agents_path.parent))
    name = "sub3" if command == "add" else "sub1"
    monkeypatch.setattr(sys, "argv", ["ductor", "agents", command, name])

    def select_provider():
        agents_path.write_bytes(invalid)
        return "claude"

    def confirm_removal():
        agents_path.write_bytes(invalid)
        return True

    answers = iter(["test-token", "1", "", "sonnet"])
    monkeypatch.setattr(questionary, "text", Mock(return_value=Mock(ask=lambda: next(answers))))
    monkeypatch.setattr(questionary, "select", Mock(return_value=Mock(ask=select_provider)))
    monkeypatch.setattr(questionary, "confirm", Mock(return_value=Mock(ask=confirm_removal)))

    with pytest.raises(SystemExit) as exit_info:
        cli_main()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("Could not update agents.json:")
    assert captured.err == ""
    assert agents_path.read_bytes() == invalid


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
@pytest.mark.parametrize("command", ["create_script", "cli_add"])
def test_registry_update_rejects_fifo(agents_path, command):
    os.mkfifo(agents_path)
    if command == "create_script":
        script = (
            Path(__file__).resolve().parents[2]
            / "ductor_bot/_home_defaults/workspace/tools/agent_tools/create_agent.py"
        )
        args = [str(script), "--name", "sub3", "--transport", "headless"]
    else:
        args = [
            "-c",
            """
import runpy
import sys
from unittest.mock import Mock

import questionary

answers = iter(["test-token", "1", "", "sonnet"])
questionary.text = Mock(return_value=Mock(ask=lambda: next(answers)))
questionary.select = Mock(return_value=Mock(ask=lambda: "claude"))
sys.argv = ["ductor", "agents", "add", "sub3"]
runpy.run_module("ductor_bot", run_name="__main__")
""",
        ]

    result = subprocess.run(
        [sys.executable, *args],
        env={**os.environ, "DUCTOR_HOME": str(agents_path.parent)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1
    if command == "create_script":
        assert result.stderr == "Error: Failed to read agents.json\n"
    else:
        assert result.stdout.strip().startswith("Could not update agents.json:")
        assert result.stderr == ""
    assert stat.S_ISFIFO(agents_path.stat().st_mode)


@pytest.mark.skipif(file_lock.fcntl is None, reason="flock unavailable")
def test_cli_remove_reports_lock_timeout(agents_path, monkeypatch, capsys):
    original = '[{"name":"sub1"}]'
    agents_path.write_text(original)
    monkeypatch.setenv("DUCTOR_HOME", str(agents_path.parent))
    monkeypatch.setattr(sys, "argv", ["ductor", "agents", "remove", "sub1"])
    monkeypatch.setattr(questionary, "confirm", Mock(return_value=Mock(ask=lambda: True)))
    lock = file_lock.exclusive_file_lock
    monkeypatch.setattr(agents_cli, "exclusive_file_lock", partial(lock, timeout=0))

    with lock(agents_path), pytest.raises(SystemExit) as exit_info:
        cli_main()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.strip() == (
        "Could not update agents.json: agents.json.lock is locked by another writer"
    )
    assert agents_path.read_text() == original
