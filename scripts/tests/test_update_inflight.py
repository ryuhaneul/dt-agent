"""Exercise update.sh in disposable homes with fake git and Docker commands."""

# ruff: noqa: INP001 -- Standalone unittest discovery needs no package initializer.
from __future__ import annotations

import json
import os
import pty
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPT_SWAP = "Swap now? Running work will be interrupted. [y/N] "
ABORTED_BUILT = (
    "The new image is built and the running container is unchanged. "
    "Run docker compose up -d later to swap."
)
FAKE_GIT = """#!/bin/sh
printf 'git %s\\n' "$*" >> "$FAKE_LOG"
if [ "$1" = rev-parse ]; then
    if [ "$2" = HEAD ]; then printf 'old\\n'; else printf 'new\\n'; fi
fi
exit 0
"""
FAKE_DOCKER = """#!/bin/sh
printf 'docker %s\\n' "$*" >> "$FAKE_LOG"
if [ "$1" = compose ] && [ "$2" = build ]; then
    printf '%s\\n' "$FAKE_TURNS_AFTER_BUILD" > "$FAKE_HOME/inflight_turns.json"
    printf '%s\\n' "$FAKE_TASKS_AFTER_BUILD" > "$FAKE_HOME/tasks.json"
fi
exit 0
"""


class UpdateInflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ductorverify-update-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        for name in ("_home.sh", "update.sh"):
            shutil.copy2(ROOT / "scripts" / name, self.repo / "scripts" / name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, source in (("git", FAKE_GIT), ("docker", FAKE_DOCKER)):
            command = self.bin / name
            command.write_text(source, encoding="utf-8")
            command.chmod(0o700)
        (self.bin / "python3").symlink_to(sys.executable)
        self.log = self.root / "commands.log"
        self.home = self.root / "home"

    def run_update(self, answers, *, turns=(), tasks=(), initial_turns=(), initial_tasks=()):
        self.home.mkdir(exist_ok=True)
        (self.home / "inflight_turns.json").write_text(json.dumps({"turns": dict(enumerate(initial_turns))}), encoding="utf-8")
        (self.home / "tasks.json").write_text(
            json.dumps({"tasks": initial_tasks}), encoding="utf-8"
        )
        (self.repo / ".env").write_text(
            "DUCTOR_HOME=" + json.dumps(str(self.home)) + "\n", encoding="utf-8"
        )
        self.log.unlink(missing_ok=True)
        env = dict(
            os.environ,
            HOME=str(self.root / "user"),
            ONBOARD_LANGUAGE="en",
            LC_ALL="C",
            PATH=str(self.bin) + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
            FAKE_LOG=str(self.log),
            FAKE_HOME=str(self.home),
            FAKE_TURNS_AFTER_BUILD=json.dumps({"turns": dict(enumerate(turns))}),
            FAKE_TASKS_AFTER_BUILD=json.dumps({"tasks": tasks}),
        )
        env.pop("DUCTOR_HOME", None)
        # read -p displays prompts only with terminal input; output remains captured.
        master, slave = pty.openpty()
        try:
            os.write(master, answers.encode())
            result = subprocess.run(
                ["bash", str(self.repo / "scripts/update.sh")],
                cwd=self.root,
                env=env,
                stdin=slave,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        finally:
            os.close(slave)
            os.close(master)
        calls = self.log.read_text(encoding="utf-8").splitlines()
        return result, calls

    def test_turn_arriving_during_build_decline_keeps_container(self):
        result, calls = self.run_update("y\nn\n", turns=[{"chat_id": 1}])
        assert result.returncode == 0, result.stderr
        assert PROMPT_SWAP in result.stderr
        assert "WARN: 1 in-flight turn(s)" in result.stdout
        assert ABORTED_BUILT in result.stdout
        assert any(call.startswith("docker compose build ") for call in calls)
        assert "docker compose up -d" not in calls

    def test_turn_arriving_during_build_accept_swaps_container(self):
        result, calls = self.run_update("y\ny\n", turns=[{"chat_id": 1}])
        assert result.returncode == 0, result.stderr
        assert "docker compose up -d" in calls

    def test_no_running_work_swaps_without_extra_prompt(self):
        result, calls = self.run_update("y\n")
        assert result.returncode == 0, result.stderr
        assert PROMPT_SWAP not in result.stderr
        assert "docker compose up -d" in calls

    def test_two_inflight_turns_are_counted(self):
        result, calls = self.run_update("y\nn\n", turns=[{}, {}])
        assert result.returncode == 0, result.stderr
        assert "WARN: 2 in-flight turn(s)" in result.stdout
        assert "docker compose up -d" not in calls

    def test_apostrophe_home_counts_turns_and_tasks(self):
        self.home = self.root / "user's home"
        for kind in ("turn", "task"):
            with self.subTest(kind=kind):
                turns = [{"chat_id": 1}] if kind == "turn" else []
                tasks = [{"status": "running"}, {"status": "done"}] if kind == "task" else []
                result, calls = self.run_update(
                    "y\nn\n",
                    turns=turns,
                    tasks=tasks,
                    initial_turns=turns,
                    initial_tasks=tasks,
                )
                assert result.returncode == 0, result.stderr
                warning = (
                    "WARN: 1 in-flight turn(s)" if kind == "turn" else "WARN: 1 background task(s)"
                )
                assert warning in result.stdout.split("=== Building new image ===")[0]
                assert result.stdout.count(warning) == 2
                assert PROMPT_SWAP in result.stderr
                assert ABORTED_BUILT in result.stdout
                assert "docker compose up -d" not in calls


if __name__ == "__main__":
    unittest.main()
