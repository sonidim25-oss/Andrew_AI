import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]


def load(name):
    spec=importlib.util.spec_from_file_location(name.replace('-','_'),ROOT/'scripts'/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


class RunnerRecoveryInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.module=load('install-runner-recovery');self.manifest=json.loads(self.module.MANIFEST.read_text())
        self.controller=self.root/'controller/andrewbot';self.runner=self.root/'runner-package/andrewbot'
        for target in (self.controller,self.runner):shutil.copytree(ROOT/'prototype/andrewbot',target,ignore=shutil.ignore_patterns('__pycache__'))
        self.release=self.root/'release';self.release.mkdir();(self.release/'recovery-release.json').write_text('{}')
        def tree(root,**kwargs):
            result={}
            for path in sorted(Path(root).rglob('*')):
                if '__pycache__' in path.parts or path.suffix=='.pyc':continue
                if path.is_symlink():raise ValueError('unsafe fixture link')
                if path.is_file():result[path.relative_to(root).as_posix()]=self.module.sha(path)
            return result
        def checkpoint(path):
            db=sqlite3.connect(path)
            try:db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            finally:db.close()
        self.stub=SimpleNamespace(tree_hashes=tree,verify_release=lambda p:self.module.sha(Path(p)/'recovery-release.json'),checkpoint_controller_wal=checkpoint)
        self.enterContext(patch.object(self.module,'helper',return_value=self.stub))
        self.directory=self.root/'candidate'
        self.candidate=self.module.build_candidate(self.release,self.controller,self.runner,self.directory)
        self.accept()
        from andrewbot.store import Store
        self.database=self.root/'control/controller.sqlite3';store=Store(self.database)
        for k,v in dict(paused='1',sleeping='0',owner_control_revision='7',local_recovery_controller_store_id='a'*32).items():store.set_state(k,v)
        store.close()
        self.backend=self.root/'backend';(self.backend/'local-recovery-v1').mkdir(parents=True)
        (self.backend/'local-recovery-v1/identity.json').write_text(json.dumps(dict(store_id='b'*32,version=1)))
        self.paths=dict(controller=str(self.controller),runner=str(self.runner),database=str(self.database),backend_home=str(self.backend),runner_root=str(self.root/'receipts'),
            adoption=str(self.root/'etc/adoption.json'),installing=str(self.root/'etc/installing.json'),overrides=str(self.root/'systemd'),guard=str(self.root/'bin/guard.py'),
            artifacts=str(self.root/'releases'),credential_dir=str(self.root/'credentials'))
        self.events=[]
        self.service=lambda action,unit:self.events.append((action,unit))
        self.runtime=lambda p:dict(proof={},policy={},image='fixture')
        self.renew=lambda p,b:None

    def accept(self,**checks):
        keys={'node':'passed','core':'passed','scripts':'passed','pristine_protocol':'passed','service_imports':'passed','distinct_uid':'passed'};keys.update(checks)
        self.module._write(self.directory/'acceptance.json',dict(candidate_sha256=self.module.sha(self.directory/'candidate.json'),checks=keys,logs={}))

    def provision(self,paths,control_uid,runner_uid,runner_gid):
        from andrewbot.runner_receipts import RunnerReceiptStore
        receipts=RunnerReceiptStore(Path(paths['runner_root']),b'k'*32,expected_uid=os.getuid())
        receipts.provision(controller_store_id='a'*32);identity=receipts._identity;receipts.close()
        with sqlite3.connect(self.database) as db:
            for k,v in dict(runner_receipt_store_id=identity['runner_store_id'],runner_receipts_enabled='0').items():
                db.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,v))
        self.module._checkpoint(self.database)
        return identity

    def activate(self,**kwargs):
        return self.module.activate(self.candidate,paths=self.paths,service=self.service,provision=self.provision,uid=(os.getuid(),os.getuid(),os.getgid()),
            runtime_snapshot=self.runtime,renew_runtime=self.renew,no_workers=lambda *args:None,**kwargs)

    def test_candidate_source_removes_group_and_other_write_permissions(self):
        source = Path(self.candidate['source'])
        writable = [str(path.relative_to(source)) for path in [source, *source.rglob('*')]
                    if path.stat().st_mode & 0o022]
        self.assertEqual(writable, [])

    def test_candidate_binds_both_packages_and_refuses_tampering(self):
        self.module.verify_candidate(self.candidate,require_acceptance=True)
        (self.directory/'source/runtime-services/andrewbot/runner_service.py').write_text('tampered')
        with self.assertRaises(ValueError):self.module.verify_candidate(self.candidate)
        self.assertEqual(self.events,[])

    def test_first_install_migrates_controller_before_provisioning_and_snapshot(self):
        with sqlite3.connect(self.database) as db:
            for table in ('runner_research_results','runner_imports','runner_operations'):db.execute('DROP TABLE '+table)
        provision=self.provision
        def check(paths,*ids):
            with sqlite3.connect(self.database) as db:
                db.execute('SELECT operation_id FROM runner_operations')
                db.execute('SELECT operation_id FROM runner_imports')
            return provision(paths,*ids)
        self.provision=check
        self.activate()

    def test_controller_migration_ignores_a_caller_package_with_the_same_name(self):
        shadow = self.root / 'shadow'
        package = shadow / 'andrewbot'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        (package / 'store.py').write_text("raise RuntimeError('wrong controller package')\n")
        before = Path.cwd()
        try:
            os.chdir(shadow)
            self.module._migrate_controller(self.paths, os.getuid())
        finally:
            os.chdir(before)
        with sqlite3.connect(self.database) as db:
            db.execute('SELECT operation_id FROM runner_operations')

    def test_unknown_deployed_module_is_refused_before_service_actions(self):
        (self.controller/'controller.py').write_text('unknown deployment')
        with self.assertRaisesRegex(ValueError,'Unknown deployed'):
            self.module.build_candidate(self.release,self.controller,self.runner,self.root/'rejected')
        self.assertEqual(self.events,[])

    def test_distinct_uid_acceptance_is_required_for_activation(self):
        self.accept(distinct_uid='skipped')
        with self.assertRaisesRegex(ValueError,'acceptance incomplete'):self.activate()
        self.assertEqual(self.events,[])

    def test_disabled_activation_snapshots_all_stores_and_preserves_pause(self):
        before=self.module._owner(self.database);result=self.activate()
        self.assertFalse(result['execution_enabled']);self.assertFalse(result['automatic_resume'])
        self.assertEqual(self.module._owner(self.database),before)
        self.assertTrue((self.directory/'paired-before/paired-recovery.json').exists())
        self.assertTrue((self.directory/'three-store-before-adoption/runner-recovery.json').exists())
        self.assertFalse(Path(self.paths['installing']).exists())
        self.assertNotIn(('start','andrew-control.service'),self.events)
        self.assertEqual(json.loads(Path(self.paths['adoption']).read_text())['mode'],'disabled')

    def test_incomplete_stop_or_backup_failure_never_starts_any_service(self):
        def service(action,unit):
            self.events.append((action,unit))
            if action=='assert-stopped':raise ValueError('busy worker')
        self.service=service
        with self.assertRaises(ValueError):self.activate()
        self.assertFalse(any(action=='start' for action,unit in self.events))

    def test_snapshot_failure_keeps_install_fence_and_services_stopped(self):
        with patch.object(self.module,'create_paired_recovery_snapshot',side_effect=ValueError('snapshot failed')):
            with self.assertRaises(ValueError):self.activate()
        self.assertTrue(Path(self.paths['installing']).exists())
        self.assertFalse(any(action=='start' for action,unit in self.events))

    def test_changed_owner_after_stop_refuses_installation_and_resume(self):
        original=self.service
        def service(action,unit):
            original(action,unit)
            if action=='assert-stopped' and unit=='andrew-control.service':
                with sqlite3.connect(self.database) as db:db.execute("UPDATE state SET value='8' WHERE key='owner_control_revision'")
        self.service=service
        with self.assertRaises(ValueError):self.activate()
        self.assertFalse(any(action=='start' for action,unit in self.events))

    def test_enable_requires_exact_peers_and_keeps_owner_controls(self):
        self.activate();record=json.loads(Path(self.paths['adoption']).read_text());before=self.module._owner(self.database)
        peer=dict(backend_contract=self.module.CONTRACT,backend_store_id=record['backend_store_id'],held_tool_result_v1=1,
            receiver=dict(contract=self.module.CONTRACT,version=1,formatter='runner_text_v1',controller_store_id=record['controller_store_id'],runner_store_id=record['runner_store_id'],execution_enabled=False))
        with self.assertRaises(ValueError):self.module.enable(self.candidate,paths=self.paths,peers={},service=self.service,runtime_snapshot=self.runtime,renew_runtime=self.renew)
        result=self.module.enable(self.candidate,paths=self.paths,peers=peer,service=self.service,runtime_snapshot=self.runtime,renew_runtime=self.renew)
        self.assertTrue(result['enabled']);self.assertFalse(result['wake_requested']);self.assertEqual(self.module._owner(self.database),before)
        self.assertEqual(json.loads(Path(self.paths['adoption']).read_text())['mode'],'enabled')

    def test_failed_stop_records_fence_and_attempts_to_stop_every_unit(self):
        original=self.service
        def service(action,unit):
            original(action,unit)
            if action=='stop' and unit=='andrew-letta.service':raise OSError('fixture stop failed')
        self.service=service
        with self.assertRaises(OSError):self.activate()
        self.assertTrue(Path(self.paths['installing']).exists())
        self.assertEqual(json.loads((self.directory/'installation.json').read_text())['phase'],'interrupted')
        self.assertTrue(all(('stop',unit) in self.events for unit in self.module.UNITS))
        self.assertFalse(any(action=='start' for action,unit in self.events))

    def test_failed_service_start_reinstates_fence_and_stops_every_unit(self):
        original=self.service
        def service(action,unit):
            original(action,unit)
            if action=='start' and unit=='andrew-runner.service':raise OSError('fixture service start failed')
        self.service=service
        with self.assertRaises(OSError):self.activate()
        self.assertTrue(Path(self.paths['installing']).exists())
        self.assertEqual(self.events[-4:],[('stop',unit) for unit in self.module.UNITS])

    def test_failed_enable_start_reinstates_fence_and_stops_every_unit(self):
        self.activate();record=json.loads(Path(self.paths['adoption']).read_text())
        peer=dict(backend_contract=self.module.CONTRACT,backend_store_id=record['backend_store_id'],held_tool_result_v1=1,
            receiver=dict(contract=self.module.CONTRACT,version=1,formatter='runner_text_v1',controller_store_id=record['controller_store_id'],runner_store_id=record['runner_store_id'],execution_enabled=False))
        original=self.service
        def service(action,unit):
            original(action,unit)
            if action=='start' and unit=='andrew-control.service':raise OSError('fixture controller start failed')
        with self.assertRaises(OSError):
            self.module.enable(self.candidate,paths=self.paths,peers=peer,service=service,runtime_snapshot=self.runtime,renew_runtime=self.renew)
        self.assertTrue(Path(self.paths['installing']).exists())
        self.assertEqual(self.events[-4:],[('stop',unit) for unit in self.module.UNITS])

    def test_interruptions_after_each_install_phase_leave_a_fence_and_no_restarts(self):
        for point in ('stopped','guards_installed','controller_installed','runner_installed','controller_schema_ready','installed_disabled'):
            with self.subTest(point=point):
                other=RunnerRecoveryInstallerTests('runTest');other.setUp();self.addCleanup(other.doCleanups)
                record=other.module._record
                def interrupted(directory,phase,**fields):
                    record(directory,phase,**fields)
                    if phase==point:raise OSError('fixture installer process lost')
                with patch.object(other.module,'_record',side_effect=interrupted):
                    with self.assertRaises(OSError):other.activate()
                self.assertTrue(Path(other.paths['installing']).exists())
                self.assertFalse(any(action=='start' for action,unit in other.events))

    def test_adopted_store_refuses_automatic_rollback(self):
        self.activate()
        with self.assertRaisesRegex(ValueError,'cannot roll back'):self.module.rollback(self.candidate,paths=self.paths)

    def test_new_diagnostics_expose_only_fixed_receipt_states_and_counts(self):
        import subprocess
        secret='PRIVATE-RUNNER-REQUEST-AND-OUTPUT'
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO cycles(cycle_id,session_id,local_day,status,started_at) VALUES('cycle','aaaaaaaaaaaa','2026-10-19','failed','2026-10-19T00:00:00+00:00')")
            db.execute("INSERT INTO runner_operations VALUES(?,?,?,?,?,?,?,?,?,?)",('c'*64,'effect','attempt','b'*64,secret,'d'*64,'imported','2026-10-19',None,None))
            db.execute("INSERT INTO runner_imports VALUES(?,?,?,?,?,?)",('c'*64,'e'*64,secret,'f'*64,'a'*64,'2026-10-19'))
        result=subprocess.run([sys.executable,str(ROOT/'scripts/diagnose-session.py'),'--current','--brief','--database',str(self.database),'--letta-home',str(self.backend)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr);self.assertNotIn(secret,result.stdout)
        self.assertEqual(json.loads(result.stdout)['runner_result_recovery']['operations']['imported'],1)

    def test_credential_creation_is_idempotent_and_never_replaces_missing_history_key(self):
        module=load('provision-runner-receipts-key');directory=self.root/'credentials';receipts=self.root/'receipts'
        def creds(args,**kwargs):
            import subprocess
            if 'encrypt' in args:Path(args[-1]).write_bytes(b'encrypted:'+kwargs['input']);output=b''
            else:output=Path(args[-2]).read_bytes()[10:]
            return subprocess.CompletedProcess(args,0,output,b'')
        with patch.object(module.subprocess,'run',side_effect=creds):
            first=module.provision(directory,receipts);encrypted=(directory/module.NAME).read_bytes()
            self.assertEqual(module.provision(directory,receipts),first);self.assertEqual((directory/module.NAME).read_bytes(),encrypted)
            (directory/module.NAME).unlink();receipts.mkdir();(receipts/'identity.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'original credential'):module.provision(directory,receipts)
