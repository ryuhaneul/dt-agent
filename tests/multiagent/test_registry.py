"""Tests for multiagent/registry.py: AgentRegistry load."""

from __future__ import annotations

import json
import runpy
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
import questionary

from ductor_bot.__main__ import main as cli_main
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
