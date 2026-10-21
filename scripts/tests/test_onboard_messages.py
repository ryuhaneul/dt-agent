"""Onboarding speaks the chosen language; the message files stay in step with English."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('onboard_messages_test', ROOT / 'scripts/onboard.py')
ONBOARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ONBOARD)
MESSAGES = ROOT / 'scripts/onboard_i18n'
LANGUAGES = ('de', 'en', 'es', 'fr', 'id', 'ko', 'nl', 'pt', 'ru')
PLACEHOLDER = re.compile(r'\{(\w+)\}')


def catalog(language):
    return json.loads((MESSAGES / (language + '.json')).read_text(encoding='utf-8'))


def said(lang, key, /, **values):
    """The text *key* has in *lang*, as onboarding prints it (the module state is not touched)."""
    text = catalog(lang)[key]
    return text.format(**values) if values else text


class MessageFileTests(unittest.TestCase):
    def test_every_bundled_language_has_a_message_file(self):
        self.assertEqual(sorted(p.stem for p in MESSAGES.glob('*.json')), list(LANGUAGES))
        self.assertEqual(tuple(ONBOARD.supported_languages()), LANGUAGES)

    def test_keys_and_placeholders_match_english(self):
        english = catalog('en')
        for language in LANGUAGES:
            data = catalog(language)
            with self.subTest(language=language):
                self.assertEqual(list(data), list(english))
                for key, text in data.items():
                    self.assertTrue(text.strip(), key)
                    self.assertEqual(sorted(PLACEHOLDER.findall(text)),
                                     sorted(PLACEHOLDER.findall(english[key])), key)

    def test_only_korean_file_contains_hangul(self):
        for language in LANGUAGES:
            text = (MESSAGES / (language + '.json')).read_text(encoding='utf-8')
            with self.subTest(language=language):
                self.assertEqual(bool(re.search('[\uac00-\ud7a3]', text)), language == 'ko')

    def test_commands_options_and_paths_are_not_translated(self):
        for language in LANGUAGES:
            data = catalog(language)
            with self.subTest(language=language):
                self.assertIn('DUCTOR_HOME/config/config.json', data['out_reset'])
                self.assertIn('--force', data['err_symlink'])
                self.assertIn('(y/N)', data['q_api'] + data['q_webhook'])
                self.assertTrue(data['q_home'].endswith('DUCTOR_HOME'))

    def test_unknown_language_and_missing_key_fall_back_to_english(self):
        ONBOARD.set_language('zz')
        self.assertEqual(ONBOARD.message('err_bind'), catalog('en')['err_bind'])
        ONBOARD.set_language('ko')
        try:
            with patch.dict(ONBOARD._catalogs, {'ko': {}}):
                self.assertEqual(ONBOARD.message('err_bind'), catalog('en')['err_bind'])
        finally:
            ONBOARD.set_language('en')


class OnboardLanguageSpeechTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ductorverify-speech-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        (self.repo / 'scripts').mkdir(parents=True)
        shutil.copy2(ROOT / 'scripts/onboard.py', self.repo / 'scripts/onboard.py')
        shutil.copy2(ROOT / 'config.example.json', self.repo / 'config.example.json')
        shutil.copytree(ROOT / 'ductor_bot/i18n', self.repo / 'ductor_bot/i18n')
        shutil.copytree(MESSAGES, self.repo / 'scripts/onboard_i18n')
        self.token = self.root / 'token'
        self.token.write_text('123:NOT_A_REAL_TOKEN')
        self.home = self.root / 'home'
        self.base = {k: v for k, v in os.environ.items()
                     if not k.startswith(('ONBOARD_', 'DUCTOR_'))
                     and k not in {'LC_ALL', 'LC_MESSAGES', 'LANG'}}
        ONBOARD.set_language('en')
        self.addCleanup(ONBOARD.set_language, 'en')

    def run_onboard(self, *options, environment=None, home=None, token=True):
        command = [sys.executable, str(self.repo / 'scripts/onboard.py'), '--no-prompt',
                   '--home', str(home or self.home), '--allowed-users', '123',
                   '--tz', 'UTC', '--transport', 'none', '--force', *options]
        if token:
            command += ['--telegram-token-file', str(self.token)]
        return subprocess.run(command, env={**self.base, **(environment or {})},
                              capture_output=True, text=True)

    def first_line(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()[0]

    def test_language_sources_in_order(self):
        cases = [
            ('option', ['--language', 'ko'], {}, 'ko'),
            ('option=', ['--language=ko'], {}, 'ko'),
            ('option beats env and locale', ['--language', 'ko'],
             {'ONBOARD_LANGUAGE': 'de', 'LC_ALL': 'fr_FR.UTF-8'}, 'ko'),
            ('env', [], {'ONBOARD_LANGUAGE': 'ru', 'LC_ALL': 'fr_FR.UTF-8'}, 'ru'),
            ('locale', [], {'LC_ALL': 'ko_KR.UTF-8'}, 'ko'),
            ('LC_ALL first', [], {'LC_ALL': 'es_ES.UTF-8', 'LANG': 'ko_KR.UTF-8'}, 'es'),
            ('C locale', [], {'LC_ALL': 'C', 'LANG': 'ko_KR.UTF-8'}, 'en'),
            ('POSIX locale', [], {'LANG': 'POSIX'}, 'en'),
            ('no evidence', [], {}, 'en'),
        ]
        for name, options, environment, expected in cases:
            with self.subTest(name=name):
                result = self.run_onboard(*options, environment=environment,
                                          home=self.root / name.replace(' ', '-'))
                self.assertEqual(self.first_line(result),
                                 said(expected, 'out_language', language=expected))

    def test_saved_language_is_used_on_rerun_and_the_option_still_wins(self):
        self.assertEqual(self.first_line(self.run_onboard('--language', 'ko')),
                         said('ko', 'out_language', language='ko'))
        rerun = self.run_onboard(environment={'LC_ALL': 'fr_FR.UTF-8'})
        self.assertEqual(self.first_line(rerun), said('ko', 'out_language', language='ko'))
        self.assertEqual(rerun.stdout.splitlines()[1], said('ko', 'out_ready'))
        changed = self.run_onboard('--language', 'de', environment={'LC_ALL': 'fr_FR.UTF-8'})
        self.assertEqual(self.first_line(changed), said('de', 'out_language', language='de'))

    def test_translated_notice_keeps_commands_and_hides_secrets(self):
        result = self.run_onboard('--language', 'ko')
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ('  docker compose up -d --build', '  docker compose logs --tail=100 ductor-bot',
                        '  ./scripts/login.sh claude', '  ./scripts/status.sh'):
            self.assertIn(command, result.stdout.splitlines())
        self.assertIn(said('ko', 'out_check_online'), result.stdout)
        self.assertNotIn('123:NOT_A_REAL_TOKEN', result.stdout + result.stderr)

    def test_unsupported_explicit_language_is_an_error_in_the_next_language(self):
        listing = ', '.join(LANGUAGES)
        for options, environment, speaker in [
            (['--language', 'xx'], {}, 'en'),
            (['--language', 'xx'], {'LC_ALL': 'de_DE.UTF-8'}, 'de'),
            (['--language=xx'], {'LC_ALL': 'ko_KR.UTF-8'}, 'ko'),
            ([], {'ONBOARD_LANGUAGE': 'xx', 'LC_ALL': 'fr_FR.UTF-8'}, 'fr'),
        ]:
            with self.subTest(options=options, environment=environment):
                home = self.root / ('bad-' + speaker + str(len(options)))
                result = self.run_onboard(*options, environment=environment, home=home)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr.strip(),
                                 'onboard: ' + said(speaker, 'err_language', languages=listing))
                self.assertFalse(home.exists())

    def test_input_errors_are_in_the_chosen_language_and_write_nothing(self):
        self.token.write_text('123:BAD TOKEN')
        result = self.run_onboard('--language', 'ko')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), 'onboard: ' + said('ko', 'err_token_format'))
        self.assertNotIn('BAD TOKEN', result.stdout + result.stderr)
        self.assertFalse(self.home.exists())
        missing = self.run_onboard('--language', 'es', token=False, home=self.root / 'empty')
        self.assertEqual(missing.stderr.strip(), 'onboard: ' + said('es', 'err_token_required'))

    def test_removed_ssh_option_is_reported_in_the_environment_language(self):
        result = subprocess.run(
            [sys.executable, str(self.repo / 'scripts/onboard.py'), '--no-prompt', '--ssh'],
            env={**self.base, 'ONBOARD_LANGUAGE': 'pt'}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), 'onboard: ' + said('pt', 'err_ssh_removed'))

    def test_existing_file_warning_is_translated(self):
        self.assertEqual(self.run_onboard('--language', 'en').returncode, 0)
        env = self.repo / '.env'
        env.chmod(0o644)
        command = [sys.executable, str(self.repo / 'scripts/onboard.py'), '--no-prompt',
                   '--home', str(self.home), '--allowed-users', '123', '--tz', 'UTC',
                   '--transport', 'none', '--telegram-token-file', str(self.token),
                   '--language', 'de']
        result = subprocess.run(command, env=self.base, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(said('de', 'warn_exists', path=env) + said('de', 'warn_perm'), result.stderr)


class InteractiveLanguageSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ductorverify-switch-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token = self.root / 'token'
        self.token.write_text('123:NOT_A_REAL_TOKEN')
        environment = {k: v for k, v in os.environ.items()
                       if not k.startswith(('ONBOARD_', 'DUCTOR_'))
                       and k not in {'LC_ALL', 'LC_MESSAGES', 'LANG'}}
        patcher = patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(ONBOARD.set_language, 'en')

    def test_answering_the_language_question_changes_the_later_texts(self):
        asked = []
        languages = ','.join(ONBOARD.supported_languages())

        def answer(label, current, default='', secret=False):
            asked.append(label)
            if label == said('en', 'q_language', languages=languages):
                return 'ko'
            return current or default

        args = ONBOARD.parser().parse_args([
            '--home', str(self.root / 'home'), '--telegram-token-file', str(self.token),
            '--allowed-users', '123', '--tz', 'UTC', '--transport', 'none', '--bind', '127.0.0.1'])
        with patch.object(ONBOARD.sys.stdin, 'isatty', return_value=True), patch.object(
            ONBOARD.sys.stdout, 'isatty', return_value=True,
        ), patch.object(ONBOARD, 'ask', side_effect=answer):
            _, config, _, _ = ONBOARD.collect(args)
        self.assertEqual(config['language'], 'ko')
        position = asked.index(said('en', 'q_language', languages=languages))
        self.assertEqual(asked[position + 1:], [said('ko', 'q_api'), said('ko', 'q_webhook')])
        self.assertEqual(said('ko', 'q_api')[:2], '7.')

    def test_texts_before_the_language_question_use_the_system_language(self):
        asked = []
        with patch.dict(os.environ, {'LC_ALL': 'de_DE.UTF-8'}):
            args = ONBOARD.parser().parse_args([
                '--telegram-token-file', str(self.token), '--allowed-users', '123',
                '--tz', 'UTC', '--transport', 'none', '--bind', '127.0.0.1'])
            with patch.object(ONBOARD.sys.stdin, 'isatty', return_value=True), patch.object(
                ONBOARD.sys.stdout, 'isatty', return_value=True,
            ), patch.object(ONBOARD, 'ask', side_effect=lambda label, current, default='', secret=False: (
                asked.append(label) or str(self.root / 'home') if label == said('de', 'q_home') else current or default)):
                ONBOARD.collect(args)
        self.assertEqual(asked[0], said('de', 'q_home'))


class FirstErrorsAndHelpTests(unittest.TestCase):
    """The errors argparse raises before any option is read, and --help, use the chosen language."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ductorverify-first-errors-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.base = {k: v for k, v in os.environ.items()
                     if not k.startswith(('ONBOARD_', 'DUCTOR_', 'PYTHONIOENCODING'))
                     and k not in {'LC_ALL', 'LC_MESSAGES', 'LANG'}}
        self.base.update(LC_ALL='C', HOME=str(self.root))

    def run_onboard(self, *arguments, environment=None):
        return subprocess.run(
            [sys.executable, str(ROOT / 'scripts/onboard.py'), *arguments],
            env={**self.base, **(environment or {})}, capture_output=True)

    def lines(self, data):
        return data.decode('utf-8').splitlines()

    def test_every_kind_of_command_line_error_is_in_the_chosen_language(self):
        samples = (
            ('--ssh', 'err_ssh_removed', {}),
            ('--unknown', 'err_arg_unrecognized', {'arguments': '--unknown'}),
            ('--tz', 'err_arg_expected', {'option': '--tz'}),
            ('--transport=bogus', 'err_arg_choice',
             {'option': '--transport', 'value': "'bogus'", 'choices': ''}),
            ('--no-prompt=x', 'err_arg_ignored', {'option': '--no-prompt', 'value': "'x'"}),
        )
        for language in ('ko', 'de'):
            for argument, key, values in samples:
                with self.subTest(language=language, argument=argument):
                    result = self.run_onboard('--language=' + language, '--home', str(self.home),
                                              argument)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, b'')
                    expected = 'onboard: ' + said(language, key, **values)
                    last = self.lines(result.stderr)[-1]
                    if key == 'err_arg_choice':
                        expected = expected[:expected.index('(')]
                        self.assertIn("'none', 'matrix', 'slack'", last)
                    self.assertEqual(last[:len(expected)] if key == 'err_arg_choice' else last, expected)
                    if key != 'err_ssh_removed':
                        self.assertTrue(self.lines(result.stderr)[0].startswith(
                            said(language, 'usage_prefix')))
                    self.assertFalse(self.home.exists())

    def test_arguments_with_line_breaks_are_kept_and_the_error_is_translated(self):
        samples = (
            ('--x\ny', 'err_arg_unrecognized', '--x\ny'),
            ('extra\nargument', 'err_arg_unrecognized', 'extra\nargument'),
        )
        for language in ('ko', 'de', 'fr'):
            for argument, key, shown in samples:
                with self.subTest(language=language, argument=argument):
                    result = self.run_onboard('--language=' + language, '--home', str(self.home),
                                              argument)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, b'')
                    text = result.stderr.decode('utf-8')
                    self.assertTrue(text.endswith(
                        'onboard: ' + said(language, key, arguments=shown) + '\n'), text[-200:])
                    for english in ('unrecognized', 'arguments:', 'error:'):
                        self.assertNotIn(english, text)
                    self.assertFalse(self.home.exists())

    def test_unknown_error_kind_is_generic_and_keeps_only_what_was_typed(self):
        with patch.object(ONBOARD.sys, 'argv', ['onboard.py', '--language=de', 'a b']):
            ONBOARD.set_language('de')
            try:
                text = ONBOARD.argument_error('some new argparse wording')
            finally:
                ONBOARD.set_language('en')
        self.assertEqual(text, said('de', 'err_arg_invalid', arguments='--language=de a b'))
        self.assertNotIn('argparse', text)

    def test_abbreviated_option_is_an_unknown_argument_in_the_decided_language(self):
        for language in ('ko', 'de'):
            for abbreviation in ('--lang=' + language, '--ho=/nowhere'):
                with self.subTest(language=language, abbreviation=abbreviation):
                    result = self.run_onboard(abbreviation, '--home', str(self.home),
                                              environment={'ONBOARD_LANGUAGE': language})
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, b'')
                    self.assertEqual(self.lines(result.stderr)[-1], 'onboard: ' + said(
                        language, 'err_arg_unrecognized', arguments=abbreviation))
                    self.assertFalse(self.home.exists())

    def test_full_option_names_still_work(self):
        result = self.run_onboard('--language=de', '--help')
        self.assertEqual(result.returncode, 0)

    def test_mutually_exclusive_options_error_names_both_options(self):
        result = self.run_onboard('--language=de', '--api', '--no-api')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.lines(result.stderr)[-1], 'onboard: ' + said(
            'de', 'err_arg_conflict', option='--no-api', other='--api'))

    def test_help_text_is_in_the_chosen_language(self):
        for language in ('ko', 'de'):
            with self.subTest(language=language):
                result = self.run_onboard('--language=' + language, '--help')
                self.assertEqual(result.returncode, 0)
                shown = ' '.join(result.stdout.decode('utf-8').split())
                for key in ('usage_prefix', 'help_description', 'help_options', 'help_help',
                            'help_language'):
                    self.assertIn(' '.join(said(language, key).split()), shown, key)
                for english in ('usage:', 'options:', 'show this help', 'Prepare a runnable'):
                    self.assertNotIn(english, shown)
                self.assertIn('--language LANGUAGE', shown)
                self.assertIn('{none,matrix,slack}', shown)

    def test_language_comes_from_the_environment_and_the_saved_config_too(self):
        result = self.run_onboard('--unknown', environment={'ONBOARD_LANGUAGE': 'es'})
        self.assertEqual(self.lines(result.stderr)[-1], 'onboard: ' + said(
            'es', 'err_arg_unrecognized', arguments='--unknown'))
        (self.home / 'config').mkdir(parents=True)
        config = self.home / 'config/config.json'
        config.write_text('{"language": "fr"}')
        result = self.run_onboard('--home', str(self.home), '--unknown')
        self.assertEqual(self.lines(result.stderr)[-1], 'onboard: ' + said(
            'fr', 'err_arg_unrecognized', arguments='--unknown'))
        config.unlink()
        config.symlink_to(self.root / 'elsewhere.json')
        (self.root / 'elsewhere.json').write_text('{"language": "fr"}')
        result = self.run_onboard('--home', str(self.home), '--unknown')
        self.assertEqual(self.lines(result.stderr)[-1], 'onboard: ' + said(
            'en', 'err_arg_unrecognized', arguments='--unknown'))


class OutputEncodingTests(unittest.TestCase):
    """A language is shown whatever encoding the terminal locale has (no traceback)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ductorverify-encoding-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token = self.root / 'token'
        self.token.write_text('123:NOT_A_REAL_TOKEN')
        self.base = {k: v for k, v in os.environ.items()
                     if not k.startswith(('ONBOARD_', 'DUCTOR_', 'PYTHONIOENCODING'))
                     and k not in {'LC_ALL', 'LC_MESSAGES', 'LANG'}}
        self.base['HOME'] = str(self.root)

    def run_onboard(self, *options, environment):
        shutil.rmtree(self.root / 'home', ignore_errors=True)
        command = [sys.executable, str(ROOT / 'scripts/onboard.py'), '--no-prompt',
                   '--home', str(self.root / 'home'), '--allowed-users', '123', '--tz', 'UTC',
                   '--transport', 'none', '--telegram-token-file', str(self.token), *options]
        return subprocess.run(command, env={**self.base, **environment}, capture_output=True)

    def test_completion_notice_in_a_language_the_stream_encoding_cannot_show(self):
        cases = (
            ('de', {'PYTHONIOENCODING': 'euc_kr'}),
            ('de', {'PYTHONIOENCODING': 'euc_kr', 'LANG': 'ko_KR'}),
            ('de', {'PYTHONIOENCODING': 'euc_kr', 'LC_MESSAGES': 'de_DE.UTF-8', 'LANG': 'ko_KR'}),
            ('ko', {'PYTHONIOENCODING': 'ascii'}),
            ('ru', {'PYTHONIOENCODING': 'latin-1'}),
        )
        for language, environment in cases:
            with self.subTest(language=language, environment=environment):
                options = () if 'LC_MESSAGES' in environment else ('--language', language)
                result = self.run_onboard(*options, environment=environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(b'Traceback', result.stderr)
                shown = result.stdout.decode('utf-8')
                self.assertTrue(shown.startswith(said(language, 'out_language', language=language)))
                self.assertIn(said(language, 'out_ready'), shown)
                self.assertNotIn('NOT_A_REAL_TOKEN', shown + result.stderr.decode('utf-8', 'replace'))

    def test_a_stream_that_can_show_the_language_keeps_its_encoding(self):
        result = self.run_onboard('--language', 'ko', environment={'PYTHONIOENCODING': 'euc_kr'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.decode('euc_kr').startswith(
            said('ko', 'out_language', language='ko')))

    def test_errors_and_questions_are_safe_too(self):
        self.token.write_text('123:BAD TOKEN')
        result = self.run_onboard('--language', 'de', environment={'PYTHONIOENCODING': 'euc_kr'})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.decode('utf-8').strip(),
                         'onboard: ' + said('de', 'err_token_format'))
        unknown = self.run_onboard('--language=de', '--unknown',
                                   environment={'PYTHONIOENCODING': 'euc_kr'})
        self.assertEqual(unknown.returncode, 2)
        self.assertEqual(unknown.stderr.decode('utf-8').splitlines()[-1], 'onboard: ' + said(
            'de', 'err_arg_unrecognized', arguments='--unknown'))

    def test_a_value_that_cannot_be_encoded_is_escaped_not_fatal(self):
        script = ('import sys; sys.path.insert(0, %r); import onboard; '
                  'onboard.set_language("ko"); print(onboard.message("out_ready"), "\\u00fc")'
                  % str(ROOT / 'scripts'))
        result = subprocess.run([sys.executable, '-c', script], capture_output=True,
                                env={**self.base, 'PYTHONIOENCODING': 'ascii'})
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
