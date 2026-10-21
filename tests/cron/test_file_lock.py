"""Writers of cron_jobs.json lock the file, read it again and change only their own fields."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot._home_defaults.workspace.tools.cron_tools import _shared as tool_shared
from ductor_bot.cron import jobs_file
from ductor_bot.cron.jobs_file import save_definitions
from ductor_bot.cron.manager import CronJob, CronManager
from ductor_bot.infra.file_lock import LockTimeoutError, exclusive_file_lock
from tests.cron.test_observer import _make_observer, _make_paths


def _job(job_id: str = "daily", **fields: object) -> CronJob:
    base: dict[str, object] = {
        "id": job_id,
        "title": "Daily Report",
        "description": "d",
        "schedule": "0 9 * * *",
        "task_folder": job_id,
        "agent_instruction": "work",
    }
    base.update(fields)
    return CronJob(**base)  # type: ignore[arg-type]


def _file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _raw(path: Path, job_id: str) -> dict:
    return next(j for j in _file(path)["jobs"] if j["id"] == job_id)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "cron_jobs.json"


@pytest.fixture
def quick_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A short lock wait, so the tests of a taken lock do not take five seconds."""
    monkeypatch.setattr(jobs_file, "exclusive_file_lock", partial(exclusive_file_lock, timeout=0.2))


def _hold_lock(path: Path) -> int:
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


# -- the two kinds of writer do not undo each other -----------------------------------------------------


def test_status_written_after_a_tool_edit_keeps_the_edit(path: Path) -> None:
    manager = CronManager(jobs_path=path)
    manager.add_job(_job())
    # a tool edits the definition; the manager's cache still has the old title
    edited = _file(path)
    edited["jobs"][0]["title"] = "Edited by the tool"
    save_definitions(path, edited, upsert=[("daily", "daily")])

    manager.update_run_status("daily", status="success", delivery_status="ok")

    raw = _raw(path, "daily")
    assert raw["title"] == "Edited by the tool"  # not overwritten by the manager's old copy
    assert raw["last_run_status"] == "success"
    assert manager.get_job("daily").title == "Edited by the tool"  # type: ignore[union-attr]
    assert manager.take_external_change() is True
    assert manager.take_external_change() is False


def test_tool_save_after_a_status_write_keeps_the_status(path: Path) -> None:
    manager = CronManager(jobs_path=path)
    manager.add_job(_job())
    stale = _file(path)  # the tool read the file before the run finished
    stale["jobs"][0]["title"] = "Edited by the tool"
    manager.update_run_status(
        "daily",
        status="error:timeout",
        delivery_status="failed",
        delivery_error="offline",
        failed_transports=["tg"],
        result_text="the preserved result",
    )

    save_definitions(path, stale, upsert=[("daily", "daily")])

    raw = _raw(path, "daily")
    assert raw["title"] == "Edited by the tool"
    assert raw["last_run_status"] == "error:timeout"
    assert raw["last_delivery_status"] == "failed"
    assert raw["failed_transports"] == ["tg"]
    assert raw["last_result_text"] == "the preserved result"


def test_tool_rename_carries_the_status_to_the_new_id(path: Path) -> None:
    manager = CronManager(jobs_path=path)
    manager.add_job(_job("old-name"))
    stale = _file(path)
    stale["jobs"][0].update(id="new-name", task_folder="new-name")
    manager.update_run_status("old-name", status="success")

    save_definitions(path, stale, upsert=[("new-name", "old-name")])

    ids = [j["id"] for j in _file(path)["jobs"]]
    assert ids == ["new-name"]
    assert _raw(path, "new-name")["last_run_status"] == "success"


def test_tool_removal_keeps_the_other_jobs_as_the_file_has_them(path: Path) -> None:
    manager = CronManager(jobs_path=path)
    manager.add_job(_job("a"))
    manager.add_job(_job("b"))
    stale = _file(path)
    stale["jobs"] = [j for j in stale["jobs"] if j["id"] != "a"]
    manager.update_run_status("b", status="success")

    save_definitions(path, stale, remove=["a"])

    assert [j["id"] for j in _file(path)["jobs"]] == ["b"]
    assert _raw(path, "b")["last_run_status"] == "success"


def test_a_job_added_by_a_tool_while_the_manager_writes_is_kept(path: Path) -> None:
    manager = CronManager(jobs_path=path)
    manager.add_job(_job("a"))
    tool_view = _file(path)
    tool_view["jobs"].append(_job("b").to_dict())
    save_definitions(path, tool_view, upsert=[("b", "b")])

    manager.update_run_status("a", status="success")

    assert [j["id"] for j in _file(path)["jobs"]] == ["a", "b"]
    assert [j.id for j in manager.list_jobs()] == ["a", "b"]  # the cache holds the merged file
    assert manager.take_external_change() is True


def test_two_writers_at_the_same_time_lose_nothing(path: Path) -> None:
    manager = CronManager(jobs_path=path)
    manager.add_job(_job())
    errors: list[BaseException] = []

    def run_status() -> None:
        try:
            for i in range(25):
                manager.update_run_status("daily", status=f"s{i}")
        except BaseException as exc:
            errors.append(exc)

    def edit_definition() -> None:
        try:
            for i in range(25):
                data = _file(path)
                data["jobs"][0]["title"] = f"t{i}"
                save_definitions(path, data, upsert=[("daily", "daily")])
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run_status), threading.Thread(target=edit_definition)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert errors == []
    raw = _raw(path, "daily")
    assert (raw["last_run_status"], raw["title"]) == ("s24", "t24")  # the last of each survives


# -- what is kept from the file ----------------------------------------------------------------------------


def test_bare_list_file_and_unknown_fields_are_kept(path: Path) -> None:
    job = _job().to_dict()
    job["note_from_a_newer_version"] = {"keep": True}
    path.write_text(json.dumps([job]), encoding="utf-8")
    manager = CronManager(jobs_path=path)

    manager.update_run_status("daily", status="success")

    data = _file(path)
    assert isinstance(data, dict)
    assert data["jobs"][0]["note_from_a_newer_version"] == {"keep": True}
    assert data["jobs"][0]["last_run_status"] == "success"


def test_delivery_retry_compares_with_the_result_the_file_holds(path: Path) -> None:
    manager = CronManager(jobs_path=path)
    manager.add_job(_job())
    manager.update_run_status(
        "daily", status="success", delivery_status="failed", result_text="Old result"
    )
    other = CronManager(jobs_path=path)  # another process saves a newer failed result
    other.update_run_status(
        "daily", status="success", delivery_status="failed", result_text="New result"
    )

    manager.update_delivery_retry("daily", delivered=True, expected_text="Old result")

    raw = _raw(path, "daily")
    assert raw["last_result_text"] == "New result"
    assert raw["last_delivery_status"] == "failed"


def test_status_write_logs_one_info_line(path: Path, caplog: pytest.LogCaptureFixture) -> None:
    manager = CronManager(jobs_path=path)
    manager.add_job(_job())

    with caplog.at_level(logging.INFO, logger="ductor_bot.cron.manager"):
        manager.update_run_status("daily", status="success")

    assert [r.message for r in caplog.records if "recorded" in r.message] == [
        "Cron job daily status=success recorded"
    ]


# -- atomic save and the fixed lock file ---------------------------------------------------------------------


def test_save_is_atomic_and_the_lock_file_keeps_its_inode(path: Path) -> None:
    manager = CronManager(jobs_path=path)
    manager.add_job(_job())
    lock = path.with_name(path.name + ".lock")
    lock_inode = lock.stat().st_ino
    data_inode = path.stat().st_ino

    # Keep the original data file open so its inode stays allocated. Otherwise ext4 can hand the
    # freed inode number to the second replacement and the comparison below would match by reuse.
    with path.open("rb"):
        manager.update_run_status("daily", status="success")
        manager.update_run_status("daily", status="error")

    assert lock.stat().st_ino == lock_inode  # the lock is on a file that is never replaced
    assert path.stat().st_ino != data_inode  # the data was replaced by a rename
    assert sorted(p.name for p in path.parent.iterdir()) == [
        "cron_jobs.json",
        "cron_jobs.json.lock",
    ]  # no temp file left


def test_failed_save_leaves_the_old_file_intact(path: Path) -> None:
    manager = CronManager(jobs_path=path)
    manager.add_job(_job())
    before = path.read_text(encoding="utf-8")

    with (
        patch.object(jobs_file, "atomic_text_save", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        manager.set_enabled("daily", enabled=False)

    assert path.read_text(encoding="utf-8") == before


def test_unreadable_file_is_not_overwritten(path: Path) -> None:
    path.write_text("{ not json", encoding="utf-8")
    manager = CronManager(jobs_path=path)

    with pytest.raises(ValueError, match="Corrupt"):
        manager.add_job(_job())

    assert path.read_text(encoding="utf-8") == "{ not json"


# -- a taken lock ----------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("quick_lock")
def test_status_write_waits_for_the_next_save_when_the_lock_is_taken(
    path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager = CronManager(jobs_path=path)
    manager.add_job(_job("a"))
    manager.add_job(_job("b"))
    fd = _hold_lock(path)
    try:
        with caplog.at_level(logging.WARNING, logger="ductor_bot.cron.manager"):
            manager.update_run_status("a", status="success")
        assert any("is locked" in r.message for r in caplog.records)
        assert manager.get_job("a").last_run_status == "success"  # type: ignore[union-attr]
        assert _raw(path, "a")["last_run_status"] is None  # not on disk yet
    finally:
        os.close(fd)

    manager.update_run_status("b", status="error")  # the next save carries the earlier one

    assert _raw(path, "a")["last_run_status"] == "success"
    assert _raw(path, "b")["last_run_status"] == "error"


@pytest.mark.usefixtures("quick_lock")
def test_tool_save_exits_with_an_error_when_the_lock_is_taken(
    path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    CronManager(jobs_path=path).add_job(_job())
    fd = _hold_lock(path)
    try:
        with pytest.raises(SystemExit) as exit_info:
            tool_shared.save_jobs(path, _file(path), upsert=[("daily", "daily")])
    finally:
        os.close(fd)

    assert exit_info.value.code == 1
    assert "locked by another writer" in capsys.readouterr().out


def test_lock_error_type_is_an_os_error() -> None:
    assert issubclass(LockTimeoutError, OSError)


# -- the watcher keeps seeing changes it has not loaded ----------------------------------------------------


async def test_observer_leaves_the_watcher_behind_when_a_write_merged_a_change(
    tmp_path: Path,
) -> None:
    paths = _make_paths(tmp_path)
    manager = CronManager(jobs_path=paths.cron_jobs_path)
    manager.add_job(_job("a"))
    observer = _make_observer(paths, manager)
    observer._watcher.update_mtime = AsyncMock()  # type: ignore[method-assign]
    tool_view = _file(paths.cron_jobs_path)
    tool_view["jobs"].append(_job("b").to_dict())
    save_definitions(paths.cron_jobs_path, tool_view, upsert=[("b", "b")])

    manager.update_run_status("a", status="success")
    await observer._refresh_mtime()

    observer._watcher.update_mtime.assert_not_awaited()  # it will see the file as changed


async def test_observer_takes_its_own_write_as_the_baseline(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    manager = CronManager(jobs_path=paths.cron_jobs_path)
    manager.add_job(_job("a"))
    observer = _make_observer(paths, manager)
    observer._watcher.update_mtime = AsyncMock()  # type: ignore[method-assign]

    manager.update_run_status("a", status="success")
    await observer._refresh_mtime()

    observer._watcher.update_mtime.assert_awaited_once()
