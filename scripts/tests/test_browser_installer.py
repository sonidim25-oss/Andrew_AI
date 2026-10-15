"""Installer ordering and real rollback control flow with mocked OS mutations."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
PROJECT=Path(__file__).resolve().parents[2]
SCRIPT=(PROJECT/'scripts/enable-browser-runtime.sh').read_text()

class BrowserInstallerTests(unittest.TestCase):
    def test_acceptance_precedes_activation(self):
        self.assertLess(SCRIPT.index('checks/verify-runtime.py |'),SCRIPT.index('controller_stopped=1'))
        self.assertLess(SCRIPT.index('controller_stopped=1'),SCRIPT.index("proof={**bindings()"))
        self.assertLess(SCRIPT.index("proof={**bindings()"),SCRIPT.index('ANDREW_ISOLATED_RUNTIME_ENABLED=1'))
        self.assertNotIn('flush ruleset',SCRIPT)
        self.assertIn('verify-runtime-state.py" verify',SCRIPT)
    def test_rollback_restores_files_and_only_restarts_previously_stopped_controller(self):
        function=SCRIPT[SCRIPT.index('rollback() {'):SCRIPT.index('trap rollback EXIT')]
        with tempfile.TemporaryDirectory() as root:
            path=Path(root); (path/'etc/andrew').mkdir(parents=True)
            (path/'etc/andrew/runner.conf').write_text('old')
            for name in ('firewall-enabled','andrew-runner.socket.enabled','andrew-browser.socket.enabled','andrew-egress.socket.enabled'):
                (path/name).write_text('disabled\n')
            for stopped in (0,1):
                log=path/f'calls-{stopped}'
                prelude='''cp() { printf 'cp %s\\n' "$*" >> "$CALLS"; }
rm() { printf 'rm %s\\n' "$*" >> "$CALLS"; }
systemctl() { printf 'systemctl %s\\n' "$*" >> "$CALLS"; }
nft() { printf 'nft %s\\n' "$*" >> "$CALLS"; }
'''
                code=prelude+f'BACKUP={json.dumps(str(path))}\nCALLS={json.dumps(str(log))}\ncontroller_stopped={stopped}\nsuccess=0\nmanaged=(/etc/andrew/runner.conf /etc/andrew/runtime-proof.json)\n'+function+'\nfalse\nrollback\n'
                result=subprocess.run(['bash','-c',code],capture_output=True,text=True)
                self.assertEqual(result.returncode,1)
                calls=log.read_text()
                self.assertIn('cp -a '+str(path)+'/etc/andrew/runner.conf /etc/andrew/runner.conf',calls)
                self.assertEqual('restart andrew-control.service' in calls,bool(stopped))
                self.assertNotIn('flush ruleset',calls)

    def test_runroot_paths_are_scoped_and_bound_to_proof(self):
        self.assertIn('50-rootless-runtime.conf',SCRIPT)
        self.assertIn('ReadWritePaths=/run/user/%s',SCRIPT)
        readiness=(PROJECT/'prototype/andrewbot/runtime_readiness.py').read_text()
        self.assertIn('50-rootless-runtime.conf',readiness)
    def test_validation_environment_files_use_distinct_properties(self):
        text=(PROJECT/'scripts/validate-and-start-controller.sh').read_text()
        self.assertIn('--property=EnvironmentFile=/etc/andrew/controller.conf',text)
        self.assertIn('--property=EnvironmentFile=-/etc/andrew/browser-runtime.conf',text)

    def test_installer_preamble_sets_accessible_cwd_before_switching_identity(self):
        preamble=SCRIPT[:SCRIPT.index('if [ "$(id -u)" -ne 0 ]; then')]
        with tempfile.TemporaryDirectory() as root:
            path=Path(root); private=path/'private'; private.mkdir(mode=0o700)
            probe=path/'probe.sh'; probe.write_text(preamble+'printf "%s\\n" "$PWD"\n')
            result=subprocess.run(['bash',str(probe)],cwd=private,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),'/')
