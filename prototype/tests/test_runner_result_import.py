import json
import os
import subprocess
import sys
import unittest

from andrewbot.recovery_records import canonical, digest
from andrewbot.runner_format import format_runner_response
from andrewbot.runner_protocol import validate_identity
from andrewbot.recovery import RecoveryCoordinator, RecoveryInterrupted, check_recovery_outcomes
from andrewbot.runner_recovery import reconcile_pending_runner_result
from runner_recovery_fixture import RunnerRecoveryFixture, RUNNER_ID


class RunnerResultImportTests(RunnerRecoveryFixture, unittest.TestCase):
    def saved_result(self):
        self.client.lose_reply = True
        with self.assertRaises(Exception):
            self.adapter.execute(self.metadata, self.name, self.arguments)
        return validate_identity(json.loads(self.operation()['identity_json'])), self.client.receipt

    def test_import_twice_has_one_result_legacy_settlement_and_research_event(self):
        identity, receipt = self.saved_result()
        first = self.adapter.import_result(identity, receipt)
        self.assertEqual(first, format_runner_response(receipt.to_dict()['response']))
        self.assertEqual(self.adapter.import_result(identity, receipt), first)
        self.assertEqual(self.client.calls, 1)
        for table in ['recovery_results','runner_imports','runner_research_results']:
            self.assertEqual(self.store.connection.execute('SELECT count(*) FROM '+table).fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM research_events WHERE kind='tool_used'").fetchone()[0], 1)
        proof = self.records.effect_result(identity.admission_id)
        self.assertTrue(proof['runner_receipt']['cleanup_verified'])
        self.assertEqual(proof['runner_receipt']['operation_id'], identity.operation_id)

    def test_historical_import_preserves_changed_owner_controls_and_original_focus(self):
        identity, receipt = self.saved_result()
        for key, value in dict(paused='1', sleeping='1', owner_control_revision='99',
                               restore_recovery_required='1', local_recovery_restore_required='1',
                               research_focus_id='changed', research_focus_session=identity.binding['session_id']).items():
            self.store.set_state(key, value)
        before = dict(self.store.connection.execute('SELECT key,value FROM state'))
        self.adapter.import_result(identity, receipt)
        self.assertEqual(dict(self.store.connection.execute('SELECT key,value FROM state')), before)
        event = self.store.connection.execute("SELECT entry_id,cycle_id,session_id FROM research_events WHERE kind='tool_used'").fetchone()
        self.assertEqual(tuple(event), ('', identity.binding['cycle_id'], identity.binding['session_id']))
        self.assertEqual(self.store.connection.execute('SELECT untrusted FROM recovery_attempts').fetchone()[0], 1)

    def test_bounded_reconciliation_uses_lookup_without_execute(self):
        identity, receipt = self.saved_result()
        self.assertTrue(reconcile_pending_runner_result(self.store, self.client, self.binding, expected_runner_uid=os.getuid()))
        self.assertEqual(self.client.lookups, 1)
        self.assertEqual(self.client.calls, 1)
        self.assertFalse(reconcile_pending_runner_result(self.store, self.client, self.binding, expected_runner_uid=os.getuid()))
        self.assertEqual(self.client.lookups, 1)

    def test_non_success_missing_or_conflicting_receipts_never_import(self):
        identity, receipt = self.saved_result()
        for changes in [dict(response_sha='0'*64), dict(cleanup_verified=False), dict(state='prepared'),
                        dict(identity_sha='0'*64), dict(extra='PRIVATE')]:
            value = receipt.to_dict(); value.update(changes)
            with self.assertRaises(Exception):
                self.adapter.import_result(identity, value)
        self.assertEqual(self.operation()['state'], 'admitted')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM recovery_results').fetchone()[0], 0)
        self.assertEqual(self.client.calls, 1)

    def test_import_commit_fault_is_all_or_nothing_in_real_subprocess(self):
        identity, receipt = self.saved_result()
        script = '''import json,os,sys
from andrewbot.store import Store
from andrewbot.runner_protocol import validate_identity
from andrewbot.runner_recovery import RunnerRecoveryAdapter
value=json.load(sys.stdin);s=Store(value['database'])
a=RunnerRecoveryAdapter(s,None,expected_runner_uid=os.getuid(),runner_store_id=value['runner'])
a._fault=lambda point: os._exit(71) if point==value['point'] else None
a.import_result(validate_identity(value['identity']),value['receipt'])
'''
        for point in ['import_before_commit', 'import_after_commit']:
            value=dict(database=str(self.path), runner=RUNNER_ID, identity=identity.to_dict(), receipt=receipt.to_dict(), point=point)
            result = subprocess.run([sys.executable,'-c',script], input=json.dumps(value), text=True,capture_output=True)
            self.assertEqual(result.returncode, 71, result.stderr)
            committed = point == 'import_after_commit'
            for table in ['runner_imports','recovery_results','runner_research_results']:
                self.assertEqual(self.store.connection.execute('SELECT count(*) FROM '+table).fetchone()[0], int(committed))
            self.assertEqual(self.operation()['state'], 'imported' if committed else 'admitted')

    def test_valid_missing_prepared_admitted_uncertain_or_timeout_evidence_stays_held(self):
        identity, receipt = self.saved_result()
        for state in ['missing','prepared','admitted','uncertain']:
            value = receipt.to_dict(); value.update(state=state, response=None, response_sha=None, cleanup_verified=False)
            self.client.receipt = value
            self.assertFalse(reconcile_pending_runner_result(self.store, self.client, self.binding, expected_runner_uid=os.getuid()))
        error = receipt.to_dict(); error['response'].update(status='error', safe_error='timeout')
        error['response_sha'] = digest(error['response'])
        with self.assertRaises(Exception):
            self.adapter.import_result(identity, error)
        self.assertEqual(self.operation()['state'], 'admitted')
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM runner_imports').fetchone()[0], 0)

    def test_consistently_changed_identity_cannot_import_under_another_mapping(self):
        identity, receipt = self.saved_result()
        for key, value in [('admission_id','0'*64), ('operation_id','0'*64), ('request_sha','0'*64),
                           ('worker_request_sha','0'*64), ('effect_id','another-effect'), ('legacy_attempt_id','another-attempt')]:
            document = identity.to_dict(); document[key] = value
            other = validate_identity(document)
            proof = receipt.to_dict(); proof.update(identity=other.to_dict(), identity_sha=digest(other.to_dict()))
            with self.assertRaises(Exception):
                self.adapter.import_result(other, proof)
        self.assertEqual(self.operation()['state'], 'admitted')

    def test_changed_store_identity_refuses_historical_import(self):
        identity, receipt = self.saved_result()
        self.store.set_state('local_recovery_controller_store_id','21234567-1234-4321-8123-123456789abc')
        with self.assertRaises(Exception):
            self.adapter.import_result(identity, receipt)
        self.assertEqual(self.operation()['state'], 'admitted')

    def test_original_research_revision_and_topic_survive_changed_focus(self):
        original = self.store.save_research_entry({'question':'Original question'})
        self.store.focus_research_entry(original['record_id'])
        identity, receipt = self.saved_result()
        other = self.store.save_research_entry({'question':'New question'})
        self.store.focus_research_entry(other['record_id'])
        self.store.save_research_entry({'findings':'Changed after execution'}, original['record_id'], 1)
        self.adapter.import_result(identity, receipt)
        observation = self.store.connection.execute("SELECT entry_id FROM research_events WHERE kind='tool_used'").fetchone()
        self.assertEqual(observation['entry_id'], original['record_id'])
        saved = json.loads(self.store.connection.execute('SELECT context_json FROM runner_research_results').fetchone()[0])
        self.assertEqual(saved['research_id'], original['record_id'])
        self.assertEqual(saved['research_revision'], 1)
        self.assertEqual(self.store.research_entry(original['record_id'])['revision'], 2)

    def test_import_preserves_legacy_unknowns_held_messages_and_provider_reservations(self):
        identity, receipt = self.saved_result()
        attempts = [self.store.begin_cycle_tool(self.store.research_tool_context(), 'python.run') for _ in range(3)]
        for _ in range(15):
            self.store.add_message(17, 'user', 'old held fixture')
        with self.store.connection:
            self.store.connection.execute('UPDATE messages SET queued=3')
            self.store.connection.execute('INSERT INTO budget_reservations VALUES(?,?,?,?,?)', ('f'*32,'2026-10','2026-10-19',.1,self.store.now()))
        reservations = [tuple(row) for row in self.store.connection.execute('SELECT * FROM budget_reservations')]
        self.adapter.import_result(identity, receipt)
        self.assertEqual([tuple(row) for row in self.store.connection.execute('SELECT * FROM budget_reservations')], reservations)
        self.assertEqual(self.store.uncertain_message_count(17), 15)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM cycle_tool_attempts WHERE outcome='started'").fetchone()[0], 3)
        with self.assertRaises(RecoveryInterrupted):
            check_recovery_outcomes(self.store, self.binding)
        self.assertEqual(self.client.calls, 1)

    def test_coordinator_imports_before_held_scope_return_without_backend_or_model_call(self):
        identity, receipt = self.saved_result()
        with self.store.connection:
            self.store.connection.execute("UPDATE recovery_attempts SET state='held'")
        self.store.set_state('paused','1')
        def forbidden():
            self.fail('historical import called the backend')
        coordinator = RecoveryCoordinator(self.store, self.controller, forbidden, enabled=True)
        self.assertTrue(coordinator.reconcile_one())
        self.assertEqual(self.operation()['state'], 'imported')
        self.assertEqual(self.store.get_state('paused'), '1')
        self.assertEqual(self.client.calls, 1)
