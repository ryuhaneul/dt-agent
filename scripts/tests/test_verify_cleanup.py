"""Clean isolated verification resources on exit, signals, preparation failure, and token leaks."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'verify-clean-clone.sh').exists():ROOT=Path(__file__).parent
SHARED=ROOT/'verify-clean-clone.sh'


SHIM='''#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$VERIFY_AUDIT"
if [[ $1 == compose ]]; then
    [[ " $* " == *" -p $VERIFY_PROJECT "* ]] || exit 90
    [[ " $* " == *" down "* ]] || exit 91
    [[ ${VERIFY_FAIL_CLEANUP:-0} != 1 ]] || exit 7
    rm -f -- "$VERIFY_RESOURCES"
elif [[ $1 == image && $2 == ls ]]; then
    cat "$VERIFY_IMAGES"
elif [[ $1 == image && $2 == rm ]]; then
    tag=${@: -1};[[ $tag == "$VERIFY_PROJECT-"* ]] || exit 92
    awk -v tag="$tag" '$0!=tag' "$VERIFY_IMAGES" > "$VERIFY_IMAGES.new"
    mv "$VERIFY_IMAGES.new" "$VERIFY_IMAGES"
elif [[ $1 == ps || $1 == network || $1 == volume ]]; then
    [[ " $* " == *" label=com.docker.compose.project=$VERIFY_PROJECT "* ]] && \
        [[ -f $VERIFY_RESOURCES ]] && printf '%s\\n' "$VERIFY_PROJECT-resource" || true
else
    exit 93
fi
'''


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.external=tempfile.TemporaryDirectory(prefix='cleanup-fixture.')
        self.base=Path(self.external.name);self.project='ductorverifySYNTHETIC'
        (self.base/'docker').write_text(SHIM);(self.base/'docker').chmod(0o700)
        self.primary=self.base/'primary';self.secondary=self.base/'secondary'
        self.primary.write_text('SYNTHETIC_PRIMARY_ONLY');self.secondary.write_text('SYNTHETIC_SECONDARY_ONLY')
        for path in (self.primary,self.secondary):path.chmod(0o600)
        text=SHARED.read_text()
        self.functions=text[text.index('dc() {'):text.index('recreate_bot() {')]
        self.functions+=text[text.index('cleanup_actions() {'):text.index('prepare() {')]
        self.functions+=text[text.index('check_log_secrets() {'):text.index('EXPECTED_AUTH_FAIL_RE=')]
    def tearDown(self):self.external.cleanup()
    def run_harness(self,mode,defect=None,partial=None):
        with tempfile.TemporaryDirectory(prefix='ductor-verify.') as private:
            root=Path(private);(root/'repo').mkdir();(root/'home').mkdir()
            for path in ('repo/compose.yaml','repo/.env','verify.override.yml'): (root/path).touch()
            (root/'live-token').write_bytes(self.primary.read_bytes())
            (root/'live-token').chmod(0o600)
            (root/'home/.env.shared').write_text('BROWSER_TOKEN="SYNTHETIC_BROWSER_ONLY"\n')
            (root/'verify.log').write_text('SYNTHETIC cleanup\n'+('SYNTHETIC_PRIMARY_ONLY\n' if defect=='leak' else ''))
            audit=self.base/'audit';audit.write_text('')
            images=self.base/'images';images.write_text(self.project+'-ductor-bot:latest\n'+self.project+'-browser:latest\n')
            assets=self.base/'resources';assets.touch()
            if partial:
                assets.unlink();images.write_text('')
                if partial in ('before-clone','token-copy-only'):
                    for path in ('repo/compose.yaml','repo/.env','verify.override.yml'):(root/path).unlink()
                    (root/'home/.env.shared').unlink()
                    if partial=='before-clone':(root/'live-token').unlink()
            before=[(p.stat().st_mode,p.stat().st_size,p.stat().st_mtime_ns) for p in (self.primary,self.secondary)]
            header='set -euo pipefail\nTMP='+shlex.quote(str(root))+'\nP='+self.project+'\nSTAGE=live-lifecycle\nMODE=live\nKEEP=0\nLIVE_SRC='+shlex.quote(str(self.primary))+'\n'
            script=self.base/'harness.sh';action={'normal':'exit 0','failure':'exit 7','abort':'exit 130','deadline':'exit 130','INT':'kill -INT $$','TERM':'kill -TERM $$'}[mode]
            script.write_text(header+self.functions+'\n'+action+'\n')
            env=dict(os.environ,PATH=str(self.base)+os.pathsep+os.environ['PATH'],VERIFY_PROJECT=self.project,
                     VERIFY_AUDIT=str(audit),VERIFY_IMAGES=str(images),VERIFY_RESOURCES=str(assets),VERIFY_FAIL_CLEANUP='1' if defect=='cleanup' else '0')
            result=subprocess.run(['bash',str(script)],env=env,capture_output=True,text=True,timeout=10)
            expected={'normal':0,'failure':7,'abort':130,'deadline':130,'INT':130,'TERM':143}[mode]
            if defect:expected=1
            self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
            self.assertFalse(root.exists(),'private home/token copies were retained')
            self.assertEqual(before,[(p.stat().st_mode,p.stat().st_size,p.stat().st_mtime_ns) for p in (self.primary,self.secondary)])
            self.assertEqual(images.read_text(),'')
            self.assertEqual(assets.exists(),defect=='cleanup')
            if partial=='before-clone':self.assertNotIn('PASS token log check',result.stdout)
            else:self.assertIn('FAIL: token appeared' if defect=='leak' else 'PASS token log check',result.stdout)
            for secret in ('SYNTHETIC_PRIMARY_ONLY','SYNTHETIC_SECONDARY_ONLY','SYNTHETIC_BROWSER_ONLY'):self.assertNotIn(secret,result.stdout+result.stderr)
            commands=audit.read_text().splitlines()
            self.assertTrue(commands);self.assertFalse(any('--force-recreate' in command for command in commands))
            for command in commands:
                if command.startswith('compose '):self.assertIn(' -p '+self.project+' ',command)
                if command.startswith('image rm '):self.assertIn(self.project+'-',command)
    def test_cleanup_trap_for_all_terminal_paths(self):
        for mode in ('normal','failure','abort','deadline','INT','TERM'):
            with self.subTest(mode=mode):self.run_harness(mode)
    def test_partial_preparation_failures_remove_created_files_and_preserve_originals(self):
        for partial in ('before-clone','token-copy-only','prepared-home'):
            with self.subTest(partial=partial):self.run_harness('failure',partial=partial)
    def test_cleanup_failure_and_token_leak_cannot_pass(self):
        for defect in ('cleanup','leak'):
            with self.subTest(defect=defect):self.run_harness('normal',defect=defect)


if __name__=='__main__':unittest.main()
