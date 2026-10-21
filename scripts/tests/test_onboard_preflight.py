"""onboard.sh preflight messages in the chosen language; fake PATH, no Docker or Python needed."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/onboard.sh"

# Expected preflight texts (python3 missing, docker missing, compose too old) per language.
# This test file holds the Korean row because it asserts Korean output.
TEXTS = {
    "en": (
        "python3 is required; install Python 3 before onboarding",
        "docker is required; install Docker Engine before onboarding",
        "docker compose v2 or newer is required",
    ),
    "de": (
        "python3 wird benötigt; installiere Python 3, bevor du das Onboarding startest",
        "docker wird benötigt; installiere die Docker Engine, bevor du das Onboarding startest",
        "docker compose v2 oder neuer wird benötigt",
    ),
    "es": (
        "se necesita python3; instala Python 3 antes de la incorporación",
        "se necesita docker; instala Docker Engine antes de la incorporación",
        "se necesita docker compose v2 o posterior",
    ),
    "fr": (
        "python3 est requis ; installe Python 3 avant l'onboarding",
        "docker est requis ; installe Docker Engine avant l'onboarding",
        "docker compose v2 ou plus récent est requis",
    ),
    "id": (
        "python3 diperlukan; instal Python 3 sebelum onboarding",
        "docker diperlukan; instal Docker Engine sebelum onboarding",
        "docker compose v2 atau yang lebih baru diperlukan",
    ),
    "ko": (
        "python3가 필요해요. 온보딩 전에 Python 3를 설치하세요",
        "docker가 필요해요. 온보딩 전에 Docker Engine을 설치하세요",
        "docker compose v2 이상이 필요해요",
    ),
    "nl": (
        "python3 is vereist; installeer Python 3 vóór de onboarding",
        "docker is vereist; installeer Docker Engine vóór de onboarding",
        "docker compose v2 of nieuwer is vereist",
    ),
    "pt": (
        "python3 é necessário; instale o Python 3 antes do onboarding",
        "docker é necessário; instale o Docker Engine antes do onboarding",
        "é necessário docker compose v2 ou mais recente",
    ),
    "ru": (
        "нужен python3; установи Python 3 перед онбордингом",
        "нужен docker; установи Docker Engine перед онбордингом",
        "нужен docker compose v2 или новее",
    ),
}
PROBLEMS = ("python3", "docker", "compose")


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ductorverify-preflight-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = {}
        for problem in PROBLEMS:
            path = self.root / ("bin-" + problem)
            path.mkdir()
            self.bin[problem] = path
        # docker missing: only python3 exists; compose too old: both exist, docker prints 1.x.
        (self.bin["docker"] / "python3").write_text("#!/bin/sh\nexit 0\n")
        (self.bin["compose"] / "python3").write_text("#!/bin/sh\nexit 0\n")
        (self.bin["compose"] / "docker").write_text("#!/bin/sh\nprintf '1.29.2\\n'\n")
        for path in self.bin.values():
            for tool in path.iterdir():
                tool.chmod(0o755)

    def run_script(self, problem, *options, environment=None):
        env = {
            "HOME": str(self.root / "home"),
            "PATH": str(self.bin[problem]),
            **(environment or {}),
        }
        return subprocess.run(
            ["/bin/bash", str(SCRIPT), "--no-prompt", *options],
            env=env,
            capture_output=True,
            text=True,
            cwd=self.root,
        )

    def assert_says(self, result, language, problem):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        # bash itself warns on stderr when the host lacks the requested locale; ignore that line.
        lines = [line for line in result.stderr.splitlines() if "setlocale" not in line]
        self.assertEqual(lines, ["onboard: " + TEXTS[language][PROBLEMS.index(problem)]])

    def test_every_language_and_problem(self):
        for language in TEXTS:
            for problem in PROBLEMS:
                with self.subTest(language=language, problem=problem):
                    result = self.run_script(problem, "--language", language)
                    self.assert_says(result, language, problem)

    def test_language_sources_and_order(self):
        saved = self.root / "saved"
        (saved / "config").mkdir(parents=True)
        (saved / "config/config.json").write_text('{"a": 1, "language": "fr", "b": {}}')
        cases = [
            ("option", ["--language", "ko"], {}, "ko"),
            ("option=", ["--language=nl"], {}, "nl"),
            (
                "option beats everything",
                ["--language", "ko", "--home", str(saved)],
                {"ONBOARD_LANGUAGE": "de", "LC_ALL": "es_ES.UTF-8"},
                "ko",
            ),
            ("env", [], {"ONBOARD_LANGUAGE": "ru", "LC_ALL": "es_ES.UTF-8"}, "ru"),
            ("saved by --home", ["--home", str(saved)], {"LC_ALL": "es_ES.UTF-8"}, "fr"),
            ("saved by --home=", ["--home=" + str(saved)], {"LC_ALL": "es_ES.UTF-8"}, "fr"),
            (
                "saved by DUCTOR_HOME",
                [],
                {"DUCTOR_HOME": str(saved), "LC_ALL": "es_ES.UTF-8"},
                "fr",
            ),
            (
                "--home beats DUCTOR_HOME",
                ["--home", str(self.root / "none")],
                {"DUCTOR_HOME": str(saved), "LC_ALL": "es_ES.UTF-8"},
                "es",
            ),
            ("saved by the default home", [], {"HOME": str(self.root / "default")}, "fr"),
            ("locale", [], {"LC_ALL": "ko_KR.UTF-8"}, "ko"),
            (
                "LC_ALL first",
                [],
                {"LC_ALL": "pt_BR.UTF-8", "LC_MESSAGES": "de_DE", "LANG": "ko_KR"},
                "pt",
            ),
            ("LC_MESSAGES second", [], {"LC_MESSAGES": "de_DE.UTF-8", "LANG": "ko_KR"}, "de"),
            ("LANG last", [], {"LANG": "id_ID"}, "id"),
            ("dash and modifier", [], {"LANG": "fr-FR@euro"}, "fr"),
            ("C locale", [], {"LC_ALL": "C", "LANG": "ko_KR.UTF-8"}, "en"),
            ("POSIX locale", [], {"LANG": "POSIX"}, "en"),
            ("unsupported locale", [], {"LANG": "zh_CN.UTF-8"}, "en"),
            ("no evidence", [], {}, "en"),
        ]
        default_config = self.root / "default/.ductor/config"
        default_config.mkdir(parents=True)
        (default_config / "config.json").write_text('{"language": "fr"}')
        for name, options, environment, expected in cases:
            with self.subTest(name=name):
                self.assert_says(
                    self.run_script("python3", *options, environment=environment),
                    expected,
                    "python3",
                )

    def test_unsupported_values_are_skipped_to_the_next_source(self):
        saved = self.root / "saved"
        (saved / "config").mkdir(parents=True)
        (saved / "config/config.json").write_text('{"language": "zz"}')
        cases = [
            (["--language", "xx"], {"ONBOARD_LANGUAGE": "ru"}, "ru"),
            (["--language="], {"LC_ALL": "de_DE"}, "de"),
            ([], {"ONBOARD_LANGUAGE": "KO", "LANG": "nl_NL"}, "nl"),
            (["--home", str(saved)], {"LANG": "pt_PT"}, "pt"),
            (["--language", "xx"], {}, "en"),
        ]
        for options, environment, expected in cases:
            with self.subTest(options=options, environment=environment):
                self.assert_says(
                    self.run_script("docker", *options, environment=environment), expected, "docker"
                )

    def test_a_symbolic_link_config_is_not_read(self):
        target = self.root / "target.json"
        target.write_text('{"language": "ko"}')
        home = self.root / "linked"
        (home / "config").mkdir(parents=True)
        (home / "config/config.json").symlink_to(target)
        self.assert_says(self.run_script("compose", "--home", str(home)), "en", "compose")

    def test_unreadable_or_broken_config_falls_back(self):
        home = self.root / "broken"
        (home / "config").mkdir(parents=True)
        (home / "config/config.json").write_text('{not json "language": ')
        self.assert_says(
            self.run_script("python3", "--home", str(home), environment={"LANG": "es_ES"}),
            "es",
            "python3",
        )

    def run_system_preflight(self, uid, version, language="en"):
        binary = self.root / "bin-system"
        binary.mkdir(exist_ok=True)
        (binary / "id").write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_UID"\n')
        (binary / "docker").write_text('#!/bin/sh\nprintf "2.0.0\\n"\n')
        (binary / "python3").write_text(
            "#!" + sys.executable + "\n"
            "import os, sys\n"
            "if sys.argv[1] == '-c':\n"
            "    sys.version_info = tuple(map(int, os.environ['FAKE_VERSION'].split('.')))\n"
            "    exec(sys.argv[2])\n"
            "else:\n"
            "    print('onboard dispatched')\n"
        )
        for tool in binary.iterdir():
            tool.chmod(0o755)
        return subprocess.run(
            ["/bin/bash", str(SCRIPT), "--no-prompt", "--language", language],
            env={"PATH": str(binary), "HOME": str(self.root / "home"),
                 "FAKE_UID": str(uid), "FAKE_VERSION": version},
            cwd=self.root, capture_output=True, text=True, check=False,
        )

    def test_root_is_rejected_in_every_language(self):
        for language in TEXTS:
            with self.subTest(language=language):
                result = self.run_system_preflight(0, "3.11.0", language)
                assert result.returncode == 1
                assert result.stdout == ""
                assert "root" in result.stderr
                assert "UID/GID" in result.stderr

    def test_old_python_is_rejected_in_every_language(self):
        for language in TEXTS:
            with self.subTest(language=language):
                result = self.run_system_preflight(1000, "3.8.19", language)
                assert result.returncode == 1
                assert result.stdout == ""
                assert "3.9" in result.stderr
                assert "3.8.19" in result.stderr

    def test_supported_uid_and_python_continue_to_onboarding(self):
        for version in ("3.9.0", "3.11.0"):
            with self.subTest(version=version):
                result = self.run_system_preflight(1001, version)
                assert result.returncode == 0, result.stderr
                assert result.stdout == "onboard dispatched\n"

    def test_help_dispatches_without_system_preflight(self):
        for uid in (0, 1001):
            with self.subTest(uid=uid):
                binary = self.root / "help-bin"
                binary.mkdir(exist_ok=True)
                (binary / "python3").write_text("#!" + sys.executable + "\nprint('help dispatched')\n")
                (binary / "python3").chmod(0o755)
                result = subprocess.run(["/bin/bash", str(SCRIPT), "--help"],
                                        env={"PATH": str(binary), "HOME": str(self.root), "FAKE_UID": str(uid)},
                                        capture_output=True, text=True, check=False)
                assert result.returncode == 0, result.stderr
                assert result.stdout == "help dispatched\n"

    def test_script_syntax(self):
        result = subprocess.run(["/bin/bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
