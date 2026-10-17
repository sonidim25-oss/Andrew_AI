import os
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from andrewbot.memory_backup import PeriodicMemoryBackup, create_encrypted_snapshot, restore_snapshot
from andrewbot.main import build_memory_backup_scheduler
from andrewbot.memory_backup_cli import main as backup_cli
from andrewbot.store import Store


class MemoryBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "controller.sqlite3")
        self.key = bytes(range(32))
        self.backup_dir = self.root / "backups"

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_snapshot_contains_wal_committed_memory_and_diary_rows(self):
        revision_id = self.store.create_personality_revision(
            "I enjoy astronomy.", "A durable interest.", "andrew"
        )
        diary_id = self.store.create_diary_entry("2026-10-14", "I looked at the night sky.")
        snapshot = create_encrypted_snapshot(self.store, self.backup_dir, self.key)
        restored_path = self.root / "restored.sqlite3"

        restore_snapshot(snapshot, restored_path, self.key)
        restored = Store(restored_path)
        try:
            self.assertEqual(restored.current_personality_revision()["revision_id"], revision_id)
            self.assertEqual(restored.diary_entry("2026-10-14")["entry_id"], diary_id)
            self.assertEqual(restored.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            restored.close()

    def test_snapshot_is_encrypted_and_restorable(self):
        private_text = "Private astronomy observation for the backup test."
        self.store.create_memory_record("experience", private_text, "andrew")
        snapshot = create_encrypted_snapshot(self.store, self.backup_dir, self.key)
        blob = snapshot.read_bytes()
        self.assertNotIn(private_text.encode(), blob)
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)

        restored_path = self.root / "restored.sqlite3"
        restore_snapshot(snapshot, restored_path, self.key)
        restored = Store(restored_path)
        try:
            rows = restored.search_memory_rows('"astronomy"', 8)
            self.assertEqual(rows[0]["content"], private_text)
        finally:
            restored.close()

    def test_corrupt_or_wrong_key_restore_leaves_destination_unchanged(self):
        snapshot = create_encrypted_snapshot(self.store, self.backup_dir, self.key)
        destination = self.root / "existing.sqlite3"
        destination.write_bytes(b"existing destination bytes")
        before = destination.read_bytes()

        with self.assertRaises(ValueError):
            restore_snapshot(snapshot, destination, b"x" * 32)
        self.assertEqual(destination.read_bytes(), before)

        snapshot.write_bytes(b"truncated")
        with self.assertRaises(ValueError):
            restore_snapshot(snapshot, destination, self.key)
        self.assertEqual(destination.read_bytes(), before)

    def test_restore_preserves_newer_spending_and_quarantines_delivered_reply(self):
        self.store.create_memory_record('experience', 'A retained memory.', 'andrew')
        self.store.queue_reply(17, 'Previously sent reply')
        snapshot = create_encrypted_snapshot(self.store, self.backup_dir, self.key)
        row = self.store.claim_reply('normal')
        self.store.finish_reply(row['id'], row['claim_token'])
        self.store.complete_update(100)
        self.store.record_usage('2026-10', '2026-10-17', 1.0, 1, 1)
        self.store.reserve_usage('latest-uncertain-attempt', '2026-10', '2026-10-17', .1)
        path = self.store.path
        self.store.close()
        restore_snapshot(snapshot, path, self.key)
        self.store = Store(path)
        self.assertIsNone(self.store.claim_reply('normal'))
        self.assertEqual(self.store.last_update_id(), 100)
        self.assertEqual(self.store.connection.execute('SELECT sum(cost_usd) FROM usage').fetchone()[0], 1.0)
        self.assertEqual(self.store.connection.execute('SELECT reservation_id FROM budget_reservations').fetchone()[0], 'latest-uncertain-attempt')
        self.assertEqual(self.store.get_state('restore_recovery_required'), '1')
        self.assertEqual(len(self.store.search_memory_rows('retained', 8)), 1)

    def test_fresh_restore_blocks_paid_work_until_explicit_reconciliation(self):
        from andrewbot.gateway import ModelGateway
        from andrewbot.media import MediaService, MediaPolicy
        from unittest.mock import Mock
        snapshot = create_encrypted_snapshot(self.store, self.backup_dir, self.key)
        restored_path = self.root / 'restored.sqlite3'
        restore_snapshot(snapshot, restored_path, self.key)
        restored = Store(restored_path)
        self.addCleanup(restored.close)
        provider = Mock()
        gateway = ModelGateway(provider, restored, 'fixture-token')
        # Ordinary wake flags cannot clear the separate recovery gate.
        restored.transition_state({'paused': '0', 'sleeping': '0', 'autonomy_error': ''}, 'fixture wake')
        status, _ = gateway.complete('Bearer fixture-token', {'model': gateway.model_id,
            'messages': [{'role': 'user', 'content': 'must not spend'}]})
        self.assertEqual(status, 423)
        provider.complete.assert_not_called()
        media = MediaService(restored, self.root / 'media', 17, MediaPolicy(enabled=True))
        with self.assertRaisesRegex(ValueError, 'reconcil'):
            media.submit('image', 'fixture', 'restore-test', owner_requested=True)

    def test_restored_queues_stay_quarantined_after_reconciliation(self):
        from andrewbot.memory_backup import reconcile_restored_database
        from andrewbot.media import MediaService, MediaPolicy
        media = MediaService(self.store, self.root / 'artifacts', 17, MediaPolicy(enabled=True))
        job = media.submit('image', 'fixture', 'saved-image', owner_requested=True)
        self.store.add_message(17, 'user', 'old saved input', queued=True)
        self.store.enqueue_update({'update_id': 1, 'message': {'text': '/wake'}})
        diary = self.store.create_diary_entry('2026-10-17', 'A saved diary.')
        snapshot = create_encrypted_snapshot(self.store, self.backup_dir, self.key)
        path = self.root / 'restored.sqlite3'
        restore_snapshot(snapshot, path, self.key)
        restored = Store(path)
        self.addCleanup(restored.close)
        restored_media = MediaService(restored, self.root / 'restored-artifacts', 17, MediaPolicy(enabled=True))
        with self.assertRaises(ValueError):
            reconcile_restored_database(restored, 'not confirmed')
        self.assertEqual(restored.pending_updates(), [])
        self.assertEqual(restored.connection.execute('SELECT count(*) FROM telegram_uncertain').fetchone()[0], 1)
        self.assertEqual(restored.diary_entry('2026-10-17')['delivery_state'], 'uncertain')
        self.assertEqual(restored_media.job(job)['state'], 'uncertain')
        reconcile_restored_database(restored, 'BILLING_AND_ACTIONS_RECONCILED')
        self.assertEqual(restored.get_state('restore_recovery_required'), '0')
        self.assertEqual(restored.get_state('paused'), '1')
        self.assertEqual(restored.uncertain_message_count(17), 1)
        self.assertEqual(restored.claim_queued_messages(17), [])
        self.assertIsNone(restored_media.claim())
        self.assertIsNone(restored_media.claim_delivery())
        self.assertIsNone(restored.claim_diary_sender(diary))

    def test_controller_start_does_not_initialize_clients_after_restore(self):
        from andrewbot import main
        from unittest.mock import Mock
        snapshot = create_encrypted_snapshot(self.store, self.backup_dir, self.key)
        path = self.root / 'halted.sqlite3'
        restore_snapshot(snapshot, path, self.key)
        restored = Store(path)
        self.addCleanup(restored.close)
        vault = Mock()
        vault.names.return_value = ['telegram_bot', 'openrouter_api']
        vault.get_secret.return_value = 'fixture-token'
        with patch('andrewbot.main.Vault', return_value=vault), \
             patch('andrewbot.main.Store', return_value=restored), \
             patch.dict(os.environ, {'ANDREW_OWNER_ID': '17'}), \
             patch('andrewbot.main.TelegramAPI') as telegram, \
             patch('andrewbot.main.LettaAgent') as agent, \
             patch('andrewbot.main.systemd_credential_bytes') as credential:
            self.assertEqual(main.run(), 0)
        telegram.assert_not_called()
        agent.assert_not_called()
        credential.assert_not_called()

    def test_snapshot_rotation_keeps_seven_latest(self):
        paths = [create_encrypted_snapshot(self.store, self.backup_dir, self.key) for _ in range(8)]
        kept = sorted(self.backup_dir.glob("memory-*.andrewdb"))
        self.assertEqual(len(kept), 7)
        self.assertNotIn(paths[0], kept)
        self.assertIn(paths[-1], kept)

    def test_snapshot_file_permissions_are_restrictive(self):
        snapshot = create_encrypted_snapshot(self.store, self.backup_dir, self.key)
        self.assertEqual(self.backup_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)

    def test_periodic_snapshot_runs_at_start_and_after_24_hours(self):
        now = [100.0]
        scheduler = PeriodicMemoryBackup(
            self.store, self.backup_dir, self.key, clock=lambda: now[0]
        )
        self.assertTrue(scheduler.maybe_snapshot())
        self.assertEqual(len(list(self.backup_dir.glob("memory-*.andrewdb"))), 1)
        now[0] += 24 * 60 * 60 - 1
        self.assertFalse(scheduler.maybe_snapshot())
        now[0] += 1
        self.assertTrue(scheduler.maybe_snapshot())
        self.assertEqual(len(list(self.backup_dir.glob("memory-*.andrewdb"))), 2)

    def test_validate_only_does_not_create_backup_scheduler(self):
        scheduler = build_memory_backup_scheduler(
            self.store, self.key, self.backup_dir, validate_only=True
        )
        self.assertIsNone(scheduler)
        self.assertFalse(self.backup_dir.exists())

    def test_admin_cli_creates_and_restores_only_named_local_snapshots(self):
        self.store.create_personality_revision("I enjoy astronomy.", "A durable interest.", "andrew")
        self.store.close()
        credential_dir = self.root / "credentials"
        credential_dir.mkdir(mode=0o700)
        (credential_dir / "andrew-memory-backup-key").write_bytes(self.key)
        with patch.dict(os.environ, {
            "CREDENTIALS_DIRECTORY": str(credential_dir),
            "ANDREW_DATA_DIR": str(self.root),
        }):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(backup_cli(["snapshot"]), 0)
            snapshot = next(self.backup_dir.glob("memory-*.andrewdb"))
            (self.root / "controller.sqlite3").unlink()
            with contextlib.redirect_stdout(output):
                self.assertEqual(backup_cli(["restore", snapshot.name]), 0)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                backup_cli(["restore", "../outside.andrewdb"])
            self.assertNotIn(self.key.hex(), output.getvalue())
        self.store = Store(self.root / "controller.sqlite3")
        self.assertEqual(self.store.current_personality_revision()["content"], "I enjoy astronomy.")


if __name__ == "__main__":
    unittest.main()
