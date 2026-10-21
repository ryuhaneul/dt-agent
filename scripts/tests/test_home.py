"""Resolve host homes in disposable repositories; no Docker or real .env access."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class HostHomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ductorverify-home-')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        (self.repo / 'scripts').mkdir(parents=True)
        shutil.copy2(ROOT / 'scripts/_home.sh', self.repo / 'scripts/_home.sh')
        self.environment = dict(os.environ, HOME=str(Path(self.temp.name) / 'user'))
        self.environment.pop('DUCTOR_HOME', None)

    def resolve(self, env=None):
        return subprocess.run(['bash', '-euc',
            'source "$1/scripts/_home.sh"; resolve_ductor_home "$1"',
            'home-test', str(self.repo)], env=env or self.environment,
            capture_output=True, text=True)

    def test_environment_beats_repository(self):
        (self.repo / '.env').write_text('DUCTOR_HOME=/repo-home\n')
        result = self.resolve(dict(self.environment, DUCTOR_HOME='/env-home'))
        self.assertEqual((result.returncode, result.stdout), (0, '/env-home\n'))

    def test_tilde_home_from_environment_and_repository(self):
        expected = self.environment["HOME"] + "/.ductor\n"
        (self.repo / ".env").write_text("DUCTOR_HOME=~/.ductor\n")
        assert self.resolve().stdout == expected
        assert self.resolve(dict(self.environment, DUCTOR_HOME="~/.ductor")).stdout == expected

    def test_repository_home_quotes_and_json_escaping(self):
        home = '/custom/home with spaces/한글'
        for value in (home, "'" + home + "'", json.dumps(home), '"' + home + '"'):
            with self.subTest(value=value):
                (self.repo / '.env').write_text('OTHER=ignored\nDUCTOR_HOME=' + value + '\n')
                result = self.resolve()
                self.assertEqual((result.returncode, result.stdout), (0, home + '\n'))

    def test_missing_empty_key_and_empty_environment_fall_back(self):
        for content in (None, 'OTHER=value\n', 'DUCTOR_HOME=""\n'):
            with self.subTest(content=content):
                if content is not None:
                    (self.repo / '.env').write_text(content)
                result = self.resolve(dict(self.environment, DUCTOR_HOME=''))
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, self.environment['HOME'] + '/.ductor\n')

    def test_key_only_no_shell_execution_or_variable_expansion(self):
        marker = self.repo / 'must-not-exist'
        literal = '$(touch ' + str(marker) + ')/$HOME'
        (self.repo / '.env').write_text('EVIL=$(touch ' + str(marker) + ')\nDUCTOR_HOME=' + json.dumps(literal) + '\n')
        result = self.resolve()
        self.assertEqual((result.returncode, result.stdout), (0, literal + '\n'))
        self.assertFalse(marker.exists())

    def test_invalid_quoted_value_fails_without_echoing_value(self):
        (self.repo / '.env').write_text('DUCTOR_HOME="DO_NOT_PRINT\n')
        result = self.resolve()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('DO_NOT_PRINT', result.stdout + result.stderr)

    def test_each_operator_uses_same_helper_from_script_repository(self):
        # Execute only each script's initialization prefix; never invoke maintenance/Docker.
        stops = {'login.sh': 'show_help() {', 'logout.sh': '# ── helpers',
                 'update.sh': '# 1. Check clean working tree'}
        for name, stop in stops.items():
            with self.subTest(script=name):
                source = (ROOT / 'scripts' / name).read_text().split(stop, 1)[0]
                variable = 'DUCTOR_HOME' if name == 'update.sh' else 'DUCTOR_HOME_HOST'
                probe = self.repo / 'scripts' / name
                probe.write_text(source + '\nprintf "%s\\n" "$' + variable + '"\n')
                for selected in ('repo', 'env', 'fallback'):
                    env = self.environment.copy()
                    path = self.repo / '.env'
                    if selected == 'fallback':
                        path.unlink(missing_ok=True)
                        expected = env['HOME'] + '/.ductor'
                    else:
                        path.write_text('DUCTOR_HOME="/repo home"\n')
                        expected = '/repo home'
                    if selected == 'env':
                        env['DUCTOR_HOME'] = '/env home'
                        expected = '/env home'
                    result = subprocess.run(['bash', str(probe)], cwd='/', env=env,
                                            capture_output=True, text=True)
                    self.assertEqual((result.returncode, result.stdout), (0, expected + '\n'))


if __name__ == '__main__':
    unittest.main()
