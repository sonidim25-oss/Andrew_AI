"""Durable Gmail effects and session recovery using only a simulated mailbox."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from andrewbot.controller import Controller
from andrewbot.dispatch import ControllerDispatch
from andrewbot.gmail_api import GmailError
from andrewbot.gmail_policy import SCOPES
from andrewbot.gmail_tools import GmailTools, compose_message
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store


class ActionRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(temp) / 'state.db'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.tools = GmailTools('/unused-fixture', self.store)
        self.api = Mock()
        self.api.account_fingerprint.return_value = 'a' * 64
        self.api.scopes = set(SCOPES.values())
        self.enterContext(patch('andrewbot.gmail_tools.Vault'))
        self.enterContext(patch('andrewbot.gmail_tools.GmailAPI', return_value=self.api))
        self.policy = dict(enabled=True, read=True, draft=True, send=True, delete=True)
        self.enterContext(patch('andrewbot.gmail_tools.load_permissions', side_effect=lambda: dict(self.policy)))
        self.args = dict(request_id='receipt-fixture-01', to=['fixture@example.com'], subject='Заметка', body='Привет\nSaved work.\n')
        self.agent = Mock()
        self.controller = Controller(self.store, self.agent, 17, autonomous_enabled=True, gmail_tools=self.tools)
        self.runner = SessionRunner(self.store, self.controller, self.agent, enabled=True)
        self.now = datetime.now(timezone.utc)
        self.session = self.runner.start('owner_wake', self.now)
        self.mail = None
        self.posts = 0
        self.mismatch = None
        self.api.request.side_effect = self.mailbox

    def mailbox(self, path, method='GET', payload=None, query=None):
        if method == 'POST':
            # The destination executes the mutation, then the connection dies.
            self.posts += 1
            self.mail = payload.get('raw') or payload.get('message', {}).get('raw')
            raise TimeoutError('private provider detail')
        if path in {'messages', 'drafts'}:
            return {path: [] if self.mismatch == 'missing' else [{'id': 'saved-message'}] * (2 if self.mismatch == 'multiple' else 1)}
        if path == 'messages/trash-target':
            return {'id': 'trash-target', 'labelIds': ['TRASH']}
        raw = self.mail
        if self.mismatch == 'content':
            raw = compose_message({**self.args, 'body': 'Different'}, json.loads(self.action()['lookup'])['message_id_header'])
        if self.mismatch == 'marker':
            raw = compose_message(self.args, '<wrong@andrew.invalid>')
        message = {'id': 'saved-message', 'raw': raw, 'labelIds': [] if self.mismatch == 'label' else ('NOT_SENT' if self.mismatch == 'malformed_labels' else ['SENT', 'DRAFT'])}
        return {'id': 'saved-message', 'message': message} if path.startswith('drafts/') else message

    def action(self):
        return self.store.external_action('gmail', self.args['request_id'])

    def uncertain(self, operation='send'):
        args = self.args if operation != 'delete' else dict(request_id=self.args['request_id'], message_id='trash-target')
        with self.assertRaises(GmailError):
            self.controller.execute_tool('gmail.' + operation, args)
        self.assertEqual(self.action()['state'], 'uncertain')

    def cycle(self):
        self.store.begin_cycle(self.session, 'fixture-cycle', '2026-10-17')
        self.store.checkpoint_cycle_turn('fixture-cycle', 'model_started')

    def completed_hold(self):
        self.cycle()
        self.uncertain()
        self.store.checkpoint_cycle_turn('fixture-cycle', 'model_confirmed')
        self.assertTrue(self.store.hold_confirmed_actions(self.session, 'fixture-cycle'))

    def test_destination_completed_then_timeout_reconciles_without_second_send(self):
        self.uncertain()
        self.store.close()
        self.store = Store(self.path)
        self.tools.store = self.store
        result = self.tools.reconcile(self.args['request_id'])
        self.assertEqual(result['state'], 'confirmed')
        self.assertFalse(result['replayed'])
        self.tools.execute('gmail.send', self.args)
        self.assertEqual(self.posts, 1)
        self.assertEqual(self.store.pending_external_actions('gmail'), [])
        receipt = json.loads(self.action()['receipt'])
        self.assertEqual(receipt['evidence'], 'gmail_sent_message')
        self.assertNotIn(self.args['body'], json.dumps(self.action()))
        self.assertTrue(all(len(call.args) == 1 for call in self.api.request.call_args_list[1:]))

    def test_missing_ambiguous_changed_content_marker_or_label_never_proves_success(self):
        self.uncertain()
        for mismatch in ('missing', 'multiple', 'content', 'marker', 'label', 'malformed_labels'):
            with self.subTest(mismatch=mismatch):
                self.mismatch = mismatch
                self.assertEqual(self.tools.reconcile(self.args['request_id'])['state'], 'uncertain')
                self.assertEqual(self.action()['state'], 'uncertain')
        self.assertEqual(self.posts, 1)

    def test_new_request_id_cannot_bypass_an_uncertain_send(self):
        self.uncertain()
        with self.assertRaises(GmailError):
            self.tools.execute('gmail.send', {**self.args, 'request_id': 'another-request-02'})
        self.assertEqual(self.posts, 1)

    def test_account_change_and_revoked_read_permission_keep_receipt_held(self):
        self.uncertain()
        count = self.api.request.call_count
        self.api.account_fingerprint.return_value = 'b' * 64
        self.assertEqual(self.tools.reconcile(self.args['request_id'])['reason'], 'account_changed')
        self.policy['read'] = False
        self.assertEqual(self.tools.reconcile(self.args['request_id'])['reason'], 'read_permission_disabled')
        self.assertEqual(self.api.request.call_count, count)

    def test_legacy_records_cannot_be_invented_or_bypassed(self):
        self.store.set_state('gmail_mutation:legacy-request', json.dumps({'state': 'uncertain', 'digest': 'legacy'}))
        self.assertEqual(self.tools.reconcile('legacy-request')['reason'], 'no_supported_receipt')
        with self.assertRaises(GmailError):
            self.tools.execute('gmail.send', self.args)
        self.api.request.assert_not_called()

    def test_draft_and_trash_reconciliation_verify_destination_state(self):
        for operation in ('draft', 'delete'):
            with self.subTest(operation=operation):
                self.args['request_id'] = 'fixture-' + operation
                self.uncertain(operation)
                self.assertEqual(self.tools.reconcile(self.args['request_id'])['state'], 'confirmed')
        self.assertEqual(self.posts, 2)

    def test_completed_turn_waits_then_continues_with_verified_receipt(self):
        self.completed_hold()
        held = self.store.add_message(17, 'user', 'Historical uncertain owner input', queued=3)
        self.assertFalse(self.runner.tick(self.now))
        self.agent.autonomous_turn.assert_not_called()
        self.tools.reconcile_pending()
        self.assertTrue(self.runner.tick(self.now))
        self.agent.autonomous_turn.assert_called_once()
        self.assertIn('verified_actions', self.agent.autonomous_turn.call_args.args[0])
        self.assertEqual(self.posts, 1)
        self.assertEqual(self.store.connection.execute('SELECT queued FROM messages WHERE id=?', (held,)).fetchone()[0], 3)

    def test_normal_cycle_creates_hold_when_model_returns_after_uncertain_send(self):
        self.agent.autonomous_turn.side_effect = lambda *a, **k: self.uncertain()
        self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(self.store.get_state('active_session_status'), 'reconciling')
        self.assertEqual(self.runner.active_session_id, self.session)

    def test_restart_restores_confirmed_turn_hold_without_model_or_mutation_calls(self):
        self.cycle()
        self.uncertain()
        self.store.checkpoint_cycle_turn('fixture-cycle', 'model_confirmed')
        self.store.close()
        self.store = Store(self.path)
        self.controller.store = self.store
        self.tools.store = self.store
        runner = SessionRunner(self.store, self.controller, self.agent, enabled=True)
        runner.recover()
        self.assertEqual(runner.active_session_id, self.session)
        self.assertTrue(self.store.action_hold_valid(self.session))
        runner.recover()
        self.tools.reconcile_pending()
        self.assertTrue(runner.tick(self.now))
        self.assertEqual(self.posts, 1)

    def test_started_model_turn_stays_interrupted_even_after_action_proof(self):
        self.cycle()
        self.uncertain()
        self.tools.reconcile_pending()
        self.runner.recover()
        self.assertEqual(self.store.get_state('autonomy_error'), 'interrupted')
        self.assertFalse(self.runner.tick(self.now))
        self.agent.autonomous_turn.assert_not_called()

    def test_owner_control_revision_and_paid_uncertainty_prevent_release(self):
        self.completed_hold()
        self.tools.reconcile_pending()
        self.store.reserve_usage('unsettled', '2026-10', '2026-10-17', .01)
        self.assertFalse(self.store.release_confirmed_action_hold(self.session))
        self.store.set_state('owner_control_revision', '1')
        self.assertFalse(self.store.release_confirmed_action_hold(self.session))
        self.assertEqual(self.store.get_state('autonomy_error'), 'action_outcome_uncertain')

    def test_read_only_worker_skips_sleep_and_backoffs_unsuccessful_search(self):
        self.uncertain()
        self.store.set_state('sleeping', '1')
        count = self.api.request.call_count
        self.controller.reconcile_actions()
        self.assertEqual(self.api.request.call_count, count)
        self.store.set_state('sleeping', '0')
        self.mismatch = 'missing'
        self.controller.reconcile_actions()
        self.controller.reconcile_actions()
        self.assertEqual(self.api.request.call_count, count + 1)

    def test_storage_failure_after_send_retains_receipt_for_later_verification(self):
        self.api.request.side_effect = None
        self.api.request.return_value = {'id': 'saved-message'}
        with patch.object(self.store, 'confirm_external_action', side_effect=OSError('fixture disk error')):
            self.uncertain()
        self.assertEqual(self.action()['state'], 'uncertain')
        with self.assertRaises(GmailError):
            self.tools.execute('gmail.send', self.args)
        self.assertEqual(self.api.request.call_count, 1)

    def test_claim_is_committed_before_network_and_duplicate_admission_is_denied(self):
        observed = []
        def network(*a, **k):
            other = Store(self.path)
            try:
                observed.append(other.external_action('gmail', self.args['request_id'])['state'])
                self.assertIsNone(other.claim_external_action('gmail', 'send', self.args['request_id'], '', 'a'*64, {}, None))
            finally:
                other.close()
            raise TimeoutError()
        self.api.request.side_effect = network
        self.uncertain()
        self.assertEqual(observed, ['uncertain'])

    def test_lookup_worker_shutdown_keeps_storage_open_until_it_stops(self):
        started, release = threading.Event(), threading.Event()
        def blocked():
            started.set()
            release.wait(2)
        dispatch = ControllerDispatch(self.store, self.controller, Mock(), self.runner, Mock(), 17, False, False, recovery_action=blocked)
        dispatch._recovery_worker.start()
        dispatch._recovery_worker.wake()
        self.assertTrue(started.wait(1))
        try:
            self.assertFalse(dispatch.stop(timeout=0))
            self.assertEqual(dispatch.health_snapshot()['recovery']['state'], 'stopping')
        finally:
            release.set()
            self.assertTrue(dispatch.stop(timeout=2))

    def test_receipt_survives_process_exit_before_a_network_response(self):
        path = self.path.parent / 'crash.db'
        script = """
import os,sys
from andrewbot.store import Store
s=Store(sys.argv[1])
s.claim_external_action('gmail','send','crash-request','fixture-digest','a'*64,
    {'message_id_header':'<crash@andrew.invalid>','content_sha256':'fixture-hash'},None)
os._exit(0)
"""
        subprocess.run([sys.executable, '-c', script, str(path)], check=True, env=os.environ)
        other = Store(path)
        try:
            self.assertEqual(other.external_action('gmail', 'crash-request')['state'], 'uncertain')
            self.assertEqual(json.loads(other.get_state('gmail_mutation:crash-request'))['state'], 'uncertain')
        finally:
            other.close()

    def test_sleep_restore_and_unrelated_tool_uncertainty_never_release_a_hold(self):
        self.completed_hold()
        self.tools.reconcile_pending()
        for key in ('paused', 'sleeping', 'restore_recovery_required'):
            self.store.set_state(key, '1')
            self.assertFalse(self.store.release_confirmed_action_hold(self.session))
            self.store.set_state(key, '0')
        with self.store.connection:
            self.store.connection.execute("INSERT INTO cycle_tool_attempts VALUES('opaque','fixture-cycle','shell.run','uncertain',?,NULL)", (self.store.now(),))
        self.assertFalse(self.store.release_confirmed_action_hold(self.session))

    def test_malformed_google_response_stays_uncertain(self):
        self.api.request.side_effect = None
        self.api.request.return_value = {'unexpected': True}
        self.uncertain()
        self.assertEqual(self.api.request.call_count, 1)
        self.api.request.side_effect = ConnectionError('fixture unavailable')
        self.tools.reconcile_pending()
        self.assertEqual(self.action()['state'], 'uncertain')
