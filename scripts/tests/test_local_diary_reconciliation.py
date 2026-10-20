"""Quiescent local destination proof, no callback replay, then a real fresh turn."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_local_recovery_protocol import LocalRecoveryProtocolTests
from andrewbot.store import Store
from andrewbot.memory_backup import create_paired_recovery_snapshot
import reconcile_local_diary as repair


@unittest.skipUnless(os.environ.get('ANDREW_LOCAL_RECOVERY_TEST_RELEASE'),'requires isolated recovery release')
class LocalDiaryReconciliationTests(unittest.TestCase):
    setUp=LocalRecoveryProtocolTests.setUp
    coordinated_agent=LocalRecoveryProtocolTests.coordinated_agent

    def failed_diary(self):
        agent,controller,coordinator,runner=self.coordinated_agent();self.controller=controller
        home=self.fixture.root;self.fixture.kill()
        backend=home/'.letta/lc-local-backend';backend.parent.mkdir(exist_ok=True);(home/'backend').rename(backend)
        self.fixture.environment['LETTA_LOCAL_BACKEND_DIR']=str(backend);self.fixture.restart()
        store=self.fixture.store
        with store.connection:
            store.connection.execute('CREATE TABLE IF NOT EXISTS media_jobs(state TEXT,delivery TEXT)')
            store.connection.execute('CREATE TABLE IF NOT EXISTS media_avatar_proposals(state TEXT)')
            store.connection.execute("INSERT INTO cycles(cycle_id,session_id,local_day,status,started_at) VALUES('legacy-cycle','legacy-session','2026-10-17','interrupted','2026-10-17T00:00:00+00:00')")
            for n in range(3):store.connection.execute("INSERT INTO cycle_tool_attempts VALUES(?, 'legacy-cycle','python.run','uncertain','2026-10-17T00:00:00+00:00','2026-10-17T00:01:00+00:00')",('attempt-'+str(n),))
        for n in range(15):store.add_message(17,'user','Old held fixture '+str(n),queued=3)
        self.responses.insert(0,{'id':'old-diary-failure','choices':[{'index':0,'message':{'role':'assistant','content':None,
            'tool_calls':[{'id':'old-diary-tool','type':'function','function':{'name':'save_diary_entry','arguments':json.dumps({'content':'A fixture diary that was not committed.','event_ids':[]})}}]},
            'finish_reason':'tool_calls'}],'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}})
        # Let the pre-admission main database ctime fall before SQLite's
        # second-resolution admission timestamp; writes then remain in WAL.
        time.sleep(1.05)
        original=controller._execute_tool
        def old_guard(name,args):
            if name=='save_diary_entry':raise ValueError('old autonomous-session precondition')
            return original(name,args)
        with patch.object(controller,'_execute_tool',side_effect=old_guard):
            reply=controller.handle_update({'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'Please answer once.'}})
        self.assertIn('turn_failed',reply)
        scope=store.connection.execute("SELECT admission_id FROM recovery_attempts WHERE state='active'").fetchone()[0]
        checkpoints=list(backend.glob('local-recovery-v1/conversations/*.json'))
        checkpoints=[p for p in checkpoints if not p.name.endswith('.projection.json')]
        self.assertEqual(len(checkpoints),1)
        deadline=time.monotonic()+5
        while json.loads(checkpoints[0].read_text()).get('claim'):
            self.assertLess(time.monotonic(),deadline,'the disconnected backend claim must release before maintenance')
            time.sleep(.02)
        return home,scope

    def test_exact_destination_reconciliation_then_fresh_owner_reply_preserves_unknowns(self):
        home,scope=self.failed_diary();database=home/'controller.sqlite3';store=self.fixture.store
        state_before=dict(store.connection.execute('SELECT key,value FROM state'))
        proof=repair.plan(database,home,scope)
        self.fixture.kill();store.close()
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup);snapshot=Path(directory.name)/'paired'
        create_paired_recovery_snapshot(database,home,snapshot,workers_quiescent=True)
        repair.record_result(database,home,proof,snapshot,run_as_service=False)
        self.assertTrue(repair.verify_recorded(database,home,proof,snapshot)['result_recorded'],
            'a committed receipt must support resuming the accepted install before backend finalization')
        self.fixture.store=Store(database);self.fixture.model_gateway.store=self.fixture.store;self.fixture.restart()
        from andrewbot.recovery import LocalRecoveryClient
        from andrewbot.appserver import AppServerClient
        client=LocalRecoveryClient(AppServerClient(self.fixture.url));self.addCleanup(client.close)
        marker=repair.finalize(database,proof,client=client)
        self.assertTrue(repair.verify_finalized(database,home,proof,marker))
        self.assertEqual(len(self.fixture.provider_calls),1)
        state_after=dict(self.fixture.store.connection.execute('SELECT key,value FROM state'))
        # Reopening Store may advance its history backfill cursor. Owner
        # authority, pause/error and every other state entry must be preserved.
        state_after.pop('session_history_backfill_message_id',None)
        state_before.pop('session_history_backfill_message_id',None)
        self.assertEqual(state_after,state_before)
        self.assertEqual(self.fixture.store.uncertain_message_count(17),16)
        self.assertEqual(self.fixture.store.connection.execute("SELECT count(*) FROM cycle_tool_attempts WHERE outcome='uncertain'").fetchone()[0],3)
        # Repeating finalization imports/project/acks the same failed input only.
        marker=repair.finalize(database,proof,client=client)
        self.assertTrue(marker['reconciled']);self.assertEqual(len(self.fixture.provider_calls),1)
        agent,controller,coordinator,runner=self.coordinated_agent()
        controller._command('/wake')
        reply=controller.handle_update({'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'A fresh owner question.'}})
        self.assertEqual(reply,'Persisted fixture answer.');self.assertFalse(coordinator.has_pending())
        self.assertEqual(len(self.fixture.provider_calls),2)
        history=self.fixture.provider_calls[-1]['messages']
        errors=[m for m in history if m.get('role')=='tool' and 'diary_not_committed' in str(m.get('content'))]
        self.assertEqual(len(errors),1,'the prior tool call must have its exact durable error in the fresh context')

    def test_destination_record_or_restore_identity_change_refuses_repair(self):
        home,scope=self.failed_diary();database=home/'controller.sqlite3';store=self.fixture.store
        proof=repair.plan(database,home,scope)
        with self.assertRaisesRegex(ValueError,'continuity'):
            repair.plan(database,home,scope,continuity_before={**proof['continuity_before'],'inode':proof['continuity_before']['inode']+1})
        next_day=repair.plan(database,home,scope,now=datetime.now(timezone.utc)+timedelta(days=1))
        self.assertEqual(len(next_day['destination_dates']),2)
        with self.assertRaisesRegex(ValueError,'seven-day'):
            repair.plan(database,home,scope,now=datetime.now(timezone.utc)+timedelta(days=7))
        store.create_diary_entry(proof['destination_date'],'An already committed diary.')
        with self.assertRaisesRegex(ValueError,'destination is not empty'):repair.plan(database,home,scope)
        self.assertEqual(len(self.fixture.provider_calls),1)

    def test_diary_on_a_later_possible_execution_date_also_blocks_reconciliation(self):
        home,scope=self.failed_diary();database=home/'controller.sqlite3'
        later=datetime.now(timezone.utc)+timedelta(days=1)
        proof=repair.plan(database,home,scope,now=later)
        self.fixture.store.create_diary_entry(proof['destination_dates'][-1],'A diary on the later possible execution date.')
        with self.assertRaisesRegex(ValueError,'destination is not empty'):
            repair.plan(database,home,scope,now=later)

    def test_wal_checkpoint_ctime_is_allowed_but_new_database_birth_is_not(self):
        home,scope=self.failed_diary();database=home/'controller.sqlite3'
        value=repair.identity(database);recent=str(time.time_ns()+1_000_000_000)
        if value['birth_time_seconds']>0:
            with patch.object(repair,'identity',return_value={**value,'ctime_ns':recent}):
                self.assertEqual(repair.plan(database,home,scope)['admission_id'],scope)
        with patch.object(repair,'identity',return_value={**value,'ctime_ns':recent,'birth_time_seconds':int(time.time())+1}):
            with self.assertRaisesRegex(ValueError,'continuity'):repair.plan(database,home,scope)

    def test_mismatched_binding_or_other_admitted_effect_refuses_repair(self):
        home,scope=self.failed_diary();store=self.fixture.store;database=home/'controller.sqlite3'
        with store.connection:store.connection.execute("UPDATE recovery_attempts SET binding_sha=? WHERE admission_id=?",('0'*64,scope))
        with self.assertRaises(ValueError):repair.plan(database,home,scope)
        self.assertEqual(len(self.fixture.provider_calls),1)

    def test_snapshot_of_a_different_controller_state_cannot_authorize_repair(self):
        home,scope=self.failed_diary();database=home/'controller.sqlite3'
        proof=repair.plan(database,home,scope)
        self.fixture.kill();self.fixture.store.close()
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup);snapshot=Path(directory.name)/'paired'
        create_paired_recovery_snapshot(database,home,snapshot,workers_quiescent=True)
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO activity(event,created_at) VALUES('Unrelated fixture metadata change',?)",(datetime.now(timezone.utc).isoformat(),))
        db.close()
        with self.assertRaisesRegex(ValueError,'Snapshot controller differs'):
            repair.record_result(database,home,proof,snapshot,run_as_service=False)
        with sqlite3.connect(database) as db:
            self.assertIsNone(db.execute('SELECT 1 FROM recovery_results WHERE admission_id=?',(proof['tool_admission_id'],)).fetchone())

    def test_lost_finalization_reply_resumes_same_receipt_without_dispatch(self):
        home,scope=self.failed_diary();database=home/'controller.sqlite3'
        proof=repair.plan(database,home,scope)
        self.fixture.kill();self.fixture.store.close()
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup);snapshot=Path(directory.name)/'paired'
        create_paired_recovery_snapshot(database,home,snapshot,workers_quiescent=True)
        repair.record_result(database,home,proof,snapshot,run_as_service=False)
        self.fixture.store=Store(database);self.fixture.model_gateway.store=self.fixture.store;self.fixture.restart()
        from andrewbot.recovery import LocalRecoveryClient,RecoveryInterrupted
        from andrewbot.appserver import AppServerClient
        client=LocalRecoveryClient(AppServerClient(self.fixture.url));command=client.command
        def lose_reply(kind,**fields):
            result=command(kind,**fields)
            if kind=='recovery_reconcile_local_diary':raise RecoveryInterrupted('authority_lost')
            return result
        with patch.object(client,'command',side_effect=lose_reply):
            with self.assertRaises(RecoveryInterrupted):repair.finalize(database,proof,client=client)
        client.close();self.fixture.kill()
        self.assertTrue(repair.verify_recorded(database,home,proof,snapshot)['result_recorded'])
        self.fixture.restart();client=LocalRecoveryClient(AppServerClient(self.fixture.url));self.addCleanup(client.close)
        marker=repair.finalize(database,proof,client=client)
        self.assertTrue(repair.verify_finalized(database,home,proof,marker))
        self.assertEqual(len(self.fixture.provider_calls),1)

    def test_finalization_failure_after_claim_before_commit_can_resume(self):
        home,scope=self.failed_diary();database=home/'controller.sqlite3'
        proof=repair.plan(database,home,scope)
        self.fixture.kill();self.fixture.store.close()
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup);snapshot=Path(directory.name)/'paired'
        create_paired_recovery_snapshot(database,home,snapshot,workers_quiescent=True)
        repair.record_result(database,home,proof,snapshot,run_as_service=False)
        self.fixture.store=Store(database);self.fixture.model_gateway.store=self.fixture.store;self.fixture.restart()
        from andrewbot.recovery import LocalRecoveryClient,RecoveryInterrupted
        from andrewbot.appserver import AppServerClient
        client=LocalRecoveryClient(AppServerClient(self.fixture.url))
        original=self.fixture.model_gateway.recovery_request
        def reject_result(path,payload):
            if path.endswith('/result'):return 423,{'error':{'message':'fixture proof transport failed'}}
            return original(path,payload)
        with patch.object(self.fixture.model_gateway,'recovery_request',side_effect=reject_result):
            with self.assertRaises(RecoveryInterrupted):repair.finalize(database,proof,client=client)
        client.close();self.fixture.kill()
        self.assertTrue(repair.verify_recorded(database,home,proof,snapshot)['result_recorded'])
        self.fixture.restart();client=LocalRecoveryClient(AppServerClient(self.fixture.url));self.addCleanup(client.close)
        marker=repair.finalize(database,proof,client=client)
        self.assertTrue(repair.verify_finalized(database,home,proof,marker))
        self.assertEqual(len(self.fixture.provider_calls),1)


del LocalRecoveryProtocolTests
