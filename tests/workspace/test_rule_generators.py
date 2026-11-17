"""Cron rename and webhook rule generation use provider names and safe paths."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

NAMES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")
TOOLS = Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults/workspace/tools"


@pytest.fixture
def load_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DUCTOR_HOME", str(tmp_path))

    def load(group: str, name: str):
        monkeypatch.syspath_prepend(str(TOOLS / group))
        monkeypatch.delitem(sys.modules, "_shared", raising=False)
        spec = importlib.util.spec_from_file_location(name, TOOLS / group / f"{name}.py")
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return load


def seed_parent(home: Path, names=NAMES) -> Path:
    cron = home / "workspace/cron_tasks"
    cron.mkdir(parents=True, exist_ok=True)
    for name in names:
        (cron / name).write_text("parent")
    return cron


@pytest.mark.parametrize("name", NAMES)
def test_rename_never_writes_through_rule_file_link(tmp_path: Path, load_tool, name: str) -> None:
    cron = seed_parent(tmp_path)
    project = tmp_path / "workspace/repos/x"
    project.mkdir(parents=True)
    target = project / name
    target.write_text("project instructions")
    before = (target.read_bytes(), target.stat().st_mtime_ns)
    old = cron / "old"
    old.mkdir()
    (old / name).symlink_to(target)
    tool = load_tool("cron_tools", "cron_edit")
    tool._rename_task_folder(old_task_folder="old", old_id="old", new_name="new")
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before
    assert (cron / "new" / name).is_symlink()


@pytest.mark.parametrize("generator", ["rename", "webhook"])
@pytest.mark.parametrize("kind", ["task", "ancestor"])
def test_generators_skip_linked_directories(
    tmp_path: Path, load_tool, generator: str, kind: str
) -> None:
    cron = seed_parent(tmp_path)
    project = tmp_path / "workspace/repos/x"
    project.mkdir(parents=True)
    (project / "GEMINI.md").write_text("project instructions")
    if kind == "ancestor":
        # Point cron_tasks into a user project, with no task folder there yet.
        for p in cron.iterdir():
            p.unlink()
        cron.rmdir()
        cron.symlink_to(project, target_is_directory=True)
    else:
        (cron / "old").symlink_to(project, target_is_directory=True)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in project.iterdir()}
    if generator == "rename":
        tool = load_tool("cron_tools", "cron_edit")
        tool._rename_task_folder(old_task_folder="old", old_id="old", new_name="new")
    else:
        tool = load_tool("webhook_tools", "webhook_add")
        tool._create_task_folder("old", "Hook", "Description")
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in project.iterdir()} == before


@pytest.mark.parametrize("name", NAMES)
def test_webhook_checks_rule_file_after_mkdir(
    tmp_path: Path, load_tool, monkeypatch, name: str
) -> None:
    cron = seed_parent(tmp_path)
    project = tmp_path / "workspace/repos/x"
    project.mkdir(parents=True)
    target = project / name
    target.write_text("project instructions")
    before = (target.read_bytes(), target.stat().st_mtime_ns)
    original = Path.mkdir

    def mkdir(path: Path, *args, **kwargs):
        original(path, *args, **kwargs)
        if path == cron / "hook":
            (path / name).symlink_to(target)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    tool = load_tool("webhook_tools", "webhook_add")
    tool._create_task_folder("hook", "Hook", "Description")
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before
    assert (cron / "hook" / name).is_symlink()
