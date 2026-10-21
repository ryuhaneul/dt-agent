"""Saved onboarding settings survive reruns; credentials are synthetic."""
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
SPEC = importlib.util.spec_from_file_location('onboard_preservation_test', ROOT / 'scripts/onboard.py')
ONBOARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ONBOARD)


class OnboardPreservationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ductorverify-preserve-')
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name) / 'repo'
        (self.repo / 'scripts').mkdir(parents=True)
        shutil.copy2(ROOT / 'scripts/onboard.py', self.repo / 'scripts/onboard.py')
        shutil.copy2(ROOT / 'config.example.json', self.repo / 'config.example.json')
        shutil.copytree(ROOT / 'ductor_bot/i18n', self.repo / 'ductor_bot/i18n')
        shutil.copytree(ROOT / 'scripts/onboard_i18n', self.repo / 'scripts/onboard_i18n')
        self.home = Path(temp.name) / 'home'
        self.path = self.home / 'config/config.json'
        self.path.parent.mkdir(parents=True)
        self.saved = {
            'telegram_token': '123:SYNTHETIC', 'allowed_user_ids': [321],
            'allowed_group_ids': [-10042], 'language': 'ko', 'user_timezone': 'Asia/Seoul',
            'provider': 'codex', 'model': 'custom-model', 'transport': 'telegram',
            'transports': ['telegram'], 'custom': {'keep': [1, 2]},
            'heartbeat': {'enabled': False, 'interval_minutes': 77, 'quiet_start': 2,
                          'group_targets': [{'chat_id': -10042, 'topic_id': 9}], 'custom': 42},
            'api': {'enabled': True, 'token': 'SYNTHETIC_API', 'port': 9001, 'chat_id': 444},
            'webhooks': {'enabled': True, 'token': 'SYNTHETIC_WEBHOOK', 'host': '192.0.2.1'},
        }
        self.save()
        environment = {k: v for k, v in os.environ.items()
                       if not k.startswith(('ONBOARD_', 'DUCTOR_'))
                       and k not in {'LC_ALL', 'LC_MESSAGES', 'LANG'}}
        env_patch = patch.dict(os.environ, environment, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        repo_patch = patch.object(ONBOARD, 'REPO', self.repo)
        repo_patch.start()
        self.addCleanup(repo_patch.stop)

    def save(self):
        self.path.write_text(json.dumps(self.saved))

    def collect(self, *options, interactive=False):
        args = ONBOARD.parser().parse_args(['--home', str(self.home), '--force',
                                          *([] if interactive else ['--no-prompt']), *options])
        with patch.object(sys.stdin, 'isatty', return_value=interactive), patch.object(
            sys.stdout, 'isatty', return_value=interactive,
        ):
            return ONBOARD.collect(args)[1]

    def assert_saved(self, config, saved):
        for key, value in saved.items():
            if isinstance(value, dict):
                self.assert_saved(config[key], value)
            else:
                self.assertEqual(config[key], value, key)

    def test_force_rerun_preserves_values_and_fills_missing_defaults(self):
        result = subprocess.run([sys.executable, str(self.repo / 'scripts/onboard.py'),
                                 '--no-prompt', '--home', str(self.home), '--force'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(self.path.read_text())
        self.assert_saved(config, self.saved)
        self.assertTrue(config['heartbeat']['auto_targets'])
        self.assertEqual(config['api']['host'], '0.0.0.0')
        self.assertEqual(config['log_level'], 'INFO')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        for secret in ('123:SYNTHETIC', 'SYNTHETIC_API', 'SYNTHETIC_WEBHOOK'):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_explicit_options_change_only_selected_config_values(self):
        config = self.collect('--language', 'en', '--allowed-users', '999',
                              '--allowed-groups=', '--no-api')
        expected = dict(self.saved, language='en', allowed_user_ids=[999], allowed_group_ids=[])
        expected['api'] = dict(self.saved['api'], enabled=False)
        self.assert_saved(config, expected)

    def test_environment_groups_and_cli_precedence(self):
        with patch.dict(os.environ, {'ONBOARD_ALLOWED_GROUPS': '-10099', 'ONBOARD_LANGUAGE': 'fr'}):
            self.assertEqual(self.collect()['allowed_group_ids'], [-10099])
            self.assertEqual(self.collect()['language'], 'fr')
            self.assertEqual(self.collect('--allowed-groups=-10077')['allowed_group_ids'], [-10077])

    def test_interactive_enter_keeps_saved_defaults_and_hides_token(self):
        with patch('builtins.input', return_value='') as inputs, patch.object(
            ONBOARD.getpass, 'getpass', return_value='',
        ) as hidden:
            config = self.collect(interactive=True)
        self.assert_saved(config, self.saved)
        prompts = '\n'.join(call.args[0] for call in inputs.call_args_list)
        self.assertIn('[321]', prompts)
        self.assertIn('[Asia/Seoul]', prompts)
        self.assertIn('[ko]', prompts)
        self.assertIn('[y]', prompts)
        self.assertNotIn('group', prompts.lower())
        self.assertNotIn(self.saved['telegram_token'], hidden.call_args.args[0])

    def test_interactive_answers_replace_only_asked_fields(self):
        answers = iter(['888', 'UTC', 'en', '', 'N', 'N', '127.0.0.1'])
        with patch('builtins.input', side_effect=lambda _: next(answers)), patch.object(
            ONBOARD.getpass, 'getpass', return_value='456:SYNTHETIC_NEW',
        ):
            config = self.collect(interactive=True)
        self.assertEqual(config['allowed_user_ids'], [888])
        self.assertEqual(config['telegram_token'], '456:SYNTHETIC_NEW')
        self.assertEqual(config['allowed_group_ids'], [-10042])
        self.assertEqual(config['heartbeat']['interval_minutes'], 77)

    def test_saved_transport_credentials_and_custom_options_survive(self):
        self.saved['transports'] = ['telegram', 'matrix', 'slack']
        self.saved['matrix'] = {
            'homeserver': 'https://matrix.invalid', 'user_id': '@bot:invalid',
            'password': 'SYNTHETIC_MATRIX', 'allowed_rooms': ['!saved:invalid'],
            'store_path': 'custom-store', 'custom': 7,
        }
        self.saved['slack'] = {
            'bot_token': 'SYNTHETIC_SLACK_BOT', 'app_token': 'SYNTHETIC_SLACK_APP',
            'allowed_channels': ['C_SAVED'], 'custom': 8,
        }
        self.save()
        self.assert_saved(self.collect(), self.saved)
        self.assert_saved(self.collect('--transport', 'none'), dict(self.saved, transports=['telegram']))

    def test_interactive_enter_does_not_add_telegram_to_matrix_only_config(self):
        self.saved['transport'] = 'matrix'
        self.saved['transports'] = ['matrix']
        self.saved['matrix'] = {
            'homeserver': 'https://matrix.invalid', 'user_id': '@bot:invalid',
            'password': 'SYNTHETIC_MATRIX', 'allowed_rooms': ['!saved:invalid'],
        }
        self.save()
        with patch('builtins.input', return_value=''), patch.object(
            ONBOARD.getpass, 'getpass', return_value='',
        ):
            self.assert_saved(self.collect(interactive=True), self.saved)

    def test_missing_defaults_never_replace_false_empty_or_custom_values(self):
        config = {'flag': False, 'items': [], 'text': '', 'nil': None,
                  'nested': {'port': 9001, 'custom': [1]}}
        defaults = {'flag': True, 'items': [2], 'text': 'default', 'nil': 'default',
                    'nested': {'port': 8741, 'new': True}, 'added': 0}
        ONBOARD.fill_missing(config, defaults)
        self.assertEqual(config, {'flag': False, 'items': [], 'text': '', 'nil': None,
                                 'nested': {'port': 9001, 'custom': [1], 'new': True}, 'added': 0})

    def test_unreadable_saved_json_error_does_not_repeat_contents(self):
        self.path.write_text('{SYNTHETIC_PRIVATE_INVALID')
        with self.assertRaises(ONBOARD.InputError) as raised:
            self.collect()
        self.assertEqual(str(raised.exception), 'existing config.json must be a readable JSON object')

    def test_new_installation_still_requires_token_and_users(self):
        self.path.unlink()
        with self.assertRaisesRegex(ONBOARD.InputError, 'Telegram token file is required'):
            self.collect()
        token_file = self.home / 'token'
        token_file.write_text('123:SYNTHETIC')
        config = self.collect('--telegram-token-file', str(token_file), '--allowed-users', '321')
        self.assertEqual(config['allowed_group_ids'], [])
        self.assertEqual(config['heartbeat']['group_targets'], [])
        self.assertFalse(config['webhooks']['enabled'])
        self.assertEqual(config['matrix']['password'], '')


if __name__ == '__main__':
    unittest.main()
