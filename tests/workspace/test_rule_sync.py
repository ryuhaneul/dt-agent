"""Rules are deployed at startup; the periodic task only backfills cron files."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.workspace import init as workspace_init


async def run_one_tick(workspace: Path) -> None:
    with (
        patch.object(
            workspace_init.asyncio,
            "sleep",
            new=AsyncMock(side_effect=[None, asyncio.CancelledError]),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await workspace_init.watch_rule_files(workspace)


def test_content_sync_api_removed() -> None:
    from ductor_bot import workspace

    name = "sync_" + "rule_files"
    assert not hasattr(workspace_init, name)
    assert not hasattr(workspace, name)


async def test_tick_only_backfills_cron_and_preserves_existing_rules(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    cron = workspace / "cron_tasks"
    task = cron / "daily"
    task.mkdir(parents=True)
    (cron / "CLAUDE.md").write_text("parent")
    (cron / "GEMINI.md").write_text("parent")
    (task / "CLAUDE.md").write_text("task")
    protected = []
    for relative in (".", "repos/x", "projects/x", "custom", "skills/custom", "cron_tasks/daily"):
        directory = workspace / relative
        directory.mkdir(parents=True, exist_ok=True)
        for index, name in enumerate(("CLAUDE.md", "AGENTS.md")):
            p = directory / name
            p.write_text(name)
            os.utime(p, ns=(100 + index, 100 + index))
            protected.append((p, p.read_bytes(), p.stat().st_mtime_ns))
    with (
        patch.object(Path, "rglob", side_effect=AssertionError("recursive traversal")) as recursive,
        patch("os.walk", side_effect=AssertionError("recursive traversal")) as walks,
    ):
        await run_one_tick(workspace)
    recursive.assert_not_called()
    walks.assert_not_called()
    assert (task / "GEMINI.md").read_text() == "CLAUDE.md"
    for p, content, mtime in protected:
        assert (p.read_bytes(), p.stat().st_mtime_ns) == (content, mtime)


async def test_tick_ignores_large_repos(tmp_path: Path, record_property) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "repos" / "large"
    repo.mkdir(parents=True)
    for i in range(50_000):
        (repo / str(i)).touch()
    (workspace / "cron_tasks").mkdir()
    original = Path.iterdir

    def guarded(path: Path):
        assert path == workspace / "cron_tasks", f"unexpected directory listing: {path}"
        return original(path)

    with (
        patch.object(Path, "iterdir", guarded),
        patch.object(Path, "rglob", side_effect=AssertionError("recursive traversal")) as recursive,
        patch("os.walk", side_effect=AssertionError("recursive traversal")) as walks,
    ):
        start = time.perf_counter()
        await run_one_tick(workspace)
        duration = time.perf_counter() - start
    recursive.assert_not_called()
    walks.assert_not_called()
    record_property("elapsed_seconds", duration)
    assert duration < 0.1
