"""Coordinated snapshots; memory rollback never rolls back execution evidence."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from contextlib import contextmanager, closing
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .recovery_identity import canonical, digest
from .runner_protocol import validate_identity, validate_evidence, CONTRACT, VERSION, _uuid

CONTRACT_SNAPSHOT = 'runner_recovery_snapshot_v1'


def _hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@contextmanager
def _locks(paths):
    descriptors = []
    try:
        for path in paths:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            descriptors.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        for fd in descriptors:
            os.close(fd)


def _runner(root, *, key=None):
    from .memory_backup import _quiescent_database, _recovery_tree
    root = Path(root)
    files = _recovery_tree(root)
    if not {'identity.json', 'receipts.sqlite3', 'owner.lock'} <= files.keys():
        raise ValueError('Incomplete runner receipt store')
    identity = json.loads((root/'identity.json').read_text())
    if (set(identity) != {'contract','version','controller_store_id','runner_store_id','key_fingerprint'}
            or identity['contract'] != CONTRACT or identity['version'] != VERSION
            or not _uuid(identity['controller_store_id']) or not _uuid(identity['runner_store_id'])
            or not isinstance(identity['key_fingerprint'],str) or len(identity['key_fingerprint']) != 64
            or any(c not in '0123456789abcdef' for c in identity['key_fingerprint'])):
        raise ValueError('Invalid runner snapshot identity')
    if key is not None and (not isinstance(key,bytes) or len(key)!=32 or hashlib.sha256(key).hexdigest()!=identity['key_fingerprint']):
        raise ValueError('Runner snapshot credential mismatch')
    with closing(_quiescent_database(root/'receipts.sqlite3')) as db:
        db.row_factory = sqlite3.Row
        if db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()[0] != canonical(identity):
            raise ValueError('Runner ledger identity mismatch')
        rows = {}
        for row in db.execute('SELECT * FROM receipts'):
            row = dict(row); operation = validate_identity(json.loads(row['identity_json']))
            if (row['identity_sha'] != digest(operation.to_dict()) or row['identity_json'] != canonical(operation.to_dict())
                    or row['operation_id'] != operation.operation_id or row['admission_id'] != operation.admission_id
                    or row['legacy_attempt_id'] != operation.legacy_attempt_id
                    or operation.runner_store_id != identity['runner_store_id'] or operation.controller_store_id != identity['controller_store_id']):
                raise ValueError('Runner ledger operation mismatch')
            if not isinstance(row.get('reservation'),bytes) or len(row['reservation'])!=(65536 if row['ciphertext'] is None else 0):
                raise ValueError('Runner result storage reservation is invalid')
            if row['ciphertext'] is not None:
                if (not isinstance(row['nonce'],bytes) or len(row['nonce']) != 12 or not isinstance(row['ciphertext'],bytes)
                        or not 16 <= len(row['ciphertext']) <= 40976 or row['state'] not in {'result','uncertain'}):
                    raise ValueError('Invalid encrypted runner result')
                if key is not None:
                    from .runner_receipts import RunnerReceiptStore
                    response = json.loads(AESGCM(key).decrypt(row['nonce'], row['ciphertext'], RunnerReceiptStore._aad(operation,row['response_sha'],bool(row['cleanup_verified']))))
                    validate_evidence(dict(contract=CONTRACT,version=VERSION,identity=operation.to_dict(),identity_sha=row['identity_sha'],state=row['state'],
                        response=response,response_sha=row['response_sha'],cleanup_verified=bool(row['cleanup_verified'])),operation)
            elif row['state'] == 'result' or any(row[name] is not None for name in ('nonce','response_sha','finished_at')) or row['cleanup_verified']:
                raise ValueError('Incomplete runner result')
            rows[row['operation_id']] = row
    return identity, rows, files


def _controller_pins(path, runner_identity, backend_id, receipts=None):
    from .memory_backup import _quiescent_database
    with closing(_quiescent_database(path)) as db:
        db.row_factory=sqlite3.Row
        state = dict(db.execute('SELECT key,value FROM state'))
        if (state.get('local_recovery_controller_store_id') != runner_identity['controller_store_id']
                or state.get('runner_receipt_store_id') != runner_identity['runner_store_id']):
            raise ValueError('Snapshot stores are not paired')
        from types import SimpleNamespace
        from .runner_recovery import _mapping,confirmed_runner_metadata,prepared_cancellation_proven
        shim=SimpleNamespace(connection=db,get_state=lambda key,default='':state.get(key,default))
        for (document,) in db.execute('SELECT identity_json FROM runner_operations'):
            operation = validate_identity(json.loads(document))
            if (operation.controller_store_id != runner_identity['controller_store_id'] or operation.runner_store_id != runner_identity['runner_store_id']
                    or operation.binding['store_id'] != backend_id):
                raise ValueError('Snapshot operation belongs to different stores')
            mapping,effect,legacy=_mapping(shim,operation)
            receipt=receipts.get(operation.operation_id) if receipts is not None else None
            if receipt is not None and (receipt['identity_sha']!=mapping['identity_sha'] or mapping['state'] in {'prepared','cancelled'} and receipt['state']!='prepared'):
                raise ValueError('Controller and receiver admission evidence disagree')
            if mapping['state']=='imported':
                result=db.execute('SELECT * FROM recovery_results WHERE admission_id=?',(operation.admission_id,)).fetchone()
                if result is None:raise ValueError('Imported runner result is missing')
                metadata=confirmed_runner_metadata(shim,operation.admission_id,result['result_sha'],json.loads(result['result_json']),json.loads(result['usage_json']))
                if receipts is not None:
                    receipt=receipts.get(operation.operation_id)
                    if receipt is None or receipt['identity_sha']!=mapping['identity_sha'] or receipt['state']!='result' or receipt['cleanup_verified']!=1 or receipt['response_sha']!=metadata['response_sha']:
                        raise ValueError('Controller and receiver result evidence disagree')
            elif mapping['state']=='cancelled':
                if not prepared_cancellation_proven(shim,operation.admission_id):raise ValueError('Invalid prepared cancellation')
            elif effect['state']!=mapping['state'] or legacy['outcome'] not in ({'started'} if mapping['state']=='prepared' else {'started','uncertain'}):
                raise ValueError('Controller runner admission evidence disagrees')


def _snapshot_locked(controller_path, backend_home, runner_root, backup_dir):
    from .memory_backup import create_paired_recovery_snapshot, _atomic_write
    identity, rows, before = _runner(runner_root)
    metadata = create_paired_recovery_snapshot(controller_path,backend_home,backup_dir,workers_quiescent=True)
    _controller_pins(controller_path,identity,metadata['backend_store_id'],rows)
    shutil.copytree(runner_root,Path(backup_dir)/'runner')
    for path in [Path(backup_dir)/'runner', *(Path(backup_dir)/'runner').rglob('*')]:
        path.chmod(0o700 if path.is_dir() else 0o600)
    if _runner(runner_root)[2] != before or _runner(Path(backup_dir)/'runner')[2] != before:
        raise ValueError('Runner changed during snapshot')
    result = dict(contract=CONTRACT_SNAPSHOT,version=1,controller_store_id=identity['controller_store_id'],
        backend_store_id=metadata['backend_store_id'],runner_store_id=identity['runner_store_id'],key_fingerprint=identity['key_fingerprint'],
        paired_manifest_sha256=_hash(Path(backup_dir)/'paired-recovery.json'),runner_files=before,operations=len(rows))
    _atomic_write(Path(backup_dir)/'runner-recovery.json',canonical(result).encode())
    # Copied data must reach durable storage before the snapshot is published.
    for path in Path(backup_dir).rglob('*'):
        fd = os.open(path,os.O_RDONLY | os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)
    return result


def _separate(*paths):
    resolved = [Path(path).absolute() for path in paths]
    for path in resolved:
        for parent in [path,*path.parents]:
            if parent.is_symlink():raise ValueError('Symlink in recovery path')
    if any(a == b or a in b.parents or b in a.parents for n,a in enumerate(resolved) for b in resolved[n+1:]):
        raise ValueError('Recovery paths overlap')


def create_runner_recovery_snapshot(controller_path,backend_home,runner_root,backup_dir,*,workers_quiescent=False):
    if workers_quiescent is not True:raise ValueError('All workers must be stopped')
    _separate(controller_path,backend_home,runner_root,backup_dir)
    with _locks([Path(runner_root)/'owner.lock']):
        return _snapshot_locked(controller_path,backend_home,runner_root,backup_dir)


def verify_runner_recovery_snapshot(directory):
    from .memory_backup import verify_paired_recovery_snapshot
    directory=Path(directory)
    _separate(directory)
    if (directory/'runner-recovery.json').is_symlink() or (directory/'paired-recovery.json').is_symlink():raise ValueError('Unsafe snapshot manifest')
    metadata=json.loads((directory/'runner-recovery.json').read_text())
    paired=verify_paired_recovery_snapshot(directory)
    identity,rows,files=_runner(directory/'runner')
    backend_identity=json.loads((directory/'backend'/paired['recovery_directory']/'identity.json').read_text())
    if backend_identity.get('store_id')!=paired['backend_store_id']:raise ValueError('Backend snapshot identity mismatch')
    expected=dict(contract=CONTRACT_SNAPSHOT,version=1,controller_store_id=identity['controller_store_id'],backend_store_id=paired['backend_store_id'],
        runner_store_id=identity['runner_store_id'],key_fingerprint=identity['key_fingerprint'],paired_manifest_sha256=_hash(directory/'paired-recovery.json'),runner_files=files,operations=len(rows))
    if metadata != expected:raise ValueError('Runner snapshot digest mismatch')
    _controller_pins(directory/'controller.sqlite3',identity,paired['backend_store_id'],rows)
    return metadata


def _descendant(old_rows,current_rows):
    for operation_id,old in old_rows.items():
        current=current_rows.get(operation_id)
        if current is None:raise ValueError('Current runner history is missing snapshot evidence')
        for name in ('operation_id','admission_id','legacy_attempt_id','identity_json','identity_sha','prepared_at'):
            if old[name] != current[name]:raise ValueError('Divergent runner identity history')
        if old['admitted_at'] is not None and old['admitted_at'] != current['admitted_at']:
            raise ValueError('Runner admission regressed')
        if old['ciphertext'] is not None and old != current:
            raise ValueError('Divergent runner result history')
        if old['state'] in {'admitted','uncertain','result'} and current['state']=='prepared':
            raise ValueError('Runner admission regressed')


def _fault(point):
    pass


def restore_runner_recovery_snapshot(snapshot,controller_path,backend_home,runner_root,*,key,workers_quiescent=False):
    from .memory_backup import _atomic_write, _prepare_recovery_snapshot, _recovery_tree
    if workers_quiescent is not True:raise ValueError('All workers must be stopped')
    _separate(snapshot,controller_path,backend_home,runner_root)
    snapshot,controller_path,backend_home,runner_root=map(Path,(snapshot,controller_path,backend_home,runner_root))
    metadata=verify_runner_recovery_snapshot(snapshot)
    old_identity,old_rows,_=_runner(snapshot/'runner',key=key)
    with _locks([runner_root/'owner.lock']):
        identity,rows,_=_runner(runner_root,key=key)
        if identity != old_identity:raise ValueError('Restore runner identity mismatch')
        _descendant(old_rows,rows)
        retained=controller_path.parent/('runner-before-restore-'+uuid4().hex)
        current=_snapshot_locked(controller_path,backend_home,runner_root,retained)
        if any(current[k]!=metadata[k] for k in ('controller_store_id','runner_store_id','backend_store_id','key_fingerprint')):
            raise ValueError('Restore store identity mismatch')
        with _locks(sorted(backend_home.rglob('*.execution'))):
            paired=json.loads((retained/'paired-recovery.json').read_text())
            if _recovery_tree(backend_home)!=paired['backend_files'] or _hash(controller_path)!=paired['controller_sha256']:
                raise ValueError('Stores changed before restore')
            barrier=canonical(dict(reason='restore_required',contract=CONTRACT_SNAPSHOT,retained=retained.name)).encode()
            # A durable external barrier survives every subsequent partial write.
            _atomic_write(controller_path.parent/'runner-restore-required.json',barrier)
            _fault('external_fence')
            _atomic_write(backend_home/paired['recovery_directory']/'restore-required.json',barrier)
            _atomic_write(runner_root/'restore-required.json',barrier)
            _fault('store_fences')
            serialized=_prepare_recovery_snapshot((snapshot/'controller.sqlite3').read_bytes(),controller_path,key)
            _fault('before_controller_replace')
            owner=controller_path.stat();_atomic_write(controller_path,serialized)
            os.chown(controller_path,owner.st_uid,owner.st_gid)
            _fault('after_controller_replace')
    # Backend and receiver remain at the latest verified history. Restoring an
    # old execution checkpoint is never needed to restore conversational memory.
    return dict(restored=True,reconciliation_required=True,retained=str(retained),execution_histories_preserved=True)


def preserve_controller_runner(restored, current):
    """Inside the restore transaction, retain linked immutable receipt history."""
    db=restored.connection
    tables={r[0] for r in current.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'runner_operations' not in tables:
        if db.execute('SELECT 1 FROM runner_operations LIMIT 1').fetchone():raise ValueError('Runner mapping history missing')
        return
    current.row_factory=sqlite3.Row
    mappings={r['operation_id']:dict(r) for r in current.execute('SELECT * FROM runner_operations')}
    for old in db.execute('SELECT * FROM runner_operations'):
        latest=mappings.get(old['operation_id'])
        if latest is None or any(old[k]!=latest[k] for k in ('operation_id','admission_id','legacy_attempt_id','binding_sha','identity_json','identity_sha','created_at')):
            raise ValueError('Runner mapping histories conflict')
        if (old['state'] in {'cancelled','imported'} and dict(old)!=latest
                or old['admitted_at'] is not None and old['admitted_at']!=latest['admitted_at']):
            raise ValueError('Runner mapping outcome regressed')
    for table,key in [('runner_imports','operation_id'),('runner_research_results','attempt_id')]:
        latest={r[key]:dict(r) for r in current.execute('SELECT * FROM '+table)}
        if any(latest.get(old[key])!=dict(old) for old in db.execute('SELECT * FROM '+table)):
            raise ValueError('Runner receipt histories conflict')
    def upsert(table,rows,keys,*,immutable=False):
        for row in rows:
            row=dict(row);columns=list(row);values=tuple(row.values());marks=','.join('?' for _ in values)
            existing=db.execute('SELECT * FROM '+table+' WHERE '+' AND '.join(k+'=?' for k in keys),tuple(row[k] for k in keys)).fetchone()
            if immutable and existing is not None:
                if dict(existing)!=row:raise ValueError('Runner linked history conflict')
                continue
            update=','.join(k+'=excluded.'+k for k in columns if k not in keys)
            if table=='runner_operations':update='state=excluded.state,admitted_at=excluded.admitted_at,imported_at=excluded.imported_at'
            db.execute('INSERT INTO '+table+'('+','.join(columns)+') VALUES('+marks+') ON CONFLICT('+','.join(keys)+') DO UPDATE SET '+update,values)
    pinned=current.execute("SELECT value FROM state WHERE key='runner_receipt_store_id'").fetchone()
    if mappings or pinned and pinned[0]:
        for table,keys in [('cycles',['cycle_id']),('cycle_recovery_checkpoints',['cycle_id']),('model_turn_receipts',['cycle_id'])]:
            upsert(table,current.execute('SELECT * FROM '+table),keys)
        attempts=list(current.execute('SELECT * FROM cycle_tool_attempts'))
        for old in db.execute('SELECT * FROM cycle_tool_attempts'):
            match=next((r for r in attempts if r['attempt_id']==old['attempt_id']),None)
            if match is None or any(old[k]!=match[k] for k in ('attempt_id','cycle_id','tool_name','started_at')) or old['outcome'] in {'returned','not_executed'} and dict(old)!=dict(match):
                raise ValueError('Runner linked attempt history conflict')
        upsert('cycle_tool_attempts',attempts,['attempt_id'])
        upsert('external_action_receipts',current.execute('SELECT * FROM external_action_receipts'),['action_id'])
        held=[]
        for message in current.execute("SELECT * FROM messages WHERE role='user' AND queued IN (1,2,3)"):
            message=dict(message);message['queued']=3;held.append(message)
        upsert('messages',held,['id'])
        upsert('research_entries',current.execute('SELECT * FROM research_entries'),['record_id'])
        upsert('research_events',current.execute('SELECT * FROM research_events'),['id'],immutable=True)
        upsert('runner_operations',mappings.values(),['operation_id'])
        upsert('runner_imports',current.execute('SELECT * FROM runner_imports'),['operation_id'],immutable=True)
        upsert('runner_research_results',current.execute('SELECT * FROM runner_research_results'),['attempt_id'],immutable=True)
    for key in ('runner_receipt_store_id','local_recovery_controller_store_id','owner_control_revision'):
        row=current.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
        if row:db.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,row[0]))
    if mappings:
        db.execute("INSERT INTO state VALUES('runner_receipts_enabled','0') ON CONFLICT(key) DO UPDATE SET value='0'")
