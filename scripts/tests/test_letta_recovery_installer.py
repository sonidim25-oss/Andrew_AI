import importlib.util
import json
import os
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
                 patch.object(installer.grp,'getgrnam',return_value=SimpleNamespace(gr_gid=os.getgid())):
                result=installer.activate(candidate,service=service)
            self.assertFalse(result['automatic_resume'])
            self.assertIn('ANDREW_LOCAL_RECOVERY_ENABLED=0',(root/'controller.conf').read_text())
            self.assertEqual((target/'recovery.py').read_text(),'candidate');snapshot.assert_called_once()
            self.assertEqual([call.args for call in service.call_args_list[:4]],
                [('stop','andrew-control.service'),('stop','andrew-letta.service'),
                 ('assert-stopped','andrew-control.service'),('assert-stopped','andrew-letta.service')])
            self.assertFalse(any(call.args[0]=='start' for call in service.call_args_list))

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
