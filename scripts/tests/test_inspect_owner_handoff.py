import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'prototype/tests'));sys.path.insert(0,str(ROOT/'scripts'))
from test_recovery_dispatch import RecoveryDispatchTests
from andrewbot.recovery_records import digest
from inspect_owner_handoff import plan


class HandoffInspectionTests(unittest.TestCase):
    fixture_setup=RecoveryDispatchTests.setUp
    metadata=RecoveryDispatchTests.metadata

    def setUp(self):
        self.fixture_setup();self.store.set_state('local_recovery_owner_id','17')
        metadata=self.metadata();self.effect=metadata['admission_id']
        self.store.record_model_turn_receipt(self.binding['session_id'],self.binding['cycle_id'],self.binding,
            {'kind':'accepted','request_id':self.binding['consumer_id']})
        self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],200)
        self.store.add_message(17,'user','Private saved owner question',queued=True)
        for key,value in {'autonomy_error':'local_recovery_interrupted','model_turn_origin':'','model_turn_progress':'','autonomous_owner_id':''}.items():self.store.set_state(key,value)
        with self.store.connection:
            self.store.connection.execute("UPDATE recovery_attempts SET state='held',hold_reason='provider_uncertain'")
            self.store.connection.execute('CREATE TABLE media_jobs(state TEXT,delivery TEXT)')
            self.store.connection.execute('CREATE TABLE media_avatar_proposals(state TEXT)')
        row=self.store.connection.execute('SELECT * FROM recovery_effects').fetchone()
        result=self.store.connection.execute('SELECT * FROM recovery_results').fetchone()
        self.home=Path(self.tmp.name)/'backend-home'
        key=digest({k:self.binding[k] for k in ('agent_id','conversation_id')})
        self.checkpoint=self.home/'.letta/local-recovery-v1/conversations'/(key+'.json');self.checkpoint.parent.mkdir(parents=True)
        self.snapshot={'binding':self.binding,'phase':'held','hold_reason':'owner_blocked','held_phase':'tool_prepared','revision':9,'claim':None,'terminal':None,
            'steps':[{'admission_id':self.effect,'state':'confirmed','request_sha':row['request_sha'],'result_ref':result['result_sha']}],
            'tools':[{'name':'gmail.read','state':'prepared'}]}
        self.save()

    def save(self):self.checkpoint.write_text(json.dumps(self.snapshot))
    def inspect(self):return plan(self.path,self.home,self.scope['admission_id'])

    def test_exact_halted_checkpoint_is_eligible_without_state_writes(self):
        before='\n'.join(self.store.connection.iterdump());raw=self.checkpoint.read_bytes()
        result=self.inspect();self.assertTrue(result['eligible'])
        self.assertEqual(result['confirmed_effects'],1)
        self.assertEqual(before,'\n'.join(self.store.connection.iterdump()));self.assertEqual(self.checkpoint.read_bytes(),raw)
        self.assertNotIn('Private saved',json.dumps(result))

    def test_pause_revision_restore_and_active_work_are_rejected(self):
        for key,value in [('paused','1'),('owner_control_revision','99'),('restore_recovery_required','1'),('model_turn_origin','autonomous')]:
            old=self.store.get_state(key,'');self.store.set_state(key,value)
            with self.assertRaises(ValueError):self.inspect()
            self.store.set_state(key,old)

    def test_paused_superseded_authority_is_cancellation_only_and_survives_startup(self):
        for key in ('agent_id','conversation_id'):self.store.set_state('letta_'+key,self.binding[key])
        self.controller.handle_update({'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'/pause'}})
        result=self.inspect()
        self.assertEqual(result['mode'],'paused_cancellation')
        self.assertEqual(result['owner_revision'],'1')
        self.store.interrupt_unfinished_cycles()
        self.assertEqual(self.inspect(),result)
        self.assertEqual(self.store.get_state('paused'),'1')
        self.store.set_state('paused','0')
        with self.assertRaises(ValueError):self.inspect()

    def test_unknown_effect_is_rejected(self):
        with self.store.connection:self.store.connection.execute("UPDATE recovery_effects SET state='admitted'")
        with self.assertRaises(ValueError):self.inspect()

    def test_backend_result_or_phase_mismatch_is_rejected(self):
        self.snapshot['steps'][0]['result_ref']='f'*64;self.save()
        with self.assertRaises(ValueError):self.inspect()
        self.snapshot['phase']='tool_admitted';self.save()
        with self.assertRaises(ValueError):self.inspect()

    def test_claimed_message_is_still_settling(self):
        with self.store.connection:self.store.connection.execute('UPDATE messages SET queued=2 WHERE queued=1')
        with self.assertRaises(ValueError):self.inspect()


del RecoveryDispatchTests
