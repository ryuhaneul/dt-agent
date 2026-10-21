"""Onboarding token and completion contracts; all credentials are invented."""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('onboard_inputs_test', ROOT / 'scripts/onboard.py')
ONBOARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ONBOARD)
FAKE = '123:NOT_A_REAL_TOKEN-0'
# English output whatever the host locale is.
ENGLISH_ENV = {**os.environ, 'LC_ALL': 'C', 'LC_MESSAGES': 'C', 'LANG': 'C'}
ENGLISH_ENV.pop('ONBOARD_LANGUAGE', None)
ERROR = 'Telegram token must be digits:letters/digits/underscore/hyphen, without spaces'


class OnboardInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ductorverify-inputs-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token = self.root / 'token'
        self.token.write_text(FAKE)
        environment = {k: v for k, v in os.environ.items()
                       if not k.startswith('ONBOARD_') and k not in {'DUCTOR_HOME', 'LC_ALL', 'LC_MESSAGES', 'LANG'}}
        self.env_patch = patch.dict(os.environ, environment, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def collect(self, value, *, interactive):
        self.token.write_text(value)
        arguments = ['--home', str(self.root / 'home'), '--allowed-users', '123',
                     '--transport', 'none', '--tz', 'UTC', '--language', 'en', '--no-api', '--no-webhook',
                     '--bind', '127.0.0.1']
        if not interactive:
            arguments += ['--no-prompt', '--telegram-token-file', str(self.token)]
        with patch.object(ONBOARD.sys.stdin, 'isatty', return_value=interactive), patch.object(
            ONBOARD.sys.stdout, 'isatty', return_value=interactive,
        ), patch.object(ONBOARD.getpass, 'getpass', return_value=value):
            return ONBOARD.collect(ONBOARD.parser().parse_args(arguments))

    def test_valid_token_and_surrounding_whitespace_both_input_routes(self):
        for interactive in (False, True):
            for value in (FAKE, ' \t' + FAKE, FAKE + ' \n', ' ' + FAKE + ' '):
                with self.subTest(interactive=interactive, value=value):
                    _, config, _, _ = self.collect(value, interactive=interactive)
                    self.assertEqual(config['telegram_token'], FAKE)

    def test_invalid_tokens_both_routes_without_value_in_error(self):
        for interactive in (False, True):
            for value in ('123:BAD TOKEN', '123:BAD\tTOKEN', '123BADTOKEN',
                          'abc:BADTOKEN', '123:', ':BADTOKEN', '123:BAD.TOKEN',
                          '123:BAD:SECOND', '１２３:BADTOKEN'):
                with self.subTest(interactive=interactive, value=value):
                    with self.assertRaises(ONBOARD.InputError) as raised:
                        self.collect(value, interactive=interactive)
                    self.assertEqual(str(raised.exception), ERROR)
                    self.assertNotIn(value, str(raised.exception))

    def test_other_secrets_trim_without_token_validation(self):
        with patch.object(ONBOARD.getpass, 'getpass', return_value='  any secret!  '):
            self.assertEqual(ONBOARD.ask('secret', '', secret=True), 'any secret!')
        self.token.write_text('  any secret! \n')
        self.assertEqual(ONBOARD.read_secret(str(self.token), 'label_telegram_token'), 'any secret!')

    def test_noninteractive_run_saved_provider_and_exact_next_steps(self):
        repo = self.root / 'repo'
        (repo / 'scripts').mkdir(parents=True)
        shutil.copy2(ROOT / 'scripts/onboard.py', repo / 'scripts/onboard.py')
        shutil.copy2(ROOT / 'config.example.json', repo / 'config.example.json')
        shutil.copytree(ROOT / 'ductor_bot/i18n', repo / 'ductor_bot/i18n')
        shutil.copytree(ROOT / 'scripts/onboard_i18n', repo / 'scripts/onboard_i18n')
        home = self.root / 'home'
        command = [sys.executable, str(repo / 'scripts/onboard.py'), '--no-prompt',
                   '--home', str(home), '--telegram-token-file', str(self.token),
                   '--allowed-users', '123', '--tz', 'UTC', '--language', 'en', '--transport', 'none']
        result = subprocess.run(command, capture_output=True, text=True, env=ENGLISH_ENV)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = ('Bot language: en\n'
                    'Onboarding configuration ready. Next steps:\n'
                    '  docker compose up -d --build\n'
                    '  docker compose logs --tail=100 ductor-bot\n'
                    '  Check for Bot online before provider login.\n'
                    '  ./scripts/login.sh claude\n'
                    '  ./scripts/status.sh\n'
                    'To reset configuration, delete DUCTOR_HOME/config/config.json and rerun onboarding.\n')
        self.assertEqual(result.stdout, expected)
        self.assertNotIn(FAKE, result.stdout + result.stderr)
        self.assertEqual(json.loads((home / 'config/config.json').read_text())['telegram_token'], FAKE)
        config = home / 'config/config.json'
        saved = json.loads(config.read_text())
        saved['provider'] = 'codex'
        config.write_text(json.dumps(saved))
        result = subprocess.run(command, capture_output=True, text=True, env=ENGLISH_ENV)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('./scripts/login.sh codex', result.stdout)

    def test_invalid_file_run_is_nonzero_before_any_writes_and_redacts(self):
        shutil.copy2(ROOT / 'config.example.json', self.root / 'config.example.json')
        self.token.write_text('123:DO_NOT_PRINT ME')
        with patch.object(ONBOARD, 'REPO', self.root), patch.object(sys, 'argv', [
            'onboard', '--no-prompt', '--home', str(self.root / 'home'),
            '--telegram-token-file', str(self.token), '--allowed-users', '123',
        ]), patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(ONBOARD.main(), 2)
            self.assertIn(ERROR, error.getvalue())
            self.assertNotIn('DO_NOT_PRINT', error.getvalue())
        self.assertFalse((self.root / '.env').exists())
        self.assertFalse((self.root / 'home').exists())


if __name__ == '__main__':
    unittest.main()
