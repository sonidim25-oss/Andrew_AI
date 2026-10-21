import os
import unittest

from andrewbot.runner_format import SAFE_ERRORS
from andrewbot.runner_protocol import validate_identity
from andrewbot.runner_recovery import reconcile_pending_runner_result
from runner_recovery_fixture import RunnerRecoveryFixture
import json


class RunnerExecutionErrorTests(RunnerRecoveryFixture, unittest.TestCase):
    def error_response(self, category='execution_failed'):
        return dict(status='error', stdout='wrote artifact before failure',
                    stderr='ValueError: invalid input', artifacts=['partial.txt'],
                    safe_error=category, visit=None)

    def test_normal_execution_error_returns_saved_error_without_holding_or_reexecution(self):
        self.client.response = self.error_response()
        result = self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertIn('Status: error', result)
        self.assertIn('ValueError: invalid input', result)
        self.assertIn('partial.txt', result)
        self.assertEqual(self.adapter.execute(self.metadata, self.name, self.arguments), result)
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(self.operation()['state'], 'imported')
        proof = self.records.effect_result(self.operation()['admission_id'])
        self.assertEqual(proof['state'], 'confirmed')
        self.assertIn('Status: error', proof['result'])

    def test_lost_error_reply_imports_once_while_preserving_owner_controls_and_history(self):
        self.client.response = self.error_response()
        self.client.lose_reply = True
        with self.assertRaises(Exception):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.store.set_state('paused', '1')
        self.store.set_state('owner_control_revision', '99')
        before = dict(self.store.connection.execute('SELECT key,value FROM state'))
        self.assertTrue(reconcile_pending_runner_result(self.store, self.client, self.binding, expected_runner_uid=os.getuid()))
        self.assertFalse(reconcile_pending_runner_result(self.store, self.client, self.binding, expected_runner_uid=os.getuid()))
        identity = validate_identity(json.loads(self.operation()['identity_json']))
        self.adapter.import_result(identity, self.client.receipt)
        self.assertEqual(dict(self.store.connection.execute('SELECT key,value FROM state')), before)
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(self.client.lookups, 1)
        for table in ('runner_imports', 'recovery_results', 'runner_research_results'):
            self.assertEqual(self.store.connection.execute('SELECT count(*) FROM ' + table).fetchone()[0], 1)

    def test_other_error_categories_and_unverified_cleanup_stay_nonimportable(self):
        from andrewbot.recovery_identity import digest
        from andrewbot.runner_protocol import validate_evidence
        self.client.lose_reply = True
        with self.assertRaises(Exception):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        identity = validate_identity(json.loads(self.operation()['identity_json']))
        for category in SAFE_ERRORS - {'execution_failed'}:
            with self.subTest(category=category):
                evidence = self.client.receipt.to_dict()
                evidence['response'] = self.error_response(category)
                evidence['response_sha'] = digest(evidence['response'])
                self.assertFalse(validate_evidence(evidence, identity).importable)
                with self.assertRaises(Exception):
                    self.adapter.import_result(identity, evidence)
        evidence = self.client.receipt.to_dict()
        evidence['response'] = self.error_response()
        evidence['response_sha'] = digest(evidence['response'])
        evidence['cleanup_verified'] = False
        with self.assertRaises(Exception):
            self.adapter.import_result(identity, evidence)
        self.assertEqual(self.operation()['state'], 'admitted')
        self.assertEqual(self.client.calls, 1)
