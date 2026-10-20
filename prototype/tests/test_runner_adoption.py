import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from andrewbot import runtime_readiness as readiness
from andrewbot.runner_adoption import startup_record
from andrewbot.recovery_dispatch import RecoveryDispatch
from runner_recovery_fixture import RunnerRecoveryFixture


class RunnerAdoptionTests(RunnerRecoveryFixture,unittest.TestCase):
    def test_adopted_autonomous_tool_has_no_unjournaled_fallback_without_adapter(self):
        with self.assertRaisesRegex(ValueError,'unsupported_contract'):
            RecoveryDispatch(self.store).run_tool(self.metadata,self.name,self.arguments,lambda:self.fail('fallback executed'))
        self.assertEqual(self.operation(),None)

    def test_restore_external_fence_blocks_new_scope_even_before_database_replacement(self):
        (self.path.parent/'runner-restore-required.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'restore_required'):self.records.admit_scope(self.binding)

    def test_adoption_requires_exact_both_packages_and_is_added_to_proof_policy(self):
        directory=Path(self.tmp.name);controller=directory/'controller';service=directory/'service';controller.mkdir();service.mkdir()
        (controller/'a.py').write_text('controller');(service/'a.py').write_text('service')
        record=dict(contract='runner_result_recovery_v1',version=1,mode='disabled',controller_code=readiness.code_digest(controller),service_code=readiness.code_digest(service),
            controller_store_id='a'*32,runner_store_id='b'*32,key_fingerprint='c'*64,backend_release_sha256='d'*64,controller_uid=os.getuid(),runner_uid=os.getuid())
        path=directory/'adoption.json';path.write_text(json.dumps(record))
        with patch.object(readiness,'RUNNER_ADOPTION',path),patch.object(readiness,'protected_read',lambda p:Path(p).read_bytes()):
            self.assertEqual(readiness.runner_adoption(controller_directory=controller,service_directory=service),record)
            (service/'a.py').write_text('older binary')
            with self.assertRaises(ValueError):readiness.runner_adoption(controller_directory=controller,service_directory=service)

    def test_startup_never_enables_from_flag_alone_or_ignores_adoption_flag_mismatch(self):
        with patch('andrewbot.runner_adoption.runner_adoption',return_value=None):
            with self.assertRaises(ValueError):startup_record('runner',environment={'ANDREW_RUNNER_RECEIPTS_ENABLED':'1'})
        record=dict(mode='disabled',runner_uid=os.getuid(),controller_uid=os.getuid())
        with patch('andrewbot.runner_adoption.runner_adoption',return_value=record):
            with self.assertRaises(ValueError):startup_record('runner',environment={'ANDREW_RUNNER_RECEIPTS_ENABLED':'1'})
            self.assertEqual(startup_record('runner',environment={}),record)

    def test_receiver_owner_lock_is_acquired_before_any_container_recovery(self):
        from unittest.mock import Mock,mock_open
        from andrewbot import runner_service
        runner=Mock()
        with patch('builtins.open',mock_open(read_data='IMAGE=fixture\nWORKSPACE=/workspace\nPROXY_SOCKET=/proxy\n')),\
                patch.object(runner_service,'WorkerConfig'),\
                patch.object(runner_service,'RunnerService',return_value=runner),\
                patch('andrewbot.runner_adoption.startup_record',return_value=dict(mode='enabled')),\
                patch('andrewbot.runner_adoption.open_receiver',side_effect=ValueError('already owned')):
            self.assertEqual(runner_service.main(),1)
        runner.recover.assert_not_called()
