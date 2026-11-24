"""Exercise update.sh in disposable homes with fake git, Docker, and gateway commands."""

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

FAKE_PYTHON = """#!/bin/sh
if [ "$1" = scripts/webchat.py ]; then
    printf 'python3 %s\\n' "$*" >> "$FAKE_LOG"
    if [ "$PWD" != "$FAKE_REPO" ] || [ -t 0 ]; then exit 99; fi
    printf 'gateway output\\n'
    exit "$FAKE_GATEWAY_EXIT"
fi
exec "$FAKE_PYTHON" "$@"
"""
GATEWAY_CALL = "python3 scripts/webchat.py up"
GATEWAY_HEADING = "=== Updating web chat gateway ==="
GATEWAY_FAILED = "The web chat gateway was not updated. Run python3 scripts/webchat.py up to retry."


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
        for name, source in (
            ("git", FAKE_GIT), ("docker", FAKE_DOCKER), ("python3", FAKE_PYTHON)
        ):
            command = self.bin / name
            command.write_text(source, encoding="utf-8")
            command.chmod(0o700)
        self.log = self.root / "commands.log"
        self.home = self.root / "home"

    def run_update(
        self, answers, *, turns=(), tasks=(), initial_turns=(), initial_tasks=(), profiles="",
        gateway_exit=0,
    ):
        self.home.mkdir(exist_ok=True)
        (self.home / "inflight_turns.json").write_text(
            json.dumps({"turns": dict(enumerate(initial_turns))}), encoding="utf-8"
        )
        (self.home / "tasks.json").write_text(
            json.dumps({"tasks": initial_tasks}), encoding="utf-8"
        )
        env_file = self.repo / ".env"
        if profiles is None:
            env_file.unlink(missing_ok=True)
        else:
            env_file.write_text(
                "DUCTOR_HOME=" + json.dumps(str(self.home)) + "\n" + profiles, encoding="utf-8"
            )
        self.log.unlink(missing_ok=True)
        env = dict(
            os.environ,
            HOME=str(self.root / "user"),
            ONBOARD_LANGUAGE="en",
            LC_ALL="C",
            PATH=str(self.bin) + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
            FAKE_LOG=str(self.log),
            FAKE_PYTHON=sys.executable,
            FAKE_REPO=str(self.repo),
            FAKE_GATEWAY_EXIT=str(gateway_exit),
            FAKE_HOME=str(self.home),
            FAKE_TURNS_AFTER_BUILD=json.dumps({"turns": dict(enumerate(turns))}),
            FAKE_TASKS_AFTER_BUILD=json.dumps({"tasks": tasks}),
        )
        env.pop("DUCTOR_HOME", None)
        if profiles is None:
            env["DUCTOR_HOME"] = str(self.home)
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

    def test_webchat_profile_refreshes_gateway_after_swap(self):
        for profiles in (
            "COMPOSE_PROFILES=webchat",
            "COMPOSE_PROFILES=other,webchat",
            'COMPOSE_PROFILES="webchat"',
            "COMPOSE_PROFILES=other\nCOMPOSE_PROFILES='other, webchat' # enabled",
        ):
            with self.subTest(profiles=profiles):
                result, calls = self.run_update("y\n", profiles=profiles)
                assert result.returncode == 0, result.stderr
                assert calls.count(GATEWAY_CALL) == 1
                assert calls[-3:] == ["docker compose up -d", GATEWAY_CALL, "docker compose ps"]
                assert GATEWAY_HEADING in result.stdout
                assert "gateway output" in result.stdout

    def test_without_webchat_profile_skips_gateway(self):
        for profiles in (
            None, "", "COMPOSE_PROFILES=other", "COMPOSE_PROFILES=webchat2",
            "COMPOSE_PROFILES=webchat\nCOMPOSE_PROFILES=other",
        ):
            with self.subTest(profiles=profiles):
                result, calls = self.run_update("y\n", profiles=profiles)
                assert result.returncode == 0, result.stderr
                assert calls[-2:] == ["docker compose up -d", "docker compose ps"]
                assert GATEWAY_CALL not in calls
                assert GATEWAY_HEADING not in result.stdout

    def test_declining_update_skips_gateway(self):
        for answers, turns in (("n\n", ()), ("y\nn\n", ({},))):
            with self.subTest(answers=answers):
                result, calls = self.run_update(
                    answers, turns=turns, profiles="COMPOSE_PROFILES=webchat"
                )
                assert result.returncode == 0, result.stderr
                assert "docker compose up -d" not in calls
                assert GATEWAY_CALL not in calls
                assert GATEWAY_HEADING not in result.stdout

    def test_gateway_failure_still_shows_status_and_exits_one(self):
        result, calls = self.run_update("y\n", profiles="COMPOSE_PROFILES=webchat", gateway_exit=3)
        assert result.returncode == 1
        assert calls[-3:] == ["docker compose up -d", GATEWAY_CALL, "docker compose ps"]
        assert GATEWAY_FAILED in result.stderr
        assert "=== Status ===" in result.stdout

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
