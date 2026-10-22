"""Prepare/verify the pinned P2-10 rollout; installation requires owner pause."""
import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = Path('/home/daniel/andrew-budget-preflight-20261022')
CANDIDATE = Path('/var/backups/andrew-budget-reconciliation-20261022-candidate')
DATABASE = Path('/var/lib/andrew/control/controller.sqlite3')
ADOPTION = Path('/etc/andrew/runner-receipt-adoption.json')
BACKEND_SHA = '3f5cfd7afcd1f30844a0fb186179331a5fbc6b823d52952b1fbb3487515c33c7'
BACKEND = Path('/opt/andrew-letta-recovery/releases') / BACKEND_SHA
EXPECTED_CONTROLLER = 'db569d8eca2a0da1d44e2a1c465c93de6748f618198716796fe36da703e429ff'
BASELINE = {'mode': 'enabled',
    'controller_code': '6cd63b2e9fac78237f347572f3a9f676b76fcf4da434854e52f986585e31675d',
    'service_code': '3b42b8e16b49bca78844221eeccdab18cd9ce5e05984a27b4e26e82932c73267',
    'backend_release_sha256': BACKEND_SHA, 'backend_release': str(BACKEND)}
EXPECTED_SERVICE = BASELINE['service_code']


def verify_baseline(record):
    if any(record.get(key) != value for key, value in BASELINE.items()):
        raise ValueError('Installed baseline changed; rebuild this rollout against the current release')


def read_state(database):
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        return dict(db.execute('SELECT key,value FROM state'))


def require_pause(database, *, backend_home=None, runner_root=None):
    barriers = [Path(database).parent / 'runner-restore-required.json']
    if runner_root is not None:
        barriers.append(Path(runner_root) / 'restore-required.json')
    if backend_home is not None:
        barriers.extend(Path(backend_home).rglob('restore-required.json'))
    if any(marker.exists() or marker.is_symlink() for marker in barriers):
        raise ValueError('External restore barrier requires reconciliation before deployment')
    state = read_state(database)
    if state.get('paused') != '1':
        raise ValueError('Send /pause to Andrew before running deployment; production unchanged')
    if any(state.get(key) for key in ('active_session_id', 'active_cycle_id', 'autonomous_owner_id',
                                      'model_turn_origin', 'session_start_requested')):
        raise ValueError('Paused work is still settling; wait before running deployment')
    if any(state.get(key) == '1' for key in ('restore_recovery_required', 'local_recovery_restore_required')):
        raise ValueError('Restore reconciliation is required before deployment')
    return {key: state.get(key, '') for key in (
        'owner_control_revision', 'paused', 'sleeping', 'sleep_until', 'session_start_requested',
        'active_session_id', 'active_cycle_id', 'autonomous_owner_id', 'model_turn_origin')}


def billing_snapshot(database):
    """Metadata hashes only; no content or billing values printed."""
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        result = {}
        for table, key in (('budget_reservations', 'reservation_id'), ('usage', 'id'),
                           ('budget_reservation_reconciliations', 'reservation_id')):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                rows = db.execute('SELECT * FROM ' + table + ' ORDER BY ' + key).fetchall()
            else:
                rows = []
            result[table] = hashlib.sha256(json.dumps(rows, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        return result


def require_headroom(database, backend_home, runner_root, *, available=None):
    def size(path):
        path = Path(path)
        if not path.exists():
            return 0
        return path.stat().st_size if path.is_file() else sum(p.stat().st_size for p in path.rglob('*') if p.is_file())
    # The existing installer takes two three-store copies plus one paired copy.
    controller = size(database) + size(str(database) + '-wal')
    required = 3 * (controller + size(backend_home)) + 2 * size(runner_root) + 1024**3
    if available is None:
        fs = os.statvfs(Path(database).parent)
        available = fs.f_bavail * fs.f_frsize
    if available < required:
        raise ValueError('Insufficient disk space for deployment snapshots; production unchanged')
    return required


def verify_source(expected_commit):
    prefix = ['runuser', '-u', 'daniel', '--'] if os.geteuid() == 0 else []
    def git(*args):
        return subprocess.check_output([*prefix, 'git', '-C', str(ROOT), *args], text=True).strip()
    if git('rev-parse', 'HEAD') != expected_commit or git('status', '--porcelain'):
        raise ValueError('Pinned deployment source changed or is dirty; production unchanged')


def load_installer(root=ROOT):
    sys.path.insert(0, str(root / 'prototype'))
    spec = importlib.util.spec_from_file_location('budget_rollout_installer', root / 'scripts/install-runner-recovery.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def deploy(expected_commit):
    if os.geteuid() != 0:
        raise ValueError('Run the deployment wrapper with sudo after pausing Andrew')
    verify_source(expected_commit)
    before = json.loads(ADOPTION.read_text())
    verify_baseline(before)
    def paused_owner():
        return require_pause(DATABASE, backend_home='/var/lib/andrew/letta',
                             runner_root='/var/lib/andrew/runtime/runner-receipts')
    owner = paused_owner()
    billing = billing_snapshot(DATABASE)
    require_headroom(DATABASE, '/var/lib/andrew/letta', '/var/lib/andrew/runtime/runner-receipts')
    installer = load_installer()
    if installer.helper('install-letta-recovery').verify_release(BACKEND) != BACKEND_SHA:
        raise ValueError('Pinned installed backend changed')
    pristine = ARTIFACTS / 'pristine/letta.js'
    pin = json.loads((ROOT / 'letta_recovery/base.json').read_text())
    if installer.sha(pristine) != pin['sha256']:
        raise ValueError('Pinned pristine binary changed')
    evidence = ARTIFACTS / 'core-evidence.json'
    # Validate saved core proof before spending time on acceptance.
    installer.reuse_core_acceptance(ROOT, evidence)
    if CANDIDATE.exists():
        installer._trusted_directory(CANDIDATE)
        if (CANDIDATE / 'installation.json').exists():
            raise ValueError('Prior installation record exists; inspect its phase before retrying')
        candidate = json.loads((CANDIDATE / 'candidate.json').read_text())
        installer.verify_candidate(candidate)
    else:
        candidate = installer.build_candidate(BACKEND, installer.DEFAULT_PATHS['controller'],
                                              installer.DEFAULT_PATHS['runner'], CANDIDATE)
    if candidate['backend_release_sha256'] != BACKEND_SHA:
        raise ValueError('Candidate backend does not match this release')
    if (CANDIDATE / 'acceptance.json').exists():
        installer.verify_candidate(candidate, require_acceptance=True)
        print('Saved privileged acceptance verified; no suites rerun.', flush=True)
    else:
        installer.run_acceptance(candidate, pristine, privileged=True, core_evidence=evidence)
    # Refuse changes made while the isolated acceptance ran. No services changed yet.
    verify_source(expected_commit)
    if json.loads(ADOPTION.read_text()) != before or paused_owner() != owner:
        raise ValueError('Installed release or owner controls changed during acceptance; production unchanged')
    if billing_snapshot(DATABASE) != billing:
        raise ValueError('Billing was still settling; rerun once quiet using saved acceptance')
    require_headroom(DATABASE, '/var/lib/andrew/letta', '/var/lib/andrew/runtime/runner-receipts')
    command = [sys.executable, str(CANDIDATE / 'source/scripts/install-runner-recovery.py')]
    for action in ('--apply', '--enable'):
        subprocess.run([*command, action, '--directory', str(CANDIDATE)],
                       cwd='/opt/andrew/prototype', check=True)
    installed = json.loads((CANDIDATE / 'installation.json').read_text())
    adoption = json.loads(ADOPTION.read_text())
    from andrewbot.runtime_readiness import code_digest
    if (installed['phase'] != 'enabled' or adoption['mode'] != 'enabled'
            or adoption['backend_release_sha256'] != BACKEND_SHA
            or adoption['controller_code'] != EXPECTED_CONTROLLER
            or adoption['service_code'] != EXPECTED_SERVICE
            or paused_owner() != owner or billing_snapshot(DATABASE) != billing):
        raise ValueError('Post-install verification failed; inspect the installation record before waking Andrew')
    for role, key in (('controller', 'controller_code'), ('runner', 'service_code')):
        if code_digest(installer.DEFAULT_PATHS[role]) != adoption[key]:
            raise ValueError('Installed package digest differs from enabled adoption')
    state = read_state(DATABASE)
    if state.get('paused') != '1':
        raise ValueError('Owner pause changed; inspect installation state')
    for unit in installer.UNITS:
        subprocess.run(['systemctl', 'is-active', '--quiet', unit], check=True)
    print('Reviewed runtime package installed and enabled. Pause, billing and reservations preserved. No wake or replay.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-commit', required=True)
    args = parser.parse_args(argv)
    deploy(args.expected_commit)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
