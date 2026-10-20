"""The pre-adoption repair retains exact accepted code and every interrupted store."""
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from contextlib import ExitStack

import test_runner_recovery_installer as fixtures


class ResumeRunnerInstallationTests(unittest.TestCase):
    def incident(self):
        f = fixtures.RunnerRecoveryInstallerTests('runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        with sqlite3.connect(f.database) as db:
            for name in ('runner_research_results', 'runner_imports', 'runner_operations'):
                db.execute('DROP TABLE ' + name)
        with patch.object(f.module, '_migrate_controller', return_value=None):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'runner_operations'):
                f.activate()
        f.events.clear()
        return f

    def resume(self, f, **kwargs):
        return fixtures.load('resume-runner-installation').resume(f.module, f.candidate,
            key=b'k' * 32, uid=(os.getuid(), os.getuid(), os.getgid()),
            service=f.service, no_workers=lambda *args: None, renew_runtime=f.renew, **kwargs)

    def test_repairs_exact_missing_schema_and_retains_partial_and_original_backups(self):
        f = self.incident()
        owner = f.module._owner(f.database)
        partial = f.module.tree(f.directory / 'three-store-before-adoption')
        original = f.module.sha(f.directory / 'paired-before/controller.sqlite3')
        shadow = f.root / 'shadow/andrewbot';shadow.mkdir(parents=True)
        (shadow / '__init__.py').write_text('')
        (shadow / 'store.py').write_text("raise RuntimeError('wrong package')\n")
        before = Path.cwd()
        try:
            os.chdir(shadow.parent)
            result = self.resume(f)
        finally:
            os.chdir(before)
        self.assertTrue(result['installed'])
        self.assertFalse(result['automatic_resume'])
        self.assertFalse(result['execution_enabled'])
        self.assertEqual(f.module._owner(f.database), owner)
        self.assertEqual(f.module.tree(f.directory / 'three-store-before-adoption'), partial)
        self.assertEqual(f.module.sha(f.directory / 'paired-before/controller.sqlite3'), original)
        with sqlite3.connect(f.database) as db:
            for name in ('runner_operations', 'runner_imports', 'runner_research_results'):
                self.assertEqual(db.execute('SELECT count(*) FROM ' + name).fetchone()[0], 0)
        installation = json.loads((f.directory / 'installation.json').read_text())
        self.assertEqual(installation['phase'], 'installed_disabled')
        self.assertEqual(installation['adoption_sha256'], f.module.sha(f.paths['adoption']))
        self.assertTrue((Path(result['repair_evidence']) / 'before/paired-recovery.json').exists())
        self.assertTrue((Path(result['repair_evidence']) / 'before/runner/receipts.sqlite3').exists())
        self.assertTrue((Path(result['repair_evidence']) / 'after/runner-recovery.json').exists())
        self.assertNotIn(('start', 'andrew-control.service'), f.events)

    def test_current_evidence_directory_is_durable_before_migration(self):
        f = self.incident();module = fixtures.load('resume-runner-installation')
        synced = []
        real_fsync = os.fsync
        real_migrate = module._migrate
        def fsync(fd):
            synced.append(Path('/proc/self/fd/' + str(fd)).resolve())
            return real_fsync(fd)
        def migrate(*args):
            self.assertIn(f.directory, synced)
            return real_migrate(*args)
        with patch.object(module.os, 'fsync', side_effect=fsync), patch.object(module, '_migrate', side_effect=migrate):
            module.resume(f.module, f.candidate, key=b'k' * 32,
                uid=(os.getuid(), os.getuid(), os.getgid()), service=f.service,
                no_workers=lambda *args:None, renew_runtime=f.renew)

    def test_rejects_changed_owner_fence_adoption_code_override_key_or_pin(self):
        for kind in ('owner', 'fence', 'adoption', 'code', 'override', 'key', 'pin', 'guard', 'backend', 'backup'):
            with self.subTest(kind=kind):
                f = self.incident()
                if kind == 'owner':
                    with sqlite3.connect(f.database) as db:
                        db.execute("UPDATE state SET value='8' WHERE key='owner_control_revision'")
                if kind == 'fence':Path(f.paths['installing']).unlink()
                if kind == 'adoption':Path(f.paths['adoption']).write_text('{}')
                if kind == 'code':(f.controller / 'controller.py').write_text('changed')
                if kind == 'override':
                    (Path(f.paths['overrides']) / 'andrew-control.service.d/zz-runner-recovery.conf').write_text('changed')
                if kind == 'guard':
                    Path(f.paths['guard']).chmod(0o600);Path(f.paths['guard']).write_text('changed')
                if kind == 'backend':
                    (Path(f.paths['artifacts']) / f.candidate['backend_release_sha256'] / 'recovery-release.json').write_text('changed')
                if kind == 'backup':(f.directory / 'paired-before/controller.sqlite3').write_bytes(b'changed')
                if kind == 'pin':
                    with sqlite3.connect(f.database) as db:
                        db.execute("UPDATE state SET value=? WHERE key='runner_receipt_store_id'", ('e' * 32,))
                if kind == 'key':
                    with self.assertRaises(ValueError):
                        fixtures.load('resume-runner-installation').resume(f.module, f.candidate,
                            key=b'x' * 32, uid=(os.getuid(), os.getuid(), os.getgid()),
                            service=f.service, no_workers=lambda *args:None, renew_runtime=f.renew)
                else:
                    with self.assertRaises((ValueError, FileNotFoundError)):
                        self.resume(f)
                self.assertFalse(any(action == 'start' for action, unit in f.events))

    def test_receiver_owner_lock_or_existing_receipt_blocks_repair(self):
        from andrewbot.runner_receipts import RunnerReceiptStore
        f = self.incident()
        receiver = RunnerReceiptStore(Path(f.paths['runner_root']), b'k' * 32, expected_uid=os.getuid())
        try:
            with self.assertRaises((BlockingIOError, ValueError)):self.resume(f)
        finally:receiver.close()
        from andrewbot.runner_protocol import validate_identity
        from andrewbot.recovery_identity import digest
        receiver = RunnerReceiptStore(Path(f.paths['runner_root']), b'k' * 32, expected_uid=os.getuid())
        try:
            binding = dict(store_id='backend', agent_id='agent', conversation_id='conversation',
                session_id='session', cycle_id='cycle', client_message_id='input', consumer_id='consumer',
                owner_revision='7', input_sha='a'*64, config_sha='b'*64)
            identity = validate_identity(dict(contract='runner_result_recovery_v1', version=1,
                controller_store_id=receiver._identity['controller_store_id'], runner_store_id=receiver._identity['runner_store_id'],
                binding=binding, binding_sha=digest(binding), admission_id='c'*64, effect_id='effect',
                request_sha='d'*64, operation_id='e'*64, legacy_attempt_id='attempt', tool_name='python.run',
                worker_request_sha='f'*64, formatter='runner_text_v1', research_context=dict(
                    session_id='session', cycle_id='cycle', research_id='', research_revision=0)))
            receiver.prepare(identity)
        finally:receiver.close()
        with self.assertRaisesRegex(ValueError, 'empty'):self.resume(f)
        self.assertFalse(any(action == 'start' for action, unit in f.events))

    def test_faults_retain_fence_current_evidence_and_stop_all_units(self):
        for point in ('migration', 'snapshot', 'adoption', 'proof', 'start'):
            with self.subTest(point=point):
                f = self.incident();module = fixtures.load('resume-runner-installation')
                def fail(*args, **kwargs):raise OSError('interrupted repair')
                renew = f.renew
                service = f.service
                with ExitStack() as stack:
                    if point == 'migration':stack.enter_context(patch.object(module, '_migrate', side_effect=fail))
                    if point == 'snapshot':stack.enter_context(patch('andrewbot.runner_backup._snapshot_locked', side_effect=fail))
                    if point == 'adoption':
                        write = f.module._write
                        def stop_adoption(path, *args, **kwargs):
                            if Path(path) == Path(f.paths['adoption']):fail()
                            return write(path, *args, **kwargs)
                        stack.enter_context(patch.object(f.module, '_write', side_effect=stop_adoption))
                    if point == 'proof':renew = fail
                    if point == 'start':
                        def service(action, unit):
                            f.service(action, unit)
                            if action == 'start':fail()
                    with self.assertRaises(OSError):
                        module.resume(f.module, f.candidate, key=b'k' * 32,
                            uid=(os.getuid(), os.getuid(), os.getgid()), service=service,
                            no_workers=lambda *args:None, renew_runtime=renew)
                self.assertTrue(Path(f.paths['installing']).exists())
                self.assertEqual(f.events[-4:], [('stop', unit) for unit in f.module.UNITS])
                self.assertTrue(list(f.directory.glob('repair-*/before/paired-recovery.json')))
                self.assertEqual(f.module._owner(f.database)['paused'], '1')
