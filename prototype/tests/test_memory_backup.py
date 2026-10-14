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
