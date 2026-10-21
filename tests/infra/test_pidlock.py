"""Tests for PID lockfile management."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest


class TestIsProcessAlive:
    """Test process liveness detection."""

    def test_current_process_is_alive(self) -> None:
        from ductor_bot.infra.pidlock import _is_process_alive

        assert _is_process_alive(os.getpid()) is True

    def test_nonexistent_pid_is_dead(self) -> None:
        from ductor_bot.infra.pidlock import _is_process_alive

        # PID 2^30 is extremely unlikely to exist
        assert _is_process_alive(2**30) is False

    def test_permission_error_means_alive(self) -> None:
        from ductor_bot.infra.pidlock import _is_process_alive

        with (
            patch("ductor_bot.infra.pidlock.is_windows", return_value=False),
            patch("os.kill", side_effect=PermissionError),
        ):
            assert _is_process_alive(999) is True


class TestAcquireLock:
    """Test PID lock acquisition."""

    def test_creates_pid_file(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import acquire_lock, release_lock

        pid_file = tmp_path / "bot.pid"
        acquire_lock(pid_file=pid_file)
        try:
            assert pid_file.exists()
            assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())
        finally:
            release_lock(pid_file=pid_file)

    def test_stale_pid_file_overwritten(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import acquire_lock, release_lock

        pid_file = tmp_path / "bot.pid"
        # Write a PID that doesn't exist
        pid_file.write_text("999999999", encoding="utf-8")

        acquire_lock(pid_file=pid_file)
        try:
            assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())
        finally:
            release_lock(pid_file=pid_file)

    def test_corrupt_pid_file_overwritten(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import acquire_lock, release_lock

        pid_file = tmp_path / "bot.pid"
        pid_file.write_text("not-a-number", encoding="utf-8")

        acquire_lock(pid_file=pid_file)
        try:
            assert pid_file.exists()
        finally:
            release_lock(pid_file=pid_file)

    def test_own_pid_file_is_stale_and_overwritten(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import acquire_lock, release_lock

        pid_file = tmp_path / "bot.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        with patch("ductor_bot.infra.pidlock._kill_and_wait") as kill:
            acquire_lock(pid_file=pid_file, kill_existing=True)
        assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())
        kill.assert_not_called()
        release_lock(pid_file=pid_file)

    def test_other_active_pid_without_kill_raises_system_exit(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import acquire_lock

        pid_file = tmp_path / "bot.pid"
        pid_file.write_text("12345", encoding="utf-8")
        with (
            patch("ductor_bot.infra.pidlock._is_process_alive", return_value=True),
            pytest.raises(SystemExit) as exc,
        ):
            acquire_lock(pid_file=pid_file, kill_existing=False)
        assert exc.value.code == 1
        assert pid_file.read_text(encoding="utf-8").strip() == "12345"

    def test_active_pid_with_kill_kills_and_acquires(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import acquire_lock, release_lock

        pid_file = tmp_path / "bot.pid"
        fake_pid = 999999999
        pid_file.write_text(str(fake_pid), encoding="utf-8")

        with (
            patch("ductor_bot.infra.pidlock._is_process_alive", return_value=True),
            patch("ductor_bot.infra.pidlock._kill_and_wait"),
        ):
            acquire_lock(pid_file=pid_file, kill_existing=True)

        try:
            assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())
        finally:
            release_lock(pid_file=pid_file)

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import acquire_lock, release_lock

        pid_file = tmp_path / "deep" / "nested" / "bot.pid"
        acquire_lock(pid_file=pid_file)
        try:
            assert pid_file.exists()
        finally:
            release_lock(pid_file=pid_file)


class TestReleaseLock:
    """Test PID lock release."""

    def test_removes_own_pid_file(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import release_lock

        pid_file = tmp_path / "bot.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        release_lock(pid_file=pid_file)
        assert not pid_file.exists()

    def test_does_not_remove_other_pid(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import release_lock

        pid_file = tmp_path / "bot.pid"
        pid_file.write_text("999999999", encoding="utf-8")
        release_lock(pid_file=pid_file)
        assert pid_file.exists()  # Should NOT be removed

    def test_noop_when_no_file(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import release_lock

        pid_file = tmp_path / "bot.pid"
        release_lock(pid_file=pid_file)  # No error

    def test_removes_corrupt_pid_file(self, tmp_path: Path) -> None:
        from ductor_bot.infra.pidlock import release_lock

        pid_file = tmp_path / "bot.pid"
        pid_file.write_text("garbage", encoding="utf-8")
        release_lock(pid_file=pid_file)
        assert not pid_file.exists()


_INSTANCE_CODE = """from pathlib import Path
import sys
from ductor_bot.infra import pidlock
pidlock.acquire_instance_lock(Path(sys.argv[1]))
print('locked', flush=True)
sys.stdin.read()
"""


def test_instance_lock_rejects_second_process_and_releases_on_exit(tmp_path):
    import subprocess
    import sys

    lock = str(tmp_path / "bot.lock")
    first = subprocess.Popen(
        [sys.executable, "-c", _INSTANCE_CODE, lock],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert first.stdout.readline() == "locked\n"
        second = subprocess.run(
            [sys.executable, "-c", _INSTANCE_CODE, lock],
            input="",
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert second.returncode == 1
        assert "already" in second.stderr.lower()
    finally:
        first.communicate(input="", timeout=10)
    assert first.returncode == 0
    third = subprocess.run(
        [sys.executable, "-c", _INSTANCE_CODE, lock],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert third.returncode == 0, third.stderr
    assert third.stdout == "locked\n"


def test_instance_lock_is_not_inherited_across_exec(tmp_path):
    import subprocess
    import sys

    lock = str(tmp_path / "bot.lock")
    code = """import os, sys
from pathlib import Path
from ductor_bot.infra import pidlock
pidlock.acquire_instance_lock(Path(sys.argv[1]))
assert not os.get_inheritable(pidlock._instance_lock_handles[0].fileno())
os.execv(sys.executable, [sys.executable, '-c', "print('exec ready', flush=True); input()"])
"""
    first = subprocess.Popen(
        [sys.executable, "-c", code, lock],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert first.stdout.readline() == "exec ready\n"
        second = subprocess.run(
            [sys.executable, "-c", _INSTANCE_CODE, lock],
            input="",
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert second.returncode == 0, second.stderr
    finally:
        first.communicate(input="\n", timeout=10)
    assert first.returncode == 0


_TRY_FLOCK_CODE = """import fcntl, sys
with open(sys.argv[1], 'a') as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(1)
"""


def test_two_instance_locks_remain_held(tmp_path):
    import subprocess
    import sys

    locks = [tmp_path / "first" / "bot.lock", tmp_path / "second" / "bot.lock"]
    code = _INSTANCE_CODE.replace(
        "print('locked', flush=True)",
        "pidlock.acquire_instance_lock(Path(sys.argv[2]))\nprint('locked', flush=True)",
    )
    first = subprocess.Popen(
        [sys.executable, "-c", code, *map(str, locks)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert first.stdout.readline() == "locked\n"
        for lock in locks:
            result = subprocess.run(
                [sys.executable, "-c", _TRY_FLOCK_CODE, str(lock)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert result.returncode == 1, result.stderr
    finally:
        first.communicate(input="", timeout=10)
    assert first.returncode == 0


@pytest.mark.parametrize("symlink", [False, True])
def test_instance_lock_reuses_resolved_path(tmp_path, *, symlink):
    import subprocess
    import sys

    home = tmp_path / "home"
    home.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(home, target_is_directory=True)
    lock = home / "bot.lock"
    repeated = alias / "bot.lock" if symlink else lock
    code = _INSTANCE_CODE.replace(
        "print('locked', flush=True)",
        "pidlock.acquire_instance_lock(Path(sys.argv[2]))\nprint('locked', flush=True)",
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(lock), str(repeated)],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "locked\n"
