"""Deployment preconditions are read-only while Andrew is active."""
import copy
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('budget_deployment', ROOT / 'scripts/budget_deployment.py')
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)


class BudgetDeploymentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.database = self.root / 'controller.sqlite3'
        self.db = sqlite3.connect(self.database)
        self.addCleanup(self.db.close)
        self.db.executescript('CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT);'
                             'CREATE TABLE budget_reservations(reservation_id TEXT,cost_usd REAL);'
                             'CREATE TABLE usage(id INTEGER,cost_usd REAL);')
        self.state('paused', '1')
        self.state('owner_control_revision', '17')

    def state(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)', (key, value))

    def test_active_andrew_refused_without_writes(self):
        self.state('paused', '0')
        before = self.database.read_bytes()
        with self.assertRaisesRegex(ValueError, '/pause'):
            rollout.require_pause(self.database)
        self.assertEqual(before, self.database.read_bytes())

    def test_paused_owner_revision_is_captured_without_writes(self):
        before = self.database.read_bytes()
        state = rollout.require_pause(self.database)
        self.assertEqual(state['owner_control_revision'], '17')
        self.assertEqual(state['paused'], '1')
        self.assertEqual(before, self.database.read_bytes())

    def test_unsettled_work_and_restore_fences_refuse(self):
        for key in ('active_cycle_id', 'active_session_id', 'autonomous_owner_id',
                    'model_turn_origin', 'session_start_requested',
                    'restore_recovery_required', 'local_recovery_restore_required'):
            with self.subTest(key=key):
                self.state(key, '1')
                with self.assertRaises(ValueError):
                    rollout.require_pause(self.database)
                self.state(key, '')

    def test_external_restore_marker_without_database_flags_refuses(self):
        (self.root / 'runner-restore-required.json').write_text('{}')
        before = self.database.read_bytes()
        with self.assertRaises(ValueError):
            rollout.require_pause(self.database)
        self.assertEqual(before, self.database.read_bytes())

    def test_backend_and_runner_restore_barriers_refuse(self):
        backend = self.root / 'backend'
        runner = self.root / 'runner'
        recovery = backend / '.letta/lc-local-backend/local-recovery-v1'
        recovery.mkdir(parents=True)
        runner.mkdir()
        for marker in (recovery / 'restore-required.json', runner / 'restore-required.json'):
            marker.write_text('{}')
            try:
                with self.assertRaises(ValueError):
                    rollout.require_pause(self.database, backend_home=backend, runner_root=runner)
            finally:
                marker.unlink()

    def test_each_baseline_digest_and_mode_is_pinned(self):
        rollout.verify_baseline(copy.deepcopy(rollout.BASELINE))
        for key in rollout.BASELINE:
            record = copy.deepcopy(rollout.BASELINE)
            record[key] = 'changed'
            with self.subTest(key=key), self.assertRaises(ValueError):
                rollout.verify_baseline(record)

    def test_billing_snapshot_detects_changes_and_new_empty_schema_is_equivalent(self):
        original = rollout.billing_snapshot(self.database)
        self.db.execute('CREATE TABLE budget_reservation_reconciliations(reservation_id TEXT)')
        self.assertEqual(rollout.billing_snapshot(self.database), original)
        with self.db:
            self.db.execute("INSERT INTO budget_reservations VALUES('held',0.25)")
        held = rollout.billing_snapshot(self.database)
        self.assertNotEqual(held, original)
        self.assertNotIn('held', str(held))
        with self.db:
            self.db.execute('INSERT INTO usage VALUES(1,0.01)')
        self.assertNotEqual(rollout.billing_snapshot(self.database), held)

    def test_disk_headroom_refuses_before_changes(self):
        backend = self.root / 'backend'
        backend.mkdir()
        (backend / 'fixture').write_bytes(b'x' * 100)
        runner = self.root / 'runner'
        runner.mkdir()
        (runner / 'fixture').write_bytes(b'x' * 10)
        expected = 3 * (self.database.stat().st_size + 100) + 20 + 1024**3
        self.assertEqual(rollout.require_headroom(self.database, backend, runner, available=expected), expected)
        with self.assertRaises(ValueError):
            rollout.require_headroom(self.database, backend, runner, available=expected - 1)


if __name__ == '__main__':
    unittest.main()
