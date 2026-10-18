import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from andrewbot.store import Store
from andrewbot.recovery_records import canonical,digest
import retire_unstarted_projection as repair


class ProjectionRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.database=self.root/'controller.sqlite3';self.home=self.root/'home'
        store=Store(self.database)
        value={'model':'fixture','messages':[{'role':'user','content':'Never dispatched fixture'}]}
        self.binding={'store_id':'store','agent_id':'agent','conversation_id':'conversation','session_id':'session',
                      'cycle_id':'cycle','client_message_id':'input','consumer_id':'consumer','owner_revision':'1',
                      'config_sha':'b'*64,'input_sha':digest(value)}
        for key,item in [('paused','1'),('letta_agent_id','agent'),('letta_conversation_id','conversation')]:store.set_state(key,item)
        store.begin_cycle('session','cycle','2026-10-18')
        with store.connection:
            store.connection.execute('INSERT INTO recovery_attempts(admission_id,binding_sha,binding_json,token_sha,state,created_at) VALUES(?,?,?,?,?,?)',
                ('scope',digest(self.binding),canonical(self.binding),'c'*64,'held',store.now()))
        store.reserve_usage('legacy-cost','2026-10','2026-10-16',.01)
        store.add_message(17,'user','Old held owner input',queued=3);store.close()
        key=digest({'agent_id':'agent','conversation_id':'conversation'})
        root=self.home/'.letta/local-recovery-v1';self.checkpoint=root/'conversations'/f'{key}.json';self.checkpoint.parent.mkdir(parents=True)
        (root/'identity.json').write_text(json.dumps({'store_id':'store'}))
        self.snapshot={'version':1,'binding':self.binding,'revision':4,'fence':1,'run_id':'run','phase':'held',
            'held_phase':'input_committed','hold_reason':'projection_conflict','steps':[],'tools':[],'terminal':None,'input_ref':digest(value),'payload_refs':[digest(value)]}
        self.checkpoint.write_text(json.dumps(self.snapshot))
        payload=root/'blobs'/key/digest(value);payload.parent.mkdir(parents=True);payload.write_text(canonical(value))
        self.missing='message-'+digest('input:run')
        row={'operation_id':'input:run','kind':'input','value':value,'binding_sha':digest(self.binding),
             'input_sha':digest(value),'digest':digest({'kind':'input','value':value})}
        self.projection=self.checkpoint.with_name(key+'.projection.json');self.projection.write_text(json.dumps({'rows':[row],'baseline':'a'*64}))
        directory=self.home/'.letta/conversations/fixture';directory.mkdir(parents=True)
        self.conversation=directory/'conversation.json'
        self.conversation.write_text(json.dumps({'agent_id':'agent','id':'conversation','in_context_message_ids':['old',self.missing]}))
        self.transcript=directory/'messages.jsonl';self.transcript.write_text(json.dumps({'type':'message','message':{'id':'old','role':'user','content':'Historical fixture'}})+'\n')

    def test_retirement_retains_evidence_costs_and_held_messages(self):
        value=repair.plan(self.database,self.home,'scope');original=self.checkpoint.read_bytes();transcript=self.transcript.read_bytes()
        repair.retire_backend(value);repair.retire_controller(value)
        self.assertFalse(self.checkpoint.exists());self.assertEqual(Path(value['archive']).read_bytes(),original)
        self.assertEqual(self.transcript.read_bytes(),transcript)
        self.assertEqual(json.loads(self.conversation.read_text())['in_context_message_ids'],['old'])
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT state,hold_reason FROM recovery_attempts').fetchone(),('retired','projection_conflict'))
            self.assertEqual(db.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT queued FROM messages').fetchone()[0],3)
            self.assertEqual(db.execute('SELECT status FROM cycles').fetchone()[0],'interrupted')
            self.assertEqual(db.execute("SELECT value FROM state WHERE key='paused'").fetchone()[0],'1')

    def test_any_backend_effect_refuses_retirement(self):
        for key,value in [('steps',[{'effect_id':'unknown'}]),('tools',[{'effect_id':'unknown'}]),('held_phase','provider_admitted'),('hold_reason','provider_uncertain')]:
            with self.subTest(key=key):
                self.checkpoint.write_text(json.dumps({**self.snapshot,key:value}))
                with self.assertRaisesRegex(ValueError,'pre-effect'):repair.plan(self.database,self.home,'scope')

    def test_controller_admission_refuses_even_with_empty_backend_steps(self):
        with sqlite3.connect(self.database) as db:
            db.execute('INSERT INTO recovery_effects(admission_id,scope_id,binding_sha,kind,effect_id,request_sha,token_sha,fence,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('effect','scope',digest(self.binding),'provider','effect-id','a'*64,'b'*64,1,'admitted','today'))
        with self.assertRaisesRegex(ValueError,'admitted effects'):repair.plan(self.database,self.home,'scope')

    def test_changed_evidence_stops_before_retirement(self):
        value=repair.plan(self.database,self.home,'scope');self.transcript.write_text(self.transcript.read_text()+'\n')
        with self.assertRaisesRegex(ValueError,'changed'):repair.retire_backend(value)
        self.assertTrue(self.checkpoint.exists());self.assertFalse(Path(value['archive']).exists())

    def test_projected_input_or_other_missing_active_id_refuses_retirement(self):
        with self.transcript.open('a') as stream:stream.write(json.dumps({'type':'message','message':{'id':self.missing}})+'\n')
        with self.assertRaisesRegex(ValueError,'already exists'):repair.plan(self.database,self.home,'scope')

    def test_unpaused_owner_refuses_retirement(self):
        with sqlite3.connect(self.database) as db:db.execute("UPDATE state SET value='0' WHERE key='paused'")
        with self.assertRaisesRegex(ValueError,'pause'):repair.plan(self.database,self.home,'scope')

    def test_archive_symlink_refuses_retirement(self):
        value=repair.plan(self.database,self.home,'scope');outside=self.root/'outside';outside.mkdir()
        Path(value['archive']).parent.symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'Unsafe'):repair.retire_backend(value)
        self.assertTrue(self.checkpoint.exists());self.assertEqual(list(outside.iterdir()),[])

    def test_each_phase_runs_as_store_owner_after_paired_snapshot_verification(self):
        from andrewbot.memory_backup import create_paired_recovery_snapshot
        snapshot=self.root/'paired';create_paired_recovery_snapshot(self.database,self.home,snapshot,workers_quiescent=True)
        value=repair.plan(self.database,self.home,'scope');owners=[]
        def run(command,**kwargs):
            owners.append(command[2]);compile(command[6],'<repair-child>','exec')
            phase=command[-1];loaded=json.loads(kwargs['input'])
            (repair.retire_backend if phase=='backend' else repair.retire_controller)(loaded)
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch.object(repair.subprocess,'run',side_effect=run):repair.retire(value,snapshot)
        self.assertEqual(owners,['andrew-letta','andrew-control'])
        self.assertTrue((self.root/'backend-projection-retirement.txt').exists())
