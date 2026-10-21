"""Receipt-only retirement of one historical research rejection while paused.

Offline phases require quiescent workers, the exact inspected proof, and a
verified paired snapshot. They never call a tool/provider or alter owner controls.
Backend finalization uses the existing evidence-only owner-yield protocol.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'prototype'))
from andrewbot.recovery_records import RecoveryRecords, canonical, digest, paused_owner_yield
from andrewbot.memory_backup import verify_paired_recovery_snapshot
from andrewbot.store import Store
from inspect_research_hold import rows_digest
from inspect_owner_handoff import read


def evidence(proof):
    # CLI display adds eligible; it is not part of the inspected proof.
    return {k: v for k, v in proof.items() if k != 'eligible'}


def terminal_for(proof, original):
    return {'id': digest({'proof': digest(proof), 'kind': 'terminal'})[:32],
            'run_id': original['run_id'], 'consumer_id': proof['binding']['consumer_id'],
            'stop_reason': 'owner_input_pending', 'assistant_ids': [],
            'yield_receipt_id': digest({'proof': digest(proof), 'kind': 'yield'})[:32],
            'cancelled_admission_ids': []}


def protected_unchanged(db, proof, *, recorded=False):
    state = dict(db.execute('SELECT key,value FROM state'))
    if (not paused_owner_yield(state, proof['binding'])
            or {key: state.get(key, '') for key in proof['guards']} != proof['guards']):
        raise ValueError('Owner controls changed')
    for name, query in (
        ('research', 'SELECT * FROM research_entries ORDER BY record_id'),
        ('held_messages', 'SELECT * FROM messages WHERE queued=3 ORDER BY id'),
        ('reservations', 'SELECT * FROM budget_reservations ORDER BY reservation_id'),
        ('cycle_tools', 'SELECT * FROM cycle_tool_attempts ORDER BY attempt_id'),
    ):
        if rows_digest(db, query) != proof[name]:
            raise ValueError('Unrelated records changed: ' + name)
    rows = [dict(row) for row in db.execute('SELECT * FROM recovery_effects ORDER BY admission_id')]
    for row in rows:
        if row['admission_id'] == proof['tool_admission_id'] and recorded:
            if row['state'] != 'confirmed':
                raise ValueError('Reconciled effect differs')
            row['state'] = 'admitted'
    if {'count': len(rows), 'sha256': digest(rows)} != proof['effects']:
        raise ValueError('Other effects changed')
    rows = [dict(row) for row in db.execute('SELECT * FROM recovery_results ORDER BY admission_id')
            if not (recorded and row['admission_id'] == proof['tool_admission_id'])]
    if {'count': len(rows), 'sha256': digest(rows)} != proof['results']:
        raise ValueError('Other result receipts changed')


def original_checkpoint(snapshot, home, proof):
    metadata = verify_paired_recovery_snapshot(snapshot)
    relative = Path(proof['checkpoint_path']).relative_to(home).as_posix()
    if metadata['backend_files'].get(relative) != proof['checkpoint_sha256']:
        raise ValueError('Paired snapshot checkpoint differs')
    original = json.loads(read(Path(snapshot) / 'backend' / relative))
    if digest(original) != proof['checkpoint_sha256']:
        raise ValueError('Original checkpoint is not canonical')
    return original


def record(database, home, proof, snapshot=None, *, original=None):
    proof = evidence(proof)
    original = original if original is not None else original_checkpoint(snapshot, home, proof)
    if digest(original) != proof['checkpoint_sha256'] or original['binding'] != proof['binding']:
        raise ValueError('Original checkpoint differs')
    terminal = terminal_for(proof, original)
    # Opening Store is intentionally avoided: the offline repair must not run
    # unrelated startup migrations/history backfill inside the proof boundary.
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        with db:
            db.execute('BEGIN IMMEDIATE')
            saved = db.execute('SELECT * FROM recovery_results WHERE admission_id=?',
                               (proof['tool_admission_id'],)).fetchone()
            protected_unchanged(db, proof, recorded=saved is not None)
            if saved is None:
                # The root coordinator verifies the exact read-only inspection
                # under stopped writers before passing this checkpoint to the
                # controller UID. That UID cannot read private backend files.
                class ConnectionOnly:
                    connection = db
                    @staticmethod
                    def now():
                        from datetime import datetime, timezone
                        return datetime.now(timezone.utc).isoformat(timespec='seconds')
                records = RecoveryRecords(ConnectionOnly())
                records._confirm(proof['tool_admission_id'], canonical(proof['validation_result']),
                    {'historical_research_rejection': {'contract': proof['contract'],
                        'evidence_sha': digest(proof), 'executed': False, 'request_sha': proof['request_sha']}})
            else:
                verify_receipt(db, proof)
            ids = sorted(row[0] for row in db.execute(
                'SELECT admission_id FROM recovery_effects WHERE binding_sha=?', (digest(proof['binding']),)))
            previous = db.execute('SELECT * FROM recovery_owner_yields WHERE binding_sha=?',
                                  (digest(proof['binding']),)).fetchone()
            expected = (terminal['yield_receipt_id'], canonical(ids), '[]')
            if previous:
                if (previous['receipt_id'], previous['admission_ids_json'], previous['cancelled_ids_json']) != expected:
                    raise ValueError('Owner retirement receipt differs')
            else:
                db.execute('INSERT INTO recovery_owner_yields VALUES(?,?,?,?,?)',
                    (digest(proof['binding']), expected[0], expected[1],
                     db.execute("SELECT strftime('%Y-%m-%dT%H:%M:%S+00:00','now')").fetchone()[0], expected[2]))
    return {'recorded': True, 'evidence_sha': digest(proof), 'terminal': terminal}


def verify_receipt(db, proof):
    row = db.execute('SELECT r.*,e.state,e.kind,e.request_sha FROM recovery_results r '
                     'JOIN recovery_effects e ON e.admission_id=r.admission_id WHERE r.admission_id=?',
                     (proof['tool_admission_id'],)).fetchone()
    usage = {'historical_research_rejection': {'contract': proof['contract'],
             'evidence_sha': digest(proof), 'executed': False, 'request_sha': proof['request_sha']}}
    if (row is None or row['state'] != 'confirmed' or row['kind'] != 'tool'
            or row['request_sha'] != proof['request_sha'] or json.loads(row['result_json']) != proof['validation_result']
            or row['result_sha'] != proof['validation_result_sha'] or json.loads(row['usage_json']) != usage):
        raise ValueError('Exact research rejection receipt missing')
    return {'state': 'confirmed', 'kind': 'tool', 'admission_id': proof['tool_admission_id'],
            'request_sha': row['request_sha'], 'result': proof['validation_result'],
            'result_sha': row['result_sha'], 'usage': usage}


def migrate_checkpoint(database, home, proof, snapshot, *, write=False, node_identity=None, node_script=None):
    proof = evidence(proof)
    original = original_checkpoint(snapshot, home, proof)
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        protected_unchanged(db, proof, recorded=True)
        receipt = verify_receipt(db, proof)
        payload = {'proof': proof, 'original': original, 'receipt': receipt,
                   'terminal': terminal_for(proof, original), 'write': write}
        result = subprocess.run(['/usr/bin/node', str(node_script or ROOT / 'scripts/reconcile-research-checkpoint.mjs')],
                                input=canonical(payload), capture_output=True, text=True, timeout=30,
                                **(node_identity or {}))
        if result.returncode:
            raise ValueError('Checkpoint migration refused; preserve stopped state')
        return json.loads(result.stdout)


def finalize(database, home, proof, snapshot=None, *, client, original=None):
    """Project the already retired input and acknowledge it, with no resume."""
    proof = evidence(proof)
    original = original if original is not None else original_checkpoint(snapshot, home, proof)
    if digest(original) != proof['checkpoint_sha256']:
        raise ValueError('Original checkpoint differs')
    expected = terminal_for(proof, original)
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        protected_unchanged(db, proof, recorded=True)
        verify_receipt(db, proof)
    store = Store(database)
    try:
        records = RecoveryRecords(store)
        binding = proof['binding']
        scope = records.admit_scope(binding, evidence_only=True)
        state = client.inspect(binding)
        terminal = state.get('terminal')
        if not terminal or {k: terminal.get(k) for k in expected} != expected:
            raise ValueError('Backend has not reached the exact offline retirement')
        claim = client.command('recovery_yield', binding=binding, expected_revision=state['revision'], **scope)
        try:
            terminal = client.inspect(binding)['terminal']
            records.complete_scope(scope['admission_id'], binding, terminal)
            client.ack(binding, scope, claim, terminal)
        finally:
            client.release(binding, scope, claim)
        records.finish_owner_yield(scope['admission_id'], binding)
        with store._lock:
            protected_unchanged(store.connection, proof, recorded=True)
        return {'retired': True, 'paused': True, 'automatic_resume': False,
                'provider_calls': 0, 'tool_calls': 0, 'evidence_sha': digest(proof)}
    finally:
        store.close()


def evidence_gateway(store, token):
    from andrewbot.gateway import ModelGateway
    class EvidenceGateway(ModelGateway):
        def complete(self, *args, **kwargs):
            raise ValueError('Provider dispatch disabled during historical maintenance')

        def recovery_request(self, path, value):
            if path not in {'/v1/recovery/check_evidence', '/v1/recovery/result', '/v1/recovery/settlement'}:
                return 423, {'error': {'type': 'maintenance_evidence_only'}}
            return super().recovery_request(path, value)

    class NoProvider:
        def complete(self, *args, **kwargs):
            raise AssertionError('Provider dispatch is forbidden')
    return EvidenceGateway(NoProvider(), store, token)


def service_phase():
    """Invoked by the administrator coordinator under the controller UID."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('record', 'finalize'), required=True)
    args = parser.parse_args()
    payload = json.load(sys.stdin)
    if args.phase == 'record':
        result = record(**payload)
    else:
        from andrewbot.appserver import AppServerClient
        from andrewbot.gateway import make_server, GatewayThread
        from andrewbot.main import systemd_credential
        from andrewbot.recovery import LocalRecoveryClient
        import time

        store = Store(payload['database'])
        gateway = GatewayThread(make_server(evidence_gateway(store,
                                  systemd_credential('andrew-gateway-token')), port=8284))
        client = None
        gateway.start()
        try:
            deadline = time.monotonic() + 45
            while True:
                try:
                    client = LocalRecoveryClient(AppServerClient())
                    client.capabilities()
                    break
                except (OSError, RuntimeError):
                    if client:
                        client.close()
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.2)
            result = finalize(**payload, client=client)
        finally:
            if client:
                client.close()
            gateway.close()
            store.close()
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        service_phase()
    except Exception:
        raise SystemExit('Research maintenance refused; retain stopped controller for inspection.')
