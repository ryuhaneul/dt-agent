"""Framework rules must never write through links into user projects."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.workspace.cron_tasks import ensure_task_rule_files
from ductor_bot.workspace.paths import DuctorPaths
from ductor_bot.workspace.rules_selector import RulesSelector

RULE_NAMES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.fixture
def paths(tmp_path: Path) -> DuctorPaths:
    home = tmp_path / "home"
    defaults = tmp_path / "defaults"
    for rel in (".", "config", "workspace", "workspace/tools", "workspace/cron_tasks"):
        directory = defaults / rel
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "RULES.md").write_text("framework rules")
    home.mkdir()
    return DuctorPaths(ductor_home=home, home_defaults=defaults)


def selector(paths: DuctorPaths, *, gemini: bool) -> RulesSelector:
    auth = {
        "claude": AuthResult("claude", AuthStatus.AUTHENTICATED),
        "gemini": AuthResult(
            "gemini", AuthStatus.AUTHENTICATED if gemini else AuthStatus.INSTALLED
        ),
    }
    with patch("ductor_bot.cli.auth.check_all_auth", return_value=auth):
        return RulesSelector(paths)


@pytest.mark.parametrize(
    "relative",
    [
        "workspace/repos/x",
        "workspace/projects/x",
        "workspace/custom",
        "workspace/skills/custom",
        "agents/other/workspace",
        "workspace/cron_tasks/task",
    ],
)
def test_deploy_cleanup_preserves_user_rules(paths: DuctorPaths, relative: str) -> None:
    project = paths.ductor_home / relative
    project.mkdir(parents=True)
    for index, name in enumerate(RULE_NAMES):
        target = project / name
        target.write_text(name)
        os.utime(target, ns=(100 + index, 100 + index))
    before = snapshot(project)
    selector(paths, gemini=False).deploy_rules()
    assert snapshot(project) == before


@pytest.mark.parametrize("operation", ["deploy", "cleanup"])
@pytest.mark.parametrize("kind", ["file", "directory", "ancestor"])
def test_selector_preserves_symlink_targets(paths: DuctorPaths, operation: str, kind: str) -> None:
    project = paths.workspace / "repos" / "x"
    project.mkdir(parents=True)
    (project / "GEMINI.md").write_text("project rules")
    if kind == "file":
        destination = paths.workspace / "tools"
        destination.mkdir()
        (destination / "GEMINI.md").symlink_to(project / "GEMINI.md")
    elif kind == "directory":
        paths.workspace.joinpath("tools").symlink_to(project, target_is_directory=True)
    else:
        # Link an ancestor of a deployed directory, not only the final directory.
        paths.workspace.rename(paths.ductor_home / "projects")
        project = paths.ductor_home / "projects"
        paths.workspace.symlink_to(paths.ductor_home / "projects", target_is_directory=True)
    before = snapshot(project)
    selected = selector(paths, gemini=operation == "deploy")
    if operation == "deploy":
        selected.deploy_rules()
    else:
        selected._cleanup_stale_files()
    assert snapshot(project) == before
    if kind == "file":
        assert (destination / "GEMINI.md").is_symlink()


@pytest.mark.parametrize("kind", ["task", "ancestor", "dangling_file", "source_file"])
def test_backfill_preserves_symlink_targets(paths: DuctorPaths, kind: str) -> None:
    project = paths.workspace / "repos" / "x"
    project.mkdir(parents=True)
    (project / "CLAUDE.md").write_text("project rules")
    tasks = paths.cron_tasks_dir
    tasks.mkdir()
    for name in RULE_NAMES:
        (tasks / name).write_text("parent")
    task = tasks / "daily"
    if kind == "task":
        task.symlink_to(project, target_is_directory=True)
    else:
        task.mkdir()
        (task / "CLAUDE.md").write_text("task rules")
        if kind == "ancestor":
            tasks.rename(project / "linked-tasks")
            tasks.symlink_to(project / "linked-tasks", target_is_directory=True)
        elif kind == "dangling_file":
            (task / "GEMINI.md").symlink_to(project / "missing.md")
        else:
            (task / "CLAUDE.md").unlink()
            (task / "CLAUDE.md").symlink_to(project / "CLAUDE.md")
    before = snapshot(project)
    assert ensure_task_rule_files(tasks) == 0
    assert snapshot(project) == before


def test_deploy_cleanup_never_traverses_home(
    paths: DuctorPaths, monkeypatch, record_property
) -> None:
    project = paths.workspace / "repos" / "large"
    project.mkdir(parents=True)
    for index in range(50_000):
        (project / str(index)).touch()
    selected = selector(paths, gemini=False)
    original = Path.rglob

    def package_only(path: Path, pattern: str):
        assert path == paths.home_defaults, f"unexpected recursive scan: {path}"
        return original(path, pattern)

    monkeypatch.setattr(Path, "rglob", package_only)
    monkeypatch.setattr("os.walk", lambda *_args, **_kwargs: pytest.fail("unexpected os.walk"))
    import time

    start = time.perf_counter()
    selected.deploy_rules()
    duration = time.perf_counter() - start
    record_property("elapsed_seconds", duration)
    assert duration < 0.1


@pytest.mark.parametrize("failure", ["read", "write"])
def test_backfill_tolerates_disappearing_task(
    paths: DuctorPaths, monkeypatch, failure: str
) -> None:
    task = paths.cron_tasks_dir / "daily"
    task.mkdir(parents=True)
    (task / "CLAUDE.md").write_text("task")
    (paths.cron_tasks_dir / "GEMINI.md").write_text("parent")
    method = "read_text" if failure == "read" else "write_text"
    original = getattr(Path, method)

    def disappeared(path: Path, *args, **kwargs):
        if path.parent == task:
            raise FileNotFoundError(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, disappeared)
    assert ensure_task_rule_files(paths.cron_tasks_dir) == 0
