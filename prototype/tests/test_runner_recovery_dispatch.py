import json
import sqlite3
import time
import unittest
from unittest.mock import Mock

from andrewbot.recovery_records import digest
from andrewbot.runner_protocol import RunnerProtocolError, validate_identity
from runner_recovery_fixture import RunnerRecoveryFixture


class RunnerRecoveryDispatchTests(RunnerRecoveryFixture, unittest.TestCase):
    def test_mapping_commits_before_rpc_and_identity_is_reused(self):
        first = self.adapter.prepare(self.metadata, self.name, self.arguments)
        second = self.adapter.prepare(self.metadata, self.name, self.arguments)
        self.assertEqual(first, second)
        self.assertFalse(self.store.connection.in_transaction)
        self.assertEqual(self.operation()['state'], 'prepared')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM cycle_tool_attempts').fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute('SELECT state FROM recovery_effects').fetchone()[0], 'prepared')

    def test_controller_path_avoids_recursive_callback_and_duplicate_legacy_attempt(self):
        self.controller._execute_tool = Mock(side_effect=AssertionError('recursive callback'))
        result = self.controller.execute_tool(self.name, self.arguments, recovery=self.metadata)
        self.assertTrue(result.startswith('Status: ok\n'))
        self.controller._execute_tool.assert_not_called()
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM cycle_tool_attempts').fetchone()[0], 1)
        self.assertEqual(self.controller.execute_tool(self.name, self.arguments, recovery=self.metadata), result)
        self.assertEqual(self.client.calls, 1)

    def assert_control_blocks_grant(self, key, value):
        self.client.before_grant = lambda: self.store.set_state(key, value)
        with self.assertRaises(RunnerProtocolError):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertEqual(self.client.calls, 0)
        self.assertEqual(self.store.get_state(key), value)
        self.assertEqual(self.operation()['state'], 'cancelled')
        self.assertEqual(self.store.connection.execute('SELECT outcome FROM cycle_tool_attempts').fetchone()[0], 'not_executed')

    def test_pause_before_grant_prevents_execution(self):
        self.assert_control_blocks_grant('paused', '1')

    def test_sleep_before_grant_prevents_execution(self):
        self.assert_control_blocks_grant('sleeping', '1')

    def test_revision_change_before_grant_prevents_execution(self):
        self.assert_control_blocks_grant('owner_control_revision', '1')

    def test_restore_barrier_before_grant_prevents_execution(self):
        self.assert_control_blocks_grant('restore_recovery_required', '1')

    def test_backend_restore_barrier_before_grant_prevents_execution(self):
        self.assert_control_blocks_grant('local_recovery_restore_required', '1')

    def test_changed_controller_identity_before_grant_prevents_execution(self):
        self.assert_control_blocks_grant('local_recovery_controller_store_id', '21234567-1234-4321-8123-123456789abc')

    def test_disabled_execution_before_grant_prevents_execution(self):
        self.assert_control_blocks_grant('runner_receipts_enabled', '0')

    def test_ready_then_pending_owner_input_cancels_only_exact_prepared_attempt(self):
        self.client.before_grant = lambda: self.store.add_message(17, 'user', 'stop', queued=True)
        with self.assertRaises(Exception):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertEqual(self.client.calls, 0)
        self.assertFalse(self.store.cycle_uncertain_effects(self.binding['cycle_id']))
        identity = validate_identity(json.loads(self.operation()['identity_json']))
        self.adapter.cancel_prepared(identity)
        with self.assertRaises(Exception):
            self.adapter.grant(self.metadata, identity, dict(contract=identity.contract, version=1,
                action='ready', challenge='1'*64, operation_sha=digest(identity.to_dict())))
        proof = self.records.prepare_owner_yield(self.binding, self.claim, self.scope['scope_token'])
        self.assertEqual(proof['state'], 'owner_yield')

    def test_lost_reply_after_grant_cannot_cancel_or_execute_again(self):
        self.client.lose_reply = True
        with self.assertRaises(Exception):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertEqual(self.operation()['state'], 'admitted')
        self.assertEqual(self.store.connection.execute('SELECT outcome FROM cycle_tool_attempts').fetchone()[0], 'uncertain')
        identity = validate_identity(json.loads(self.operation()['identity_json']))
        with self.assertRaises(Exception):
            self.adapter.cancel_prepared(identity)
        with self.assertRaises(Exception):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertEqual(self.client.calls, 1)

    def test_existing_mapping_without_adapter_never_uses_legacy_callback(self):
        self.adapter.prepare(self.metadata, self.name, self.arguments)
        self.controller.runner_recovery = None
        self.controller._execute_tool = Mock()
        with self.assertRaises(Exception):
            self.controller.execute_tool(self.name, self.arguments, recovery=self.metadata)
        self.controller._execute_tool.assert_not_called()

    def test_mapping_identity_is_immutable(self):
        self.adapter.prepare(self.metadata, self.name, self.arguments)
        with self.assertRaises(sqlite3.IntegrityError), self.store.connection:
            self.store.connection.execute("UPDATE runner_operations SET identity_sha='changed'")

    def test_legacy_uncertain_attempt_is_not_given_a_mapping(self):
        self.records.begin_tool(self.metadata, self.name, self.arguments)
        with self.assertRaises(Exception):
            self.adapter.prepare(self.metadata, self.name, self.arguments)
        self.assertIsNone(self.operation())

    def test_ready_then_expired_lease_prevents_execution(self):
        self.client.before_grant = lambda: self.claim.update(expires_monotonic_ms=1)
        with self.assertRaises(Exception):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertEqual(self.client.calls, 0)
        self.assertEqual(self.operation()['state'], 'cancelled')

    def test_missing_backend_capability_prevents_preparation(self):
        self.store.set_state('runner_receipt_backend_capability', '0')
        with self.assertRaises(Exception):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertIsNone(self.operation())
        self.assertEqual(self.client.calls, 0)

    def test_wrong_receiver_store_cancels_only_prepared_work(self):
        self.client.capability_changes = {'runner_store_id': '21234567-1234-4321-8123-123456789abc'}
        with self.assertRaises(Exception):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertEqual(self.client.calls, 0)
        self.assertEqual(self.operation()['state'], 'cancelled')

    def test_unsupported_receiver_contract_never_dispatches(self):
        self.client.capability_changes = {'contract': 'unsupported'}
        with self.assertRaises(Exception):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertEqual(self.client.calls, 0)

    def test_failed_grant_commit_cancels_exact_prepared_mapping(self):
        def fault(point):
            if point == 'grant_before_commit':
                raise OSError('PRIVATE')
        self.adapter._fault = fault
        with self.assertRaises(RunnerProtocolError):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertEqual(self.operation()['state'], 'cancelled')
        self.assertEqual(self.client.calls, 0)

    def test_lost_grant_after_commit_remains_admitted_without_receiver_execution(self):
        def fault(point):
            if point == 'grant_after_commit':
                raise OSError('PRIVATE')
        self.adapter._fault = fault
        with self.assertRaises(RunnerProtocolError):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertEqual(self.operation()['state'], 'admitted')
        self.assertEqual(self.client.calls, 0)
        identity = validate_identity(json.loads(self.operation()['identity_json']))
        with self.assertRaises(Exception):
            self.adapter.cancel_prepared(identity)

    def test_owner_yield_cancels_mapping_and_legacy_attempt_in_same_transaction(self):
        self.adapter.prepare(self.metadata, self.name, self.arguments)
        self.store.add_message(17, 'user', 'stop', queued=True)
        proof = self.records.prepare_owner_yield(self.binding, self.claim, self.scope['scope_token'])
        self.assertEqual(proof['state'], 'owner_yield')
        self.assertEqual(self.operation()['state'], 'cancelled')
        self.assertFalse(self.store.cycle_uncertain_effects(self.binding['cycle_id']))

    def test_unrelated_unknown_tool_keeps_owner_yield_from_cancelling_any_record(self):
        self.adapter.prepare(self.metadata, self.name, self.arguments)
        effect = dict(kind='tool', effect_id='other-tool', request_sha=digest({'name':'recent_activity','arguments':{}}))
        authorization = self.records.admit_effect(self.binding, self.claim, effect, self.scope['scope_token'])
        other = dict(binding=self.binding, claim=self.claim, effect=effect, scope_token=self.scope['scope_token'], **authorization)
        self.records.begin_tool(other, 'recent_activity', {})
        self.store.add_message(17, 'user', 'stop', queued=True)
        with self.assertRaises(ValueError):
            self.records.prepare_owner_yield(self.binding, self.claim, self.scope['scope_token'])
        self.assertEqual(self.operation()['state'], 'prepared')
        self.assertEqual(self.store.connection.execute('SELECT outcome FROM cycle_tool_attempts').fetchone()[0], 'started')
        self.assertEqual(self.store.connection.execute('SELECT state FROM recovery_effects WHERE admission_id=?',
                         (self.metadata['admission_id'],)).fetchone()[0], 'prepared')

    def test_unrelated_unknown_legacy_attempt_prevents_grant(self):
        self.store.begin_cycle_tool(self.store.research_tool_context(), 'shell.run')
        with self.assertRaises(RunnerProtocolError):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertEqual(self.client.calls, 0)
        self.assertEqual(self.operation()['state'], 'cancelled')
        self.assertTrue(self.store.cycle_uncertain_effects(self.binding['cycle_id']))
