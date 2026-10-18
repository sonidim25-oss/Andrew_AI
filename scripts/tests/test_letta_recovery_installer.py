import importlib.util
from contextlib import redirect_stdout
import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('recovery_installer',ROOT/'scripts/install-letta-recovery.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)


class RecoveryInstallerTests(unittest.TestCase):
    def test_initial_activation_installs_disabled_override_after_both_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);target=root/'target';target.mkdir();(target/'recovery.py').write_text('old')
            source=root/'source/prototype/andrewbot';source.mkdir(parents=True);(source/'recovery.py').write_text('candidate')
            release=root/'releases'/('a'*64);release.mkdir(parents=True)
            candidate={'directory':str(root),'source':str(root/'source'),'backend_release':str(root/'backend'),
                'deployed_files':installer.tree_hashes(target),
                'backend_manifest_sha256':'a'*64,'originals':{'recovery.py':installer.sha(target/'recovery.py')}}
            service=Mock();h=Mock()
            def replace(path,data,gid,**kwargs):Path(path).write_bytes(data)
            h.replace.side_effect=replace
            with patch.multiple(installer,TARGET=target,ARTIFACTS=root/'releases',DATABASE=root/'db',BACKEND_HOME=root/'home',
                    BACKEND_DROPIN=root/'backend.conf',CONTROL_DROPIN=root/'controller.conf'),\
                 patch.object(installer,'verify_candidate'),patch.object(installer,'verify_release',return_value='a'*64),\
                 patch.object(installer,'helper',return_value=h),patch.object(installer,'create_paired_recovery_snapshot') as snapshot,\
                 patch.object(installer,'checkpoint_controller_wal') as checkpoint,\
                 patch.object(installer.grp,'getgrnam',return_value=SimpleNamespace(gr_gid=os.getgid())):
                result=installer.activate(candidate,service=service)
            self.assertFalse(result['automatic_resume'])
            self.assertIn('ANDREW_LOCAL_RECOVERY_ENABLED=0',(root/'controller.conf').read_text())
            self.assertEqual((target/'recovery.py').read_text(),'candidate');snapshot.assert_called_once()
            checkpoint.assert_called_once_with(root/'db')
            self.assertEqual([call.args for call in service.call_args_list[:4]],
                [('stop','andrew-control.service'),('stop','andrew-letta.service'),
                 ('assert-stopped','andrew-control.service'),('assert-stopped','andrew-letta.service')])
            self.assertFalse(any(call.args[0]=='start' for call in service.call_args_list))

    def test_idle_check_closes_its_sqlite_reader(self):
        from andrewbot.store import Store
        from andrewbot.media import MediaService
        media=installer.helper('install-autonomy-media')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'db';store=Store(path)
            MediaService(store,Path(directory)/'artifacts',1)
            store.set_state('paused','1');store.close()
            connect=sqlite3.connect;readers=[]
            def reader(*args,**kwargs):
                db=connect(path.resolve().as_uri()+'?mode=ro',uri=True);readers.append(db);return db
            with patch.object(media.sqlite3,'connect',side_effect=reader):media.idle()
            with self.assertRaises(sqlite3.ProgrammingError):readers[0].execute('SELECT 1')

    def test_checkpoint_retains_committed_wal_rows_before_snapshot(self):
        from andrewbot.memory_backup import create_paired_recovery_snapshot
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'db';home=root/'home';home.mkdir()
            code="from andrewbot.store import Store; import os,sys; s=Store(sys.argv[1]); s.set_state('committed_fixture','preserved'); s.reserve_usage('old-reservation','2026-10','2026-10-16',0.01); os._exit(0)"
            subprocess.run([sys.executable,'-c',code,str(path)],check=True,env={**os.environ,'PYTHONPATH':str(ROOT/'prototype')})
            self.assertGreater(Path(str(path)+'-wal').stat().st_size,0)
            result=subprocess.run([sys.executable,'-c',installer.CHECKPOINT_PROGRAM,str(path)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),[0,0,0])
            create_paired_recovery_snapshot(path,home,root/'snapshot',workers_quiescent=True)
            with sqlite3.connect(root/'snapshot/controller.sqlite3') as db:
                self.assertEqual(db.execute("SELECT value FROM state WHERE key='committed_fixture'").fetchone()[0],'preserved')
                self.assertEqual(db.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],1)

    def test_checkpoint_refuses_active_reader_without_deleting_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'db';writer=sqlite3.connect(path);self.addCleanup(writer.close)
            writer.execute('PRAGMA journal_mode=WAL');writer.execute('CREATE TABLE fixture(value)');writer.execute('INSERT INTO fixture VALUES(1)');writer.commit()
            reader=sqlite3.connect(path);self.addCleanup(reader.close);reader.execute('BEGIN');reader.execute('SELECT * FROM fixture').fetchall()
            writer.execute('INSERT INTO fixture VALUES(2)');writer.commit()
            result=subprocess.run([sys.executable,'-c',installer.CHECKPOINT_PROGRAM,str(path)],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertGreater(Path(str(path)+'-wal').stat().st_size,0)
            self.assertEqual(writer.execute('SELECT count(*) FROM fixture').fetchone()[0],2)

    def test_resume_requires_exact_accepted_candidate_before_service_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.mkdir()
            candidate={'directory':str(root),'source':str(source)};(root/'candidate.json').write_text(json.dumps(candidate))
            with patch.object(installer,'verify_candidate') as verify:
                self.assertEqual(installer.load_accepted_candidate(root),candidate)
                verify.assert_called_once_with(candidate,require_acceptance=True)
            with patch.object(installer,'verify_candidate',side_effect=ValueError('Acceptance incomplete')):
                with self.assertRaisesRegex(ValueError,'Acceptance'):installer.load_accepted_candidate(root)

    def test_failed_checkpoint_never_snapshots_replaces_or_starts_services(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);target=root/'target';target.mkdir();(target/'recovery.py').write_text('old')
            candidate={'directory':str(root),'deployed_files':installer.tree_hashes(target),
                'originals':{'recovery.py':installer.sha(target/'recovery.py')}}
            service=Mock();helper=Mock()
            with patch.multiple(installer,TARGET=target,BACKEND_DROPIN=root/'backend.conf',CONTROL_DROPIN=root/'controller.conf'),\
                 patch.object(installer,'verify_candidate'),patch.object(installer,'helper',return_value=helper),\
                 patch.object(installer,'checkpoint_controller_wal',side_effect=ValueError('Checkpoint busy')),\
                 patch.object(installer,'create_paired_recovery_snapshot') as snapshot:
                with self.assertRaisesRegex(ValueError,'Checkpoint'):installer.activate(candidate,service=service)
            snapshot.assert_not_called();helper.replace.assert_not_called()
            self.assertEqual((target/'recovery.py').read_text(),'old')
            self.assertFalse(any(call.args[0]=='start' for call in service.call_args_list))
            self.assertEqual([call.args for call in service.call_args_list[-2:]],[('stop','andrew-control.service'),('stop','andrew-letta.service')])

    def test_resume_rejects_changed_runtime_proof_before_service_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'runtime-proof-before.json').write_text('old proof')
            candidate={'directory':str(root)};helper=Mock();helper.load_readiness.return_value.protected_read.return_value=b'{}'
            helper.load_readiness.return_value.bindings.return_value={}
            with patch.object(sys,'argv',['installer','--resume-apply',str(root)]),\
                 patch.object(installer.os,'geteuid',return_value=0),patch.object(installer,'helper',return_value=helper),\
                 patch.object(installer,'load_accepted_candidate',return_value=candidate),patch.object(installer,'_service') as service:
                with self.assertRaisesRegex(ValueError,'Runtime proof changed'):installer.main()
            service.assert_not_called()

    def test_resume_uses_bound_acceptance_and_existing_activation_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'runtime-proof-before.json').write_bytes(b'{}')
            candidate={'directory':str(root)};helper=Mock();helper.load_readiness.return_value.protected_read.return_value=b'{}'
            helper.load_readiness.return_value.bindings.return_value={};result={'installed':True,'automatic_resume':False}
            with patch.object(sys,'argv',['installer','--resume-apply',str(root)]),redirect_stdout(io.StringIO()),\
                 patch.object(installer.os,'geteuid',return_value=0),patch.object(installer,'helper',return_value=helper),\
                 patch.object(installer,'load_accepted_candidate',return_value=candidate) as load,\
                 patch.object(installer,'activate',return_value=result) as activate,\
                 patch.object(installer,'run_acceptance') as acceptance,patch.object(installer,'_service') as service:
                installer.main()
            load.assert_called_once_with(root);activate.assert_called_once_with(candidate);acceptance.assert_not_called()
            self.assertEqual(json.loads((root/'activated.json').read_text()),result)
            self.assertEqual((root/'runtime-proof-before.json').read_bytes(),b'{}')
            self.assertEqual([call.args for call in service.call_args_list],[('start','andrew-letta.service'),('start','andrew-control.service')])

    def test_enable_requires_completed_disabled_activation(self):
        service=Mock()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'disabled activation'):installer.enable({'directory':directory},service=service)
        service.assert_not_called()

    def test_unknown_deployment_rejected_before_service_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);deployed=root/'deployed';deployed.mkdir();(deployed/'store.py').write_text('# unknown deployment')
            source=root/'source/prototype/andrewbot';source.mkdir(parents=True);(source/'store.py').write_text('# candidate')
            manifest=root/'manifest.json';manifest.write_text(json.dumps({'version':1,'contract':installer.CONTRACT,
                'controller':{'store.py':{'tested':installer.sha(source/'store.py'),'deployed_bases':{}}}}))
            with patch.object(installer,'ROOT',root/'source'),patch.object(installer,'MANIFEST',manifest),\
                 patch.object(installer,'verify_release',return_value='a'*64),patch.object(installer,'helper',return_value=Mock()):
                with self.assertRaisesRegex(ValueError,'Unknown deployment'):installer.build_candidate(root/'release',deployed,root/'candidate')

    def test_initial_activation_refuses_automatic_resume_before_any_service_call(self):
        service=Mock()
        with self.assertRaisesRegex(ValueError,'disabled'):installer.activate({},enable_resume=True,service=service)
        service.assert_not_called()

    def test_changed_candidate_refuses_activation_before_any_service_call(self):
        service=Mock()
        with patch.object(installer,'verify_candidate',side_effect=ValueError('Changed candidate')):
            with self.assertRaises(ValueError):installer.activate({},service=service)
        service.assert_not_called()

    def test_unsafe_rollback_keeps_both_services_stopped(self):
        service=Mock()
        with tempfile.TemporaryDirectory() as directory,patch.object(installer,'helper',return_value=Mock()),\
             patch.object(installer,'assert_rollback_exclusion',side_effect=ValueError('New admission in retained snapshot')):
            with self.assertRaises(ValueError):installer.rollback({'directory':directory},service=service)
        self.assertIn(('stop','andrew-control.service'),[call.args for call in service.call_args_list])
        self.assertIn(('stop','andrew-letta.service'),[call.args for call in service.call_args_list])
        self.assertFalse(any(call.args[0]=='start' for call in service.call_args_list))

    def test_old_runtime_exclusion_rejects_even_a_legacy_queued_input(self):
        from andrewbot.store import Store
        from andrewbot.memory_backup import create_paired_recovery_snapshot
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);database=root/'db.sqlite3';store=Store(database);store.close()
            home=root/'home';home.mkdir();(home/'input-dispositions-v2.json').write_text(json.dumps({'entries':{'old-input':{'disposition':'queued'}},'reservations':{}}))
            snapshot=root/'snapshot';create_paired_recovery_snapshot(database,home,snapshot,workers_quiescent=True)
            with self.assertRaisesRegex(ValueError,'exclusion'):installer.assert_rollback_exclusion(snapshot)
