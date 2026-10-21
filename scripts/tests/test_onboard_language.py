"""Bundled languages and host locale defaults, without credentials or services."""
from __future__ import annotations

import importlib.util
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
SPEC = importlib.util.spec_from_file_location('onboard_language_test', ROOT / 'scripts/onboard.py')
ONBOARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ONBOARD)


class OnboardLanguageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ductorverify-language-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token = self.root / 'fake-token'
        self.token.write_text('123:NOT_A_REAL_TOKEN')
        environment = {k: v for k, v in os.environ.items()
                       if not k.startswith('ONBOARD_') and k not in
                       {'DUCTOR_HOME', 'LC_ALL', 'LC_MESSAGES', 'LANG'}}
        self.patch = patch.dict(os.environ, environment, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def args(self, *extra, interactive=False):
        return ONBOARD.parser().parse_args([
            *([] if interactive else ['--no-prompt']), '--home', str(self.root / 'home'),
            '--telegram-token-file', str(self.token), '--allowed-users', '123',
            '--tz', 'UTC', '--transport', 'none', '--no-api', '--no-webhook',
            '--bind', '127.0.0.1', *extra,
        ])

    def test_supported_languages_come_from_chat_directories(self):
        directory = self.root / 'i18n'
        for name in ('en', 'ko', 'zz'):
            (directory / name).mkdir(parents=True)
            (directory / name / 'chat.toml').touch()
        (directory / 'not-a-language').mkdir()
        self.assertEqual(ONBOARD.supported_languages(directory), ('en', 'ko', 'zz'))
        self.assertEqual(ONBOARD.detect_system_language({'LANG': 'zz_ZZ.UTF-8'}, directory), 'zz')

    def test_real_bundled_languages_are_supported(self):
        for language in ONBOARD.supported_languages():
            with self.subTest(language=language):
                self.assertEqual(ONBOARD.collect(self.args('--language', language))[1]['language'], language)

    def test_locale_priority_and_language_parts(self):
        cases = [
            ({'LC_ALL': 'ko_KR.UTF-8', 'LC_MESSAGES': 'de_DE', 'LANG': 'fr_FR'}, 'ko'),
            ({'LC_ALL': '', 'LC_MESSAGES': 'de_DE.UTF-8', 'LANG': 'ko_KR'}, 'de'),
            ({'LANG': 'pt_BR.UTF-8'}, 'pt'), ({'LANG': 'fr_FR@euro'}, 'fr'),
            ({'LANG': 'EN_us.UTF-8'}, 'en'), ({'LANG': 'ko-KR.UTF-8'}, 'ko'),
            ({'LANG': 'id'}, 'id'), ({}, 'en'),
            ({'LC_ALL': 'C.UTF-8', 'LANG': 'ko_KR.UTF-8'}, 'en'),
            ({'LC_MESSAGES': 'zh_CN.UTF-8', 'LANG': 'ko_KR.UTF-8'}, 'en'),
            ({'LANG': 'not-supported'}, 'en'),
        ]
        for environment, expected in cases:
            with self.subTest(environment=environment):
                self.assertEqual(ONBOARD.detect_system_language(environment), expected)

    def test_cli_then_onboard_env_then_locale_then_english(self):
        cases = [
            (['--language', 'ko'], {'ONBOARD_LANGUAGE': 'de', 'LC_ALL': 'fr_FR'}, 'ko'),
            ([], {'ONBOARD_LANGUAGE': 'de', 'LC_ALL': 'fr_FR'}, 'de'),
            ([], {'LC_ALL': 'fr_FR.UTF-8'}, 'fr'), ([], {'LANG': 'zh_CN'}, 'en'),
        ]
        for args, environment, expected in cases:
            with self.subTest(expected=expected), patch.dict(os.environ, environment):
                self.assertEqual(ONBOARD.collect(self.args(*args))[1]['language'], expected)

    def test_invalid_explicit_values_raise_instead_of_falling_back(self):
        for language in ('xx', 'ko_KR.UTF-8', 'KO', ''):
            with self.subTest(language=language):
                with self.assertRaisesRegex(ONBOARD.InputError, 'language must be one of'):
                    ONBOARD.collect(self.args('--language', language))
                with patch.dict(os.environ, {'ONBOARD_LANGUAGE': language}), self.assertRaisesRegex(
                    ONBOARD.InputError, 'language must be one of',
                ):
                    ONBOARD.collect(self.args())

    def test_interactive_detected_default_and_selected_value(self):
        ONBOARD.set_language('ko')  # the system language of this test
        label = ONBOARD.message('q_language', languages=','.join(ONBOARD.supported_languages()))
        ONBOARD.set_language('en')
        for answer, expected in (('', 'ko'), ('de', 'de')):
            with self.subTest(answer=answer), patch.dict(os.environ, {'LANG': 'ko_KR.UTF-8'}), patch.object(
                ONBOARD.sys.stdin, 'isatty', return_value=True,
            ), patch.object(ONBOARD.sys.stdout, 'isatty', return_value=True), patch.object(
                ONBOARD, 'ask', side_effect=lambda label, current, default='': current or answer or default,
            ) as ask:
                self.assertEqual(ONBOARD.collect(self.args(interactive=True))[1]['language'], expected)
                ask.assert_any_call(label, '', default='ko')

    def test_interactive_invalid_value_is_rejected(self):
        with patch.object(ONBOARD.sys.stdin, 'isatty', return_value=True), patch.object(
            ONBOARD.sys.stdout, 'isatty', return_value=True,
        ), patch.object(ONBOARD, 'ask', side_effect=lambda label, current, default='': current or 'bad-language'), self.assertRaisesRegex(
            ONBOARD.InputError, 'language must be one of',
        ):
            ONBOARD.collect(self.args(interactive=True))

    def test_explicit_language_skips_interactive_language_question(self):
        with patch.object(ONBOARD.sys.stdin, 'isatty', return_value=True), patch.object(
            ONBOARD.sys.stdout, 'isatty', return_value=True,
        ), patch.object(ONBOARD, 'ask', side_effect=lambda label, current, default='': current or default) as ask:
            self.assertEqual(ONBOARD.collect(self.args('--language', 'ko', interactive=True))[1]['language'], 'ko')
            ONBOARD.set_language('ko')
            label = ONBOARD.message('q_language', languages=','.join(ONBOARD.supported_languages()))
            ONBOARD.set_language('en')
            self.assertNotIn(label, [call.args[0] for call in ask.call_args_list])

    def test_noninteractive_runs_save_detected_or_explicit_language_and_redact(self):
        repo = self.root / 'repo'
        (repo / 'scripts').mkdir(parents=True)
        shutil.copy2(ROOT / 'scripts/onboard.py', repo / 'scripts/onboard.py')
        shutil.copy2(ROOT / 'config.example.json', repo / 'config.example.json')
        shutil.copytree(ROOT / 'ductor_bot/i18n', repo / 'ductor_bot/i18n')
        shutil.copytree(ROOT / 'scripts/onboard_i18n', repo / 'scripts/onboard_i18n')
        for name, options, environment, expected in [
            ('cli', ['--language', 'de'], {'ONBOARD_LANGUAGE': 'fr', 'LANG': 'ko_KR.UTF-8'}, 'de'),
            ('env', [], {'ONBOARD_LANGUAGE': 'fr', 'LANG': 'ko_KR.UTF-8'}, 'fr'),
            ('locale', [], {'LC_ALL': 'ko_KR.UTF-8'}, 'ko'),
            ('bad', ['--language', 'xx'], {}, None),
        ]:
            with self.subTest(name=name):
                home = self.root / name
                command = [sys.executable, str(repo / 'scripts/onboard.py'), '--no-prompt',
                           '--home', str(home), '--telegram-token-file', str(self.token),
                           '--allowed-users', '123', '--tz', 'UTC', '--force', *options]
                result = subprocess.run(command, env={**os.environ, **environment},
                                        capture_output=True, text=True)
                self.assertNotIn('123:NOT_A_REAL_TOKEN', result.stdout + result.stderr)
                if expected is None:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('language must be one of', result.stderr)
                    self.assertFalse(home.exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    saved = json.loads((home / 'config/config.json').read_text())
                    self.assertEqual(saved['language'], expected)
                    ONBOARD.set_language(expected)
                    line = ONBOARD.message('out_language', language=expected)
                    ONBOARD.set_language('en')
                    self.assertEqual(result.stdout.splitlines()[0], line)


if __name__ == '__main__':
    unittest.main()
