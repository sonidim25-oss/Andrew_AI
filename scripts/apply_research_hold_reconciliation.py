#!/usr/bin/env python3
"""Quiescent, snapshot-backed repair for the exact inspected research rejection.

Requires root and an explicit proof digest. No model call, tool replay, wake,
budget release, or held-message retry is performed. Partial failures leave the
services stopped and fenced; rerunning resumes only the same proof and snapshot.
"""
import argparse
from contextlib import contextmanager, closing
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'prototype'))
from andrewbot.memory_backup import _atomic_write, create_paired_recovery_snapshot, create_runner_recovery_snapshot
from andrewbot.recovery_records import canonical, digest
from andrewbot.runtime_readiness import code_digest
from inspect_research_hold import inspect, rows_digest
import reconcile_research_hold as repair

UNITS = ('andrew-control.service', 'andrew-letta.service', 'andrew-runner.service', 'andrew-runner.socket')
DATABASE = Path('/var/lib/andrew/control/controller.sqlite3')
HOME_DIRECTORY = Path('/var/lib/andrew/letta')
RUNNER_ROOT = Path('/var/lib/andrew/runtime/runner-receipts')
FENCE = Path('/etc/andrew/runner-recovery-installing.json')
EXPECTED_CODE = 'a1b5fb7f47f6d6756318c241bdc4a0edc93698ce57486b36d0a09974c04aff6b'
BACKUPS_ROOT = Path('/var/backups')
REPORT = Path('/home/daniel/andrew-research-hold-result.json')
BOOT_MARKER = Path('/var/lib/andrew/control/research-hold-maintenance.json')
BOOT_GUARD = Path('/etc/systemd/system/andrew-control.service.d/zz-research-maintenance.conf')


def write_json(path, value, mode=0o600):
    _atomic_write(path, (json.dumps(value, sort_keys=True, indent=2) + '\n').encode())
    Path(path).chmod(mode)


def load_helper(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unit(action, name=None):
    subprocess.run(['systemctl', action, *([name] if name else [])], check=True, capture_output=True)


def boot_guard(proof_sha, *, remove=False):
    content = ('[Unit]\nConditionPathExists=!' + str(BOOT_MARKER) + '\n').encode()
    for path in (BOOT_MARKER, BOOT_GUARD):
        if path.is_symlink():
            raise ValueError('Unsafe maintenance boot guard')
    if BOOT_GUARD.exists() and BOOT_GUARD.read_bytes() != content:
        raise ValueError('Another controller boot guard exists')
    if BOOT_MARKER.exists() and json.loads(BOOT_MARKER.read_text()) != {'proof_sha': proof_sha}:
        raise ValueError('Another controller maintenance marker exists')
    if remove:
        for path in (BOOT_MARKER, BOOT_GUARD):
            if path.exists():
                path.unlink()
    else:
        write_json(BOOT_MARKER, {'proof_sha': proof_sha}, 0o644)
        BOOT_GUARD.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(BOOT_GUARD, content)
        BOOT_GUARD.chmod(0o644)
    unit('daemon-reload')


def verify_installed():
    adoption = json.loads(Path('/etc/andrew/runner-receipt-adoption.json').read_text())
    if (adoption.get('mode') != 'enabled' or adoption.get('controller_code') != EXPECTED_CODE
            or code_digest('/opt/andrew/prototype/andrewbot') != EXPECTED_CODE):
        raise ValueError('Expected enabled P0-0b release is not installed')
    load_helper('check-runner-adoption').check(adoption)


def assert_stopped():
    for name in UNITS:
        value = subprocess.check_output(['systemctl', 'show', name, '-p', 'ActiveState', '--value'], text=True).strip()
        if value not in ('inactive', 'failed'):
            raise ValueError('A writer service is still active')


@contextmanager
def execution_locks():
    descriptors = []
    try:
        for path in sorted(HOME_DIRECTORY.rglob('*.execution')):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            descriptors.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        for fd in descriptors:
            os.close(fd)


def stage_source(directory):
    source = directory / 'source'
    files = ['scripts/reconcile_research_hold.py', 'scripts/inspect_research_hold.py',
             'scripts/inspect_owner_handoff.py', 'scripts/reconcile-research-checkpoint.mjs',
             'letta_recovery/contracts.mjs', 'letta_recovery/payload-store.mjs', 'letta_recovery/state-machine.mjs']
    if source.exists():
        for name in files:
            if (source / name).is_symlink() or (source / name).read_bytes() != (ROOT / name).read_bytes():
                raise ValueError('Maintenance source differs from the pinned run')
        return source
    source.mkdir(mode=0o755)
    for name in files:
        target = source / name
        target.parent.mkdir(mode=0o755, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
        target.chmod(0o444)
    return source


def run_controller(source, phase, payload, proof_sha):
    if phase == 'record':
        account = pwd.getpwnam('andrew-control')
        command = ['/usr/bin/python3', str(source / 'scripts/reconcile_research_hold.py'), '--phase', phase]
        identity = dict(user=account.pw_uid, group=account.pw_gid,
                        extra_groups=os.getgrouplist(account.pw_name, account.pw_gid))
    else:
        command = ['systemd-run', '--wait', '--pipe', '--collect',
                   '--unit=andrew-research-maintenance-' + proof_sha[:12],
                   '--uid=andrew-control', '--property=WorkingDirectory=/opt/andrew/prototype',
                   '--property=LoadCredentialEncrypted=andrew-gateway-token:/etc/credstore.encrypted/andrew-gateway-token',
                   '--setenv=PYTHONPATH=/opt/andrew/prototype',
                   '/usr/bin/python3', str(source / 'scripts/reconcile_research_hold.py'), '--phase', phase]
        identity = {}
    try:
        result = subprocess.run(command, input=canonical(payload), text=True, capture_output=True,
                                timeout=120, cwd='/opt/andrew/prototype',
                                env={**os.environ, 'PYTHONPATH': '/opt/andrew/prototype'}, **identity)
        if result.returncode:
            raise ValueError('Controller maintenance phase failed: ' + phase)
    except BaseException:
        if phase == 'finalize':
            subprocess.run(['systemctl', 'stop', 'andrew-research-maintenance-' + proof_sha[:12] + '.service'],
                           capture_output=True, timeout=30)
        raise
    return json.loads(result.stdout)


def verify_retired(proof, original, snapshot):
    expected = repair.terminal_for(proof, original)
    from inspect_owner_handoff import read
    cp = json.loads(read(proof['checkpoint_path']))
    if (cp.get('binding') != proof['binding'] or cp['phase'] != 'terminal_committed' or cp.get('claim')
            or {k: cp['terminal'].get(k) for k in expected} != expected
            or cp['terminal'].get('acknowledged') is not True or cp['terminal'].get('settled') is not True):
        raise ValueError('Backend retirement is not fully acknowledged')
    target = next(t for t in cp['tools'] if t.get('admission_id') == proof['tool_admission_id'])
    old_target = next(t for t in original['tools'] if t.get('admission_id') == proof['tool_admission_id'])
    if target != {**old_target, 'state': 'confirmed', 'result_ref': proof['validation_result_sha']}:
        raise ValueError('Retired result differs')
    if cp['steps'] != original['steps']:
        raise ValueError('Prior provider history changed')
    before = [t for t in original['tools'] if t.get('admission_id') != proof['tool_admission_id']]
    after = [t for t in cp['tools'] if t.get('admission_id') != proof['tool_admission_id']]
    if before != after:
        raise ValueError('Prior tool history changed')
    with closing(sqlite3.connect(DATABASE.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        repair.protected_unchanged(db, proof, recorded=True)
        repair.verify_receipt(db, proof)
        if db.execute("SELECT 1 FROM recovery_attempts WHERE binding_sha=? AND state IN ('active','held','evidence','yielding')",
                      (digest(proof['binding']),)).fetchone():
            raise ValueError('Original binding still has pending recovery')
        usage = rows_digest(db, 'SELECT * FROM usage ORDER BY id')
    with closing(sqlite3.connect((Path(snapshot) / 'controller.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if usage != rows_digest(db, 'SELECT * FROM usage ORDER BY id'):
            raise ValueError('Provider usage changed during maintenance')


def apply(proof_path, expected_sha, directory):
    if os.geteuid() != 0:
        raise ValueError('Administrator execution is required')
    proof = repair.evidence(json.loads(Path(proof_path).read_text()))
    if digest(proof) != expected_sha or proof['tool_admission_id'] != __import__('inspect_research_hold').TARGET:
        raise ValueError('Explicit historical proof digest differs')
    directory = Path(directory)
    if directory.parent != BACKUPS_ROOT or directory.is_symlink():
        raise ValueError('Unsafe maintenance directory')
    verify_installed()
    installer = load_helper('install-runner-recovery')
    first = not directory.exists()
    if first:
        # Any drift refuses before services or fences are touched.
        if FENCE.exists() or inspect(DATABASE, HOME_DIRECTORY) != proof:
            raise ValueError('Fresh inspection differs; production unchanged')
        directory.mkdir(mode=0o755)
        (directory / 'private').mkdir(mode=0o700)
        write_json(directory / 'private/journal.json', {'proof_sha': expected_sha, 'phase': 'prepared'})
    installer._trusted_directory(directory)
    lock = os.open(directory / 'private/maintenance.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal_path = directory / 'private/journal.json'
        journal = json.loads(journal_path.read_text())
        if journal.get('proof_sha') != expected_sha:
            raise ValueError('Cannot resume another proof')
        source = stage_source(directory)
        if FENCE.exists() and json.loads(FENCE.read_text()) != {'reason': 'historical_research_reconciliation', 'proof_sha': expected_sha}:
            raise ValueError('Another maintenance fence exists')
        snapshot = directory / 'private/paired-before'
        if journal['phase'] == 'complete':
            original = repair.original_checkpoint(snapshot, HOME_DIRECTORY, proof)
            verify_retired(proof, original, snapshot)
            if FENCE.exists():
                FENCE.unlink()
                boot_guard(expected_sha, remove=True)
                for name in ('andrew-letta.service', 'andrew-runner.socket', 'andrew-runner.service', 'andrew-control.service'):
                    unit('start', name)
            return {'retired': True, 'already_complete': True, 'paused': True}
        write_json(FENCE, {'reason': 'historical_research_reconciliation', 'proof_sha': expected_sha}, 0o644)
        try:
            # The persistent condition also blocks automatic controller startup
            # if power fails while the backend is briefly running for projection.
            boot_guard(expected_sha)
            for name in UNITS:
                unit('stop', name)
            assert_stopped()
            runtime = pwd.getpwnam('andrew-runtime')
            installer._no_workers(installer.DEFAULT_PATHS, runtime.pw_uid)
            if journal['phase'] == 'prepared':
                if inspect(DATABASE, HOME_DIRECTORY) != proof:
                    raise ValueError('Quiescent inspection changed')
                load_helper('install-letta-recovery').checkpoint_controller_wal(DATABASE)
                create_paired_recovery_snapshot(DATABASE, HOME_DIRECTORY, snapshot, workers_quiescent=True)
                create_runner_recovery_snapshot(DATABASE, HOME_DIRECTORY, RUNNER_ROOT,
                    directory / 'private/three-store-before', workers_quiescent=True)
                journal['phase'] = 'snapshotted'
                write_json(journal_path, journal)
            original = repair.original_checkpoint(snapshot, HOME_DIRECTORY, proof)
            payload = {'database': str(DATABASE), 'home': str(HOME_DIRECTORY), 'proof': proof, 'original': original}
            with execution_locks():
                run_controller(source, 'record', payload, expected_sha)
                if journal['phase'] not in ('finalizing', 'finalized'):
                    letta = pwd.getpwnam('andrew-letta')
                    repair.migrate_checkpoint(DATABASE, HOME_DIRECTORY, proof, snapshot, write=True,
                        node_script=source / 'scripts/reconcile-research-checkpoint.mjs',
                        node_identity=dict(user=letta.pw_uid, group=letta.pw_gid,
                            extra_groups=os.getgrouplist(letta.pw_name, letta.pw_gid)))
                journal['phase'] = 'finalizing'
                write_json(journal_path, journal)
            # The main controller stays stopped. Only a temporary evidence-only
            # gateway serves the acknowledgement and transcript projection.
            FENCE.unlink()
            unit('start', 'andrew-letta.service')
            result = run_controller(source, 'finalize', payload, expected_sha)
            verify_retired(proof, original, snapshot)
            journal['phase'] = 'finalized'
            write_json(journal_path, journal)
            boot_guard(expected_sha, remove=True)
            for name in ('andrew-runner.socket', 'andrew-runner.service', 'andrew-control.service'):
                unit('start', name)
            verify_retired(proof, original, snapshot)
            result.update({'research_entries_preserved': proof['research']['count'],
                           'held_messages_preserved': proof['held_messages']['count'],
                           'reservations_preserved': proof['reservations']['count']})
            write_json(REPORT, result, 0o644)
            journal['phase'] = 'complete'
            write_json(journal_path, journal)
            return result
        except BaseException:
            write_json(FENCE, {'reason': 'historical_research_reconciliation', 'proof_sha': expected_sha}, 0o644)
            boot_guard(expected_sha)
            for name in UNITS:
                try:
                    unit('stop', name)
                except Exception:
                    pass
            raise
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proof', required=True)
    parser.add_argument('--expect-sha', required=True)
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(apply(args.proof, args.expect_sha, args.directory), sort_keys=True, indent=2))
    except Exception as error:
        print('Research reconciliation stopped (' + type(error).__name__ + '). Inspect the maintenance journal; no automatic replay.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
