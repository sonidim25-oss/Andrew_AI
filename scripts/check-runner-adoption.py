#!/usr/bin/env python3
"""Root-owned systemd preflight; independent of either installed Python package."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3

ADOPTION=Path('/etc/andrew/runner-receipt-adoption.json')
INSTALLING=Path('/etc/andrew/runner-recovery-installing.json')


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def code_digest(directory):
    directory=Path(directory)
    if not directory.is_dir() or directory.is_symlink():raise ValueError('Package unavailable')
    value=hashlib.sha256()
    for path in sorted(directory.glob('*.py')):
        if path.is_symlink() or not path.is_file():raise ValueError('Unsafe package')
        value.update(path.name.encode()+b'\0'+path.read_bytes()+b'\0')
    return value.hexdigest()


def check(record, *, controller=Path('/opt/andrew/prototype/andrewbot'),service=Path('/opt/andrew/runtime-services/andrewbot'),
          database=Path('/var/lib/andrew/control/controller.sqlite3'),runner=Path('/var/lib/andrew/runtime/runner-receipts'),backend=Path('/var/lib/andrew/letta')):
    if record.get('contract')!='runner_result_recovery_v1' or record.get('version')!=1 or record.get('mode') not in {'disabled','enabled'}:
        raise ValueError('Invalid adoption contract')
    if code_digest(controller)!=record['controller_code'] or code_digest(service)!=record['service_code']:
        raise ValueError('Partial or older code rollout')
    if (Path(database).parent/'runner-restore-required.json').exists() or (Path(runner)/'restore-required.json').exists() or any(Path(backend).rglob('restore-required.json')):
        raise ValueError('Restore reconciliation required')
    identity=json.loads((Path(runner)/'identity.json').read_text())
    if any(identity[k]!=record[k] for k in ('controller_store_id','runner_store_id','key_fingerprint')):raise ValueError('Runner identity changed')
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as db:
        state=dict(db.execute('SELECT key,value FROM state'))
    if state.get('local_recovery_controller_store_id')!=record['controller_store_id'] or state.get('runner_receipt_store_id')!=record['runner_store_id']:
        raise ValueError('Controller identity changed')
    if (state.get('runner_receipts_enabled','0')=='1')!=(record['mode']=='enabled'):raise ValueError('Controller enable state changed')
    release=Path(record['backend_release']);manifest=json.loads((release/'recovery-release.json').read_text())
    if sha(release/'recovery-release.json')!=record['backend_release_sha256']:raise ValueError('Backend release changed')
    for name,expected in manifest['files'].items():
        path=release/name
        if not path.resolve().is_relative_to(release.resolve()):raise ValueError('Unsafe backend path')
        if expected.startswith('link:'):
            if not path.is_symlink() or os.readlink(path)!=expected[5:]:raise ValueError('Backend link changed')
        elif path.is_symlink() or sha(path)!=expected:raise ValueError('Backend file changed')
    for name,expected in record.get('service_overrides',{}).items():
        if sha(name)!=expected:raise ValueError('Service configuration changed')
    return True


if __name__=='__main__':
    try:
        if INSTALLING.exists():raise ValueError('Installation is incomplete')
        for path in [ADOPTION,*ADOPTION.parents]:
            info=path.lstat()
            if path.is_symlink() or info.st_uid!=0 or info.st_mode&0o022:raise ValueError('Unprotected adoption record')
        check(json.loads(ADOPTION.read_text()))
    except Exception as error:raise SystemExit('Runner recovery preflight refused ('+type(error).__name__+')')
