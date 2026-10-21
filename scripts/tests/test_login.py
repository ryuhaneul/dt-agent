"""Exercise the real login helper with fake Docker and disposable auth/config files."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'login.sh'
PROMPT = b'Update config.json with the proposed list? [Y/n] '


class LoginAccountUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ductorverify-login-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.config = self.home / 'config/config.json'
        self.config.parent.mkdir(parents=True)
        self.original = {'claude_accounts': ['claude', 'claude-unused'],
                         'model': 'sonnet', 'nested': {'note': '계정 테스트', 'enabled': False}}
        self.config.write_text(json.dumps(self.original, ensure_ascii=False))
        self.config.chmod(0o600)
        self.original_stat = self.config.stat()
        self.proposed = ['claude', 'claude-unused', 'claude-alt', 'claude-work']
        for slot in ('claude', 'claude-alt', 'claude-work'):
            path = self.home / '.cli-auth' / slot
            path.mkdir(parents=True)
            (path / '.credentials.json').write_text('{}')
        binary = self.root / 'bin'
        binary.mkdir()
        docker = binary / 'docker'
        docker.write_text('''#!/usr/bin/env bash
set -eu
[[ "$1" == compose && "$2" == --project-directory ]] || exit 97
shift 3
case "$1" in
    ps) printf 'ductorverify-login-fixture\\n' ;;
    exec) [[ " $* " == *" ductor-bot claude /login "* ]] || exit 98 ;;
    *) exit 99 ;;
esac
''')
        docker.chmod(0o700)
        self.env = dict(os.environ, HOME=str(self.root), DUCTOR_HOME=str(self.home),
                        PATH=str(binary) + os.pathsep + os.environ['PATH'])
        # The messages follow the language setting; these tests read the English ones.
        self.env.pop('ONBOARD_LANGUAGE', None)
        self.env['LC_ALL'] = 'C'
        # Credentials and JSON in this fixture are invented; no real Docker/provider is run.
        self.command = ['bash', str(SCRIPT), 'claude', 'alt']

    def run_tty(self, answer, *, break_config=False, before_reply=None, prompt=PROMPT):
        master, slave = pty.openpty()
        output = bytearray()
        proc = None
        replied = False
        try:
            proc = subprocess.Popen(self.command, env=self.env, stdin=slave,
                                    stdout=slave, stderr=slave)
            os.close(slave)
            slave = -1
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    output.extend(chunk)
                    if not replied and prompt in output:
                        if before_reply is not None:
                            before_reply()
                        if break_config:
                            self.config.write_text('invalid fixture JSON')
                        os.write(master, answer.encode() + b'\n')
                        replied = True
                elif proc.poll() is not None:
                    break
            else:
                self.fail('login helper did not finish before the fixture timeout')
            code = proc.wait(timeout=2)
            self.assertTrue(replied, output.decode(errors='replace'))
            return code, output.decode(errors='replace')
        finally:
            if slave != -1:
                os.close(slave)
            os.close(master)
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()

    def assert_private_unchanged_identity(self, *, replaced=False):
        current = self.config.stat()
        self.assertEqual(current.st_mode & 0o777, 0o600)
        if replaced:
            assert current.st_ino != self.original_stat.st_ino
        else:
            self.assertEqual(current.st_ino, self.original_stat.st_ino)
        self.assertEqual((current.st_uid, current.st_gid),
                         (self.original_stat.st_uid, self.original_stat.st_gid))

    def test_tty_registration_preserves_a_newer_order_and_model(self):
        def reorder_before_reply():
            import fcntl
            with (self.config.parent / '.config.json.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                latest = dict(self.original, claude_accounts=['claude-work', 'claude-unused'], model='opus')
                self.config.write_text(json.dumps(latest))
        code, output = self.run_tty('y', before_reply=reorder_before_reply)
        self.assertEqual(code, 0, output)
        expected = dict(self.original, claude_accounts=['claude-work', 'claude-unused', 'claude-alt'], model='opus')
        self.assertEqual(json.loads(self.config.read_text()), expected)

    def test_tty_empty_input_updates_proposed_accounts(self):
        code, output = self.run_tty('')
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(self.config.read_text()),
                         dict(self.original, claude_accounts=self.proposed))
        self.assertIn('Updated.', output)

    def test_login_exit_130_still_shares_and_registers_credentials(self):
        credentials = self.home / '.cli-auth/claude-alt/.credentials.json'
        credentials.unlink()
        (self.home / '.cli-auth/claude/settings.json').write_text('{}')
        docker = self.root / 'bin/docker'
        text = docker.read_text()
        text = text.replace('exec) [[ " $* " == *" ductor-bot claude /login "* ]] || exit 98 ;;',
                            'exec) [[ " $* " == *" ductor-bot claude /login "* ]] || exit 98; '
                            'printf "{}" > "$DUCTOR_HOME/.cli-auth/claude-alt/.credentials.json"; exit 130 ;;')
        docker.write_text(text)
        result = subprocess.run(self.command, env=self.env, input='',
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(credentials.is_file())
        self.assertTrue((credentials.parent / 'settings.json').is_symlink())
        self.assertEqual(json.loads(self.config.read_text()),
                         dict(self.original, claude_accounts=self.proposed))
        self.assertIn('Updated.', result.stdout)

    def test_tty_yes_updates_proposed_accounts_only(self):
        code, output = self.run_tty('y')
        self.assertEqual(code, 0, output)
        expected = dict(self.original, claude_accounts=self.proposed)
        self.assertEqual(json.loads(self.config.read_text()), expected)
        self.assertIn('Updated. Restart the bot to apply:', output)
        self.assert_private_unchanged_identity(replaced=True)

    def test_tty_yes_in_korean_updates_the_same_accounts(self):
        self.env['ONBOARD_LANGUAGE'] = 'ko'
        code = 'source "$1"; HOST_LANGUAGE=ko host_text login "$2"'
        texts = {key: subprocess.run(['bash', '-c', code, 'x', str(SCRIPT), key], env=self.env,
                                     capture_output=True, text=True, check=True).stdout
                 for key in ('prompt_update', 'updated')}
        self.assertNotEqual(texts['prompt_update'].encode(), PROMPT)
        code, output = self.run_tty('y', prompt=texts['prompt_update'].encode())
        self.assertEqual(code, 0, output)
        expected = dict(self.original, claude_accounts=self.proposed)
        self.assertEqual(json.loads(self.config.read_text()), expected)
        self.assertIn(texts['updated'], output)
        self.assertNotIn('Updated.', output)
        self.assert_private_unchanged_identity(replaced=True)

    def test_tty_no_keeps_config(self):
        before = self.config.read_bytes()
        code, output = self.run_tty('N')
        self.assertEqual(code, 0, output)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertIn('Skipped.', output)
        self.assertNotIn('Updated.', output)
        self.assert_private_unchanged_identity()

    def test_non_tty_automatically_updates_accounts(self):
        result = subprocess.run(self.command, env=self.env, input='n\n',
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.config.read_text()),
                         dict(self.original, claude_accounts=self.proposed))
        self.assertIn('stdin is not a TTY', result.stdout)
        self.assertIn('Updated.', result.stdout)
        self.assert_private_unchanged_identity(replaced=True)

    def test_tty_update_failure_is_nonzero_without_success_notice(self):
        code, output = self.run_tty('y', break_config=True)
        self.assertNotEqual(code, 0, output)
        self.assertNotIn('Updated.', output)
        self.assertEqual(self.config.read_text(), 'invalid fixture JSON')
        self.assert_private_unchanged_identity()

    def test_full_login_write_failure_has_no_success_notice_and_continues(self):
        self.command = ['bash', str(SCRIPT)]
        calls = self.root / 'full-login-calls.txt'
        self.env['FAKE_LOGIN_CALLS'] = str(calls)
        docker = self.root / 'bin/docker'
        source = docker.read_text()
        source = source.replace(
            'exec) [[ " $* " == *" ductor-bot claude /login "* ]] || exit 98 ;;',
            'exec) printf "%s\\n" "$*" >> "$FAKE_LOGIN_CALLS"; '
            '[[ " $* " != *" sh -c "* ]] || exit 1 ;;')
        docker.write_text(source)
        code, output = self.run_tty('y', break_config=True)
        self.assertEqual(code, 1, output)
        self.assertIn('JSONDecodeError', output)
        self.assertNotIn('Updated.', output)
        self.assertNotIn('claude: OK', output)
        self.assertIn('claude: Failed', output)
        self.assertEqual(self.config.read_text(), 'invalid fixture JSON')
        recorded = calls.read_text()
        for command in ('codex login', 'gemini', 'grok login'):
            self.assertIn(command, recorded)


class ProviderLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ductorverify-provider-login-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        binary = self.root / 'bin'
        binary.mkdir()
        docker = binary / 'docker'
        docker.write_text('''#!/usr/bin/env bash
set -eu
[[ "$1" == compose && "$2" == --project-directory ]] || exit 97
shift 3
case "$1" in
    ps) printf 'provider-login-fixture\\n' ;;
    exec)
        shift
        [ "${1:-}" != -T ] || shift
        if [ "${2:-}" = sh ]; then shift; exec "$@"; fi
        [[ "$1" == ductor-bot && "$2" == "$FAKE_PROVIDER" ]] || exit 98
        if [ "$FAKE_CREATE_CREDENTIALS" = 1 ]; then
            mkdir -p "$DUCTOR_HOME/.cli-auth/$FAKE_PROVIDER"
            printf '{}' > "$DUCTOR_HOME/.cli-auth/$FAKE_PROVIDER/$FAKE_CREDENTIAL_FILE"
        fi
        exit "$FAKE_LOGIN_EXIT"
        ;;
    *) exit 99 ;;
esac
''')
        docker.chmod(0o700)
        self.env = dict(os.environ, HOME=str(self.root), DUCTOR_HOME=str(self.home),
                        PATH=str(binary) + os.pathsep + os.environ['PATH'],
                        ONBOARD_LANGUAGE='en', LC_ALL='C')
        for key in ('GEMINI_API_KEY', 'GOOGLE_API_KEY'):
            self.env.pop(key, None)

    def run_provider(self, provider, filename, create_credentials, exit_code):
        env = dict(self.env, FAKE_PROVIDER=provider, FAKE_CREDENTIAL_FILE=filename,
                   FAKE_CREATE_CREDENTIALS=str(int(create_credentials)),
                   FAKE_LOGIN_EXIT=str(exit_code))
        return subprocess.run(['bash', str(SCRIPT), provider], env=env, input='',
                              capture_output=True, text=True, timeout=10)

    def test_credentials_make_exit_130_a_success(self):
        for provider, filename in (('codex', 'auth.json'), ('gemini', 'oauth_creds.json'),
                                   ('grok', 'auth.json')):
            with self.subTest(provider=provider):
                result = self.run_provider(provider, filename, True, 130)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("OK: slot '{}' is authenticated.".format(provider), result.stdout)
                self.assertTrue((self.home / '.cli-auth' / provider / filename).is_file())

    def test_exit_zero_without_credentials_is_a_failure(self):
        for provider, filename in (('codex', 'auth.json'), ('gemini', 'oauth_creds.json'),
                                   ('grok', 'auth.json')):
            with self.subTest(provider=provider):
                result = self.run_provider(provider, filename, False, 0)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('Warning:', result.stderr)
                self.assertIn(str(self.home / '.cli-auth' / provider), result.stderr)
                self.assertNotIn('OK:', result.stdout)


    def test_gemini_api_keys_do_not_require_oauth_credentials(self):
        for key in ('GEMINI_API_KEY', 'GOOGLE_API_KEY'):
            with self.subTest(key=key):
                self.env[key] = 'synthetic-api-key'
                try:
                    result = self.run_provider('gemini', 'oauth_creds.json', False, 0)
                finally:
                    self.env.pop(key)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("OK: slot 'gemini' is authenticated.", result.stdout)
                self.assertNotIn('Warning:', result.stderr)
                self.assertFalse((self.home / '.cli-auth/gemini/oauth_creds.json').exists())

    def test_all_login_continues_and_summarizes_each_provider_failure(self):
        binary = self.root / 'bin/docker'
        binary.write_text(r"""#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[4:]
if args[0] == 'ps':
    print('synthetic-container')
    sys.exit(0)
args = args[1:]
while args[0] in ('-e', '-T'):
    flag = args.pop(0)
    if flag == '-e': args.pop(0)
command = args[1:]
if command[0] == 'sh':
    sys.exit(subprocess.run(command).returncode)
provider = command[0]
with open(os.environ['FAKE_LOGIN_CALLS'], 'a') as log:
    log.write(provider + '\n')
if provider != os.environ['FAKE_MISSING_PROVIDER']:
    directory = Path(os.environ['DUCTOR_HOME']) / '.cli-auth' / provider
    directory.mkdir(parents=True, exist_ok=True)
    filename = {'claude': '.credentials.json', 'codex': 'auth.json',
                'gemini': 'oauth_creds.json', 'grok': 'auth.json'}[provider]
    (directory / filename).write_text('{}')
""")
        config = self.home / 'config/config.json'
        config.parent.mkdir()
        config.write_text('{"claude_accounts": []}')
        calls = self.root / 'login-calls.txt'
        providers = ('claude', 'codex', 'gemini', 'grok')
        for missing in providers:
            with self.subTest(missing=missing):
                for provider in providers:
                    directory = self.home / '.cli-auth' / provider
                    for filename in ('.credentials.json', 'auth.json', 'oauth_creds.json'):
                        path = directory / filename
                        if path.exists(): path.unlink()
                calls.write_text('')
                env = dict(self.env, FAKE_MISSING_PROVIDER=missing, FAKE_LOGIN_CALLS=str(calls))
                result = subprocess.run(['bash', str(SCRIPT)], env=env, input='',
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(calls.read_text().splitlines(), list(providers))
                self.assertIn('Warning:', result.stderr)
                self.assertIn('Login summary:', result.stdout)
                summary = result.stdout.split('Login summary:', 1)[1]
                for provider in providers:
                    self.assertIn(provider + ': ' + ('Failed' if provider == missing else 'OK'), summary)

    def _install_permission_login_probe(self):
        if os.geteuid() == 0:
            self.skipTest('Filesystem permission checks require an unprivileged user')
        config = self.home / 'config/config.json'
        config.parent.mkdir()
        config.write_text('{"language":"en","claude_accounts":[]}')
        calls = self.root / 'permission-login-calls.txt'
        self.env['FAKE_LOGIN_CALLS'] = str(calls)
        docker = self.root / 'bin/docker'
        docker.write_text(r"""#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[4:]
if args[0] == 'ps':
    print('synthetic-container')
    sys.exit(0)
assert args[0] == 'exec'
args = args[1:]
while args[0] in ('-e', '-T'):
    flag = args.pop(0)
    if flag == '-e': args.pop(0)
command = args[1:]
if command[0] == 'sh':
    sys.exit(1)
provider = command[0]
with open(os.environ['FAKE_LOGIN_CALLS'], 'a') as out:
    out.write(provider + '\n')
directory = Path(os.environ['DUCTOR_HOME']) / '.cli-auth' / provider
directory.mkdir(parents=True, exist_ok=True)
name = {'claude': '.credentials.json', 'codex': 'auth.json',
        'gemini': 'oauth_creds.json', 'grok': 'auth.json'}[provider]
(directory / name).write_text('{}')
""")
        return calls

    def _check_readonly_auth_parent(self, all_providers):
        calls = self._install_permission_login_probe()
        auth = self.home / '.cli-auth'
        session = auth / 'claude/projects/fixture-project/fixture-session.jsonl'
        session.parent.mkdir(parents=True)
        session.write_text('fixture-session-record')
        shared = auth / '_shared-projects'
        shared.mkdir()
        for provider in ('codex', 'gemini', 'grok'):
            (auth / provider).mkdir()
        auth.chmod(0o555)
        try:
            self.assertEqual(auth.stat().st_mode & 0o777, 0o555)
            args = [] if all_providers else ['claude']
            result = subprocess.run(['bash', str(SCRIPT), *args], env=self.env, input='',
                                    capture_output=True, text=True, timeout=15)
            observed = {
                'exit': result.returncode,
                'calls': calls.read_text().splitlines() if calls.exists() else [],
                'original_session_visible': session.is_file() and session.read_text() == 'fixture-session-record',
                'nested_projects': (shared / 'projects').exists(),
                'updated': 'Updated.' in result.stdout,
            }
        finally:
            auth.chmod(0o755)
        self.assertEqual(observed, {
            'exit': 1,
            'calls': ['codex', 'gemini', 'grok'] if all_providers else [],
            'original_session_visible': True, 'nested_projects': False, 'updated': False,
        }, result.stdout + result.stderr)
        self.assertIn('Permission denied', result.stderr)
        if all_providers:
            self.assertIn('claude: Failed', result.stdout)
            for provider in ('codex', 'gemini', 'grok'):
                self.assertIn(provider + ': OK', result.stdout)

    def test_readonly_auth_parent_stops_single_login_before_session_move(self):
        self._check_readonly_auth_parent(False)

    def test_readonly_auth_parent_marks_full_login_failed_and_continues(self):
        self._check_readonly_auth_parent(True)

    def test_readonly_alt_slot_preserves_stale_link_without_nested_link(self):
        calls = self._install_permission_login_probe()
        auth = self.home / '.cli-auth'
        shared = auth / '_shared-projects'
        session = shared / 'fixture-project/fixture-session.jsonl'
        session.parent.mkdir(parents=True)
        session.write_text('fixture-session-record')
        default = auth / 'claude'
        default.mkdir()
        (default / '.credentials.json').write_text('{}')
        (default / 'projects').symlink_to('../_shared-projects')
        (default / 'skills').mkdir()
        (default / 'skills/fixture.md').write_text('default-skill')
        alt = auth / 'claude-alt'
        alt.mkdir()
        (alt / '.credentials.json').write_text('{}')
        (alt / 'projects').symlink_to('../_shared-projects')
        stale = auth / 'stale-skills'
        stale.mkdir()
        (stale / 'fixture.md').write_text('original-skill')
        (alt / 'skills').symlink_to('../stale-skills')
        alt.chmod(0o555)
        try:
            self.assertEqual(alt.stat().st_mode & 0o777, 0o555)
            result = subprocess.run(['bash', str(SCRIPT), 'claude', 'alt'], env=self.env,
                                    input='', capture_output=True, text=True, timeout=15)
            observed = {
                'exit': result.returncode,
                'calls': calls.read_text().splitlines() if calls.exists() else [],
                'original_session_visible': (alt / 'projects/fixture-project/fixture-session.jsonl').read_text() == 'fixture-session-record',
                'original_skill_visible': (alt / 'skills/fixture.md').read_text() == 'original-skill',
                'original_link': os.readlink(alt / 'skills'),
                'unexpected_nested_link': os.path.lexists(stale / 'skills'),
                'updated': 'Updated.' in result.stdout,
            }
        finally:
            alt.chmod(0o755)
        self.assertEqual(observed, {
            'exit': 1, 'calls': ['claude'], 'original_session_visible': True,
            'original_skill_visible': True, 'original_link': '../stale-skills',
            'unexpected_nested_link': False, 'updated': False,
        }, result.stdout + result.stderr)
        self.assertIn('Permission denied', result.stderr)

    def test_filesystem_failure_reaches_full_login_summary(self):
        docker = self.root / 'bin/docker'
        docker.write_text(r"""#!/usr/bin/env python3
import os, subprocess, sys
from pathlib import Path
args = sys.argv[4:]
if args[0] == 'ps':
    print('synthetic-container')
    sys.exit(0)
args = args[1:]
while args[0] in ('-e', '-T'):
    flag = args.pop(0)
    if flag == '-e': args.pop(0)
command = args[1:]
if command[0] == 'sh':
    sys.exit(subprocess.run(command).returncode)
provider = command[0]
with open(os.environ['FAKE_LOGIN_CALLS'], 'a') as out:
    out.write(provider + '\n')
directory = Path(os.environ['DUCTOR_HOME']) / '.cli-auth' / provider
directory.mkdir(parents=True, exist_ok=True)
name = {'claude': '.credentials.json', 'codex': 'auth.json',
        'gemini': 'oauth_creds.json', 'grok': 'auth.json'}[provider]
(directory / name).write_text('{}')
""")
        config = self.home / 'config/config.json'
        config.parent.mkdir()
        config.write_text('{"claude_accounts": []}')
        calls = self.root / 'login-calls.txt'
        self.env['FAKE_LOGIN_CALLS'] = str(calls)
        for operation in ('mkdir', 'mv', 'ln'):
            wrapper = self.root / 'bin' / operation
            wrapper.write_text('#!/usr/bin/env python3\n' +
                'import os,sys\n' +
                'selected=os.environ.get("FAKE_FS_FAILURE", "")\n' +
                f'operation={operation!r}\n' +
                'if selected.startswith(operation+":") and any(selected.split(":",1)[1] in a for a in sys.argv[1:]): sys.exit(1)\n' +
                f'os.execv("/usr/bin/{operation}", [operation]+sys.argv[1:])\n')
            wrapper.chmod(0o700)
        for failure in ('mkdir:_shared-projects', 'mkdir:/.cli-auth/claude',
                        'mv:/.cli-auth/claude/projects', 'ln:/.cli-auth/claude/projects'):
            with self.subTest(failure=failure):
                import shutil
                shutil.rmtree(self.home / '.cli-auth', ignore_errors=True)
                if failure.startswith('mv:'):
                    (self.home / '.cli-auth/claude/projects').mkdir(parents=True)
                calls.write_text('')
                env = dict(self.env, FAKE_FS_FAILURE=failure)
                result = subprocess.run(['bash', str(SCRIPT)], env=env, input='',
                                        capture_output=True, text=True, timeout=15)
                self.assertIn('claude: Failed', result.stdout)
                self.assertNotIn('claude: OK', result.stdout)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(calls.read_text().splitlines(), ['codex', 'gemini', 'grok'])
                single = subprocess.run(['bash', str(SCRIPT), 'claude'], env=env, input='',
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(single.returncode, 1, single.stdout + single.stderr)
        self.env.pop('FAKE_FS_FAILURE', None)
        success = subprocess.run(['bash', str(SCRIPT)], env=self.env, input='',
                                 capture_output=True, text=True, timeout=15)
        self.assertEqual(success.returncode, 0, success.stdout + success.stderr)
        for provider in ('claude', 'codex', 'gemini', 'grok'):
            self.assertIn(provider + ': OK', success.stdout)

    def test_slot_name_validation_and_aliases(self):
        config = self.home / 'config/config.json'
        config.parent.mkdir(exist_ok=True)
        config.write_text('{"claude_accounts": []}')
        for script in ('login.sh', 'logout.sh'):
            path = SCRIPT.with_name(script)
            for name in ('..', 'a/b', 'UPPER', 'has space', '-bad', 'slots',
                         'claude-slots', 'claude-', 'claude-/bad'):
                with self.subTest(script=script, name=name):
                    command = (['bash', '-c', 'source "$1"; resolve_claude_slot_name "$2"',
                                'fixture', str(path), name] if script == 'login.sh' else
                               ['bash', str(path), 'claude', '--disable', name])
                    result = subprocess.run(command, env=self.env,
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertIn('Invalid Claude slot name', result.stderr)
            for name, expected in (('', 'claude'), ('default', 'claude'), ('claude', 'claude'),
                                   ('alt', 'claude-alt'), ('claude-a_b-2', 'claude-a_b-2'),
                                   ('2work', 'claude-2work')):
                with self.subTest(script=script, name=name):
                    command = (['bash', '-c', 'source "$1"; resolve_claude_slot_name "$2"',
                                'fixture', str(path), name] if script == 'login.sh' else
                               ['bash', str(path), 'claude', '--disable', name])
                    result = subprocess.run(command, env=self.env,
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if script == 'login.sh':
                        self.assertEqual(result.stdout.strip(), expected)
                    else:
                        self.assertIn(expected, result.stdout)


class DocumentationConsistencyTests(unittest.TestCase):
    def test_public_module_docs_omit_internal_verification_notes(self):
        root = SCRIPT.parents[1]
        for file, forbidden in (
            ('bot.md', 'unverified by that observation'),
            ('tasks.md', 'test-server confirmation'),
            ('bus.md', 'No external ingress'),
            ('bus.md', 'single commit'),
            ('messenger.md', 'NPM is not applicable'),
            ('messenger.md', 'commit revert restores'),
        ):
            with self.subTest(file=file, forbidden=forbidden):
                self.assertNotIn(forbidden, (root / 'docs/modules' / file).read_text())

    def test_documented_defaults_and_supported_values_match_current_code(self):
        root = SCRIPT.parents[1]
        config = root / 'ductor_bot/_home_defaults/config'
        text = (config / 'RULES-all-clis.md').read_text()
        self.assertIn('`["PERSONA.md", "BEHAVIOR.md"]`', text)
        self.assertNotIn('Empty (default)', text)
        for file in ('RULES-all-clis.md', 'RULES-claude-only.md',
                     'RULES-codex-only.md', 'RULES-gemini-only.md'):
            line = next(line for line in (config / file).read_text().splitlines()
                        if 'heartbeat.suspend_on_ack' in line)
            self.assertIn('default `true`', line)
            self.assertNotIn('opt in', line)
        workspace = root / 'ductor_bot/_home_defaults/workspace'
        webhook = (workspace / 'tools/webhook_tools/RULES-all-clis.md').read_text()
        self.assertNotIn('`max`', webhook)
        self.assertNotIn('"max"', webhook)
        user_tools = (workspace / 'tools/user_tools/RULES.md').read_text()
        self.assertIn('create_task.py', user_tools)
        self.assertNotIn('nohup', user_tools)

    def test_installation_and_readme_describe_current_runtime(self):
        root = SCRIPT.parents[1]
        for file in ('installation.md', 'developer_quickstart.md', 'system_overview.md'):
            text = (root / 'docs' / file).read_text()
            self.assertIn('DUCTOR_UID', text)
            self.assertIn('DUCTOR_GID', text)
            self.assertNotIn('runs as UID/GID 1000', text)
        self.assertNotIn('`run.py` is an optional development supervisor',
                         (root / 'docs/architecture.md').read_text())
        english = (root / 'README.md').read_text()
        self.assertNotIn('status.sh checks API-key env only', english)
        self.assertIn('CleanupObserver', english)
        self.assertIn('user settings cannot shorten', english)
        korean = (root / 'README.ko.md').read_text()
        self.assertIn('CleanupObserver', korean)
        script = (root / 'scripts/verify-clean-clone.sh').read_text()
        self.assertNotIn('live-telegramuild.log', script)
        self.assertIn('live-build.log', script)


if __name__ == '__main__':
    unittest.main()
