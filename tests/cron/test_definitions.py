"""Shared cron lifecycle consistency, previews and CLI compatibility."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from ductor_bot.cron import definitions as defs
from ductor_bot.cron.jobs_file import read_jobs_data
from ductor_bot.cron.manager import CronManager

TOOLS = Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults/workspace/tools/cron_tools"


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "cron_jobs.json", tmp_path / "workspace/cron_tasks"


def create(paths, **overrides):
    jobs, tasks = paths
    job = {"id": "daily", "title": "Daily", "schedule": "0 9 * * *", **overrides}
    return defs.create_job(jobs, tasks, job, "Original assignment.\n", ductor_home=jobs.parent)


@pytest.mark.parametrize("expression", ["0 9 1 1 *", "0 9 * * 1-5", "*/15 * * * *"])
def test_schedule_preview_and_summary(expression):
    assert defs.schedule_problem(expression) == ""
    runs = defs.next_runs(expression, "Asia/Seoul", now=datetime(2026, 1, 2, tzinfo=UTC))
    assert len(runs) == 3
    assert runs == sorted(runs)
    assert defs.schedule_summary(expression)


def test_annual_and_leap_day_preview():
    start = datetime(2026, 1, 2, tzinfo=UTC)
    assert [run.year for run in defs.next_runs("0 9 1 1 *", "UTC", now=start)] == [2027, 2028, 2029]
    assert [run.year for run in defs.next_runs("0 9 29 2 *", "UTC", now=start)] == [2028]
    assert len(defs.next_runs("0 9 1 1 *", "UTC", horizon=timedelta(days=370), now=start)) == 1
    assert (
        len(defs.next_runs("0 9 1 1 *", "UTC", n=20, horizon=timedelta(days=9999), now=start)) == 5
    )


@pytest.mark.parametrize("timezone", ["", "Mars/Olympus", "/etc/passwd", "../UTC"])
def test_invalid_timezone(timezone):
    with pytest.raises(ValueError, match="IANA timezone"):
        defs.validate_timezone(timezone)


def test_creation_publishes_final_body_before_enabled_definition(paths, monkeypatch):
    jobs, tasks = paths
    save = defs.save_definitions

    def inspect_save(*args, **kwargs):
        assert (tasks / "daily/TASK_DESCRIPTION.md").read_text() == "Original assignment.\n"
        assert not read_jobs_data(jobs)["jobs"]
        assert list(tasks.glob(".staging-*")) == []
        save(*args, **kwargs)

    monkeypatch.setattr(defs, "save_definitions", inspect_save)
    result = create(paths, enabled=False)
    assert result["enabled"] is True
    assert result["agent_instruction"] == defs.DEFAULT_INSTRUCTION
    assert read_jobs_data(jobs)["jobs"] == [result]
    assert (tasks / "daily/scripts").is_dir()
    assert (tasks / "daily/daily_MEMORY.md").read_text() == "# daily Memory\n"
    assert "automated agent" in (tasks / "daily/CLAUDE.md").read_text()


@pytest.mark.parametrize("failure", ["write", "rename", "save"])
def test_create_failure_leaves_no_definition_or_workspace(paths, monkeypatch, failure):
    jobs, tasks = paths
    if failure == "write":
        original = defs.create_task_folder

        def fail_write(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("write failed")

        monkeypatch.setattr(defs, "create_task_folder", fail_write)
    elif failure == "rename":
        monkeypatch.setattr(Path, "rename", lambda *args: (_ for _ in ()).throw(OSError("rename")))
    else:
        monkeypatch.setattr(
            defs, "save_definitions", lambda *a, **k: (_ for _ in ()).throw(OSError("save"))
        )
    with pytest.raises(OSError):
        create(paths)
    assert not read_jobs_data(jobs)["jobs"]
    assert not (tasks / "daily").exists()
    assert not list(tasks.glob(".staging-*"))


@pytest.mark.parametrize(
    "fields",
    [
        {"id": "../escape"},
        {"title": ""},
        {"schedule": "61 * * * *"},
        {"timezone": "Invalid/Zone"},
        {"delivery_mode": "inject"},
        {"autocompact": 1},
    ],
)
def test_invalid_creation_does_not_touch_workspace(paths, fields):
    jobs, tasks = paths
    with pytest.raises((ValueError, TypeError)):
        create(paths, **fields)
    assert not jobs.exists()
    assert not tasks.exists()


def test_duplicate_and_concurrent_creation(paths):
    def attempt():
        try:
            create(paths)
            return "created"
        except defs.DefinitionConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == ["conflict", "created"]
    assert len(read_jobs_data(paths[0])["jobs"]) == 1
    with pytest.raises(defs.DefinitionConflictError):
        create(paths)


def test_cleanup_only_removes_staging(paths):
    _, tasks = paths
    for name in (".staging-daily-abcd", ".archive", ".history", "daily"):
        (tasks / name).mkdir(parents=True)
    defs.cleanup_staging(tasks)
    assert sorted(p.name for p in tasks.iterdir() if p.is_dir()) == [
        ".archive",
        ".history",
        "daily",
    ]


def test_update_body_conflict_history_retention_and_status(paths):
    jobs, tasks = paths
    create(paths, provider="codex", model="existing-model", chat_id=42)
    manager = CronManager(jobs_path=jobs)
    manager.update_run_status("daily", status="success")
    body = "Original assignment.\n"
    for index in range(12):
        new_body = f"Assignment {index}\n"
        result = defs.update_job(
            jobs,
            tasks,
            "daily",
            {"enabled": False, "schedule": "0 10 * * *", "autocompact": "off"},
            body=new_body,
            expected_sha256=defs.body_sha256(body),
        )
        body = new_body
    history = list((tasks / "daily/.history").glob("*.md"))
    assert len(history) == 10
    assert {file.read_text() for file in history} == {f"Assignment {i}\n" for i in range(1, 11)}
    assert result["enabled"] is False
    assert result["provider"] == "codex"
    assert result["model"] == "existing-model"
    assert read_jobs_data(jobs)["jobs"][0]["last_run_status"] == "success"
    before = jobs.read_bytes()
    with pytest.raises(defs.DefinitionConflictError):
        defs.update_job(
            jobs, tasks, "daily", {"title": "Changed"}, body="stale", expected_sha256="bad"
        )
    assert jobs.read_bytes() == before
    assert (tasks / "daily/TASK_DESCRIPTION.md").read_text() == body


def test_update_rolls_back_body_when_json_save_fails(paths, monkeypatch):
    jobs, tasks = paths
    create(paths)
    before = jobs.read_bytes()
    with patch.object(defs, "save_definitions", side_effect=OSError("save failed")):
        for index in range(12):
            monkeypatch.setattr(defs, "_kst_stamp", lambda: f"20261119-160000-{index:06d}")
            with pytest.raises(OSError, match="save failed"):
                defs.update_job(
                    jobs,
                    tasks,
                    "daily",
                    {"title": "Changed"},
                    body="New body",
                    expected_sha256=defs.body_sha256("Original assignment.\n"),
                )
            assert jobs.read_bytes() == before
            assert (tasks / "daily/TASK_DESCRIPTION.md").read_text() == "Original assignment.\n"
            assert len(list((tasks / "daily/.history").glob("*.md"))) <= 10
    history = list((tasks / "daily/.history").glob("*.md"))
    assert {file.name for file in history} == {
        f"20261119-160000-{index:06d}.md" for index in range(2, 12)
    }
    assert all(file.read_text() == "Original assignment.\n" for file in history)


def test_body_size_checked_before_changes(paths):
    jobs, tasks = paths
    with pytest.raises(ValueError, match="256 KiB"):
        defs.create_job(
            jobs,
            tasks,
            {"id": "large", "title": "Large", "schedule": "* * * * *"},
            "\u20AC" * (defs.BODY_LIMIT // 3 + 1),
            ductor_home=jobs.parent,
        )
    assert not tasks.exists()
    create(paths)
    with pytest.raises(ValueError, match="256 KiB"):
        defs.update_job(jobs, tasks, "daily", {}, body="a" * (defs.BODY_LIMIT + 1))
    assert not (tasks / "daily/.history").exists()


def test_archive_moves_before_removing_definition(paths, monkeypatch):
    jobs, tasks = paths
    create(paths)
    save = defs.save_definitions

    def inspect_save(*args, **kwargs):
        assert not (tasks / "daily").exists()
        assert len(list((tasks / ".archive").iterdir())) == 1
        assert read_jobs_data(jobs)["jobs"][0]["id"] == "daily"
        save(*args, **kwargs)

    monkeypatch.setattr(defs, "save_definitions", inspect_save)
    archived = defs.archive_job(jobs, tasks, "daily")
    assert archived.parent == tasks / ".archive"
    assert (archived / "TASK_DESCRIPTION.md").read_text() == "Original assignment.\n"
    assert not read_jobs_data(jobs)["jobs"]


@pytest.mark.parametrize("failure", ["rename", "save"])
def test_archive_failure_preserves_definition_and_restores_folder(paths, failure):
    jobs, tasks = paths
    create(paths)
    before = jobs.read_bytes()
    target = (
        "pathlib.Path.rename"
        if failure == "rename"
        else "ductor_bot.cron.definitions.save_definitions"
    )
    with patch(target, side_effect=OSError("failure")):
        with pytest.raises(OSError):
            defs.archive_job(jobs, tasks, "daily")
    assert jobs.read_bytes() == before
    assert (tasks / "daily/TASK_DESCRIPTION.md").read_text() == "Original assignment.\n"
    assert not list((tasks / ".archive").iterdir())


async def test_dot_directories_are_not_jobs_for_observer_or_tool_list(paths):
    from tests.cron.test_observer import _make_manager, _make_observer, _make_paths

    jobs, tasks = paths
    create(paths)
    for name in (".archive/old-job", ".history", ".staging-unfinished-1234"):
        (tasks / name).mkdir(parents=True)
    env = {**os.environ, "DUCTOR_HOME": str(jobs.parent)}
    listed = subprocess.run(
        [sys.executable, str(TOOLS / "cron_list.py")],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert [job["id"] for job in json.loads(listed.stdout)["jobs"]] == ["daily"]
    observer_paths = _make_paths(jobs.parent / "observer")
    observer_paths.cron_jobs_path.write_text(jobs.read_text())
    for name in ("daily", ".archive", ".history", ".staging-test-1234"):
        (observer_paths.cron_tasks_dir / name).mkdir()
    observer = _make_observer(observer_paths, _make_manager(observer_paths))
    await observer.start()
    try:
        assert set(observer._scheduled) == {"daily"}
    finally:
        await observer.stop()


def test_shared_static_checker_names_only(paths):
    _, tasks = paths
    create(paths)
    (tasks / "daily/TASK_DESCRIPTION.md").write_text('Use load_secrets("SERVICE_TOKEN").')
    (tasks / "daily/scripts/run.py").write_text('load_secrets("CUSTOM_NAME")\n')
    result = defs.scan_secrets(tasks / "daily", set(), None)
    assert result["keys"] == ["CUSTOM_NAME", "SERVICE_TOKEN"]
    assert result["requires_secrets"] is True
    assert "SECRETS_LOCKED" in result["warning"]


def test_body_backup_and_rollback_preserve_original_bytes(paths):
    jobs, tasks = paths
    create(paths)
    body_path = tasks / "daily/TASK_DESCRIPTION.md"
    original = b"First line\r\nSecond line\r\n"
    body_path.write_bytes(original)
    with patch.object(defs, "save_definitions", side_effect=OSError("save failed")):
        with pytest.raises(OSError):
            defs.update_job(
                jobs,
                tasks,
                "daily",
                {},
                body="Changed",
                expected_sha256=defs.body_sha256(original.decode()),
            )
    assert body_path.read_bytes() == original
    assert next((tasks / "daily/.history").glob("*.md")).read_bytes() == original


def test_external_edit_during_backup_is_not_overwritten(paths, monkeypatch):
    jobs, tasks = paths
    create(paths)
    body_path = tasks / "daily/TASK_DESCRIPTION.md"
    save = defs.atomic_bytes_save
    previous_json = jobs.read_bytes()

    def edit_during_backup(path, body):
        save(path, body)
        body_path.write_text("External edit")

    monkeypatch.setattr(defs, "atomic_bytes_save", edit_during_backup)
    with pytest.raises(defs.DefinitionConflictError):
        defs.update_job(
            jobs,
            tasks,
            "daily",
            {"title": "Changed"},
            body="New assignment",
            expected_sha256=defs.body_sha256("Original assignment.\n"),
        )
    assert body_path.read_text() == "External edit"
    assert jobs.read_bytes() == previous_json
    assert not list((tasks / "daily/.history").glob("*.md"))


def test_archive_id_cannot_escape_archive_directory(paths):
    jobs, tasks = paths
    create(paths)
    data = read_jobs_data(jobs)
    data["jobs"][0]["id"] = "../escape"
    jobs.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Invalid job id"):
        defs.archive_job(jobs, tasks, "../escape")
    assert (tasks / "daily").is_dir()
    assert len(read_jobs_data(jobs)["jobs"]) == 1


def test_static_scan_of_unsaved_body_includes_existing_scripts(paths):
    jobs, tasks = paths
    body = 'load_secrets("PROPOSED_TOKEN")'
    result = defs.scan_secrets(tasks / "daily", set(), None, body=body)
    assert result["keys"] == ["PROPOSED_TOKEN"]
    assert result["skipped_files"] == 0
    assert not tasks.exists()
    create(paths)
    (tasks / "daily/scripts/job.py").write_text('load_secrets("SCRIPT_TOKEN")')
    result = defs.scan_secrets(tasks / "daily", set(), None, body=body)
    assert result["keys"] == ["PROPOSED_TOKEN", "SCRIPT_TOKEN"]
    assert (tasks / "daily/TASK_DESCRIPTION.md").read_text() == "Original assignment.\n"
