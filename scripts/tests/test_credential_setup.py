import importlib.util
import contextlib
import io
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "provision-controller-data-keys.py"


class DataCredentialSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.creds = self.root / "credentials"
        self.creds.mkdir()
        self.state = self.root / "control"
        self.state.mkdir()
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))

    def module(self):
        self.assertTrue(SCRIPT.exists(), "controller data-key provisioning must exist")
        spec = importlib.util.spec_from_file_location("data_key_setup", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def fake_creds(self, args, **kwargs):
        # systemd's host key is root-only, so replace just this external boundary.
        if "encrypt" in args:
            key = kwargs["input"]
            self.assertEqual(len(key), 32)
            Path(args[-1]).write_bytes(b"encrypted:" + key)
            return subprocess.CompletedProcess(args, 0, b"", b"")
        blob = Path(args[-2]).read_bytes()
        if not blob.startswith(b"encrypted:"):
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, blob[len(b"encrypted:"):], b"")

    def test_creates_distinct_keys_and_keeps_existing_keys_on_retry(self):
        module = self.module()
        with patch.object(module.subprocess, "run", side_effect=self.fake_creds):
            module.provision(self.creds, self.state)
            original = {p.name: p.read_bytes() for p in self.creds.iterdir()}
            module.provision(self.creds, self.state)
        self.assertEqual(len(original), 2)
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.creds.iterdir()})
        self.assertEqual(len(set(original.values())), 2)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in self.creds.iterdir()))
        self.assertTrue(all(value[len(b"encrypted:"):].hex() not in self.output.getvalue() for value in original.values()))

    def test_refuses_missing_backup_key_when_snapshots_exist(self):
        module = self.module()
        backups = self.state / "backups"
        backups.mkdir()
        snapshot = backups / "memory-existing.andrewdb"
        snapshot.write_bytes(b"existing ciphertext")
        with self.assertRaisesRegex(RuntimeError, "snapshots"):
            module.provision(self.creds, self.state)
        self.assertEqual(list(self.creds.iterdir()), [])
        self.assertEqual(snapshot.read_bytes(), b"existing ciphertext")

    def test_refuses_missing_history_key_when_visits_exist(self):
        module = self.module()
        history = self.state / "browser-history"
        history.mkdir()
        with sqlite3.connect(history / "history.sqlite3") as db:
            db.execute("CREATE TABLE visits (id INTEGER)")
            db.execute("INSERT INTO visits VALUES (1)")
        with self.assertRaisesRegex(RuntimeError, "history"):
            module.provision(self.creds, self.state)
        self.assertEqual(list(self.creds.iterdir()), [])

    def test_refuses_invalid_existing_key_without_replacing_it(self):
        module = self.module()
        existing = self.creds / "andrew-memory-backup-key"
        existing.write_bytes(b"unreadable credential")
        with patch.object(module.subprocess, "run", side_effect=self.fake_creds):
            with self.assertRaisesRegex(RuntimeError, "andrew-memory-backup-key"):
                module.provision(self.creds, self.state)
        self.assertEqual(existing.read_bytes(), b"unreadable credential")
        self.assertFalse((self.creds / "andrew-history-key").exists())

    def test_rejects_decryptable_credential_with_wrong_key_length(self):
        module = self.module()
        existing = self.creds / "andrew-memory-backup-key"
        existing.write_bytes(b"encrypted:short-key")
        with patch.object(module.subprocess, "run", side_effect=self.fake_creds):
            with self.assertRaisesRegex(RuntimeError, "32-byte"):
                module.provision(self.creds, self.state)
        self.assertEqual(existing.read_bytes(), b"encrypted:short-key")
