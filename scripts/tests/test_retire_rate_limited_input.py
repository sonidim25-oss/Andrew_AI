import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

from test_retire_unstarted_projection import ProjectionRetirementTests
from andrewbot.store import Store
from andrewbot.recovery_records import canonical,digest
import retire_rate_limited_input as repair


class RateLimitRetirementTests(unittest.TestCase):
    def setUp(self):
        self.fixture_setup()
        store=Store(self.database)
        store.checkpoint_cycle_turn('cycle','model_started')
        with store.connection:
            store.connection.execute("UPDATE recovery_attempts SET hold_reason='provider_uncertain'")
            store.connection.execute("INSERT INTO model_turn_receipts(cycle_id,session_id,owner_revision,agent_id,conversation_id,request_id,client_message_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'accepted',?,?)",('cycle','session','1','agent','conversation','consumer','input',store.now(),store.now()))
            store.connection.execute('INSERT INTO recovery_effects(admission_id,scope_id,binding_sha,kind,effect_id,request_sha,token_sha,fence,state,reservation_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',('effect','scope',digest(self.binding),'provider','effect-id',self.binding['input_sha'],'c'*64,1,'admitted','local-recovery-effect',store.now()))
        store.reserve_usage('local-recovery-effect','2026-10','2026-10-18',.02)
        store.add_activity(repair.RATE_EVENT);store.close()
        self.snapshot.update(held_phase='provider_admitted',hold_reason='provider_uncertain',steps=[{'effect_id':'effect-id','admission_id':'effect','request_sha':self.binding['input_sha'],'request_ref':self.binding['input_sha'],'kind':'provider','state':'admitted'}])
        self.checkpoint.write_text(json.dumps(self.snapshot))

    def test_exact_rejection_retires_without_changing_history_or_legacy_holds(self):
        value=repair.plan(self.database,self.home,'scope')
        before={p:p.read_bytes() for p in (self.checkpoint,self.transcript,self.conversation,self.projection)}
        repair.retire_backend(value);repair.retire_controller(value)
        self.assertEqual(Path(value['archive']).read_bytes(),before[self.checkpoint])
        for p in (self.transcript,self.conversation,self.projection):self.assertEqual(p.read_bytes(),before[p])
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT reservation_id FROM budget_reservations').fetchall(),[('legacy-cost',)])
            self.assertEqual(db.execute('SELECT queued FROM messages').fetchone()[0],3)
            self.assertEqual(db.execute('SELECT state FROM recovery_attempts').fetchone()[0],'retired')
            self.assertEqual(db.execute('SELECT state FROM model_turn_receipts').fetchone()[0],'accepted')
            self.assertEqual(db.execute("SELECT value FROM state WHERE key='paused'").fetchone()[0],'1')
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_operator_retirements').fetchone()[0],1)

    def test_timeout_other_status_and_multiple_failures_refuse_retirement(self):
        for event in ('Letta model gateway provider request failed (category=timeout, http_status=unavailable)',repair.RATE_EVENT.replace('429','503')):
            with sqlite3.connect(self.database) as db:db.execute('UPDATE activity SET event=?',(event,))
            with self.assertRaisesRegex(ValueError,'unique'):repair.plan(self.database,self.home,'scope')
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE activity SET event=?',(repair.RATE_EVENT,))
            db.execute('INSERT INTO activity(event,created_at) VALUES(?,?)',(repair.RATE_EVENT,'later'))
        with self.assertRaisesRegex(ValueError,'unique'):repair.plan(self.database,self.home,'scope')

    def test_tools_or_different_request_refuse_retirement(self):
        self.snapshot['tools']=[{'effect_id':'unknown'}];self.checkpoint.write_text(json.dumps(self.snapshot))
        with self.assertRaisesRegex(ValueError,'first-provider'):repair.plan(self.database,self.home,'scope')
        self.snapshot['tools']=[];self.snapshot['steps'][0]['request_sha']='d'*64
        self.checkpoint.write_text(json.dumps(self.snapshot))
        with self.assertRaisesRegex(ValueError,'differs'):repair.plan(self.database,self.home,'scope')

    def test_changed_controller_evidence_rolls_back_reservation_release(self):
        value=repair.plan(self.database,self.home,'scope')
        with sqlite3.connect(self.database) as db:db.execute('UPDATE activity SET event=?',('Letta model gateway unknown failure',))
        with self.assertRaises(ValueError):repair.retire_controller(value)
        with sqlite3.connect(self.database) as db:self.assertEqual(db.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],2)

    def test_completed_predecessor_refuses_first_input_only_migration(self):
        binding={**self.binding,'cycle_id':'previous-cycle','consumer_id':'previous-consumer','client_message_id':'previous-input'}
        with sqlite3.connect(self.database) as db:
            db.execute('INSERT INTO recovery_attempts(admission_id,binding_sha,binding_json,token_sha,state,created_at) VALUES(?,?,?,?,?,?)',('previous',digest(binding),canonical(binding),'c'*64,'complete','earlier'))
        with self.assertRaisesRegex(ValueError,'completed predecessor'):repair.plan(self.database,self.home,'scope')

    def test_backend_writer_does_not_import_controller_package(self):
        value=repair.plan(self.database,self.home,'scope')
        guard="""import builtins
original=builtins.__import__
def restricted(name,*args,**kwargs):
    if name=='andrewbot' or name.startswith('andrewbot.'):raise ImportError('protected controller')
    return original(name,*args,**kwargs)
builtins.__import__=restricted
"""
        result=subprocess.run([sys.executable,'-c',guard+Path(repair.__file__).read_text(),'backend'],input=json.dumps(value),capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(Path(value['archive']).exists())


# Keep the shared fixture class available to setUp, without collecting it twice.
fixture_setup=ProjectionRetirementTests.setUp
RateLimitRetirementTests.fixture_setup=fixture_setup
del ProjectionRetirementTests
