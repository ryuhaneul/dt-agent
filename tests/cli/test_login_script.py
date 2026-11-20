"""Integration-style tests for ``scripts/login.sh`` shell helpers.

The script itself is plain bash; these tests ``source`` it from a
subprocess so we can exercise ``ensure_slot_dir`` in isolation without
invoking docker. The bottom-of-file dispatch is guarded by
``[ "${BASH_SOURCE[0]}" = "$0" ]`` so sourcing is side-effect free.
"""

from __future__ import annotations

import json
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


def test_repair_preserves_detached_settings_for_host_recovery(tmp_path: Path) -> None:
    from ductor_bot.cli.claude_accounts import slot_kind
    from ductor_bot.cli.claude_slots import disconnect_api_key

    root = tmp_path / ".cli-auth"
    default = root / "claude"
    alternate = root / "claude-api"
    default.mkdir(parents=True)
    alternate.mkdir()
    default_settings = default / "settings.json"
    default_bytes = b'{ "theme": "dark", "defaultOnly": true }\n'
    default_settings.write_bytes(default_bytes)
    (default / "shared.txt").write_text("shared", encoding="utf-8")
    alt_settings = alternate / "settings.json"
    alt_settings.write_text(
        '{"apiKeyHelper":"helper.sh","extraSlotOnly":true}', encoding="utf-8"
    )

    disconnect_api_key(tmp_path, alternate.name)
    detached_bytes = alt_settings.read_bytes()
    assert slot_kind(alternate) == "api_key_detached"

    repair = subprocess.run(
        ["bash", "-c", 'source "$1" && login_claude_dispatch --repair', "bash", str(_LOGIN_SH)],
        env={**os.environ, "DUCTOR_HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert repair.returncode == 0, repair.stderr
    assert alt_settings.is_file()
    assert not alt_settings.is_symlink()
    assert alt_settings.read_bytes() == detached_bytes
    assert (alternate / "shared.txt").resolve() == default / "shared.txt"
    assert slot_kind(alternate) == "api_key_detached"
    marker = alternate / "api-key-helper.detached"
    restored = json.loads(alt_settings.read_text(encoding="utf-8"))
    restored["apiKeyHelper"] = marker.read_text(encoding="utf-8").removesuffix("\n")
    alt_settings.write_text(json.dumps(restored), encoding="utf-8")

    assert slot_kind(alternate) == "api_key"
    assert default_settings.read_bytes() == default_bytes
    assert slot_kind(default) == "oauth"


def test_repair_keeps_detached_helpers_private_to_each_slot(tmp_path: Path) -> None:
    from ductor_bot.cli.claude_accounts import slot_kind
    from ductor_bot.cli.claude_slots import disconnect_api_key

    root = tmp_path / ".cli-auth"
    default = root / "claude"
    alternate = root / "claude-api"
    oauth = root / "claude-oauth"
    oauth.mkdir(parents=True)
    for slot, helper in ((default, "default-helper.sh"), (alternate, "extra-helper.sh")):
        slot.mkdir()
        settings = {"apiKeyHelper": helper, "slot": slot.name}
        (slot / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        disconnect_api_key(tmp_path, slot.name)
        assert slot_kind(slot) == "api_key_detached"

    default_settings = default / "settings.json"
    alt_settings = alternate / "settings.json"
    alt_marker = alternate / "api-key-helper.detached"
    default_bytes = default_settings.read_bytes()
    alt_bytes = alt_settings.read_bytes()
    alt_helper = b"extra-helper.sh\n"
    assert alt_marker.read_bytes() == alt_helper

    repair = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" && login_claude_dispatch --repair && login_claude_dispatch --repair',
            "bash",
            str(_LOGIN_SH),
        ],
        env={**os.environ, "DUCTOR_HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert repair.returncode == 0, repair.stderr
    assert alt_marker.is_file()
    assert not alt_marker.is_symlink()
    assert alt_marker.read_bytes() == alt_helper
    assert alt_settings.is_file()
    assert not alt_settings.is_symlink()
    assert alt_settings.read_bytes() == alt_bytes
    assert slot_kind(alternate) == "api_key_detached"
    oauth_marker = oauth / alt_marker.name
    assert not oauth_marker.exists()
    assert not oauth_marker.is_symlink()
    assert (oauth / "settings.json").resolve() == default_settings

    restored = json.loads(alt_settings.read_text(encoding="utf-8"))
    restored["apiKeyHelper"] = alt_marker.read_text(encoding="utf-8").removesuffix("\n")
    alt_settings.write_text(json.dumps(restored), encoding="utf-8")

    assert slot_kind(alternate) == "api_key"
    assert restored["apiKeyHelper"] == "extra-helper.sh"
    assert default_settings.read_bytes() == default_bytes
    assert slot_kind(default) == "api_key_detached"
