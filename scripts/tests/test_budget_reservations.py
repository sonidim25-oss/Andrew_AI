"""Real SQLite reconciliation: no provider traffic, replay, or lifecycle writes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'prototype'))
from andrewbot.store import Store
from andrewbot.gateway import ModelGateway
from prototype.tests.test_gateway import FakeProvider


class BudgetReservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'controller.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.store.set_state('paused', '1')
        self.store.set_state('owner_control_revision', '7')
        self.payload = {'model': 'qwen/qwen3.8-flash',
                        'messages': [{'role': 'user', 'content': 'PRIVATE request'}]}
        identity = json.dumps({'request': self.payload, 'owner_retry': ''},
                              sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        self.rid = 'provider-' + hashlib.sha256(identity.encode()).hexdigest()
        self.store.reserve_usage(self.rid, '2026-09', '2026-09-03', 0.25)
        with self.store.connection:
            self.store.connection.execute("UPDATE budget_reservations SET created_at='2026-09-03T12:00:00+00:00'")

    def api(self):
        path = ROOT / 'scripts/budget_reservations.py'
        self.assertTrue(path.is_file(), 'Missing evidence-based reservation reconciliation tool')
        spec = importlib.util.spec_from_file_location('budget_reservations', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def attempt(self, outcome='not_reached'):
        keys = {'active_session_id': 'session-private', 'active_cycle_id': 'cycle-private', 'autonomous_owner_id': '17'}
        previous = {key: self.store.get_state(key, '') for key in keys}
        for key, value in keys.items():
            self.store.set_state(key, value)
        try:
            instance = self.store.connection.execute(
                'SELECT instance_id FROM legacy_provider_reservations WHERE reservation_id=?', (self.rid,)).fetchone()[0]
            attempt_id = self.store.record_provider_attempt(self.rid, reservation_instance=instance)
            if outcome != 'dispatching':
                self.store.finish_provider_attempt(attempt_id, outcome)
        finally:
            for key, value in previous.items():
                self.store.set_state(key, value)

    def evidence(self, outcome='not_reached'):
        row = self.api().inspect(self.path)['reservations'][0]
        proof = {'contract': 'budget_reservation_reconciliation_v1', 'reservation_id': self.rid,
                 'snapshot_sha256': row['snapshot_sha256'], 'owner_revision': '7', 'outcome': outcome}
        if outcome == 'completed':
            proof['billing'] = {'source': 'owner_verified_provider_billing', 'record_id': 'gen-verified-123',
                'request_identity_sha256': self.rid[9:], 'cost_usd': 0.031,
                'input_tokens': 123, 'output_tokens': 45}
        return proof

    def apply(self, proof):
        api = self.api()
        return api.reconcile(self.path, proof, approved_evidence_sha256=api.digest(proof))

    def count(self, table):
        return self.store.connection.execute('SELECT count(*) FROM ' + table).fetchone()[0]

    def test_inspection_is_read_only_private_and_does_not_expire_old_reservations(self):
        before = self.store.connection.serialize()
        report = self.api().inspect(self.path)
        self.assertEqual(before, self.store.connection.serialize())
        self.assertEqual(report['held_cost_usd'], 0.25)
        self.assertEqual(report['reservations'][0]['classification'], 'unresolved')
        self.assertNotIn('PRIVATE', json.dumps(report))
        self.assertEqual(self.count('budget_reservations'), 1)

    def test_proven_unsent_release_is_atomic_and_keeps_durable_duplicate_guard(self):
        self.attempt()
        proof = self.evidence()
        state = list(self.store.connection.execute('SELECT * FROM state'))
        self.assertTrue(self.apply(proof)['reconciled'])
        self.assertEqual(self.count('budget_reservations'), 0)
        self.assertEqual(self.count('usage'), 1)
        self.assertEqual(self.store.connection.execute('SELECT cost_usd FROM usage').fetchone()[0], 0.0)
        self.assertEqual(state, list(self.store.connection.execute('SELECT * FROM state')))
        self.assertFalse(self.store.reserve_usage(self.rid, '2026-10', '2026-10-22', 0.9))
        reopened = Store(self.path)
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.reserve_usage(self.rid, '2026-10', '2026-10-22', 0.9))

    def test_confirmed_billing_records_real_charge_in_original_period_once(self):
        proof = self.evidence('completed')
        self.apply(proof)
        usage = dict(self.store.connection.execute('SELECT * FROM usage').fetchone())
        self.assertEqual((usage['cost_usd'], usage['input_tokens'], usage['output_tokens']), (0.031, 123, 45))
        self.assertEqual(usage['created_at'], '2026-09-03T12:00:00+00:00')
        self.assertEqual(self.store.usage_totals('2026-09', '2026-09-03')['monthly_cost'], 0.031)
        self.assertTrue(self.apply(proof)['already_reconciled'])
        self.assertEqual(self.count('usage'), 1)
        self.assertEqual(self.api().inspect(self.path)['held_cost_usd'], 0)

    def test_missing_or_ambiguous_unsent_proof_refuses_without_changes(self):
        for outcome in (None, 'uncertain', 'dispatching', 'completed'):
            with self.subTest(outcome=outcome):
                self.store.connection.execute('DELETE FROM legacy_provider_attempt_instances')
                self.store.connection.execute('DELETE FROM provider_attempts')
                self.store.connection.commit()
                if outcome:
                    self.attempt(outcome)
                proof = self.evidence()
                before = self.store.connection.serialize()
                with self.assertRaises(ValueError):
                    self.apply(proof)
                self.assertEqual(before, self.store.connection.serialize())

    def test_changed_reservation_or_attempt_refuses_stale_snapshot(self):
        self.attempt()
        proof = self.evidence()
        self.attempt('uncertain')
        with self.assertRaises(ValueError):
            self.apply(proof)
        self.assertEqual(self.count('budget_reservations'), 1)

    def test_old_unsent_attempt_cannot_release_a_reused_request_after_timeout(self):
        from unittest.mock import Mock
        from andrewbot.model import ProviderRequestError
        self.attempt()
        self.store.settle_usage(self.rid, '2026-09', '2026-09-03', 0, 0, 0)
        self.store.set_state('paused', '0')
        provider = FakeProvider()
        provider.complete = Mock(side_effect=ProviderRequestError('timeout', request_sent=True))
        gateway = ModelGateway(provider, self.store, 'fixture-token')
        status, _ = gateway.complete('Bearer fixture-token', self.payload)
        self.assertEqual(status, 422)
        self.assertEqual(provider.complete.call_count, 1)
        self.store.set_state('paused', '1')
        report = self.api().inspect(self.path)
        self.assertEqual(report['reservations'][0]['classification'], 'unresolved')
        with self.assertRaises(ValueError):
            self.apply(self.evidence())
        self.assertEqual(self.count('budget_reservations'), 1)

    def test_changed_owner_pause_active_work_and_restore_fences_refuse(self):
        self.attempt()
        proof = self.evidence()
        for key, value in [('owner_control_revision', '8'), ('paused', '0'),
                           ('active_cycle_id', 'busy'), ('model_turn_origin', 'owner'),
                           ('restore_recovery_required', '1'), ('local_recovery_restore_required', '1')]:
            with self.subTest(key=key):
                old = self.store.get_state(key, '')
                self.store.set_state(key, value)
                try:
                    with self.assertRaises(ValueError):
                        self.apply(proof)
                finally:
                    self.store.set_state(key, old)
        marker = self.path.parent / 'runner-restore-required.json'
        marker.write_text('{}')
        with self.assertRaises(ValueError):
            self.apply(proof)

    def test_journaled_effects_refuse_generic_ledger_repair(self):
        self.attempt()
        with self.store.connection:
            self.store.connection.execute('INSERT INTO recovery_effects VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                ('admission', 'scope', 'binding', 'provider', 'effect', 'request', 'token', 1,
                 'admitted', self.rid, '2026-09-03'))
        self.assertEqual(self.api().inspect(self.path)['reservations'][0]['classification'], 'journaled_requires_protocol')
        with self.assertRaises(ValueError):
            self.apply(self.evidence())

    def test_malformed_nonfinite_or_unbound_billing_cannot_release_reservation(self):
        for field, value in [('cost_usd', float('nan')), ('cost_usd', -1), ('cost_usd', True),
                             ('input_tokens', 1.5), ('output_tokens', 2**63),
                             ('record_id', ''), ('request_identity_sha256', '0' * 64),
                             ('source', 'inferred_from_timeout')]:
            with self.subTest(field=field, value=value):
                proof = self.evidence('completed')
                proof['billing'][field] = value
                with self.assertRaises(ValueError):
                    self.apply(proof)
                self.assertEqual(self.count('usage'), 0)
                self.assertEqual(self.count('budget_reservations'), 1)

    def test_owner_must_pin_exact_reviewed_evidence(self):
        self.attempt()
        with self.assertRaises(ValueError):
            self.api().reconcile(self.path, self.evidence(), approved_evidence_sha256='0' * 64)
        self.assertEqual(self.count('usage'), 0)

    def test_conflicting_second_reconciliation_refuses(self):
        proof = self.evidence('completed')
        self.apply(proof)
        proof['billing']['cost_usd'] = 0.2
        with self.assertRaises(ValueError):
            self.apply(proof)
        self.assertEqual(self.count('usage'), 1)

    def test_damaged_usage_audit_cannot_be_reported_as_reconciled(self):
        proof = self.evidence('completed')
        self.apply(proof)
        with self.store.connection:
            self.store.connection.execute('UPDATE usage SET cost_usd=0')
        with self.assertRaises(ValueError):
            self.apply(proof)

    def test_confirmed_billing_conflicting_with_current_unsent_proof_stays_held(self):
        self.attempt()
        with self.assertRaises(ValueError):
            self.apply(self.evidence('completed'))
        self.assertEqual(self.count('budget_reservations'), 1)

    def test_receipt_cannot_pay_for_two_different_reservations(self):
        self.apply(self.evidence('completed'))
        second = 'provider-' + 'b' * 64
        self.store.reserve_usage(second, '2026-09', '2026-09-03', 0.3)
        self.rid = second
        with self.assertRaises(ValueError):
            self.apply(self.evidence('completed'))
        self.assertEqual(self.count('budget_reservations'), 1)
        self.assertEqual(self.count('usage'), 1)

    def test_interrupted_write_rolls_back_charge_guard_and_deletion(self):
        self.attempt()
        proof = self.evidence()
        with self.store.connection:
            self.store.connection.execute("CREATE TRIGGER fail_reconcile BEFORE DELETE ON budget_reservations BEGIN SELECT RAISE(ABORT,'fixture interruption'); END")
        with self.assertRaises(sqlite3.Error):
            self.apply(proof)
        self.assertEqual(self.count('budget_reservations'), 1)
        self.assertEqual(self.count('usage'), 0)
        self.assertEqual(self.count('budget_reservation_reconciliations'), 0)

    def test_two_connections_settle_once(self):
        import concurrent.futures
        self.attempt()
        proof = self.evidence()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.apply(proof), range(2)))
        self.assertEqual(sum(bool(r.get('already_reconciled')) for r in results), 1)
        self.assertEqual(self.count('usage'), 1)

    def test_private_backup_is_valid_and_never_overwrites_existing_files(self):
        import stat
        api = self.api()
        destination = self.path.parent / 'before.sqlite3'
        api.backup(self.path, destination)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        with sqlite3.connect(destination) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM budget_reservations').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        before = destination.read_bytes()
        with self.assertRaises(FileExistsError):
            api.backup(self.path, destination)
        self.assertEqual(destination.read_bytes(), before)

    def test_cli_refuses_live_services_before_backup_or_mutation(self):
        from unittest.mock import patch
        api = self.api()
        proof = self.evidence('completed')
        evidence = self.path.parent / 'proof.json'
        evidence.write_text(json.dumps(proof))
        backup = self.path.parent / 'before.sqlite3'
        before = self.store.connection.serialize()
        with patch.object(api.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'active\n', '')):
            with self.assertRaises(ValueError):
                api.main(['--database', str(self.path), '--apply', str(evidence),
                          '--approve-evidence-sha256', api.digest(proof), '--backup', str(backup)])
        self.assertFalse(backup.exists())
        self.assertEqual(before, self.store.connection.serialize())

    def test_changed_instance_before_attempt_blocks_dispatch_attribution(self):
        instance = self.store.connection.execute('SELECT instance_id FROM legacy_provider_reservations').fetchone()[0]
        self.store.settle_usage(self.rid, '2026-09', '2026-09-03', 0, 0, 0)
        self.store.reserve_usage(self.rid, '2026-10', '2026-10-22', 0.25)
        with self.assertRaises(ValueError):
            self.store.record_provider_attempt(self.rid, reservation_instance=instance)

    def test_older_database_report_needs_no_migration_but_apply_refuses(self):
        self.attempt()
        proof = self.evidence()
        with self.store.connection:
            self.store.connection.execute('DROP TABLE budget_reservation_reconciliations')
        before = self.store.connection.serialize()
        self.assertFalse(self.api().inspect(self.path)['runtime_guard_installed'])
        with self.assertRaises(ValueError):
            self.apply(proof)
        self.assertEqual(before, self.store.connection.serialize())

    def test_gateway_does_not_dispatch_reconciled_request(self):
        self.apply(self.evidence('completed'))
        self.store.set_state('paused', '0')
        provider = FakeProvider()
        gateway = ModelGateway(provider, self.store, 'fixture-token')
        status, _ = gateway.complete('Bearer fixture-token', self.payload)
        self.assertEqual(status, 422)
        self.assertEqual(provider.calls, [])

    def test_cli_report_is_read_only_and_missing_database_is_not_created(self):
        api = self.api()
        before = self.store.connection.serialize()
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/budget_reservations.py'),
                                 '--database', str(self.path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['read_only'])
        self.assertEqual(before, self.store.connection.serialize())
        with self.assertRaises(ValueError):
            api.inspect(self.path.parent / 'missing.sqlite3')
        self.assertFalse((self.path.parent / 'missing.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
