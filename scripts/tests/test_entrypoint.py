"""Run entrypoint against a disposable mounted home."""

# ruff: noqa: INP001 -- Standalone unittest discovery needs no package initializer.
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class EntrypointTests(unittest.TestCase):
    def test_default_link_does_not_follow_account_priority(self):
        with tempfile.TemporaryDirectory(prefix="ductorverify-entrypoint-") as folder:
            root = Path(folder)
            volume, home = root / "volume", root / "user"
            (volume / "config").mkdir(parents=True)
            home.mkdir()
            for slot in ("claude-alt", "claude"):
                path = volume / ".cli-auth" / slot
                path.mkdir(parents=True)
                (path / ".credentials.json").write_text("{}")
            (volume / "bot.pid").write_text("preserved")
            (volume / "config/config.json").write_text(
                json.dumps({"claude_accounts": ["claude-alt", "claude"]})
            )
            script = root / "entrypoint.sh"
            script.write_text(
                (ROOT / "docker/ductor-bot/entrypoint.sh").read_text().replace(
                    "/ductor", str(volume)
                )
            )
            result = subprocess.run(
                ["sh", str(script), "true"], env=dict(os.environ, HOME=str(home)),
                capture_output=True, text=True, check=False,
            )
            assert result.returncode == 0, result.stderr
            assert (home / ".claude").resolve() == volume / ".cli-auth/claude"
            assert (volume / "bot.pid").read_text() == "preserved"


    def test_ductor_link_and_existing_directory(self):
        for existing in ("missing", "symlink", "directory"):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory(
                prefix="ductorverify-entrypoint-"
            ) as folder:
                root = Path(folder)
                volume, home = root / "volume", root / "user"
                home.mkdir()
                link = home / ".ductor"
                if existing == "symlink":
                    link.symlink_to(root / "old-volume")
                elif existing == "directory":
                    link.mkdir()
                    (link / "keep.txt").write_text("preserved")
                script = root / "entrypoint.sh"
                script.write_text(
                    (ROOT / "docker/ductor-bot/entrypoint.sh").read_text().replace(
                        "/ductor", str(volume)
                    )
                )
                result = subprocess.run(
                    ["sh", str(script), "true"], env=dict(os.environ, HOME=str(home)),
                    capture_output=True, text=True, check=False,
                )
                assert result.returncode == 0, result.stderr
                if existing == "directory":
                    assert not link.is_symlink()
                    assert (link / "keep.txt").read_text() == "preserved"
                    assert "WARNING:" in result.stderr
                else:
                    assert link.is_symlink()
                    assert link.resolve() == volume
