"""Integration-style tests for ``scripts/login.sh`` shell helpers.

The script itself is plain bash; these tests ``source`` it from a
subprocess so we can exercise ``ensure_slot_dir`` in isolation without
invoking docker. The bottom-of-file dispatch is guarded by
``[ "${BASH_SOURCE[0]}" = "$0" ]`` so sourcing is side-effect free.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOGIN_SH = _REPO_ROOT / "scripts" / "login.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="bash not available on this platform",
)


def _run_ensure_slot_dir(home: Path, slot: str) -> subprocess.CompletedProcess[str]:
    """Source ``login.sh`` and invoke ``ensure_slot_dir`` under *home*."""
    # The messages follow the language setting; these tests read the English ones.
    env = {**os.environ, "DUCTOR_HOME": str(home), "LC_ALL": "C"}
    env.pop("ONBOARD_LANGUAGE", None)
    script = f"source '{_LOGIN_SH}' && ensure_shared_projects && ensure_slot_dir {slot}"
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class TestEnsureSlotDirMigration:
    def test_fresh_install_creates_symlink(self, tmp_path: Path) -> None:
        proc = _run_ensure_slot_dir(tmp_path, "claude")
        assert proc.returncode == 0, proc.stderr

        projects = tmp_path / ".cli-auth" / "claude" / "projects"
        assert projects.is_symlink()
        assert str(projects.readlink()) == "../_shared-projects"
        # _shared-projects must exist as a real directory.
        assert (tmp_path / ".cli-auth" / "_shared-projects").is_dir()

    def test_existing_symlink_is_left_alone(self, tmp_path: Path) -> None:
        # First run sets up the symlink.
        _run_ensure_slot_dir(tmp_path, "claude")
        projects = tmp_path / ".cli-auth" / "claude" / "projects"
        assert projects.is_symlink()
        link_target_before = projects.readlink()

        # Second run must be a no-op.
        proc = _run_ensure_slot_dir(tmp_path, "claude")
        assert proc.returncode == 0, proc.stderr
        assert projects.is_symlink()
        assert str(projects.readlink()) == str(link_target_before)

    def test_legacy_real_dir_migrates_to_shared(self, tmp_path: Path) -> None:
        """Pre-``claude_accounts`` installs have ``projects/`` as a real dir."""
        legacy = tmp_path / ".cli-auth" / "claude" / "projects"
        legacy.mkdir(parents=True)
        sentinel = legacy / "-home-user-project" / "session-abc.jsonl"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text('{"type":"user"}', encoding="utf-8")

        proc = _run_ensure_slot_dir(tmp_path, "claude")
        assert proc.returncode == 0, proc.stderr

        # projects is now a symlink pointing at ../_shared-projects.
        assert legacy.is_symlink()
        assert str(legacy.readlink()) == "../_shared-projects"

        # Session data survived the relocation.
        shared_sentinel = (
            tmp_path / ".cli-auth" / "_shared-projects" / "-home-user-project" / "session-abc.jsonl"
        )
        assert shared_sentinel.is_file()
        assert shared_sentinel.read_text(encoding="utf-8") == '{"type":"user"}'

    def test_legacy_real_dir_with_existing_shared_data_errors(self, tmp_path: Path) -> None:
        """When both projects/ and _shared-projects/ hold data, bail out."""
        legacy = tmp_path / ".cli-auth" / "claude" / "projects"
        legacy.mkdir(parents=True)
        (legacy / "legacy.jsonl").write_text("legacy", encoding="utf-8")

        shared = tmp_path / ".cli-auth" / "_shared-projects"
        shared.mkdir(parents=True)
        (shared / "shared.jsonl").write_text("shared", encoding="utf-8")

        proc = _run_ensure_slot_dir(tmp_path, "claude")
        assert proc.returncode != 0
        assert "Refusing to auto-merge" in proc.stderr
        # Neither side was modified.
        assert (legacy / "legacy.jsonl").is_file()
        assert (shared / "shared.jsonl").is_file()

    def test_extra_slot_just_gets_symlink(self, tmp_path: Path) -> None:
        """Non-default slots should always land on a fresh symlink."""
        proc = _run_ensure_slot_dir(tmp_path, "claude-alt")
        assert proc.returncode == 0, proc.stderr

        projects = tmp_path / ".cli-auth" / "claude-alt" / "projects"
        assert projects.is_symlink()
        assert str(projects.readlink()) == "../_shared-projects"
