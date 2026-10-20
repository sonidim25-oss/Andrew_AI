import json
import unittest
from unittest.mock import Mock

from andrewbot.recovery import LocalRecoveryClient, RecoveryCoordinator, RecoveryInterrupted
from andrewbot.runner_protocol import validate_identity
from runner_recovery_fixture import RunnerRecoveryFixture


class RunnerBackendRecoveryTests(RunnerRecoveryFixture, unittest.TestCase):
    def imported(self):
        self.client.lose_reply = True
        with self.assertRaises(Exception):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        identity = validate_identity(json.loads(self.operation()['identity_json']))
        self.adapter.import_result(identity, self.client.receipt)
        self.store.connection.execute("UPDATE recovery_attempts SET state='held',hold_reason='tool_uncertain'")
        self.store.connection.commit()
        self.coordinator = RecoveryCoordinator(self.store, self.controller, enabled=True)
        self.backend = Mock()
        self.backend.claim.return_value = dict(claim_id='new-claim', fence=2, revision=15)
        self.backend.inspect.return_value = dict(revision=17)
        self.row = self.coordinator._pending()[0]
        self.state = dict(phase='held', hold_reason='tool_uncertain', revision=14,
                          effect_admission_ids=[identity.admission_id], state='held')
        self.capability = dict(runner_result_recovery_v1=1, held_tool_result_v1=1)
        return identity

    def test_exact_import_uses_fresh_claim_confirmation_and_release_without_resume(self):
        identity = self.imported()
        self.assertTrue(self.coordinator._runner_tool_receipt(self.backend, self.binding, self.row, self.state, self.capability))
        args = self.backend.reconcile_tool.call_args.args
        self.assertEqual(args[0], self.binding)
        self.assertEqual(args[3:], (identity.admission_id, 15))
        self.assertNotEqual(args[1]['admission_id'], self.scope['admission_id'])
        self.backend.release.assert_called_once()
        self.backend.resume.assert_not_called()
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(self.coordinator._pending()[0]['state'], 'active')

    def test_pause_restore_sleep_or_owner_change_prevents_new_claim(self):
        self.imported()
        for key, value in [('paused','1'), ('sleeping','1'), ('restore_recovery_required','1'),
                           ('local_recovery_restore_required','1'), ('owner_control_revision','99')]:
            old = self.store.get_state(key, '0')
            self.store.set_state(key, value)
            with self.assertRaises(ValueError):
                self.coordinator._runner_tool_receipt(self.backend,self.binding,self.row,self.state,self.capability)
            self.store.set_state(key, old)
        self.store.add_message(17,'user','Owner input during recovery',queued=True)
        with self.assertRaises(ValueError):
            self.coordinator._runner_tool_receipt(self.backend,self.binding,self.row,self.state,self.capability)
        self.backend.claim.assert_not_called()
        self.assertEqual(self.client.calls, 1)

    def test_missing_capability_other_hold_or_missing_import_cannot_confirm(self):
        self.imported()
        for cap in [{}, dict(runner_result_recovery_v1=1)]:
            self.assertFalse(self.coordinator._runner_tool_receipt(self.backend,self.binding,self.row,self.state,cap))
        for reason in ['provider_uncertain','owner_blocked','storage_limit','restore_required']:
            self.assertFalse(self.coordinator._runner_tool_receipt(self.backend,self.binding,self.row,
                dict(self.state,hold_reason=reason),self.capability))
        self.backend.claim.assert_not_called()

    def test_lost_confirmation_reply_releases_claim_and_reuses_saved_operation(self):
        self.imported();self.backend.reconcile_tool.side_effect=RecoveryInterrupted('authority_lost')
        with self.assertRaises(RecoveryInterrupted):
            self.coordinator._runner_tool_receipt(self.backend,self.binding,self.row,self.state,self.capability)
        self.backend.release.assert_called_once()
        self.assertEqual(self.operation()['state'], 'imported')
        self.assertEqual(self.client.calls, 1)

    def test_client_sends_ids_and_fence_without_result_bytes(self):
        self.imported();transport=Mock();client=LocalRecoveryClient(transport)
        client.reconcile_tool(self.binding,self.scope,self.claim,self.operation()['admission_id'],12)
        command=transport.request.call_args.args[0]
        self.assertEqual(command['type'],'recovery_reconcile_tool')
        self.assertEqual(command['expected_revision'],12)
        self.assertNotIn('result',command)
        self.assertNotIn('runner_receipt',command)
