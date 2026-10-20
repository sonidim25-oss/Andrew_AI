import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from runner_recovery_fixture import RunnerRecoveryFixture, CONTROLLER_ID
from andrewbot.runner_receipts import RunnerReceiptStore
from andrewbot.runner_protocol import validate_identity
from andrewbot.runner_backup import (create_runner_recovery_snapshot,verify_runner_recovery_snapshot,restore_runner_recovery_snapshot)
from andrewbot.memory_backup import _prepare_recovery_snapshot, restore_paired_recovery_snapshot
from andrewbot.store import Store


class RunnerRestoreTests(RunnerRecoveryFixture,unittest.TestCase):
    def setUp(self):
        super().setUp();self.key=b'k'*32;self.receiver=Path(self.tmp.name)/'runner'
        self.receipts=RunnerReceiptStore(self.receiver,self.key,expected_uid=os.getuid())
        runner_id=self.receipts.provision(controller_store_id=CONTROLLER_ID)
        self.store.set_state('runner_receipt_store_id',runner_id);self.adapter.runner_store_id=runner_id
        self.client.capability_changes['runner_store_id']=runner_id
        self.receipts.close()
        self.backend=Path(self.tmp.name)/'backend';private=self.backend/'local-recovery-v1';private.mkdir(parents=True)
        (private/'identity.json').write_text(json.dumps(dict(store_id=self.binding['store_id'],version=1)))
        self.snapshot=Path(self.tmp.name)/'snapshot'

    def checkpoint(self):
        self.store.connection.commit();self.store.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def snapshot_now(self):
        self.checkpoint()
        return create_runner_recovery_snapshot(self.path,self.backend,self.receiver,self.snapshot,workers_quiescent=True)

    def execute(self):
        self.adapter.execute(self.metadata,self.name,self.arguments)
        identity=validate_identity(json.loads(self.operation()['identity_json']))
        with closing(RunnerReceiptStore(self.receiver,self.key,expected_uid=os.getuid())) as receiver:
            receiver.prepare(identity);receiver.admit(identity);receiver.finish(identity,self.client.receipt.to_dict()['response'],cleanup_verified=True)
        self.checkpoint();return identity

    def test_three_stores_are_bound_and_metadata_has_no_request_output_or_key(self):
        self.snapshot_now();metadata=verify_runner_recovery_snapshot(self.snapshot)
        self.assertEqual(metadata['controller_store_id'],CONTROLLER_ID)
        text=(self.snapshot/'runner-recovery.json').read_text()
        for secret in ['print(42)','saved exact result',self.key.hex(),'scope_token','challenge']:
            self.assertNotIn(secret,text)
        self.assertEqual(self.snapshot.stat().st_mode&0o777,0o700)

    def test_newer_result_mapping_research_and_held_messages_survive_memory_restore(self):
        self.snapshot_now();identity=self.execute()
        for n in range(15):self.store.add_message(17,'user','Held fixture '+str(n),queued=True)
        self.store.set_state('owner_control_revision','19');self.checkpoint()
        self.store.close()
        restored=restore_runner_recovery_snapshot(self.snapshot,self.path,self.backend,self.receiver,key=self.key,workers_quiescent=True)
        self.store=Store(self.path)
        self.assertTrue(restored['execution_histories_preserved'])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM runner_imports').fetchone()[0],1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM runner_research_results').fetchone()[0],1)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM cycle_tool_attempts WHERE outcome='returned'").fetchone()[0],1)
        self.assertEqual(self.store.get_state('owner_control_revision'),'19')
        self.assertEqual(self.store.get_state('paused'),'1');self.assertEqual(self.store.get_state('runner_receipts_enabled'),'0')
        self.assertEqual(self.store.uncertain_message_count(17),15)
        self.assertEqual(self.store.connection.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertTrue((self.path.parent/'runner-restore-required.json').exists())
        with closing(RunnerReceiptStore(self.receiver,self.key,expected_uid=os.getuid())) as receiver:
            self.assertEqual(receiver.lookup(identity).state,'result')
            with self.assertRaises(Exception):receiver.prepare(identity)
        self.assertTrue(Path(restored['retained']).is_dir())

    def test_owner_lock_non_quiescent_and_overlapping_paths_refuse_snapshot(self):
        self.checkpoint()
        with self.assertRaises(ValueError):create_runner_recovery_snapshot(self.path,self.backend,self.receiver,self.snapshot)
        with closing(RunnerReceiptStore(self.receiver,self.key,expected_uid=os.getuid())):
            with self.assertRaises(BlockingIOError):self.snapshot_now()
        with self.assertRaises(ValueError):create_runner_recovery_snapshot(self.path,self.backend,self.receiver,self.receiver/'snapshot',workers_quiescent=True)

    def test_tampered_or_truncated_snapshot_refuses(self):
        self.snapshot_now();path=self.snapshot/'runner/receipts.sqlite3';original=path.read_bytes()
        path.write_bytes(original[:-32])
        with self.assertRaises(Exception):verify_runner_recovery_snapshot(self.snapshot)
        path.write_bytes(original);verify_runner_recovery_snapshot(self.snapshot)
        path=self.snapshot/'runner/identity.json';identity=json.loads(path.read_text());identity['runner_store_id']='0'*32;path.write_text(json.dumps(identity))
        with self.assertRaises(Exception):verify_runner_recovery_snapshot(self.snapshot)

    def test_wrong_key_or_current_store_pin_refuses_without_replacing_stores(self):
        self.snapshot_now();self.checkpoint();before=self.path.read_bytes()
        with self.assertRaises(ValueError):restore_runner_recovery_snapshot(self.snapshot,self.path,self.backend,self.receiver,key=b'z'*32,workers_quiescent=True)
        self.assertEqual(self.path.read_bytes(),before)
        self.store.set_state('runner_receipt_store_id','0'*32);self.checkpoint()
        with self.assertRaises(ValueError):restore_runner_recovery_snapshot(self.snapshot,self.path,self.backend,self.receiver,key=self.key,workers_quiescent=True)

    def test_legacy_paired_restore_cannot_roll_back_adopted_runner_stores(self):
        self.snapshot_now()
        with self.assertRaisesRegex(ValueError,'three-store'):
            restore_paired_recovery_snapshot(self.snapshot,self.path,self.backend,key=self.key,workers_quiescent=True)

    def test_memory_only_restore_keeps_newer_receipts_and_fences_execution(self):
        self.snapshot_now();self.execute();self.checkpoint()
        data=_prepare_recovery_snapshot((self.snapshot/'controller.sqlite3').read_bytes(),self.path,self.key)
        target=Path(self.tmp.name)/'memory-restored.sqlite3';target.write_bytes(data)
        with closing(Store(target)) as restored:
            self.assertEqual(restored.connection.execute('SELECT count(*) FROM runner_imports').fetchone()[0],1)
            self.assertEqual(restored.get_state('runner_receipts_enabled'),'0')
            self.assertEqual(restored.get_state('local_recovery_restore_required'),'1')

    def test_corrupt_newer_ciphertext_refuses_restore_without_replacing_current_stores(self):
        import sqlite3
        self.snapshot_now();self.execute();self.store.close()
        with sqlite3.connect(self.receiver/'receipts.sqlite3') as db:
            row=db.execute('SELECT operation_id,ciphertext FROM receipts').fetchone()
            value=bytearray(row[1]);value[-1]^=1
            db.execute('UPDATE receipts SET ciphertext=? WHERE operation_id=?',(bytes(value),row[0]))
        before=self.path.read_bytes()
        with self.assertRaises(Exception):restore_runner_recovery_snapshot(self.snapshot,self.path,self.backend,self.receiver,key=self.key,workers_quiescent=True)
        self.assertEqual(self.path.read_bytes(),before)
        self.store=Store(self.path)

    def test_backend_execution_lock_and_hot_rollback_journal_refuse_snapshot(self):
        import fcntl
        self.checkpoint();lock=self.backend/'active.execution';fd=os.open(lock,os.O_CREAT|os.O_RDWR,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):self.snapshot_now()
        finally:os.close(fd)
        journal=self.receiver/'receipts.sqlite3-journal';journal.write_bytes(b'interrupted transaction')
        with self.assertRaisesRegex(ValueError,'journal'):self.snapshot_now()

    def test_older_receiver_cannot_be_claimed_as_a_coordinated_snapshot(self):
        self.snapshot_now();empty=(self.snapshot/'runner/receipts.sqlite3').read_bytes();self.execute()
        (self.receiver/'receipts.sqlite3').write_bytes(empty)
        with self.assertRaisesRegex(ValueError,'evidence disagree'):
            create_runner_recovery_snapshot(self.path,self.backend,self.receiver,Path(self.tmp.name)/'wrong-pair',workers_quiescent=True)

    def test_restore_process_loss_keeps_durable_external_fence_and_retained_stores(self):
        self.snapshot_now();self.execute();self.store.close()
        program='''import os,sys,json
from andrewbot import runner_backup as b
v=json.loads(sys.argv[1]);b._fault=lambda p:os._exit(71) if p==v['point'] else None
b.restore_runner_recovery_snapshot(v['snapshot'],v['controller'],v['backend'],v['runner'],key=b'k'*32,workers_quiescent=True)
'''
        for point in ['external_fence','store_fences','before_controller_replace','after_controller_replace']:
            value=dict(snapshot=str(self.snapshot),controller=str(self.path),backend=str(self.backend),runner=str(self.receiver),point=point)
            child=subprocess.run([sys.executable,'-c',program,json.dumps(value)],capture_output=True,text=True,timeout=10)
            self.assertEqual(child.returncode,71,child.stderr)
            self.assertTrue((self.path.parent/'runner-restore-required.json').exists())
            self.assertTrue(list(self.path.parent.glob('runner-before-restore-*')))
        self.store=Store(self.path)
