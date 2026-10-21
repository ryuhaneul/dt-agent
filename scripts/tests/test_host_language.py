"""login.sh, logout.sh and _home.sh speak the configured language; no Docker, fake commands only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
LANGUAGES = ('en', 'de', 'es', 'fr', 'id', 'ko', 'nl', 'pt', 'ru')
HANGUL = re.compile('[' + chr(0x1100) + '-' + chr(0x11ff) + chr(0xac00) + '-' + chr(0xd7a3) + ']')
FUNCTION = re.compile(r'^(?P<table>login|logout|home|status|update|rebuild)_text_(?P<lang>[a-z]{2})\(\) \{$', re.MULTILINE)
ENTRY = re.compile(r"^        (?P<key>[a-z0-9_]+)\) printf '%s' (?P<text>.*) ;;$", re.MULTILINE)


class HostLanguageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ductorverify-hostlang-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        (self.home / 'config').mkdir(parents=True)
        for slot, name in (('claude', '.credentials.json'), ('claude-alt', 'credentials.json'),
                           ('claude-x', None)):
            path = self.home / '.cli-auth' / slot
            path.mkdir(parents=True)
            if name:
                (path / name).write_text('{}')
        binary = self.root / 'bin'
        binary.mkdir()
        docker = binary / 'docker'
        # Invented commands: no real Docker, container or provider is touched.
        docker.write_text('#!/bin/sh\n[ "$1" = compose ] && [ "$4" = ps ] && echo ductorverify-fixture\nexit 0\n')
        docker.chmod(0o700)
        self.bin = binary
        self.config = self.home / 'config/config.json'

    def environment(self, **extra):
        env = {'HOME': str(self.root), 'DUCTOR_HOME': str(self.home), 'LC_ALL': 'C',
               'PATH': str(self.bin) + os.pathsep + '/usr/bin' + os.pathsep + '/bin'}
        env.update(extra)
        return env

    def run_script(self, name, *args, **extra):
        return subprocess.run(['/bin/bash', str(SCRIPTS / name), *args], env=self.environment(**extra),
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30, check=False)

    def message(self, table, key, language, *values):
        """The text a script puts out for *key*, read from the script's own table."""
        source = (SCRIPTS / ((table if table != 'home' else '_home') + '.sh')).read_text(encoding='utf-8')
        tables = source[FUNCTION.search(source).start():source.rindex('# hangul-allowed-' + 'end') + 20]
        code = 'source "$1"; eval "$2"; HOST_LANGUAGE="$3" host_text "$4" "$5" "${@:6}"'
        result = subprocess.run(['/bin/bash', '-c', code, 'x', str(SCRIPTS / '_home.sh'), tables, language,
                                 table, key, *values], env=self.environment(), capture_output=True,
                                text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def save_language(self, language):
        self.config.write_text(json.dumps({'language': language}))

    def test_gemini_key_guidance_requires_container_recreation(self):
        for language in LANGUAGES:
            with self.subTest(language=language):
                result = self.run_script('login.sh', 'gemini', ONBOARD_LANGUAGE=language)
                self.assertIn('docker compose up -d', result.stdout)

    def test_language_sources_and_order(self):
        cases = [
            ('nothing', {}, None, 'en'),
            ('locale', {'LC_ALL': 'ko_KR.UTF-8'}, None, 'ko'),
            ('locale C', {'LC_ALL': 'C'}, None, 'en'),
            ('stored', {}, 'ko', 'ko'),
            ('stored beats locale', {'LC_ALL': 'es_ES.UTF-8'}, 'de', 'de'),
            ('environment beats stored', {'ONBOARD_LANGUAGE': 'fr'}, 'de', 'fr'),
            ('unknown stored falls back to locale', {'LC_ALL': 'nl_NL.UTF-8'}, 'xx', 'nl'),
        ]
        for script, table in (('login.sh', 'login'), ('logout.sh', 'logout')):
            for label, extra, stored, expected in cases:
                with self.subTest(script=script, case=label):
                    if stored:
                        self.save_language(stored)
                    elif self.config.exists():
                        self.config.unlink()
                    result = self.run_script(script, '--help', **extra)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    first = result.stdout.splitlines()[0]
                    self.assertEqual(first, self.message(table, table + '_help_title', expected).rstrip('\n'))

    def test_korean_output_has_no_fixed_english_and_keeps_commands(self):
        self.save_language('ko')
        for script, args, command in (
            ('login.sh', ('claude', '--list'), None),
            ('login.sh', ('--help',), './scripts/login.sh claude --repair'),
            ('logout.sh', ('claude', '--list'), None),
            ('logout.sh', ('--help',), './scripts/logout.sh claude --disable <slot>'),
            ('login.sh', ('bogus',), None),
        ):
            with self.subTest(script=script, args=args):
                result = self.run_script(script, *args)
                self.assertTrue(HANGUL.search(result.stdout), result.stdout)
                for english in ('not authenticated', 'Tip:', 'Usage:', 'login to', 'logout from',
                                'Unknown provider', 'show this help', 'no slots found'):
                    self.assertNotIn(english, result.stdout)
                if command:
                    self.assertIn(command, result.stdout)
                if args[-1] == '--list':
                    # The slot names are kept; so are the claude_accounts key and the default marker slot.
                    for slot in ('claude-alt', 'claude-x'):
                        self.assertIn(slot, result.stdout)
                    self.assertIn('claude_accounts', result.stdout)

    def test_internal_slot_state_is_english_in_every_language(self):
        code = ('source "$1"; HOST_LANGUAGE="$2"; slot_auth_state "$3/.cli-auth/claude"; '
                'slot_auth_state "$3/.cli-auth/claude-x"; list_authenticated_claude_slots')
        for language in LANGUAGES:
            with self.subTest(language=language):
                result = subprocess.run(['/bin/bash', '-c', code, 'x', str(SCRIPTS / 'login.sh'), language,
                                         str(self.home)], env=self.environment(), capture_output=True,
                                        text=True, timeout=30, check=False)
                self.assertEqual(result.stdout.split('\n'),
                                 ['authenticated', 'not authenticated', 'claude', 'claude-alt', ''],
                                 result.stderr)

    def test_slot_listing_marks_the_same_slots_in_every_language(self):
        for language in LANGUAGES:
            with self.subTest(language=language):
                self.save_language(language)
                result = self.run_script('login.sh', 'claude', '--list')
                rows = {line.split()[0]: line for line in result.stdout.splitlines()
                        if line.startswith('  claude')}
                self.assertEqual(sorted(rows), ['claude', 'claude-alt', 'claude-x'])
                unauthenticated = self.message('login', 'state_not', language)
                self.assertIn(unauthenticated, rows['claude-x'])
                for slot in ('claude', 'claude-alt'):
                    self.assertNotIn(unauthenticated, rows[slot])
                    self.assertIn(self.message('login', 'state_auth', language), rows[slot])
                self.assertIn(self.message('login', 'default_marker', language), rows['claude'])

    def test_accounts_update_result_does_not_depend_on_the_language(self):
        results = {}
        for language in ('en', 'ko', 'de'):
            self.config.write_text(json.dumps({'claude_accounts': ['claude', 'claude-x'], 'language': language}))
            result = self.run_script('login.sh', 'claude', 'alt')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(self.message('login', 'no_tty', language), result.stdout)
            self.assertIn('claude, claude-x, claude-alt', result.stdout)
            tree = sorted(str(p.relative_to(self.home)) for p in self.home.rglob('*'))
            results[language] = (tree, json.loads(self.config.read_text()).get('claude_accounts'))
        self.assertEqual(results['en'], results['ko'])
        self.assertEqual(results['en'], results['de'])

    def test_logout_config_edit_does_not_depend_on_the_language(self):
        outcomes = {}
        for language in ('en', 'ko'):
            self.config.write_text(json.dumps({'claude_accounts': ['claude', 'claude-alt'], 'language': language}))
            result = self.run_script('logout.sh', 'claude', '--disable', 'alt')
            self.assertEqual(result.returncode, 0, result.stderr)
            outcomes[language] = json.loads(self.config.read_text())['claude_accounts']
            expected = self.message('logout', 'slot_changed', language, str(['claude', 'claude-alt']), str(['claude']))
            self.assertIn(expected, result.stdout)
        self.assertEqual(outcomes['en'], outcomes['ko'])
        self.assertEqual(outcomes['en'], ['claude'])

    def test_home_error_is_in_the_chosen_language(self):
        repo = self.root / 'repo'
        repo.mkdir()
        (repo / '.env').write_text('DUCTOR_HOME="unterminated\n')
        env = self.environment()
        env.pop('DUCTOR_HOME')
        for language in ('en', 'de', 'ko'):
            with self.subTest(language=language):
                code = 'source "$1/_home.sh"; resolve_ductor_home "$2"'
                result = subprocess.run(['/bin/bash', '-c', code, 'x', str(SCRIPTS), str(repo)],
                                        env=dict(env, ONBOARD_LANGUAGE=language), capture_output=True,
                                        text=True, timeout=30, check=False)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertEqual(result.stderr, self.message('home', 'home_unreadable', language) + '\n')
        english = self.message('home', 'home_unreadable', 'en')
        self.assertEqual(english, 'Cannot read DUCTOR_HOME from repository .env; check its format')

    def test_tables_have_the_same_keys_and_placeholders_in_every_language(self):
        for name in ('login', 'logout', '_home', 'status', 'update', 'rebuild'):
            source = (SCRIPTS / (name + '.sh')).read_text(encoding='utf-8')
            tables = {}
            for found in FUNCTION.finditer(source):
                end = source.index('\n}\n', found.end())
                body = source[found.end():end]
                tables[found['lang']] = {m['key']: sorted(re.findall(r'\{\d\}', m['text']))
                                         for m in ENTRY.finditer(body)}
            self.assertEqual(sorted(tables), sorted(LANGUAGES), name)
            for language, table in tables.items():
                self.assertEqual(table, tables['en'], (name, language))
            self.assertGreater(len(tables['en']), 0)

    def test_fill_puts_values_in_literally(self):
        code = 'source "$1"; fill "${@:2}"'
        values = ['<{1}|{2}|{3}|{4}>', 'a&b', '{2}', 'x\\\\y']
        result = subprocess.run(['/bin/bash', '-c', code, 'x', str(SCRIPTS / '_home.sh'), *values],
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.stdout, '<a&b|{2}|x\\\\y|>')



FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >> "$FAKE_LOG"
case "$1" in
    inspect) [ "${FAKE_NO_CONTAINER:-}" = 1 ] && exit 1; echo true ;;
    compose) [ "$2" = ps ] && echo "NAME STATUS fixture-container Up" ;;
    exec)
        case "$*" in
            *"python3 -c"*) echo "claude" ;;
            *"claude auth status"*) [ "${FAKE_AUTH:-all}" = none ] && exit 1; echo "Logged in as fixture" ;;
            *GEMINI*) [ "${FAKE_AUTH:-all}" = none ] && echo "not authenticated" || echo "authenticated (API key)" ;;
            *) [ "${FAKE_AUTH:-all}" = none ] && echo "not authenticated" || echo "authenticated" ;;
        esac ;;
esac
exit 0
"""
GIT_IDENTITY = {'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.test',
                'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.test',
                'GIT_CONFIG_GLOBAL': '/dev/null'}
MAINTENANCE = ('status.sh', 'update.sh', 'rebuild.sh', '_home.sh')


class MaintenanceScriptTests(unittest.TestCase):
    """status.sh, update.sh and rebuild.sh with a fake docker and a disposable git origin."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ductorverify-maint-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.bin / 'docker').write_text(FAKE_DOCKER)
        (self.bin / 'docker').chmod(0o700)
        self.home = self.root / 'home'
        (self.home / 'config').mkdir(parents=True)
        (self.home / 'inflight_turns.json').write_text('[{"a": 1}, {"b": 2}]')
        (self.home / 'tasks.json').write_text('{"tasks": [{"status": "running"}, {"status": "done"}]}')
        self.log = self.root / 'docker.log'
        self.clone = self.make_clone()

    def git(self, *args, cwd):
        subprocess.run(['git', *args], cwd=cwd, env=dict({name: value for name, value in os.environ.items()
                                if name not in {'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS'}
                                and not name.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))},
                                **GIT_IDENTITY), check=True,
                       capture_output=True)

    def make_clone(self):
        origin, seed, clone = self.root / 'origin.git', self.root / 'seed', self.root / 'clone'
        self.git('init', '-q', '--bare', '--initial-branch=main', str(origin), cwd=self.root)
        seed.mkdir()
        self.git('init', '-q', '--initial-branch=main', cwd=seed)
        (seed / 'scripts').mkdir()
        (seed / 'README').write_text('one\n')
        for name in MAINTENANCE:
            (seed / 'scripts' / name).write_text((SCRIPTS / name).read_text(encoding='utf-8'), encoding='utf-8')
        self.git('add', '-A', cwd=seed)
        self.git('commit', '-q', '-m', 'one', cwd=seed)
        self.git('remote', 'add', 'origin', str(origin), cwd=seed)
        self.git('push', '-q', 'origin', 'main', cwd=seed)
        self.git('clone', '-q', str(origin), str(clone), cwd=self.root)
        self.seed = seed
        return clone

    def rewind(self):
        """Put the clone back on the first commit (the local branch only; origin keeps the new one)."""
        first = subprocess.run(['git', 'rev-list', '--max-parents=0', 'HEAD'], cwd=self.clone, check=True,
                               env={name: value for name, value in os.environ.items()
                                    if name not in {'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS'}
                                    and not name.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))},
                               capture_output=True, text=True).stdout.split()[0]
        self.git('reset', '-q', '--hard', first, cwd=self.clone)

    def publish(self, touch_scripts=False):
        (self.seed / 'README').write_text('two\n')
        if touch_scripts:
            for name in MAINTENANCE:
                with (self.seed / 'scripts' / name).open('a', encoding='utf-8') as handle:
                    handle.write('# touched\n')
        self.git('add', '-A', cwd=self.seed)
        self.git('commit', '-q', '-m', 'two', cwd=self.seed)
        self.git('push', '-q', 'origin', 'main', cwd=self.seed)

    def run_script(self, name, stdin='', language=None, locale='C', **extra):
        config = self.home / 'config/config.json'
        config.unlink(missing_ok=True)
        if language:
            config.write_text(json.dumps({'language': language}))
        self.log.unlink(missing_ok=True)
        env = {'HOME': str(self.root), 'DUCTOR_HOME': str(self.home), 'LC_ALL': locale,
               'FAKE_LOG': str(self.log), 'GIT_CONFIG_GLOBAL': '/dev/null',
               'PATH': str(self.bin) + os.pathsep + '/usr/bin' + os.pathsep + '/bin'}
        env.update(extra)
        result = subprocess.run(['/bin/bash', str(self.clone / 'scripts' / name)], input=stdin, env=env,
                                capture_output=True, text=True, timeout=60, cwd=self.root, check=False)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return result, [c for c in calls if not c.startswith('docker inspect')]

    def message(self, table, key, language, *values):
        return HostLanguageTests.message(self, table, key, language, *values)

    environment = HostLanguageTests.environment

    def test_status_probes_follow_the_language_and_keep_docker_output(self):
        for language in ('en', 'ko', 'de'):
            for auth in ('all', 'none'):
                with self.subTest(language=language, auth=auth):
                    result, _ = self.run_script('status.sh', language=language, FAKE_AUTH=auth)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    out = result.stdout
                    self.assertIn(self.message('status', 'h_container', language), out)
                    self.assertIn(self.message('status', 'h_auth', language), out)
                    self.assertIn('NAME STATUS fixture-container Up', out)
                    lines = {line.split(':')[0]: line for line in out.splitlines() if line[:6] in
                             ('Claude', 'Codex:', 'Gemini', 'Grok: ')}
                    self.assertEqual(sorted(lines), ['Claude (claude)', 'Codex', 'Gemini', 'Grok'])
                    if auth == 'all':
                        self.assertIn('Logged in as fixture', lines['Claude (claude)'])
                        self.assertIn(self.message('status', 'state_auth', language), lines['Codex'])
                        self.assertIn(self.message('status', 'state_auth_key', language), lines['Gemini'])
                    else:
                        for line in lines.values():
                            self.assertIn(self.message('status', 'state_not', language), line)

    def test_status_without_a_container_fails_with_the_chosen_language(self):
        for language in ('en', 'ko'):
            with self.subTest(language=language):
                result, _ = self.run_script('status.sh', language=language, FAKE_NO_CONTAINER='1')
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout,
                                 self.message('status', 'err_container', language, 'ductor-bot') + '\n')

    def test_rebuild_headers_follow_the_language_and_commands_are_the_same(self):
        calls = {}
        for language, locale in (('en', 'C'), ('ko', 'C'), (None, 'ko_KR.UTF-8')):
            with self.subTest(language=language, locale=locale):
                result, log = self.run_script('rebuild.sh', language=language, locale=locale)
                self.assertEqual(result.returncode, 0, result.stderr)
                shown = language or 'ko'
                for key in ('h_building_images', 'h_recreating', 'h_status'):
                    self.assertIn(self.message('rebuild', key, shown), result.stdout)
                calls[(language, locale)] = [c.split(' CACHEBUST')[0] for c in log]
        self.assertEqual(len({tuple(v) for v in calls.values()}), 1)

    def test_update_decisions_do_not_depend_on_the_language(self):
        self.publish()
        outcomes = {}
        for label, language, locale in (('en', 'en', 'C'), ('ko', 'ko', 'C'), ('locale ko', None, 'ko_KR.UTF-8')):
            for answers in ('y\ny\n', 'N\n', 'y\ny\ny\n', '\n'):
                self.rewind()
                with self.subTest(language=label, answers=answers):
                    result, log = self.run_script('update.sh', answers, language=language, locale=locale)
                    outcomes[(label, answers)] = (result.returncode, [c.split(' CACHEBUST')[0] for c in log])
                    shown = language or 'ko'
                    for key in ('h_current', 'h_pulling', 'new_commits', 'warn_inflight'):
                        values = ('2',) if key == 'warn_inflight' else ()
                        self.assertIn(self.message('update', key, shown, *values), result.stdout)
                    if label != 'en':
                        for english in ('Pulling upstream', 'Current HEAD', 'will be interrupted', 'Aborted'):
                            self.assertNotIn(english, result.stdout)
        for answers in ('y\ny\n', 'N\n', 'y\ny\ny\n', '\n'):
            self.assertEqual(outcomes[('en', answers)], outcomes[('ko', answers)])
            self.assertEqual(outcomes[('en', answers)], outcomes[('locale ko', answers)])
        built = outcomes[('en', 'y\ny\n')][1]
        self.assertTrue(any('build' in c for c in built) and any('up -d' in c for c in built))
        self.assertFalse(any('build' in c for c in outcomes[('en', 'N\n')][1]))

    def test_update_when_already_current_asks_before_rebuilding(self):
        for language in ('en', 'ko'):
            for answers, builds in (('n\n', False), ('y\nN\n', False), ('y\ny\ny\n', True)):
                with self.subTest(language=language, answers=answers):
                    result, log = self.run_script('update.sh', answers, language=language)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(self.message('update', 'up_to_date', language), result.stdout)
                    self.assertEqual(any('build' in c for c in log), builds)

    def test_update_refuses_local_changes_in_the_chosen_language(self):
        (self.clone / 'README').write_text('changed\n')
        for language in ('en', 'ko'):
            with self.subTest(language=language):
                result, log = self.run_script('update.sh', 'y\n', language=language)
                self.assertEqual(result.returncode, 1)
                self.assertIn(self.message('update', 'err_local_changes', language), result.stdout)
                self.assertIn(' M README', result.stdout)
                self.assertEqual(log, [])

    def test_update_without_a_container_skips_the_question(self):
        self.publish()
        result, log = self.run_script('update.sh', '', language='ko', FAKE_NO_CONTAINER='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(self.message('update', 'warn_inflight', 'ko', '2'), result.stdout)
        self.assertTrue(any('build' in c for c in log))

    def test_update_that_replaces_its_own_scripts_finishes(self):
        self.publish(touch_scripts=True)
        for language in ('en', 'ko'):
            self.rewind()
            with self.subTest(language=language):
                result, log = self.run_script('update.sh', 'y\ny\n', language=language)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((self.clone / 'scripts/update.sh').read_text().endswith('# touched\n'))
                self.assertTrue(any('up -d' in c for c in log))
                self.assertIn(self.message('update', 'h_status', language), result.stdout)


if __name__ == '__main__':
    unittest.main()
