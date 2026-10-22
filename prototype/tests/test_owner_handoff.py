"""Owner handoff cancels only undispatched work and preserves uncertainty."""
import unittest
from test_recovery_dispatch import RecoveryDispatchTests
from andrewbot.sessions import SessionRunner


class OwnerHandoffTests(unittest.TestCase):
    setUp=RecoveryDispatchTests.setUp
    metadata=RecoveryDispatchTests.metadata

    def prepare(self):
        self.store.set_state('local_recovery_owner_id','17')
        metadata=self.metadata()
        self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],200)
        self.store.add_message(17,'user','Owner input now waits',queued=True)
        return metadata

    def test_yield_receipt_is_idempotent_and_fences_further_dispatch(self):
        metadata=self.prepare()
        proof=self.records.prepare_owner_yield(self.binding,self.claim,self.scope['scope_token'])
        self.assertEqual(self.records.prepare_owner_yield(self.binding,self.claim,self.scope['scope_token']),proof)
        self.assertEqual(proof['admission_ids'],[metadata['admission_id']])
        with self.assertRaises(ValueError):self.metadata(effect_id='another-provider')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM recovery_owner_yields').fetchone()[0],1)
        self.assertEqual(self.provider.complete.call_count,1)

    def test_no_pending_owner_or_changed_owner_authority_cannot_yield(self):
        with self.assertRaises(ValueError):self.records.prepare_owner_yield(self.binding,self.claim,self.scope['scope_token'])
        self.prepare()
        for key,value in (('paused','1'),('sleeping','1'),('owner_control_revision','99'),('restore_recovery_required','1')):
            old=self.store.get_state(key,'0');self.store.set_state(key,value)
            with self.assertRaises(ValueError):self.records.prepare_owner_yield(self.binding,self.claim,self.scope['scope_token'])
            self.store.set_state(key,old)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM recovery_owner_yields').fetchone()[0],0)

    def test_prepared_dispatch_is_cancelled_durably_but_never_executed(self):
        self.store.set_state('local_recovery_owner_id','17')
        metadata=self.metadata()
        self.store.add_message(17,'user','Owner interrupts before dispatch',queued=True)
        self.assertNotEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],200)
        proof=self.records.prepare_owner_yield(self.binding,self.claim,self.scope['scope_token'])
        self.assertEqual(proof['cancelled_admission_ids'],[metadata['admission_id']])
        self.assertEqual(self.records.prepare_owner_yield(self.binding,self.claim,self.scope['scope_token']),proof)
        self.assertEqual(self.store.connection.execute('SELECT state FROM recovery_effects').fetchone()[0],'cancelled')
        self.provider.complete.assert_not_called()

    def test_local_status_does_not_block_dispatch_or_require_yield(self):
        import json
        self.store.set_state('local_recovery_owner_id','17')
        for index,text in enumerate(('/status','/STATUS@Andrew','📊 Status','/unknown')):
            update={'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':text}}
            self.store.connection.execute('INSERT INTO telegram_inbox(update_id,payload,received_at) VALUES(?,?,?)',(index,json.dumps(update),self.store.now()))
        self.assertFalse(self.store.pending_owner_input(17))
        metadata=self.metadata()
        self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],200)
        self.store.connection.execute('DELETE FROM telegram_inbox')
        for index,text in enumerate(('/voice_reply hello','/image_reply hello','ordinary owner input')):
            update={'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':text}}
            self.store.connection.execute('INSERT INTO telegram_inbox(update_id,payload,received_at) VALUES(?,?,?)',(index,json.dumps(update),self.store.now()))
            self.assertTrue(self.store.pending_owner_input(17))
            self.store.connection.execute('DELETE FROM telegram_inbox')

    def test_time_prompt_reply_is_local_and_does_not_preempt_autonomy(self):
        self.store.set_state('local_recovery_owner_id','17')
        self.controller.handle_update({'update_id':5,'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'/wake_at'}})
        update={'update_id':6,'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'oops'}}
        self.store.enqueue_update(update)
        self.assertFalse(self.store.pending_owner_input(17))
        self.records._guard(self.binding)
        self.assertTrue(self.store.claim_update(6))
        self.assertIn('24-hour time',self.controller.handle_update(update))
        self.assertEqual(self.store.get_state('owner_control_revision','0'),'0')

    def test_owner_pause_allows_cancellation_only_without_restoring_old_authority(self):
        self.prepare()
        self.store.set_state('autonomy_error','local_recovery_interrupted')
        self.store.set_state('model_turn_progress','')
        for key in ('agent_id','conversation_id'):self.store.set_state('letta_'+key,self.binding[key])
        self.controller.handle_update({'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'/pause'}})
        scope=self.records.admit_scope(self.binding,yield_only=True)
        claim={**self.claim,'admission_id':scope['admission_id'],'fence':2}
        proof=self.records.prepare_owner_yield(self.binding,claim,scope['scope_token'])
        self.assertEqual(proof['state'],'owner_yield')
        self.assertEqual(self.store.get_state('paused'),'1')
        self.assertEqual(self.store.get_state('owner_control_revision'),'1')
        self.assertEqual(self.store.get_state('active_session_id'),'')
        self.assertEqual(self.store.get_state('active_cycle_id',''),'')
        with self.assertRaises(ValueError):self.metadata(effect_id='no-dispatch-after-pause')
        with self.assertRaises(ValueError):self.records.admit_scope(self.binding)
        self.assertEqual(self.provider.complete.call_count,1)

    def wake_after_hold(self):
        self.prepare()
        self.store.set_state('autonomy_error','local_recovery_interrupted')
        for key in ('agent_id','conversation_id'):self.store.set_state('letta_'+key,self.binding[key])
        self.controller.handle_update({'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'/wake'}})
        self.assertEqual((self.store.get_state('owner_control_revision'),self.store.get_state('autonomy_error')),('1',''))

    def test_owner_wake_allows_cancellation_for_waiting_input_without_old_authority(self):
        self.wake_after_hold()
        self.store.set_state('model_turn_progress','{"phase": "processing the model response", "at": 0}')
        scope=self.records.admit_scope(self.binding,yield_only=True)
        claim={**self.claim,'admission_id':scope['admission_id'],'fence':2}
        proof=self.records.prepare_owner_yield(self.binding,claim,scope['scope_token'])
        self.assertEqual(proof['state'],'owner_yield')
        self.assertEqual(self.store.get_state('paused'),'0')
        with self.assertRaises(ValueError):self.metadata(effect_id='no-dispatch-after-wake')
        with self.assertRaises(ValueError):self.records.admit_scope(self.binding)
        self.assertEqual(self.provider.complete.call_count,1)

    def test_owner_wake_cancellation_refuses_running_or_changed_work(self):
        self.wake_after_hold()
        for key,value in (('active_session_id','other-session'),('active_cycle_id','other-cycle'),('model_turn_origin','owner'),
                          ('autonomous_owner_id','17'),('sleeping','1'),('autonomy_error','action_outcome_uncertain'),
                          ('autonomy_error','turn_failed'),('local_recovery_restore_required','1'),('local_recovery_config_sha','c'*64),
                          ('letta_agent_id','other-agent'),('owner_control_revision','0')):
            with self.subTest(key=key,value=value):
                old=self.store.get_state(key,'');self.store.set_state(key,value)
                with self.assertRaises(ValueError):self.records.admit_scope(self.binding,yield_only=True)
                self.store.set_state(key,old)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM recovery_attempts WHERE state='yielding'").fetchone()[0],0)

    def test_owner_message_waits_behind_held_work_instead_of_failing(self):
        from unittest.mock import Mock
        self.store.set_state('local_recovery_owner_id','17')
        self.controller.local_recovery=Mock(enabled=True,has_pending=Mock(return_value=True))
        reply=self.controller.handle_update({'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'Are you there?'}})
        self.assertIn('closing an earlier unfinished step',reply)
        self.agent.reply.assert_not_called()
        self.assertEqual(self.store.connection.execute("SELECT queued FROM messages WHERE content='Are you there?'").fetchone()[0],1)
        self.assertTrue(self.store.pending_owner_input(17))
        self.assertEqual(self.store.get_state('autonomy_error',''),'')

    def test_unknown_provider_or_tool_cannot_be_cancelled_as_unexecuted(self):
        self.prepare()
        # Use a separate scope prepared before owner input to simulate a request
        # that was already admitted when the owner arrived.
        with self.store.connection:
            self.store.connection.execute("UPDATE recovery_effects SET state='admitted'")
        with self.assertRaises(ValueError):self.records.prepare_owner_yield(self.binding,self.claim,self.scope['scope_token'])
        self.assertEqual(self.store.connection.execute('SELECT state FROM recovery_effects').fetchone()[0],'admitted')

    def test_disabled_recovery_startup_preserves_journaled_session(self):
        session=self.binding['session_id']
        self.agent.local_recovery=None
        runner=SessionRunner(self.store,self.controller,self.agent,enabled=True)
        runner.recover()
        self.assertEqual(runner.active_session_id,session)
        self.assertEqual(self.store.get_state('active_session_id'),session)
        self.assertEqual(self.store.connection.execute('SELECT status FROM cycles').fetchone()[0],'running')
        self.assertEqual(self.store.get_state('autonomy_error'),'local_recovery_interrupted')

    def test_restore_preserves_handoff_receipts_and_refuses_divergence(self):
        from andrewbot.store import Store
        from andrewbot.memory_backup import _preserve_newer_ledgers
        old=Store(self.path.with_name('old-handoff.db'));self.addCleanup(old.close)
        self.prepare();proof=self.records.prepare_owner_yield(self.binding,self.claim,self.scope['scope_token'])
        self.assertTrue(_preserve_newer_ledgers(old,self.path))
        self.assertEqual(old.connection.execute('SELECT receipt_id FROM recovery_owner_yields').fetchone()[0],proof['receipt_id'])
        with self.store.connection:self.store.connection.execute("UPDATE recovery_owner_yields SET receipt_id='different-receipt'")
        with self.assertRaisesRegex(ValueError,'receipts conflict'):_preserve_newer_ledgers(old,self.path)


del RecoveryDispatchTests
