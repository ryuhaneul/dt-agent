"""Provider maintenance with fake Docker and disposable credentials only."""

# ruff: noqa: INP001 -- Standalone unittest discovery needs no package initializer.
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FAKE_DOCKER = r"""import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(json.dumps({'args': args, 'cwd': os.getcwd()}) + '\n')
home = Path(os.environ['FAKE_HOME'])
if args[0] == 'inspect':
    if '--format' in args:
        print('false' if os.environ.get('FAKE_STOPPED_CONTAINER') == '1' else 'true')
    sys.exit(int(os.environ.get('FAKE_NO_CONTAINER', '0')))
if args[0] == 'compose':
    args = args[1:]
    if args[:1] == ['--project-directory']:
        args = args[2:]
    if args[:1] == ['ps']:
        print('synthetic-container')
        sys.exit(0)
if args[0] == 'exec':
    args = args[1:]
    while args[0] in ('-it', '-i', '-t', '-e'):
        flag = args.pop(0)
        if flag == '-e':
            key, value = args.pop(0).split('=', 1)
            os.environ[key] = value.replace('/ductor', str(home))
    command = args[1:]
    if command[:4] == ['python3', '-I', '-m', 'ductor_bot.cli.claude_slots'] or command[:3] == ['python3', '-m', 'ductor_bot.cli.claude_slots']:
        error = os.environ.get('FAKE_SLOT_ERROR')
        print(json.dumps({'error': error} if error else {'slots': []}))
        sys.exit(2 if error else 0)
    if command == ['codex', 'logout']:
        (home / '.cli-auth/codex/auth.json').unlink(missing_ok=True)
    elif command[:2] == ['rm', '-f']:
        Path(command[2].replace('/ductor', str(home))).unlink(missing_ok=True)
    elif command[:2] == ['python3', '-c']:
        sys.exit(subprocess.run([sys.executable, '-c', command[2].replace('/ductor', str(home))]).returncode)
    elif command == ['claude', 'auth', 'status']:
        folder = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))
        if (folder / '.credentials.json').is_file():
            print('authenticated')
        else:
            sys.exit(1)
    elif command == ['claude', '/login']:
        target = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))
        target.mkdir(parents=True, exist_ok=True)
        (target / '.credentials.json').write_text('{"synthetic": "new"}')
    elif command[:2] == ['sh', '-c']:
        sys.exit(subprocess.run(['sh', '-c', command[2].replace('/ductor', str(home))]).returncode)
"""


class LogoutStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ductorverify-maintenance-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.home = self.root / "repo", self.root / "volume"
        (self.repo / "scripts").mkdir(parents=True)
        for name in ("_home.sh", "login.sh", "logout.sh", "status.sh"):
            shutil.copy2(ROOT / "scripts" / name, self.repo / "scripts" / name)
        (self.home / "config").mkdir(parents=True)
        (self.home / "config/config.json").write_text(
            json.dumps({"claude_accounts": ["claude-alt", "claude"]})
        )
        for slot in ("claude", "claude-alt", "codex", "gemini"):
            (self.home / ".cli-auth" / slot).mkdir(parents=True)
        self.user = self.root / "user"
        self.user.mkdir()
        (self.user / ".claude").symlink_to(self.home / ".cli-auth/claude")
        binary = self.root / "bin"
        binary.mkdir()
        docker = binary / "docker"
        docker.write_text("#!" + sys.executable + "\n" + FAKE_DOCKER)
        docker.chmod(0o700)
        self.log = self.root / "commands.jsonl"
        self.env = dict(os.environ, HOME=str(self.user), DUCTOR_HOME=str(self.home),
                        ONBOARD_LANGUAGE="en", LC_ALL="C", FAKE_HOME=str(self.home),
                        FAKE_LOG=str(self.log), PATH=str(binary) + os.pathsep + os.environ["PATH"])
        for name in ("CLAUDE_CONFIG_DIR", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            self.env.pop(name, None)

    def run_script(self, name, *args):
        result = subprocess.run(["bash", str(self.repo / "scripts" / name), *args],
                                cwd=self.root, env=self.env, input="", capture_output=True,
                                text=True, timeout=15, check=False)
        assert result.returncode == 0, result.stderr
        return result

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_disable_and_remove_do_not_relink(self):
        for action in ("--disable", "--remove"):
            with self.subTest(action=action):
                (self.home / "config/config.json").write_text(
                    json.dumps({"claude_accounts": ["claude-alt", "claude"]})
                )
                self.log.unlink(missing_ok=True)
                self.run_script("logout.sh", "claude", action, "claude")
                assert (self.user / ".claude").resolve() == self.home / ".cli-auth/claude"
                assert not self.log.exists(), "config-only edits must not call Docker"

    def test_remove_then_login_pins_default_without_overwriting_alternate(self):
        default = self.home / ".cli-auth/claude/.credentials.json"
        alternate = self.home / ".cli-auth/claude-alt/.credentials.json"
        default.write_text("default-old")
        alternate.write_text("alternate-original")
        self.run_script("logout.sh", "claude", "--remove", "claude")
        self.run_script("login.sh", "claude")
        assert default.is_file()
        assert alternate.read_text() == "alternate-original"
        logins = [c["args"] for c in self.calls() if c["args"][-2:] == ["claude", "/login"]]
        assert len(logins) == 1
        assert "CLAUDE_CONFIG_DIR=/ductor/.cli-auth/claude" in logins[0]

    def test_codex_device_login_and_logout_preserve_other_files(self):
        folder = self.home / ".cli-auth/codex"
        for name in ("auth.json", "config.toml", "session.jsonl"):
            (folder / name).write_text("synthetic")
        self.run_script("login.sh", "codex")
        assert ["codex", "login", "--device-auth"] in [c["args"][-3:] for c in self.calls()]
        self.run_script("logout.sh", "codex")
        assert not (folder / "auth.json").exists()
        for name in ("config.toml", "session.jsonl"):
            assert (folder / name).read_text() == "synthetic"
        assert ["codex", "logout"] in [c["args"][-2:] for c in self.calls()]

    def test_gemini_oauth_status_and_logout(self):
        credential = self.home / ".cli-auth/gemini/oauth_creds.json"
        credential.write_text("{}")
        result = self.run_script("status.sh")
        assert "Gemini: authenticated" in result.stdout
        self.run_script("logout.sh", "gemini")
        assert not credential.exists()
        result = self.run_script("status.sh")
        assert "Gemini: not authenticated" in result.stdout

    def test_status_compose_runs_from_repository(self):
        self.run_script("status.sh")
        commands = [c for c in self.calls() if c["args"][:2] == ["compose", "ps"]]
        assert [c["cwd"] for c in commands] == [str(self.repo)]

    def test_non_tty_claude_logout_has_no_terminal_flags(self):
        self.run_script("logout.sh", "claude")
        commands = [c["args"] for c in self.calls() if c["args"][-3:] == ["claude", "auth", "logout"]]
        assert commands == [["exec", "-e", "CLAUDE_CONFIG_DIR=/ductor/.cli-auth/claude", "ductor-bot", "claude", "auth", "logout"]]


    def test_status_checks_each_configured_claude_slot(self):
        main = self.home / ".cli-auth/claude-main"
        main.mkdir()
        (main / ".credentials.json").write_text("{}")
        (self.home / ".cli-auth/claude/.credentials.json").write_text("{}")
        config = self.home / "config/config.json"
        for accounts, expected in (
            (["claude-main"], ["claude-main"]),
            (["claude", "claude-alt"], ["claude", "claude-alt"]),
            ([], ["claude"]),
            (None, ["claude"]),
        ):
            with self.subTest(accounts=accounts):
                if accounts is None:
                    config.unlink()
                else:
                    config.write_text(json.dumps({"claude_accounts": accounts}))
                self.log.unlink(missing_ok=True)
                result = self.run_script("status.sh")
                lines = [line for line in result.stdout.splitlines() if line.startswith("Claude")]
                assert lines == [
                    "Claude (" + slot + "): " + (
                        "not authenticated" if slot == "claude-alt" else "authenticated"
                    ) for slot in expected
                ]
                commands = [call["args"] for call in self.calls()
                            if call["args"][-3:] == ["claude", "auth", "status"]]
                assert commands == [
                    ["exec", "-e", "CLAUDE_CONFIG_DIR=/ductor/.cli-auth/" + slot,
                     "ductor-bot", "claude", "auth", "status"] for slot in expected
                ]


    def legacy_claude_directory(self):
        main = self.home / ".cli-auth/claude-main"
        main.mkdir()
        legacy = self.home / ".cli-auth/claude-slots"
        nested = legacy / "claude-main"
        nested.mkdir(parents=True)
        for folder in (self.home / ".cli-auth/claude", main, nested):
            (folder / ".credentials.json").write_text("{}")
        return legacy

    def test_claude_lists_skip_legacy_slot_directory(self):
        self.legacy_claude_directory()
        for name in ("login.sh", "logout.sh"):
            with self.subTest(script=name):
                result = self.run_script(name, "claude", "--list")
                self.assertNotIn("claude-slots", result.stdout + result.stderr)
                slots = [line.split()[0] for line in result.stdout.splitlines()
                         if line.startswith("  claude")]
                self.assertIn("claude", slots)
                self.assertIn("claude-main", slots)

    def test_authenticated_claude_slots_skip_legacy_slot_directory(self):
        legacy = self.legacy_claude_directory()
        (legacy / ".credentials.json").write_text("{}")
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; list_authenticated_claude_slots',
             "fixture", str(self.repo / "scripts/login.sh")],
            cwd=self.root, env=self.env, input="", capture_output=True,
            text=True, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["claude", "claude-main"])
        self.assertNotIn("claude-slots", result.stdout + result.stderr)

    def test_claude_repair_skips_legacy_slot_directory(self):
        legacy = self.legacy_claude_directory()
        result = self.run_script("login.sh", "claude", "--repair")
        self.assertNotIn("claude-slots", result.stdout + result.stderr)
        self.assertIn("claude-main", result.stdout)
        self.assertFalse((legacy / "projects").exists())
        self.assertEqual((legacy / "claude-main/.credentials.json").read_text(), "{}")
        self.assertTrue((self.home / ".cli-auth/claude-main/projects").is_symlink())

    def test_status_ignores_unconfigured_legacy_slot_directory(self):
        self.legacy_claude_directory()
        (self.home / "config/config.json").write_text(
            json.dumps({"claude_accounts": ["claude", "claude-main"]})
        )
        result = self.run_script("status.sh")
        self.assertNotIn("claude-slots", result.stdout + result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.startswith("Claude")]
        self.assertEqual(lines, ["Claude (claude): authenticated",
                                 "Claude (claude-main): authenticated"])


    def test_slot_options_delegate_normalized_names(self):
        for flag, value, expected in (
            ("--prefer", "alt", "claude-alt"),
            ("--priority", "alt,default", "claude-alt,claude"),
            ("--clear-cooldown", "alt", "claude-alt"),
            ("--clear-cooldown", "all", "all"),
        ):
            with self.subTest(flag=flag, value=value):
                self.log.unlink(missing_ok=True)
                result = self.run_script("logout.sh", "claude", flag, value)
                self.assertIn("Priority changes apply to main within seconds", result.stdout)
                commands = [call["args"] for call in self.calls()]
                self.assertEqual(commands, [["inspect", "--format", "{{.State.Running}}", "ductor-bot"],
                    ["exec", "ductor-bot", "python3", "-I", "-m",
                     "ductor_bot.cli.claude_slots", flag[2:], expected]])

    def test_slot_options_translate_all_module_errors(self):
        errors = {
            "unknown_slot": "Unknown slot.",
            "invalid_priority": "Priority must contain every configured slot exactly once.",
            "invalid_delta": "Invalid move direction.",
            "invalid_arguments": "Invalid arguments.",
        }
        for flag, value in (("--prefer", "alt"), ("--priority", "alt,default"),
                            ("--clear-cooldown", "all")):
            for code, message in errors.items():
                with self.subTest(flag=flag, code=code):
                    result = subprocess.run(
                        ["bash", str(self.repo / "scripts/logout.sh"), "claude", flag, value],
                        cwd=self.root, env=dict(self.env, FAKE_SLOT_ERROR=code),
                        input="", capture_output=True, text=True, timeout=15,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stderr.strip(), message)
                    self.assertNotIn("Claude Code Slots", result.stdout)

    def test_slot_options_require_container(self):
        for flag in ("--prefer", "--priority", "--clear-cooldown"):
            with self.subTest(flag=flag):
                result = subprocess.run(
                    ["bash", str(self.repo / "scripts/logout.sh"), "claude", flag, "alt"],
                    env=dict(self.env, FAKE_NO_CONTAINER="1"),
                    capture_output=True, text=True, timeout=15,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("container 'ductor-bot' not found", result.stderr)

    def test_slot_list_reads_cooldowns_without_container(self):
        state = self.home / ".cli-auth/_claude-slots.json"
        for until, expected in ((time.time() + 3660, "1h 1m"),
                                (time.time() - 60, "none"), (None, "none")):
            with self.subTest(until=until):
                if until is None:
                    state.unlink()
                else:
                    state.write_text(json.dumps({"cooldowns": {"claude-alt": until}}))
                before = state.read_bytes() if state.exists() else None
                self.env["FAKE_NO_CONTAINER"] = "1"
                result = self.run_script("logout.sh", "claude", "--list")
                row = next(line for line in result.stdout.splitlines()
                           if line.startswith("  claude-alt"))
                self.assertIn("cooldown=" + expected, row)
                self.assertEqual(state.read_bytes() if state.exists() else None, before)
                self.assertFalse(self.log.exists())

    def test_slot_management_help_and_translations(self):
        languages = ("en", "de", "es", "fr", "id", "nl", "pt", "ru", "ko")
        messages = set()
        for language in languages:
            with self.subTest(language=language):
                self.env["ONBOARD_LANGUAGE"] = language
                help_result = self.run_script("logout.sh", "-h")
                for flag in ("--prefer", "--priority", "--clear-cooldown"):
                    self.assertIn(flag, help_result.stdout)
                success = self.run_script("logout.sh", "claude", "--prefer", "alt")
                self.assertTrue(success.stdout.strip())
                for code in ("unknown_slot", "invalid_priority", "invalid_delta", "invalid_arguments"):
                    result = subprocess.run(
                        ["bash", str(self.repo / "scripts/logout.sh"), "claude", "--prefer", "alt"],
                        env=dict(self.env, FAKE_SLOT_ERROR=code), capture_output=True,
                        text=True, timeout=15,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertTrue(result.stderr.strip())
                    self.assertNotIn("slot_error_", result.stderr)
                    messages.add(result.stderr.strip())
        self.assertEqual(len(messages), 36)


    def test_stopped_container_reports_state_without_exec(self):
        result = subprocess.run(
            ['bash', str(self.repo / 'scripts/logout.sh'), 'claude', '--prefer', 'alt'],
            env=dict(self.env, FAKE_STOPPED_CONTAINER='1'), capture_output=True,
            text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('not running', result.stderr)
        self.assertNotIn('Invalid arguments', result.stderr)
        self.assertFalse(any(call['args'][0] == 'exec' for call in self.calls()))

    def test_invalid_slot_names_never_mutate_or_call_docker(self):
        config = self.home / 'config/config.json'
        before = config.read_bytes()
        for flag, value in (('--disable', 'slots'), ('--remove', '../bad'),
                            ('--prefer', 'slots'), ('--priority', 'alt,slots'),
                            ('--clear-cooldown', 'has space')):
            with self.subTest(flag=flag):
                self.log.unlink(missing_ok=True)
                result = subprocess.run(
                    ['bash', str(self.repo / 'scripts/logout.sh'), 'claude', flag, value],
                    env=self.env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('Invalid Claude slot name', result.stderr)
                self.assertEqual(config.read_bytes(), before)
                self.assertFalse(self.log.exists())
