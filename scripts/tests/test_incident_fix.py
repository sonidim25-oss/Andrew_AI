import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock, Mock, patch


def load(name):
    path = Path(__file__).resolve().parents[1] / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IncidentFixTests(unittest.TestCase):
    def test_checked_in_deployment_manifest_matches_prepared_sources(self):
        root = Path(__file__).resolve().parents[2]
        manifest = json.loads((root / 'scripts/p1-fix-manifest.json').read_text())
        self.assertIn('memory.py', manifest)
        self.assertIn('telegram_menu.py', manifest)
        for name, entry in manifest.items():
            with self.subTest(name=name):
                actual = hashlib.sha256((root / 'prototype/andrewbot' / name).read_bytes()).hexdigest()
                self.assertEqual(entry['tested'], actual)

    def test_metadata_uses_pinned_letta_date_and_approval_schema(self):
        diagnostic = load('diagnose-session')
        result = diagnostic.message_metadata({
            'message_type': 'approval_request_message', 'date': '2026-10-15T13:48:47.220Z',
            'tool_call': {'name': 'memory_search', 'arguments': '{"query":"private text"}'},
        })
        self.assertEqual(result['created_at'], '2026-10-15T13:48:47.220000+00:00')
        self.assertEqual(result['message_type'], 'approval_request_message')
        self.assertTrue(result['tool_calls'][0]['arguments_valid_object'])
        self.assertNotIn('private text', json.dumps(result))

    def exercise_installer(self, fail_start=False, changed_source=False, fail_acceptance=False, fail_proof=False,
                           p1=False, intermediate=False, unknown_installed=False):
        installer = load('install-p1-fixes' if p1 else 'install-incident-fix')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'prototype/andrewbot'
            source.mkdir(parents=True)
            target = root / 'installed'
            target.mkdir()
            (root / 'scripts').mkdir()
            backup = root / 'backup'
            backup.mkdir()
            proof_path = root / 'runtime-proof.json'
            proof_path.write_bytes(b'{}')
            readiness = Mock(PROOF=proof_path)
            readiness.protected_read.return_value = b'{}'
            readiness.bindings.return_value = {'code': 'old', 'policy': 'same'}
            manifest = {}
            installed_data = (b'# unreviewed change\n' if unknown_installed else
                              b'# reviewed intermediate\n' if intermediate else b'# original\n')
            names = (['appserver.py', 'browser_history.py', 'controller.py', 'diary.py', 'dispatch.py', 'main.py', 'memory.py', 'sessions.py', 'store.py', 'telegram.py', 'telegram_menu.py']
                     if p1 else ['appserver.py', 'configuration.py', 'gateway.py', 'sessions.py'])
            for name in names:
                (source / name).write_bytes(b'# tested\n')
                (target / name).write_bytes(installed_data)
                manifest[name] = {k: hashlib.sha256(v).hexdigest() for k, v in
                                  [('tested', b'# tested\n'), ('original', b'# original\n')]}
                if p1:
                    manifest[name]['reviewed'] = {hashlib.sha256(b'# reviewed intermediate\n').hexdigest(): ['fixture-revision']}
            (root / 'scripts' / ('p1-fix-manifest.json' if p1 else 'incident-fix-manifest.json')).write_text(json.dumps(manifest))
            if changed_source:
                (source / names[0]).write_bytes(b'# untested change\n')
            starts = []
            def command(*args):
                if args[:2] == ('systemctl', 'start'):
                    starts.append(args)
                    if fail_start and len(starts) == 1:
                        raise RuntimeError('startup failed')
                return 'Andrew Telegram controller started' if args[0] == 'journalctl' else 'active'
            with patch.object(installer, 'ROOT', root), patch.object(installer, 'TARGET', target), \
                 patch.object(installer.os, 'geteuid', return_value=0), \
                 patch.object(installer.grp, 'getgrnam', return_value=types.SimpleNamespace(gr_gid=1000)), \
                 patch.object(installer, 'check_idle'), patch.object(installer, 'command', side_effect=command) as commands, \
                 patch.object(installer, 'wait_for_letta', create=True), \
                 patch.object(installer, 'load_readiness', return_value=readiness), \
                 patch.object(installer, 'acceptance', side_effect=RuntimeError('failed acceptance') if fail_acceptance else None) as acceptance, \
                 patch.object(installer, 'renew_proof', side_effect=RuntimeError('failed proof') if fail_proof else None) as renewal, \
                 patch.object(installer.tempfile, 'mkdtemp', return_value=str(backup)), \
                 patch.object(installer, 'replace', side_effect=lambda path, data, gid, **kwargs: path.write_bytes(data)), \
                 patch('builtins.print'):
                if fail_start or changed_source or fail_acceptance or fail_proof or unknown_installed:
                    with self.assertRaises(SystemExit):
                        installer.main()
                else:
                    installer.main()
                if changed_source or unknown_installed:
                    commands.assert_not_called()
                elif fail_acceptance:
                    self.assertFalse(any(call.args[:2] == ('systemctl', 'stop') for call in commands.call_args_list))
                    renewal.assert_not_called()
                else:
                    acceptance.assert_called_once()
                    renewal.assert_called_once()
                    if p1:
                        actions = [call.args for call in commands.call_args_list]
                        stop = actions.index(('systemctl', 'stop', installer.SERVICE))
                        restart = actions.index(('systemctl', 'restart', 'andrew-letta.service'))
                        start = actions.index(('systemctl', 'start', installer.SERVICE))
                        self.assertLess(stop, restart)
                        self.assertLess(restart, start)
            expected = installed_data if fail_start or changed_source or fail_acceptance or fail_proof or unknown_installed else b'# tested\n'
            self.assertTrue(all((target / name).read_bytes() == expected for name in names))
            if fail_start:
                self.assertEqual(len(starts), 2)
            self.assertEqual(proof_path.read_bytes(), b'{}')

    def test_installer_verifies_startup(self):
        self.exercise_installer()

    def test_installer_restores_modules_after_startup_failure(self):
        self.exercise_installer(fail_start=True)

    def test_installer_rejects_untested_source_before_stopping_service(self):
        self.exercise_installer(changed_source=True)

    def test_acceptance_failure_leaves_controller_running(self):
        self.exercise_installer(fail_acceptance=True)

    def test_failed_proof_renewal_restores_code_and_proof(self):
        self.exercise_installer(fail_proof=True)

    def test_proof_renewal_rejects_unrelated_binding_changes(self):
        installer = load('install-incident-fix')
        readiness = Mock()
        readiness.bindings.return_value = {'code': 'new', 'policy': 'changed'}
        with patch.object(installer, 'replace') as replace, self.assertRaises(RuntimeError):
            installer.renew_proof(readiness, {}, {'code': 'old', 'policy': 'original'}, Path('/unused'))
        replace.assert_not_called()

    def test_proof_renewal_updates_only_code_and_provenance(self):
        installer = load('install-incident-fix')
        readiness = Mock(PROOF=Path('/unused/proof.json'))
        readiness.bindings.return_value = {'code': 'new', 'policy': 'same', 'service_code': 'same'}
        original = {'code': 'old', 'policy': 'same', 'service_code': 'same', 'schema': 1,
                    'checks': {'network': 'passed'}}
        with patch.object(installer, 'replace') as replace:
            installer.renew_proof(readiness, original, {'code': 'old', 'policy': 'same', 'service_code': 'same'}, Path('/evidence'))
        proof = json.loads(replace.call_args.args[1])
        self.assertEqual(proof['code'], 'new')
        self.assertEqual(proof['checks'], original['checks'])
        self.assertEqual(proof['evidence'], '/evidence')
        readiness.validate_runtime_proof.assert_called_once()

    def test_p1_installer_success(self):
        self.exercise_installer(p1=True)

    def test_p1_installer_accepts_exact_reviewed_intermediate_hashes(self):
        self.exercise_installer(p1=True, intermediate=True)

    def test_p1_installer_restores_intermediate_modules_after_failed_startup(self):
        self.exercise_installer(p1=True, intermediate=True, fail_start=True)

    def test_p1_installer_rejects_unknown_installed_hashes_before_service_changes(self):
        self.exercise_installer(p1=True, unknown_installed=True)

    def test_p1_installer_restores_modules_and_proof_after_failed_startup(self):
        self.exercise_installer(p1=True, fail_start=True)

    def test_p1_installer_leaves_controller_running_after_failed_acceptance(self):
        self.exercise_installer(p1=True, fail_acceptance=True)

    def test_p1_installer_runs_service_identity_commands_from_accessible_directory(self):
        installer = load('install-p1-fixes')
        with patch.object(installer.subprocess, 'run', return_value=types.SimpleNamespace(stdout='ok')) as run:
            installer.command('fixture-command')
        self.assertEqual(run.call_args.kwargs['cwd'], '/')

    def test_startup_verification_allows_more_than_twenty_seconds_and_saves_evidence(self):
        installer = load('install-p1-fixes')
        journals = iter(['Startup check: Telegram bot authentication (getMe)', 'Andrew Telegram controller started'])
        def command(*args):
            if args[0] == 'journalctl':
                return next(journals)
            return 'active'
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(installer, 'command', side_effect=command), \
             patch.object(installer.time, 'monotonic', side_effect=[0, 25]), \
             patch.object(installer.time, 'sleep') as sleep:
            installer.verify_controller_startup(Path(directory))
            sleep.assert_called_once()
            self.assertIn('controller started', (Path(directory) / 'controller-startup.txt').read_text())

    def test_startup_verification_keeps_failure_logs_and_stops_when_service_exits(self):
        installer = load('install-p1-fixes')
        def command(*args):
            return 'Startup validation failed: fixture' if args[0] == 'journalctl' else 'failed'
        with tempfile.TemporaryDirectory() as directory, patch.object(installer, 'command', side_effect=command):
            with self.assertRaises(RuntimeError):
                installer.verify_controller_startup(Path(directory))
            self.assertIn('Startup validation failed', (Path(directory) / 'controller-startup.txt').read_text())

    def test_backend_readiness_retries_connection_refusal_without_model_work(self):
        installer = load('install-p1-fixes')
        with patch.object(installer.socket, 'create_connection', side_effect=[ConnectionRefusedError(), MagicMock()]) as connect, \
             patch.object(installer.time, 'sleep'):
            installer.wait_for_letta()
        self.assertEqual(connect.call_count, 2)
