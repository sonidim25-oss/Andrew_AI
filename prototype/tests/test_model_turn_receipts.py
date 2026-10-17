"""A completed protocol turn must not look unfinished after a controller crash."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from andrewbot.appserver import AppServerClient
from andrewbot.controller import Controller
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store


class ModelTurnReceiptTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / 'state.db'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.agent = Mock()
        self.controller = Controller(self.store, self.agent, 17, autonomous_enabled=True)
        self.runner = SessionRunner(self.store, self.controller, self.agent, enabled=True)
        self.now = datetime.now(timezone.utc)
        self.session = self.runner.start('owner_wake', self.now)
        self.runtime = dict(agent_id='fixture-agent', conversation_id='fixture-conversation')
        self.store.set_state('letta_agent_id', self.runtime['agent_id'])
        self.store.set_state('letta_conversation_id', self.runtime['conversation_id'])
        self.store.begin_cycle(self.session, 'fixture-cycle', '2026-10-17')
        self.store.checkpoint_cycle_turn('fixture-cycle', 'model_started')
        self.request = 'fixture-request'

    def event(self, **event):
        return self.store.record_model_turn_receipt(self.session, 'fixture-cycle', self.runtime, dict(request_id=self.request, **event))

    def terminal(self, **changes):
        return dict(type='turn_finished', stop_reason='end_turn', run_id='run-fixture', turn_id='turn-fixture', terminal_consumer_ids=[self.request], **changes)

    def proof(self, terminal=None):
        self.event(kind='prepared', client_message_id='fixture-message')
        self.event(kind='accepted')
        return self.event(kind='completed', terminal=self.terminal() if terminal is None else terminal, assistant_ids=['assistant-fixture'])

    def restart(self):
        self.store.close()
        self.store = Store(self.path)
        self.controller.store = self.store
        self.runner = SessionRunner(self.store, self.controller, self.agent, enabled=True)
        self.runner.recover()

    def test_bound_success_restores_session_without_replaying_the_completed_turn(self):
        self.assertTrue(self.proof())
        self.restart()
        self.assertEqual(self.runner.active_session_id, self.session)
        self.assertEqual(self.store.connection.execute('SELECT status FROM cycles').fetchone()[0], 'complete')
        self.agent.autonomous_turn.assert_not_called()
        self.assertTrue(any('no input or tool replayed' in row['event'] for row in self.store.recent_activity()))
        self.assertTrue(self.runner.tick(self.now))
        self.assertEqual(self.agent.autonomous_turn.call_count, 1)
        self.assertIn('recovery receipt', self.agent.autonomous_turn.call_args.args[0])

    def test_acceptance_without_terminal_proof_remains_held(self):
        self.event(kind='prepared', client_message_id='fixture-message')
        self.event(kind='accepted')
        self.restart()
        self.assertEqual(self.store.get_state('autonomy_error'), 'interrupted')
        self.agent.autonomous_turn.assert_not_called()

    def test_partial_history_is_not_terminal_proof(self):
        from andrewbot.memory import SessionHistory
        history = SessionHistory(self.store, 17, session_id=self.session, cycle_id='fixture-cycle')
        history.capture(dict(kind='assistant_delta', source_id='old-message', content='I finished everything.'))
        history.capture(dict(kind='step_finished', complete=True))
        self.restart()
        self.assertEqual(self.store.get_state('autonomy_error'), 'interrupted')

    def test_foreign_or_incomplete_terminal_events_never_prove_completion(self):
        self.event(kind='prepared', client_message_id='fixture-message')
        self.event(kind='accepted')
        for change in (dict(terminal_consumer_ids=['other-input']), dict(terminal_consumer_ids=None),
                       dict(run_id=''), dict(run_id='private\ntext'), dict(turn_id=''), dict(error='private error'),
                       dict(stop_reason='requires_approval'), dict(stop_reason='cancelled'),
                       dict(stop_reason='error'), dict(stop_reason='max_tokens_exceeded')):
            terminal = {**self.terminal(), **change}
            self.assertFalse(self.event(kind='completed', terminal=terminal, assistant_ids=['assistant-fixture']))
        self.assertFalse(self.store.promote_completed_model_turn(self.session))

    def test_success_before_acceptance_and_wrong_conversation_are_rejected(self):
        self.event(kind='prepared', client_message_id='fixture-message')
        self.assertFalse(self.event(kind='completed', terminal=self.terminal(), assistant_ids=['assistant-fixture']))
        self.event(kind='accepted')
        with self.assertRaises(ValueError):
            self.store.record_model_turn_receipt(self.session, 'fixture-cycle', {**self.runtime, 'conversation_id': 'other'},
                dict(kind='completed', request_id=self.request, terminal=self.terminal(), assistant_ids=['assistant-fixture']))

    def test_pause_sleep_restore_errors_and_changed_owner_revision_block_promotion(self):
        self.proof()
        for key, value, reset in (('paused','1','0'), ('sleeping','1','0'), ('restore_recovery_required','1','0'),
                ('autonomy_error','provider_attempt_uncertain',''), ('owner_control_revision','1','0'),
                ('letta_agent_id','different','fixture-agent'), ('letta_conversation_id','different','fixture-conversation')):
            self.store.set_state(key,value)
            self.assertFalse(self.store.promote_completed_model_turn(self.session), key)
            self.store.set_state(key,reset)

    def test_unknown_provider_and_tool_outcomes_still_block_resume(self):
        self.proof()
        self.store.reserve_usage('uncertain-provider', '2026-10', '2026-10-17', .01)
        self.restart()
        self.assertEqual(self.store.get_state('autonomy_error'), 'interrupted')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],1)
        self.agent.autonomous_turn.assert_not_called()

    def test_unconfirmed_tool_is_not_inferred_success_from_model_terminal(self):
        self.proof()
        attempt = self.store.begin_cycle_tool((self.session,'fixture-cycle',''), 'shell.run')
        self.store.finish_cycle_tool(attempt, False)
        self.restart()
        self.assertEqual(self.store.get_state('autonomy_error'), 'interrupted')
        self.agent.autonomous_turn.assert_not_called()

    def test_owner_input_retains_priority_after_receipt_recovery(self):
        self.proof()
        message = self.store.add_message(17,'user','Answer this before continuing.',queued=1)
        self.restart()
        self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(self.store.connection.execute('SELECT queued FROM messages WHERE id=?',(message,)).fetchone()[0],1)
        self.agent.autonomous_turn.assert_not_called()

    def test_protocol_persists_success_before_final_text_fetch_fails(self):
        outer = self
        client = AppServerClient.__new__(AppServerClient)
        client.require_input_ack = True
        client.turn_receipt_sink = lambda event: self.store.record_model_turn_receipt(self.session,'fixture-cycle',self.runtime,event)
        class Connection:
            def __init__(self): self.events=[]; self.sent=[]
            def send_json(self, packet):
                self.sent.append(packet)
                if packet['type']=='input':
                    outer.request=packet['request_id']
                    self.events=[dict(type='input_accepted',request_id=outer.request,accepted=True),
                        dict(type='stream_delta',delta=dict(message_type='assistant_message',id='assistant-fixture',content='Saved reply.')),
                        {**outer.terminal(), 'idempotency_key':'terminal-receipt-key'}]
            def recv_json(self): return self.events.pop(0)
        client.connection=Connection()
        def fetch(*args):
            outer.assertEqual(outer.store.connection.execute('SELECT state FROM model_turn_receipts').fetchone()[0],'completed')
            raise ConnectionError('fixture read failed')
        client.request=fetch
        with self.assertRaises(ConnectionError): client.send_user_turn(self.runtime,'private prompt',timeout=None)
        self.assertEqual(client.connection.sent[-1]['type'],'turn_finished_ack')
        self.assertEqual(client.connection.sent[-1]['consumer_id'],self.request)
        receipt=dict(self.store.connection.execute('SELECT * FROM model_turn_receipts').fetchone())
        self.assertNotIn('private prompt',json.dumps(receipt))
        self.restart()
        self.assertEqual(self.runner.active_session_id,self.session)

    def test_receipt_commit_survives_abrupt_process_exit(self):
        path=self.path.parent/'crash.db'
        script="""
import os,sys
from datetime import datetime,timezone
from andrewbot.store import Store
s=Store(sys.argv[1]);s.start_session_state('session',datetime.now(timezone.utc).isoformat(),'owner_wake')
s.set_state('letta_agent_id','agent');s.set_state('letta_conversation_id','conversation')
s.begin_cycle('session','cycle','2026-10-17');s.checkpoint_cycle_turn('cycle','model_started')
r={'agent_id':'agent','conversation_id':'conversation'}
s.record_model_turn_receipt('session','cycle',r,{'kind':'prepared','request_id':'request','client_message_id':'message'})
s.record_model_turn_receipt('session','cycle',r,{'kind':'accepted','request_id':'request'})
s.record_model_turn_receipt('session','cycle',r,{'kind':'completed','request_id':'request','assistant_ids':['assistant'],
 'terminal':{'type':'turn_finished','stop_reason':'end_turn','run_id':'run','turn_id':'turn','terminal_consumer_ids':['request']}})
os._exit(0)
"""
        subprocess.run([sys.executable,'-c',script,str(path)],env=os.environ,check=True)
        other=Store(path)
        try:
            self.assertTrue(other.promote_completed_model_turn('session'))
            self.assertEqual(other.recover_confirmed_session('session'),'model_confirmed')
        finally: other.close()

    def test_completed_protocol_turn_can_wait_for_a_gmail_receipt(self):
        attempt=self.store.begin_cycle_tool((self.session,'fixture-cycle',''),'gmail.send')
        action=self.store.claim_external_action('gmail','send','mail-fixture','digest','a'*64,
            {'message_id_header':'<fixture@andrew.invalid>','content_sha256':'fixture'},(attempt,self.session,'fixture-cycle'))
        self.store.finish_cycle_tool(attempt,False)
        self.proof()
        self.restart()
        self.assertEqual(self.store.get_state('active_session_status'),'reconciling')
        self.assertFalse(self.runner.tick(self.now))
        self.agent.autonomous_turn.assert_not_called()
        self.store.confirm_external_action(action['action_id'],'a'*64,
            {'operation':'send','id':'sent-fixture','confirmed':True},'gmail_sent_message')
        self.assertTrue(self.runner.tick(self.now))
        self.assertEqual(self.agent.autonomous_turn.call_count,1)
        self.assertIn('verified_actions',self.agent.autonomous_turn.call_args.args[0])

    def test_terminal_receipt_does_not_override_an_existing_session_failure(self):
        self.proof()
        self.store.record_session_failure('provider_attempt_uncertain',17,session_id=self.session)
        self.restart()
        self.assertEqual(self.store.get_state('autonomy_error'),'provider_attempt_uncertain')
        self.agent.autonomous_turn.assert_not_called()
