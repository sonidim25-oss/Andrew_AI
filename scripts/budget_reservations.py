"""Offline, owner-reviewed legacy billing repair; never dispatch or replay work.

Reports use SQLite read-only mode without Store initialization/migrations. The
writer requires the new Store tombstone guard, an unchanged paused owner context,
exact evidence review, and an unchanged per-reservation snapshot. Journaled
effects are deliberately left to their existing paired recovery protocol.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess

CONTRACT = 'budget_reservation_reconciliation_v1'
LEDGER = 'budget_reservation_reconciliations'
SERVICES = ('andrew-control.service', 'andrew-runner.service',
            'andrew-letta.service', 'andrew-browser.service')
DEFAULT_DATABASE = '/var/lib/andrew/control/controller.sqlite3'


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def connect(database, *, write=False):
    path = Path(database)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Requires an existing regular controller database')
    db = sqlite3.connect(path.resolve().as_uri() + ('?mode=rw' if write else '?mode=ro'),
                         uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    if write:
        db.execute('PRAGMA synchronous=FULL')
    else:
        db.execute('PRAGMA query_only=ON')
    return db


def has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def reservation_evidence(db, reservation_id):
    row = db.execute('SELECT * FROM budget_reservations WHERE reservation_id=?', (reservation_id,)).fetchone()
    if row is None:
        raise ValueError('Reservation is missing; no settlement inferred')
    attempts = [dict(r) for r in db.execute(
        'SELECT * FROM provider_attempts WHERE reservation_id=? ORDER BY attempt_id', (reservation_id,))] if has_table(db, 'provider_attempts') else []
    effects = [dict(r) for r in db.execute(
        'SELECT * FROM recovery_effects WHERE reservation_id=? ORDER BY admission_id', (reservation_id,))] if has_table(db, 'recovery_effects') else []
    current = db.execute('SELECT instance_id FROM legacy_provider_reservations WHERE reservation_id=?',
                         (reservation_id,)).fetchone() if has_table(db, 'legacy_provider_reservations') else None
    bindings = [dict(r) for r in db.execute(
        'SELECT b.* FROM legacy_provider_attempt_instances b JOIN provider_attempts p ON p.attempt_id=b.attempt_id '
        'WHERE p.reservation_id=? ORDER BY b.attempt_id', (reservation_id,))] if has_table(db, 'legacy_provider_attempt_instances') else []
    return {'reservation': dict(row), 'attempts': attempts, 'effects': effects,
            'instance_id': current[0] if current else None, 'attempt_instances': bindings}


def unsent_proven(evidence):
    instance = evidence['instance_id']
    current_ids = {b['attempt_id'] for b in evidence['attempt_instances'] if b['instance_id'] == instance}
    attempts = [a for a in evidence['attempts'] if a['attempt_id'] in current_ids]
    return bool(instance and attempts and all(a['outcome'] == 'not_reached' and a['finished_at'] for a in attempts))


def inspect(database):
    with closing(connect(database)) as db:
        db.execute('BEGIN')
        rows = []
        for reservation_id, in db.execute('SELECT reservation_id FROM budget_reservations ORDER BY reservation_id'):
            evidence = reservation_evidence(db, reservation_id)
            reservation = evidence['reservation']
            attempts = evidence['attempts']
            legacy = re.fullmatch(r'provider-[0-9a-f]{64}', reservation_id) is not None
            if evidence['effects'] or not legacy:
                classification = 'journaled_requires_protocol' if evidence['effects'] or reservation_id.startswith('local-recovery-') else 'unsupported_reservation'
            elif unsent_proven(evidence):
                classification = 'proven_not_reached'
            else:
                classification = 'unresolved'
            rows.append({**reservation, 'classification': classification,
                         'attempt_count': len(attempts),
                         'attempt_outcomes': sorted({a['outcome'] for a in attempts}),
                         'snapshot_sha256': digest(evidence)})
        guarded = has_table(db, LEDGER)
        count = db.execute('SELECT count(*) FROM ' + LEDGER).fetchone()[0] if guarded else 0
        owner = db.execute("SELECT value FROM state WHERE key='owner_control_revision'").fetchone()
        return {'contract': CONTRACT, 'read_only': True,
                'owner_revision': owner[0] if owner else '0',
                'runtime_guard_installed': guarded, 'reconciled_count': count,
                'held_cost_usd': math.fsum(row['cost_usd'] for row in rows), 'reservations': rows}


def validate_proof(proof, approved_evidence_sha256):
    if not isinstance(proof, dict):
        raise ValueError('Invalid reconciliation evidence')
    expected = {'contract', 'reservation_id', 'snapshot_sha256', 'owner_revision', 'outcome'}
    if proof.get('outcome') == 'completed':
        expected.add('billing')
    if (set(proof) != expected or proof.get('contract') != CONTRACT
            or proof.get('outcome') not in ('not_reached', 'completed')
            or not isinstance(proof.get('reservation_id'), str)
            or not re.fullmatch(r'provider-[0-9a-f]{64}', proof['reservation_id'])
            or not isinstance(proof.get('snapshot_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', proof['snapshot_sha256'])
            or not isinstance(proof.get('owner_revision'), str)
            or not re.fullmatch(r'[0-9]+', proof['owner_revision'])):
        raise ValueError('Invalid reconciliation identity or contract')
    evidence_sha = digest(proof)
    if approved_evidence_sha256 != evidence_sha:
        raise ValueError('Evidence differs from the exact owner-reviewed digest')
    if proof['outcome'] == 'not_reached':
        return evidence_sha, 0.0, 0, 0, None
    billing = proof['billing']
    if (not isinstance(billing, dict) or set(billing) != {
            'source', 'record_id', 'request_identity_sha256', 'cost_usd', 'input_tokens', 'output_tokens'}
            or billing['source'] != 'owner_verified_provider_billing'
            or not isinstance(billing['record_id'], str)
            or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', billing['record_id'])
            or billing['request_identity_sha256'] != proof['reservation_id'][9:]
            or type(billing['cost_usd']) not in (int, float)
            or not math.isfinite(billing['cost_usd']) or billing['cost_usd'] < 0
            or any(type(billing[k]) is not int or not 0 <= billing[k] <= 2**63 - 1
                   for k in ('input_tokens', 'output_tokens'))):
        raise ValueError('Requires a reviewed, exactly bound provider billing record')
    return (evidence_sha, billing['cost_usd'], billing['input_tokens'],
            billing['output_tokens'], billing['record_id'])


def reconcile(database, proof, *, approved_evidence_sha256):
    """Offline caller must quiesce all writers; the production CLI enforces this.

    A completed billing proof is a trusted owner attestation of an independently
    checked provider record and request linkage, NOT automatically authenticated
    evidence. Text logs, timeout age, and matching times alone are never proof.
    """
    evidence_sha, cost, inputs, outputs, record_id = validate_proof(proof, approved_evidence_sha256)
    if (Path(database).parent / 'runner-restore-required.json').exists():
        raise ValueError('Restore fence present')
    with closing(connect(database, write=True)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if not has_table(db, LEDGER):
            raise ValueError('Install the Store duplicate guard before reconciliation')
        previous = db.execute('SELECT * FROM ' + LEDGER + ' WHERE reservation_id=?', (proof['reservation_id'],)).fetchone()
        if previous:
            if (previous['evidence_sha256'] != evidence_sha or previous['evidence_json'] != canonical(proof)
                    or previous['outcome'] != proof['outcome'] or previous['provider_record_id'] != record_id):
                raise ValueError('Reservation already reconciled using different evidence')
            if db.execute('SELECT 1 FROM budget_reservations WHERE reservation_id=?', (proof['reservation_id'],)).fetchone():
                raise ValueError('Reconciled reservation reappeared; possible restore mismatch')
            original = json.loads(previous['reservation_json'])
            usage = db.execute('SELECT * FROM usage WHERE id=?', (previous['usage_id'],)).fetchone()
            if (original.get('reservation_id') != proof['reservation_id'] or usage is None
                    or tuple(usage[k] for k in ('month', 'day', 'cost_usd', 'input_tokens', 'output_tokens', 'created_at'))
                    != (original['month'], original['day'], cost, inputs, outputs, original['created_at'])):
                raise ValueError('Reconciliation accounting audit differs')
            return {'reconciled': True, 'already_reconciled': True, 'evidence_sha256': evidence_sha}
        state = dict(db.execute('SELECT key,value FROM state'))
        if (state.get('paused') != '1' or state.get('owner_control_revision', '0') != proof['owner_revision']
                or any(state.get(k) for k in ('active_session_id', 'active_cycle_id', 'model_turn_origin',
                                              'autonomous_owner_id', 'session_start_requested'))
                or any(state.get(k) == '1' for k in ('restore_recovery_required', 'local_recovery_restore_required'))):
            raise ValueError('Requires unchanged owner pause, no active work, and no restore fences')
        evidence = reservation_evidence(db, proof['reservation_id'])
        if digest(evidence) != proof['snapshot_sha256']:
            raise ValueError('Reservation or outcome evidence changed after inspection')
        if evidence['effects']:
            raise ValueError('Journaled reservations require paired protocol reconciliation')
        if proof['outcome'] == 'not_reached' and not unsent_proven(evidence):
            raise ValueError('No complete, unambiguous durable not-reached proof')
        if proof['outcome'] == 'completed' and unsent_proven(evidence):
            raise ValueError('Provider billing contradicts the current not-reached proof')
        if record_id and db.execute('SELECT 1 FROM ' + LEDGER + ' WHERE provider_record_id=?', (record_id,)).fetchone():
            raise ValueError('Provider billing record already reconciled')
        reservation = evidence['reservation']
        stamp = datetime.fromisoformat(reservation['created_at'])
        if stamp.tzinfo is None or type(reservation['cost_usd']) not in (int, float) or not math.isfinite(reservation['cost_usd']) or reservation['cost_usd'] < 0:
            raise ValueError('Malformed original reservation')
        usage_id = db.execute(
            'INSERT INTO usage(month,day,cost_usd,input_tokens,output_tokens,created_at) VALUES(?,?,?,?,?,?)',
            (reservation['month'], reservation['day'], cost, inputs, outputs, reservation['created_at'])).lastrowid
        db.execute('INSERT INTO ' + LEDGER + ' VALUES(?,?,?,?,?,?,?,?)',
                   (proof['reservation_id'], evidence_sha, canonical(proof), canonical(reservation),
                    proof['outcome'], record_id, usage_id, datetime.now(timezone.utc).isoformat(timespec='seconds')))
        db.execute('DELETE FROM budget_reservations WHERE reservation_id=?', (proof['reservation_id'],))
        return {'reconciled': True, 'already_reconciled': False, 'cost_usd': cost,
                'evidence_sha256': evidence_sha, 'automatic_resume': False}


def require_offline_runtime():
    for service in SERVICES:
        result = subprocess.run(['systemctl', 'show', service, '--property=ActiveState', '--value'],
                                capture_output=True, text=True, check=True)
        if result.stdout.strip() not in ('inactive', 'failed'):
            raise ValueError('Stop controller, runner, backend and browser services before applying')
    expected = Path(__file__).resolve().parents[1] / 'prototype/andrewbot/store.py'
    installed = Path('/opt/andrew/prototype/andrewbot/store.py')
    if hashlib.sha256(expected.read_bytes()).digest() != hashlib.sha256(installed.read_bytes()).digest():
        raise ValueError('Installed Store differs; deploy the reviewed runtime guard first')


def backup(database, destination):
    """Exclusive private SQLite backup before any ledger write; never overwrite."""
    path = Path(destination)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    with closing(connect(database)) as source, closing(sqlite3.connect(path)) as target:
        source.backup(target)
        if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Backup integrity failed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default=DEFAULT_DATABASE)
    parser.add_argument('--apply', type=Path, metavar='EVIDENCE_JSON')
    parser.add_argument('--approve-evidence-sha256')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args(argv)
    if args.apply:
        if not args.approve_evidence_sha256 or not args.backup:
            parser.error('--apply requires --approve-evidence-sha256 and a new --backup path')
        proof = json.loads(args.apply.read_text())
        validate_proof(proof, args.approve_evidence_sha256)
        require_offline_runtime()
        backup(args.database, args.backup)
        result = reconcile(args.database, proof, approved_evidence_sha256=args.approve_evidence_sha256)
    else:
        if args.approve_evidence_sha256 or args.backup:
            parser.error('Apply-only options require --apply')
        result = inspect(args.database)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError):
        raise SystemExit('Reservation reconciliation refused; verify evidence, permissions, pause, offline services, runtime guard and backup path.')
