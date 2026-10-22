"""Proven cancellation can return an error; authority/protocol ambiguity cannot."""
import json
import os
import socket
import sqlite3
import time
import unittest
from unittest.mock import Mock

from andrewbot.runner_recovery import prepared_cancellation_proven
from runner_recovery_fixture import RunnerRecoveryFixture


class RunnerSocketTransportTests(unittest.TestCase):
    def test_clean_peer_eof_is_transport_failure(self):
        from andrewbot.runner_socket import RunnerTransportError, read_authenticated_frame
        connection, peer = socket.socketpair()
        with connection, peer:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            peer.close()
            with self.assertRaises(RunnerTransportError):
                read_authenticated_frame(connection, expected_uid=os.getuid(), deadline=time.monotonic() + 1)

    def test_nonempty_data_still_requires_expected_sender(self):
        from andrewbot.runner_socket import RunnerProtocolError, RunnerTransportError, read_authenticated_frame
        connection, peer = socket.socketpair()
        with connection, peer:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            peer.sendall(b'xxxx')
            with self.assertRaises(RunnerProtocolError) as caught:
                read_authenticated_frame(connection, expected_uid=os.getuid() + 1, deadline=time.monotonic() + 1)
            self.assertNotIsInstance(caught.exception, RunnerTransportError)


class RunnerPregrantRejectionTests(RunnerRecoveryFixture, unittest.TestCase):
    def transport_failure(self):
        self.client.execute_receipted = Mock(side_effect=OSError('PRIVATE socket detail'))

    def rejected_result(self):
        try:
            result = self.controller.execute_tool(self.name, self.arguments, recovery=self.metadata)
        except Exception:
            result = None
        self.assertIsNotNone(result, 'A durable proven-unsent cancellation should return a tool error')
        decoded = json.loads(result)
        self.assertFalse(decoded['executed'])
        self.assertEqual(decoded['error']['code'], 'runner_not_executed')
        return result

    def test_transport_failure_before_grant_returns_durable_error_without_execution(self):
        self.transport_failure()
        result = self.rejected_result()
        self.assertEqual(self.client.calls, 0)
        self.assertEqual(self.operation()['state'], 'cancelled')
        self.assertIsNone(self.operation()['admitted_at'])
        self.assertEqual(self.store.connection.execute('SELECT outcome FROM cycle_tool_attempts').fetchone()[0], 'not_executed')
        proof = self.records.effect_result(self.metadata['admission_id'])
        self.assertEqual(proof['state'], 'confirmed')
        self.assertEqual(proof['result'], result)
        self.assertNotIn('runner_receipt', proof)
        self.assertEqual(proof['usage']['runner_pregrant_rejection']['contract'], 'runner_not_executed_v1')

    def test_real_missing_socket_is_classified_before_any_worker_dispatch(self):
        from andrewbot.runner_client import RunnerClient
        self.adapter.client = RunnerClient(str(self.path.parent / 'missing.sock'))
        result = json.loads(self.adapter.execute(self.metadata, self.name, self.arguments))
        self.assertFalse(result['executed'])
        self.assertEqual(result['error']['code'], 'runner_not_executed')
        self.assertEqual(self.operation()['state'], 'cancelled')

    def test_repeated_dispatch_and_restart_reuse_error_without_another_rpc(self):
        from andrewbot.store import Store
        from andrewbot.runner_recovery import RunnerRecoveryAdapter
        from andrewbot.recovery_records import RecoveryRecords
        self.transport_failure()
        result = self.rejected_result()
        self.client.execute_receipted = Mock(side_effect=AssertionError('no repeated RPC'))
        self.assertEqual(self.controller.execute_tool(self.name, self.arguments, recovery=self.metadata), result)
        self.store.close()
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.controller.store = self.store
        self.records = RecoveryRecords(self.store)
        self.adapter = RunnerRecoveryAdapter(self.store, self.client,
            expected_runner_uid=self.adapter.expected_runner_uid, runner_store_id=self.adapter.runner_store_id)
        self.controller.runner_recovery = self.adapter
        self.assertEqual(self.adapter.execute(self.metadata, self.name, self.arguments), result)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM recovery_results').fetchone()[0], 1)

    def test_expired_claim_or_changed_owner_cannot_be_settled_as_safe_error(self):
        for key, value in [('paused', '1'), ('sleeping', '1'), ('owner_control_revision', '1'),
                           ('restore_recovery_required', '1')]:
            with self.subTest(key=key):
                old = self.store.get_state(key, '')
                self.store.set_state(key, value)
                with self.assertRaises(Exception):
                    self.adapter.execute(self.metadata, self.name, self.arguments)
                self.assertEqual(self.store.connection.execute('SELECT count(*) FROM recovery_results').fetchone()[0], 0)
                self.store.set_state(key, old)

    def test_owner_change_during_transport_failure_stays_cancelled_without_receipt(self):
        def fail(*args, **kwargs):
            self.store.set_state('owner_control_revision', '1')
            raise OSError('PRIVATE')
        self.client.execute_receipted = fail
        with self.assertRaises(Exception):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertTrue(prepared_cancellation_proven(self.store, self.metadata['admission_id']))
        self.assertEqual(self.records.effect_result(self.metadata['admission_id'])['state'], 'uncertain')

    def test_lease_expiry_during_transport_failure_cannot_produce_rejection(self):
        def fail(*args, **kwargs):
            self.claim['expires_monotonic_ms'] = 1
            raise OSError('PRIVATE')
        self.client.execute_receipted = fail
        with self.assertRaises(Exception):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertEqual(self.records.effect_result(self.metadata['admission_id'])['state'], 'uncertain')

    def test_unclassified_protocol_failure_still_holds_and_logs_no_private_text(self):
        from andrewbot.runner_protocol import RunnerProtocolError
        self.client.execute_receipted = Mock(side_effect=RunnerProtocolError('PRIVATE protocol mismatch'))
        with self.assertLogs(level='WARNING') as logs:
            with self.assertRaises(Exception):
                self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertNotIn('PRIVATE', ' '.join(logs.output))
        self.assertIn('protocol_error', ' '.join(logs.output))
        self.assertEqual(self.records.effect_result(self.metadata['admission_id'])['state'], 'uncertain')

    def test_safe_failure_logs_stage_and_category_without_command_or_exception_text(self):
        self.transport_failure()
        with self.assertLogs(level='WARNING') as logs:
            self.rejected_result()
        text = ' '.join(logs.output)
        self.assertIn('handshake', text)
        self.assertIn('io_error', text)
        self.assertNotIn('PRIVATE', text)
        self.assertNotIn('print(42)', text)

    def test_failed_error_receipt_write_remains_held_and_never_reexecutes(self):
        self.transport_failure()
        with self.store.connection:
            self.store.connection.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON recovery_results BEGIN SELECT RAISE(ABORT,'fixture write failed'); END")
        with self.assertRaises(Exception):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertTrue(prepared_cancellation_proven(self.store, self.metadata['admission_id']))
        self.assertEqual(self.client.calls, 0)
        self.assertEqual(self.records.effect_result(self.metadata['admission_id'])['state'], 'uncertain')

    def test_error_after_grant_keeps_uncertainty_even_when_worker_has_not_started(self):
        def fault(point):
            if point == 'grant_after_commit':
                raise OSError('PRIVATE lost grant')
        self.adapter._fault = fault
        with self.assertRaises(Exception):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertEqual(self.operation()['state'], 'admitted')
        self.assertEqual(self.client.calls, 0)
        self.assertEqual(self.records.effect_result(self.metadata['admission_id'])['state'], 'uncertain')

    def test_prior_cancellation_without_a_rejection_receipt_is_not_reclassified(self):
        identity = self.adapter.prepare(self.metadata, self.name, self.arguments)
        self.adapter.cancel_prepared(identity)
        with self.assertRaises(Exception):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        self.assertEqual(self.records.effect_result(self.metadata['admission_id'])['state'], 'uncertain')

    def test_forged_rejection_receipt_without_matching_cancelled_mapping_refuses(self):
        self.transport_failure()
        self.rejected_result()
        with self.store.connection:
            self.store.connection.execute("UPDATE runner_operations SET state='admitted',admitted_at='now'")
        with self.assertRaises(Exception):
            self.records.effect_result(self.metadata['admission_id'])
