import json
from pathlib import Path
import tempfile
import unittest

from andrewbot.store import Store
from andrewbot.memory_backup import (create_paired_recovery_snapshot,verify_paired_recovery_snapshot,
    restore_paired_recovery_snapshot,reconcile_restored_database)


class RecoveryBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.database=self.root/'controller.sqlite3';self.home=self.root/'letta'
        store=Store(self.database);store.set_state('local_recovery_controller_store_id','controller-store-fixture');store.close()
        private=self.home/'.letta/local-recovery-v1';private.mkdir(parents=True)
        (private/'identity.json').write_text(json.dumps({'version':1,'store_id':'backend-store-fixture'}))
        (private/'payload').write_text('Private retained fixture evidence')
        self.snapshot=self.root/'paired'

    def test_paired_snapshot_requires_both_workers_and_binds_both_stores(self):
        with self.assertRaises(ValueError):create_paired_recovery_snapshot(self.database,self.home,self.snapshot)
        metadata=create_paired_recovery_snapshot(self.database,self.home,self.snapshot,workers_quiescent=True)
        self.assertEqual(metadata['controller_store_id'],'controller-store-fixture')
        self.assertEqual(metadata['backend_store_id'],'backend-store-fixture')
        self.assertEqual(verify_paired_recovery_snapshot(self.snapshot),metadata)
        self.assertEqual(self.snapshot.stat().st_mode&0o777,0o700)
        (self.snapshot/'backend/.letta/local-recovery-v1/payload').write_text('modified')
        with self.assertRaises(ValueError):verify_paired_recovery_snapshot(self.snapshot)

    def test_restore_keeps_a_separate_recovery_hold_after_legacy_reconciliation(self):
        create_paired_recovery_snapshot(self.database,self.home,self.snapshot,workers_quiescent=True)
        restored=restore_paired_recovery_snapshot(self.snapshot,self.database,self.home,key=b'k'*32,workers_quiescent=True)
        self.assertTrue(restored['reconciliation_required'])
        self.assertTrue((self.home/'.letta/local-recovery-v1/restore-required.json').is_file())
        self.assertTrue(Path(restored['retained_backend']).is_dir())
        store=Store(self.database)
        try:
            reconcile_restored_database(store,'BILLING_AND_ACTIONS_RECONCILED')
            self.assertEqual(store.get_state('local_recovery_restore_required'),'1')
            self.assertEqual(store.get_state('paused'),'1')
        finally:store.close()
