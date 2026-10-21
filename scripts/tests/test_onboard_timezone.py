"""Host timezone and retired SSH input contracts; no Docker or real credentials."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('onboard_timezone_test', ROOT / 'scripts/onboard.py')
ONBOARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ONBOARD)


class OnboardTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ductorverify-onboard-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.timezone = self.root / 'timezone'
        self.localtime = self.root / 'localtime'
        self.token = self.root / 'fake-token'
        self.token.write_text('123:NOT_A_REAL_TOKEN\n')
        environment = dict(os.environ)
        for key in tuple(environment):
            if key.startswith('ONBOARD_') or key in {'TZ', 'DUCTOR_HOME', 'LC_ALL', 'LC_MESSAGES', 'LANG'}:
                environment.pop(key)
        self.environment = environment
        self.environment_patch = patch.dict(os.environ, environment, clear=True)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)

    def detect(self, tz=''):
        return ONBOARD.detect_system_timezone(tz, self.timezone, self.localtime)

    def arguments(self, *extra, interactive=False):
        return ONBOARD.parser().parse_args([
            *([] if interactive else ['--no-prompt']),
            '--home', str(self.root / 'home'), '--telegram-token-file', str(self.token),
            '--allowed-users', '123', '--transport', 'none',
            *([] if '--api' in extra else ['--no-api']), '--no-webhook',
            '--bind', '127.0.0.1', *extra,
        ])

    def test_tz_environment_wins(self):
        self.timezone.write_text('Asia/Tokyo\n')
        self.localtime.symlink_to(self.root / 'zoneinfo/Europe/Paris')
        with patch.dict(os.environ, {'TZ': 'America/New_York'}):
            self.assertEqual(ONBOARD.detect_system_timezone(
                timezone_file=self.timezone, localtime_file=self.localtime,
            ), 'America/New_York')

    def test_timezone_file_after_invalid_tz(self):
        self.timezone.write_text('Asia/Tokyo\n')
        self.localtime.symlink_to(self.root / 'zoneinfo/Europe/Paris')
        self.assertEqual(self.detect('not/a/timezone'), 'Asia/Tokyo')

    def test_localtime_symlink_after_invalid_candidates(self):
        self.timezone.write_text('not/a/timezone\n')
        self.localtime.symlink_to(self.root / 'zoneinfo/Europe/Paris')
        self.assertEqual(self.detect('/not-an-IANA-name'), 'Europe/Paris')

    def test_unreadable_timezone_file_uses_symlink(self):
        self.timezone.mkdir()
        self.localtime.symlink_to(self.root / 'zoneinfo/Asia/Seoul')
        self.assertEqual(self.detect(), 'Asia/Seoul')

    def test_missing_candidates_fall_back_to_utc(self):
        self.assertEqual(self.detect('invalid'), 'UTC')

    def test_invalid_symlink_and_regular_localtime_fall_back(self):
        self.timezone.write_text('invalid')
        for target in (self.root / 'unrelated', self.root / 'zoneinfo/invalid'):
            with self.subTest(target=target):
                self.localtime.symlink_to(target)
                self.assertEqual(self.detect('invalid'), 'UTC')
                self.localtime.unlink()
        self.localtime.write_bytes(b'not a symlink')
        self.assertEqual(self.detect(), 'UTC')

    def test_cli_then_onboard_env_then_system_timezone(self):
        for cli, env, expected in [
            (['--tz', 'Europe/Paris'], {'ONBOARD_TZ': 'Asia/Tokyo'}, 'Europe/Paris'),
            ([], {'ONBOARD_TZ': 'Asia/Tokyo'}, 'Asia/Tokyo'),
            ([], {}, 'America/New_York'),
        ]:
            with self.subTest(cli=cli, env=env), patch.dict(os.environ, env), patch.object(
                ONBOARD, 'detect_system_timezone', return_value='America/New_York',
            ):
                _, config, extras, _ = ONBOARD.collect(self.arguments(*cli))
                self.assertEqual(config['user_timezone'], expected)
                self.assertEqual(extras, '')

    def test_explicit_invalid_timezone_is_not_replaced(self):
        with self.assertRaisesRegex(ONBOARD.InputError, 'timezone is not recognized'):
            ONBOARD.collect(self.arguments('--tz', 'invalid'))
        with patch.dict(os.environ, {'ONBOARD_TZ': 'invalid'}), self.assertRaisesRegex(
            ONBOARD.InputError, 'timezone is not recognized',
        ):
            ONBOARD.collect(self.arguments())

    def test_interactive_default_is_detected_and_ssh_question_removed(self):
        def answer(label, current, *, default='', **kwargs):
            return current or default
        with patch.object(ONBOARD, 'detect_system_timezone', return_value='Asia/Tokyo'), patch.object(
            ONBOARD.sys.stdin, 'isatty', return_value=True,
        ), patch.object(ONBOARD.sys.stdout, 'isatty', return_value=True), patch.object(
            ONBOARD, 'ask', side_effect=answer,
        ) as ask:
            _, config, extras, _ = ONBOARD.collect(self.arguments(interactive=True))
        self.assertEqual(config['user_timezone'], 'Asia/Tokyo')
        self.assertEqual(extras, '')
        ask.assert_any_call('4. Timezone', '', default='Asia/Tokyo')
        self.assertFalse(any('SSH' in call.args[0] for call in ask.call_args_list))

    def test_removed_ssh_cli_inputs_fail_before_writing(self):
        for option in ('--ssh', '--no-ssh', '--ssh=true'):
            with self.subTest(option=option):
                result = subprocess.run([
                    sys.executable, str(ROOT / 'scripts/onboard.py'), '--no-prompt',
                    '--home', str(self.root / 'home'), option,
                ], env=self.environment, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(ONBOARD.SSH_REMOVED, result.stderr)
                self.assertNotIn('configuration ready', result.stdout)
                self.assertFalse((self.root / 'home').exists())
        self.assertNotIn('--ssh', ONBOARD.parser().format_help())
        self.assertNotIn('--no-ssh', ONBOARD.parser().format_help())

    def test_removed_ssh_environment_rejected_even_when_false_or_empty(self):
        for value in ('true', 'false', ''):
            with self.subTest(value=value), patch.dict(os.environ, {'ONBOARD_SSH': value}):
                with self.assertRaisesRegex(ONBOARD.InputError, 'ONBOARD_SSH: ssh extra was removed'):
                    ONBOARD.collect(self.arguments())
                result = subprocess.run([
                    sys.executable, str(ROOT / 'scripts/onboard.py'), '--no-prompt',
                ], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(ONBOARD.SSH_REMOVED, result.stderr)
        self.assertFalse((self.root / 'home').exists())

    def test_api_extra_has_no_ssh(self):
        _, _, extras, _ = ONBOARD.collect(self.arguments('--api'))
        self.assertEqual(extras, 'api')
        self.assertNotIn('ssh', extras.split(','))


if __name__ == '__main__':
    unittest.main()
