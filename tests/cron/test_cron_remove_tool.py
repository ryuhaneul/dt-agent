"""Tests for the cron_remove.py CLI tool (subprocess-based)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

TOOL_ADD = (
    Path(__file__).resolve().parents[2]
    / "ductor_bot"
    / "_home_defaults"
    / "workspace"
    / "tools"
    / "cron_tools"
    / "cron_add.py"
)
TOOL_REMOVE = (
    Path(__file__).resolve().parents[2]
    / "ductor_bot"
    / "_home_defaults"
    / "workspace"
    / "tools"
    / "cron_tools"
    / "cron_remove.py"
)


def _run(tmp_path: Path, tool: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DUCTOR_HOME": str(tmp_path)}
    return subprocess.run(
        [sys.executable, str(tool), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _add_job(tmp_path: Path, name: str = "rm-test") -> None:
    result = _run(
        tmp_path,
        TOOL_ADD,
        [
            "--name",
            name,
            "--title",
            "Remove Test",
            "--description",
            "For removal testing",
            "--schedule",
            "0 9 * * *",
        ],
    )
    assert result.returncode == 0


def test_cron_remove_deletes_json_and_folder(tmp_path: Path) -> None:
    _add_job(tmp_path, "to-delete")
    task_dir = tmp_path / "workspace" / "cron_tasks" / "to-delete"
    assert task_dir.is_dir()

    result = _run(tmp_path, TOOL_REMOVE, ["to-delete"])
    assert result.returncode == 0

    output = json.loads(result.stdout)
    assert output["json_entry_removed"] is True
    assert output["folder_deleted"] is True

    # JSON entry gone
    data = json.loads((tmp_path / "cron_jobs.json").read_text())
    assert not any(j["id"] == "to-delete" for j in data["jobs"])

    # Folder gone
    assert not task_dir.exists()


def test_cron_remove_nonexistent_exits_1(tmp_path: Path) -> None:
    # Create an empty jobs file
    (tmp_path / "cron_jobs.json").write_text('{"jobs": []}')
    result = _run(tmp_path, TOOL_REMOVE, ["ghost"])
    assert result.returncode == 1
    output = json.loads(result.stdout)
    assert "not found" in output["error"]


def test_cron_remove_no_args_shows_tutorial(tmp_path: Path) -> None:
    result = _run(tmp_path, TOOL_REMOVE, [])
    assert result.returncode == 1
    assert "CRON REMOVE" in result.stdout


def test_cron_remove_handles_missing_folder(tmp_path: Path) -> None:
    """If the folder was already deleted, remove still removes the JSON entry."""
    _add_job(tmp_path, "orphan-json")
    # Manually delete the folder
    task_dir = tmp_path / "workspace" / "cron_tasks" / "orphan-json"
    shutil.rmtree(task_dir)

    result = _run(tmp_path, TOOL_REMOVE, ["orphan-json"])
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["json_entry_removed"] is True
    assert output["folder_deleted"] is False


def test_cron_remove_saves_under_the_lock_and_leaves_no_temp_file(tmp_path: Path) -> None:
    _add_job(tmp_path, "locked-remove")

    result = _run(tmp_path, TOOL_REMOVE, ["locked-remove"])

    assert result.returncode == 0
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("cron_jobs")) == [
        "cron_jobs.json",
        "cron_jobs.json.lock",
    ]
    assert json.loads((tmp_path / "cron_jobs.json").read_text())["jobs"] == []


def test_cron_remove_exits_with_an_error_while_another_writer_holds_the_lock(
    tmp_path: Path,
) -> None:
    import fcntl

    _add_job(tmp_path, "held-remove")
    fd = os.open(tmp_path / "cron_jobs.json.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        result = _run(tmp_path, TOOL_REMOVE, ["held-remove"])
    finally:
        os.close(fd)

    assert result.returncode == 1
    assert "locked by another writer" in json.loads(result.stdout)["error"]
    # nothing was deleted: the entry and the folder are still there
    data = json.loads((tmp_path / "cron_jobs.json").read_text())
    assert any(j["id"] == "held-remove" for j in data["jobs"])
    assert (tmp_path / "workspace" / "cron_tasks" / "held-remove").is_dir()
