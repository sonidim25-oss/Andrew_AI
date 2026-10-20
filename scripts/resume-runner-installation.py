#!/usr/bin/env python3
"""Finish only the fenced, empty-receiver, first-adoption installation incident."""
import argparse
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing


def _read_receiver(root, key):
    from andrewbot.runner_backup import _runner
    return _runner(root, key=key)


def _overrides(module, candidate, paths, release):
    result = {}
    for unit in module.UNITS[:-1]:
        text = '[Service]\nExecStartPre=+/usr/bin/python3 ' + paths['guard'] + '\n'
        if unit == 'andrew-runner.service':
            text += 'Environment=ANDREW_RUNNER_RECEIPTS_ENABLED=0\nLoadCredentialEncrypted=andrew-runner-receipts-key:' + paths['credential_dir'] + '/andrew-runner-receipts-key\n'
        if unit == 'andrew-control.service':
            text += 'Environment=ANDREW_RUNNER_RECEIPTS_ENABLED=0\nEnvironment=ANDREW_LOCAL_RECOVERY_ENABLED=1\n'
        if unit == 'andrew-letta.service':
            text += 'ExecStart=\nExecStart=/usr/bin/node ' + str(release) + '/letta.js server --backend local --listen ws://127.0.0.1:8283 --openai-api\nEnvironment=ANDREW_LOCAL_RECOVERY_ARTIFACT=1\n'
        path = Path(paths['overrides']) / (unit + '.d/zz-runner-recovery.conf')
        if path.read_text() != text:raise ValueError('Installed recovery override changed')
        result[str(path)] = module.sha(path)
    return result


def _migrate(paths, controller_uid):
    account = pwd.getpwuid(controller_uid)
    package = Path(paths['controller']).resolve()
    program = '''import sys
from pathlib import Path
from andrewbot import store as module
if Path(module.__file__).resolve()!=Path(sys.argv[2])/'store.py':raise ValueError('Wrong controller migration module')
store=module.Store(sys.argv[1])
try:
 for name in ('runner_operations','runner_imports','runner_research_results'):store.connection.execute('SELECT 1 FROM '+name+' LIMIT 1')
 if store.connection.execute('PRAGMA foreign_key_check').fetchone() is not None:raise ValueError('Invalid controller links')
finally:store.close()
'''
    identity = dict(user=controller_uid, group=account.pw_gid)
    if os.geteuid() == 0:identity['extra_groups'] = os.getgrouplist(account.pw_name, account.pw_gid)
    subprocess.run([sys.executable, '-c', program, paths['database'], str(package)],
        cwd=package.parent, env={**os.environ, 'PYTHONPATH': str(package.parent)},
        check=True, capture_output=True, timeout=60, **identity)


def resume(module, candidate, *, key=None, uid=None, service=None, no_workers=None, renew_runtime=None):
    from andrewbot.memory_backup import verify_paired_recovery_snapshot, _quiescent_database
    from andrewbot.runner_backup import _locks, _snapshot_locked, _separate
    service = service or module._service
    renew_runtime = renew_runtime or module._renew_runtime
    module.verify_candidate(candidate, require_acceptance=True)
    directory = Path(candidate['directory'])
    installed = json.loads((directory / 'installation.json').read_text())
    paths = {**module.DEFAULT_PATHS, **installed.get('paths', {})}
    if installed['phase'] != 'interrupted':raise ValueError('Unexpected installation phase')
    if not Path(paths['installing']).is_file():raise ValueError('Installation fence is missing')
    if json.loads(Path(paths['installing']).read_text()).get('contract') != module.CONTRACT:raise ValueError('Unexpected installation fence')
    if Path(paths['adoption']).exists():raise ValueError('Repair requires absent adoption')
    if json.loads((directory / 'adoption-before.json').read_text())['content'] is not None:raise ValueError('Repair is limited to first adoption')
    if (directory / 'three-store-before').exists():raise ValueError('Earlier receiver history requires manual reconciliation')
    if (directory / 'three-store-before-adoption/runner-recovery.json').exists():raise ValueError('Snapshot was already completed')
    owner = installed['owner']
    if owner.get('paused') != '1' or module._owner(paths['database']) != owner:raise ValueError('Paused owner controls changed')
    for unit in module.UNITS:service('assert-stopped', unit)
    for role in ('controller', 'runner'):
        if module.tree(paths[role]) != candidate[role + '_files']:raise ValueError('Accepted runtime package changed')
        if module.tree(directory / 'code-before' / role) != candidate['originals'][role]:raise ValueError('Original code backup changed')
    if module.sha(paths['guard']) != module.sha(Path(candidate['source']) / 'scripts/check-runner-adoption.py'):raise ValueError('Startup guard changed')
    release = Path(paths['artifacts']) / candidate['backend_release_sha256']
    if module.helper('install-letta-recovery').verify_release(release) != candidate['backend_release_sha256']:raise ValueError('Installed backend changed')
    overrides = _overrides(module, candidate, paths, release)
    original = verify_paired_recovery_snapshot(directory / 'paired-before')
    if module._owner(directory / 'paired-before/controller.sqlite3') != owner:raise ValueError('Original owner backup changed')
    if module.tree(paths['backend_home']) != original['backend_files']:raise ValueError('Backend history changed during stopped installation')
    runtime_before = json.loads((directory / 'runtime-proof-before.json').read_text())
    if any(module.sha(name) != value for name, value in runtime_before['policy'].items()):raise ValueError('Original runtime policy changed')
    if uid is None:
        control = pwd.getpwnam('andrew-control');runner = pwd.getpwnam('andrew-runtime')
        uid = (control.pw_uid, runner.pw_uid, runner.pw_gid)
    if no_workers is None:
        # The immutable accepted helper predates the explicit working-directory fix.
        account = pwd.getpwuid(uid[1])
        result = subprocess.run(['/usr/bin/podman', 'ps', '-aq', '--filter', 'label=org.andrew.runtime=managed'],
            cwd=Path(paths['runner']).parent,
            env={**os.environ, 'HOME':account.pw_dir, 'XDG_RUNTIME_DIR':'/run/user/'+str(uid[1]),
                 'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(uid[1])+'/bus'},
            user=uid[1], group=uid[2], extra_groups=os.getgrouplist(account.pw_name, uid[2]),
            stdin=subprocess.DEVNULL, check=True, capture_output=True, timeout=20)
        if result.stdout.strip():raise ValueError('Disposable worker still exists')
    else:no_workers(paths, uid[1])
    if key is None:
        credentials = module.helper('provision-runner-receipts-key')
        key = credentials.decrypt(Path(paths['credential_dir']) / credentials.NAME)
    root = Path(paths['runner_root'])
    with _locks([root / 'owner.lock']):
        identity, rows, receiver_files = _read_receiver(root, key)
        if rows:raise ValueError('Repair requires an empty receiver')
        with closing(_quiescent_database(paths['database'])) as db:
            state = dict(db.execute('SELECT key,value FROM state'))
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'runner_operations' in tables:raise ValueError('Missing-schema incident is no longer present')
        if (state.get('local_recovery_controller_store_id') != identity['controller_store_id']
                or state.get('runner_receipt_store_id') != identity['runner_store_id']
                or state.get('runner_receipts_enabled') != '0'):
            raise ValueError('Disabled receiver UUID pins changed')
        if module._owner(paths['database']) != owner:raise ValueError('Owner changed before repair')
        evidence = directory / ('repair-' + uuid.uuid4().hex)
        evidence.mkdir(mode=0o700)
        try:
            module.create_paired_recovery_snapshot(paths['database'], paths['backend_home'], evidence / 'before', workers_quiescent=True)
            shutil.copytree(root, evidence / 'before/runner')
            if module.tree(evidence / 'before/runner') != receiver_files:raise ValueError('Receiver evidence changed during copy')
            module._write(evidence / 'installation-before.json', installed)
            module._write(evidence / 'receiver-before.json', dict(identity=identity, files=receiver_files))
            for path in [*sorted((evidence / 'before').rglob('*')), evidence / 'before', evidence, directory]:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:os.fsync(fd)
                finally:os.close(fd)
            _migrate(paths, uid[0]);module._checkpoint(paths['database'])
            if module._owner(paths['database']) != owner:raise ValueError('Owner changed during schema repair')
            _separate(paths['database'], paths['backend_home'], root, evidence / 'after')
            snapshot = _snapshot_locked(paths['database'], paths['backend_home'], root, evidence / 'after')
            record = dict(contract=module.CONTRACT, version=1, mode='disabled',
                backend_store_id=snapshot['backend_store_id'], controller_code=module.code_digest(paths['controller']),
                service_code=module.code_digest(paths['runner']), controller_uid=uid[0], runner_uid=uid[1],
                backend_release=str(release), backend_release_sha256=candidate['backend_release_sha256'],
                service_overrides=overrides, **{k:identity[k] for k in ('controller_store_id','runner_store_id','key_fingerprint')})
            module._write(paths['adoption'], record, 0o644)
            renew_runtime(paths, runtime_before)
            if module._owner(paths['database']) != owner:raise ValueError('Owner changed during repair')
            module.verify_candidate(candidate, require_acceptance=True)
            for role in ('controller','runner'):
                if module.tree(paths[role]) != candidate[role+'_files']:raise ValueError('Runtime code changed during repair')
            for unit in module.UNITS:service('assert-stopped', unit)
            module._record(directory, 'installed_disabled', owner=owner, paths=paths,
                adoption_sha256=module.sha(paths['adoption']), repair_evidence=str(evidence))
            module._remove_fence(paths['installing'])
            service('daemon-reload', None)
        except BaseException:
            module._write(paths['installing'], dict(contract=module.CONTRACT, reason='schema_repair_interrupted'), 0o644)
            for unit in module.UNITS:
                try:service('stop', unit)
                except Exception:pass
            module._record(directory, 'repair_interrupted', owner=owner, paths=paths, repair_evidence=str(evidence))
            raise
    # Release the receiver owner lock before asking its service to open the store.
    try:
        for unit in ('andrew-letta.service','andrew-runner.socket','andrew-runner.service'):service('start', unit)
    except BaseException:
        module._write(paths['installing'], dict(contract=module.CONTRACT, reason='schema_repair_start_failed'), 0o644)
        for unit in module.UNITS:
            try:service('stop', unit)
            except Exception:pass
        module._record(directory, 'repair_interrupted', owner=owner, paths=paths, repair_evidence=str(evidence))
        raise
    return dict(installed=True, automatic_resume=False, execution_enabled=False, repair_evidence=str(evidence))


def _trusted(path):
    path = Path(path).absolute()
    for part in [path, *path.parents]:
        info = part.lstat()
        if part.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('Repair evidence must be administrator-owned')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    args=parser.parse_args()
    if os.geteuid()!=0:parser.error('Repair requires root')
    directory=Path(args.directory).absolute()
    for name in ('candidate.json','installation.json','runtime-proof-before.json','adoption-before.json','source/scripts/install-runner-recovery.py'):
        _trusted(directory/name)
    candidate=json.loads((directory/'candidate.json').read_text())
    if candidate.get('directory') != str(directory) or candidate.get('source') != str(directory/'source'):
        raise ValueError('Unexpected accepted candidate paths')
    installer=directory/'source/scripts/install-runner-recovery.py'
    if hashlib.sha256(installer.read_bytes()).hexdigest() != candidate['source_files']['scripts/install-runner-recovery.py']:
        raise ValueError('Accepted installer bytes changed')
    # This helper imports the immutable accepted installer, preserving its manifest
    # and test bindings rather than weakening them to accept a changed worktree.
    sys.dont_write_bytecode=True
    spec=importlib.util.spec_from_file_location('accepted_runner_installer',directory/'source/scripts/install-runner-recovery.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module._trusted_directory(directory)
    candidate=json.loads((directory/'candidate.json').read_text())
    print(json.dumps(resume(module,candidate)))


if __name__=='__main__':
    try:main()
    except Exception as error:raise SystemExit('Runner installation repair stopped: '+str(error))
