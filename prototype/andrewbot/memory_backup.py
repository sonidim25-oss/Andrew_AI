import os
from contextlib import closing
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_bytes

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_MAGIC = b"ANDREW-MEMORY-DB1\0"
_NONCE_LENGTH = 12
_REQUIRED_TABLES = {"memory_records", "personality_revisions", "diary_entries"}


def _validate_key(key):
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("memory backup key must be 32 bytes")


def _decrypt_snapshot(snapshot_path, key):
    _validate_key(key)
    path = Path(snapshot_path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("memory snapshot is unavailable")
    blob = path.read_bytes()
    minimum = len(_MAGIC) + _NONCE_LENGTH + 16
    if len(blob) < minimum or not blob.startswith(_MAGIC):
        raise ValueError("memory snapshot format is invalid")
    nonce_start = len(_MAGIC)
    nonce = blob[nonce_start:nonce_start + _NONCE_LENGTH]
    try:
        return AESGCM(key).decrypt(nonce, blob[nonce_start + _NONCE_LENGTH:], _MAGIC)
    except InvalidTag:
        raise ValueError("memory snapshot authentication failed") from None


def _validate_database(serialized):
    with tempfile.TemporaryDirectory(prefix="andrew-memory-restore-") as temporary_directory:
        database_path = Path(temporary_directory) / "snapshot.sqlite3"
        descriptor = os.open(database_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(serialized)
            target.flush()
            os.fsync(target.fileno())
        connection = sqlite3.connect(database_path)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise ValueError("memory snapshot integrity check failed")
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                ).fetchall()
            }
            if not _REQUIRED_TABLES.issubset(tables):
                raise ValueError("memory snapshot is missing required records")
        except sqlite3.DatabaseError:
            raise ValueError("memory snapshot database is invalid") from None
        finally:
            connection.close()


def _atomic_write(path, data):
    descriptor, temporary_name = tempfile.mkstemp(prefix=".andrew-memory-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _valid_snapshot_paths(directory, key):
    valid = []
    for path in sorted(directory.glob("memory-*.andrewdb"), key=lambda item: item.name, reverse=True):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            _decrypt_snapshot(path, key)
        except (OSError, ValueError):
            continue
        valid.append(path)
    return valid


def create_encrypted_snapshot(store, backup_dir, key, retention=7):
    _validate_key(key)
    if isinstance(retention, bool) or not isinstance(retention, int) or retention < 1:
        raise ValueError("snapshot retention must be a positive integer")
    directory = Path(backup_dir)
    if directory.is_symlink():
        raise ValueError("backup directory cannot be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not directory.is_dir():
        raise ValueError("backup path is not a directory")
    directory.chmod(0o700)

    snapshot_db = sqlite3.connect(":memory:")
    try:
        with store._lock:
            store.connection.backup(snapshot_db)
            serialized = snapshot_db.serialize()
    finally:
        snapshot_db.close()

    nonce = token_bytes(_NONCE_LENGTH)
    ciphertext = AESGCM(key).encrypt(nonce, serialized, _MAGIC)
    envelope = _MAGIC + nonce + ciphertext
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = directory / f"memory-{timestamp}.andrewdb"
    _atomic_write(destination, envelope)

    valid = _valid_snapshot_paths(directory, key)
    for stale in valid[retention:]:
        stale.unlink()
    return destination



def _preserve_newer_ledgers(restored, destination):
    """Use only a verified descendant ledger; never guess across conflicting histories."""
    with sqlite3.connect(destination.resolve().as_uri() + '?mode=ro', uri=True) as current:
        if current.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            return False
        for key in ('letta_agent_id', 'letta_conversation_id','local_recovery_controller_store_id'):
            row = current.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
            old = restored.get_state(key)
            if row and old and row[0] != old:
                raise ValueError('Restore ledger belongs to another conversation; manual reconciliation required')
        latest = {row[0]: tuple(row) for row in current.execute('SELECT * FROM usage')}
        if any(latest.get(row['id']) != tuple(row) for row in restored.connection.execute('SELECT * FROM usage')):
            raise ValueError('Restore ledger histories conflict; manual reconciliation required')
        tables = ('usage', 'budget_reservations', 'media_jobs', 'media_avatar_proposals', 'updates','recovery_attempts','recovery_effects','recovery_results')
        available = {row[0] for row in current.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Confirmed outcomes are immutable. A divergent receipt history cannot
        # become authoritative merely because its usage rows share a prefix.
        if 'recovery_results' in available:
            columns=[row[1] for row in current.execute('PRAGMA table_info(recovery_results)')]
            for old_result in restored.connection.execute('SELECT * FROM recovery_results'):
                latest_result=current.execute('SELECT '+','.join(columns)+' FROM recovery_results WHERE admission_id=?',(old_result['admission_id'],)).fetchone()
                if latest_result is None or any(old_result[column]!=latest_result[index] for index,column in enumerate(columns)):
                    raise ValueError('Restore recovery receipts conflict; manual reconciliation required')
        elif restored.connection.execute('SELECT 1 FROM recovery_results LIMIT 1').fetchone():
            raise ValueError('Restore recovery receipts missing; manual reconciliation required')
        with restored.connection:
            for name in reversed(tables):
                if name in available:
                    schema = current.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
                    restored.connection.execute(schema.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1))
                    restored.connection.execute('DELETE FROM ' + name)
            for name in tables:
                if name in available:
                    rows = current.execute('SELECT * FROM ' + name).fetchall()
                    if rows:
                        marks = ','.join('?' for _ in rows[0])
                        columns=','.join(row[1] for row in current.execute('PRAGMA table_info('+name+')'))
                        restored.connection.executemany('INSERT INTO ' + name + '('+columns+') VALUES(' + marks + ')', rows)
            # Do not fetch already-processed Telegram inputs again after
            # releasing recovery. Any newer pending inputs are quarantined too.
            for name in ('telegram_inbox', 'telegram_uncertain'):
                if name in available:
                    rows = current.execute('SELECT * FROM ' + name).fetchall()
                    restored.connection.executemany('INSERT OR IGNORE INTO telegram_uncertain VALUES(?,?,?)', rows)
                    restored.connection.executemany('INSERT OR IGNORE INTO updates VALUES(?)', [(r[0],) for r in rows])
            recovery_identity=current.execute("SELECT value FROM state WHERE key='local_recovery_controller_store_id'").fetchone()
            if recovery_identity:restored.connection.execute("INSERT INTO state VALUES('local_recovery_controller_store_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(recovery_identity[0],))
            revision = current.execute("SELECT value FROM state WHERE key='provider_retry_revision'").fetchone()
            if revision:
                restored.connection.execute("INSERT INTO state VALUES('provider_retry_revision',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (revision[0],))
    return True


def _prepare_recovery_snapshot(serialized, destination, key):
    from .store import Store
    # Keep encrypted pre-restore evidence outside rotating daily snapshots.
    if destination.exists():
        before = destination.read_bytes()
        nonce = token_bytes(_NONCE_LENGTH)
        evidence = destination.parent / ('restore-evidence-' +
            datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '.andrewdb')
        _atomic_write(evidence, _MAGIC + nonce + AESGCM(key).encrypt(nonce, before, _MAGIC))
    with tempfile.TemporaryDirectory(prefix='andrew-recovery-') as directory:
        path = Path(directory) / 'controller.sqlite3'
        _atomic_write(path, serialized)
        restored = Store(path)
        try:
            ledger_available = False
            if destination.exists():
                try:
                    ledger_available = _preserve_newer_ledgers(restored, destination)
                except sqlite3.DatabaseError:
                    # Corrupt live data still has encrypted evidence; billing
                    # remains gated until an administrator reconciles it.
                    pass
            with restored.connection:
                db = restored.connection
                db.execute("UPDATE messages SET queued=3 WHERE role='user' AND queued IN (1,2)")
                db.execute('INSERT OR IGNORE INTO telegram_uncertain SELECT * FROM telegram_inbox')
                db.execute('INSERT OR IGNORE INTO updates SELECT update_id FROM telegram_inbox')
                db.execute('DELETE FROM telegram_claims')
                db.execute('DELETE FROM telegram_inbox')
                db.execute("UPDATE telegram_outbox SET delivery_state='held',claim_token=NULL")
                db.execute("UPDATE diary_entries SET delivery_state='uncertain' WHERE delivery_state='pending'")
                db.execute('DELETE FROM diary_delivery_claims')
                db.execute("UPDATE cycles SET status='interrupted' WHERE status='running'")
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'media_jobs' in tables:
                    db.execute("UPDATE media_jobs SET state='uncertain',error='restore_reconciliation' WHERE state IN ('queued','running')")
                    db.execute("UPDATE media_jobs SET delivery='uncertain',error='restore_reconciliation' WHERE delivery IN ('pending','sending')")
                if 'media_avatar_proposals' in tables:
                    db.execute("UPDATE media_avatar_proposals SET state='uncertain' WHERE state IN ('proposed','approved','applying')")
                if restored.get_state('local_recovery_controller_store_id',''):
                    db.execute("INSERT INTO state VALUES('local_recovery_restore_required','1') ON CONFLICT(key) DO UPDATE SET value='1'")
                    db.execute("UPDATE recovery_attempts SET state='held' WHERE state IN ('active','evidence')")
                updates = {'restore_recovery_required': '1', 'restore_ledger_preserved': str(int(ledger_available)),
                           'paused': '1', 'sleeping': '0', 'sleep_until': '', 'session_start_requested': '',
                           'active_session_id': '', 'active_session_started_at': '',
                           'active_session_status': 'error', 'autonomy_error': 'restore_reconciliation'}
                db.executemany('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', updates.items())
                db.execute("INSERT INTO activity(event,created_at) VALUES('Snapshot restored; operational work quarantined pending billing and action reconciliation',?)", (restored.now(),))
            return restored.connection.serialize()
        finally:
            restored.close()


def reconcile_restored_database(store, confirmation):
    if confirmation != 'BILLING_AND_ACTIONS_RECONCILED':
        raise ValueError('Explicit billing and action reconciliation confirmation required')
    if store.get_state('restore_recovery_required', '0') != '1':
        raise ValueError('No restored database awaits reconciliation')
    # Old queues stay quarantined. New work still requires a separate owner wake.
    store.transition_state({'restore_recovery_required': '0', 'paused': '1',
                            'autonomy_error': '', 'active_session_status': 'paused'},
                           'Administrator confirmed restore billing and action reconciliation')


def restore_snapshot(snapshot_path, destination_path, key):
    source = Path(snapshot_path)
    destination = Path(destination_path)
    if source.resolve() == destination.resolve():
        raise ValueError("snapshot restore destination must be a different path")
    if destination.is_symlink():
        raise ValueError("restore destination cannot be a symlink")
    if Path(f"{destination}-wal").exists() or Path(f"{destination}-shm").exists():
        raise ValueError("restore destination has active SQLite sidecars")

    serialized = _decrypt_snapshot(source, key)
    _validate_database(serialized)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.parent.is_symlink():
        raise ValueError("restore parent cannot be a symlink")
    serialized = _prepare_recovery_snapshot(serialized, destination, key)
    _atomic_write(destination, serialized)


class PeriodicMemoryBackup:
    def __init__(self, store, backup_dir, key, interval_seconds=24 * 60 * 60,
                 retry_seconds=5 * 60, clock=time.monotonic):
        if interval_seconds <= 0 or retry_seconds <= 0:
            raise ValueError("backup intervals must be positive")
        _validate_key(key)
        self.store = store
        self.backup_dir = backup_dir
        self.key = key
        self.interval_seconds = interval_seconds
        self.retry_seconds = retry_seconds
        self.clock = clock
        self.next_at = clock()

    def maybe_snapshot(self):
        now = self.clock()
        if now < self.next_at:
            return False
        try:
            create_encrypted_snapshot(self.store, self.backup_dir, self.key)
        except Exception:
            self.next_at = now + self.retry_seconds
            raise
        self.next_at = now + self.interval_seconds
        return True


# Administrator-only paired snapshots. The controller's periodic backup never
# calls these functions and never reads the private backend directory.
def _recovery_tree(root):
    import hashlib
    import stat
    root=Path(root)
    if root.is_symlink() or not root.is_dir():raise ValueError('Recovery tree unavailable')
    result={}
    for path in sorted(root.rglob('*')):
        info=path.lstat()
        if stat.S_ISDIR(info.st_mode):continue
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():raise ValueError('Unsafe recovery snapshot file')
        with path.open('rb') as stream:result[path.relative_to(root).as_posix()]=hashlib.file_digest(stream,'sha256').hexdigest()
    return result


def _quiescent_database(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('Controller database unavailable')
    wal=Path(str(path)+'-wal')
    if wal.exists() and wal.stat().st_size:raise ValueError('Controller WAL is not quiescent')
    # immutable is used only after workers stop and a nonempty WAL is excluded.
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
    if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':db.close();raise ValueError('Controller database corrupt')
    return db


def create_paired_recovery_snapshot(controller_path,backend_home,backup_dir,*,workers_quiescent=False):
    import hashlib
    import json
    import shutil
    if workers_quiescent is not True:raise ValueError('Both workers must be stopped before paired backup')
    backend_home,backup_dir=Path(backend_home),Path(backup_dir)
    if backup_dir.exists() or backup_dir.is_symlink():raise ValueError('Paired snapshot destination exists')
    before=_recovery_tree(backend_home)
    # The OS execution locks must also be free, including owners outside systemd.
    import fcntl
    locks=[]
    try:
        for path in sorted(backend_home.rglob('*.execution')):
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW);locks.append(fd)
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with closing(_quiescent_database(controller_path)) as db:
            state=dict(db.execute('SELECT key,value FROM state'))
            controller_id=state.get('local_recovery_controller_store_id')
        backup_dir.mkdir(mode=0o700,parents=True)
        shutil.copyfile(controller_path,backup_dir/'controller.sqlite3');(backup_dir/'controller.sqlite3').chmod(0o600)
        shutil.copytree(backend_home,backup_dir/'backend',symlinks=False)
        for path in (backup_dir/'backend').rglob('*'):path.chmod(0o700 if path.is_dir() else 0o600)
        (backup_dir/'backend').chmod(0o700)
        after=_recovery_tree(backend_home)
        if before!=after or before!=_recovery_tree(backup_dir/'backend'):raise ValueError('Backend changed during snapshot')
        controller_sha=hashlib.sha256((backup_dir/'controller.sqlite3').read_bytes()).hexdigest()
        if hashlib.sha256(Path(controller_path).read_bytes()).hexdigest()!=controller_sha:raise ValueError('Controller changed during snapshot')
        identity=backup_dir/'backend/.letta/local-recovery-v1/identity.json'
        # LETTA_LOCAL_BACKEND_DIR can point at HOME/backend instead of HOME/.letta.
        identities=[path for path in (backup_dir/'backend').rglob('local-recovery-v1/identity.json')]
        if len(identities)>1:raise ValueError('Ambiguous backend store identity')
        backend_id=json.loads(identities[0].read_text())['store_id'] if identities else None
        metadata={'version':1,'controller_store_id':controller_id or 'legacy-'+controller_sha,
            'backend_store_id':backend_id,'recovery_directory':identities[0].parent.relative_to(backup_dir/'backend').as_posix() if identities else '.letta/local-recovery-v1','controller_sha256':controller_sha,'backend_files':before}
        _atomic_write(backup_dir/'paired-recovery.json',json.dumps(metadata,sort_keys=True).encode())
        return metadata
    finally:
        for fd in locks:os.close(fd)


def verify_paired_recovery_snapshot(directory):
    import hashlib
    import json
    directory=Path(directory)
    if directory.is_symlink():raise ValueError('Unsafe paired snapshot')
    metadata=json.loads((directory/'paired-recovery.json').read_text())
    relative=Path(metadata.get('recovery_directory',''))
    if relative.is_absolute() or '..' in relative.parts or relative.name!='local-recovery-v1':raise ValueError('Invalid recovery snapshot path')
    if (metadata.get('version')!=1 or hashlib.sha256((directory/'controller.sqlite3').read_bytes()).hexdigest()!=metadata['controller_sha256']
            or _recovery_tree(directory/'backend')!=metadata['backend_files']):raise ValueError('Paired snapshot digest mismatch')
    with closing(_quiescent_database(directory/'controller.sqlite3')) as db:
        row=db.execute("SELECT value FROM state WHERE key='local_recovery_controller_store_id'").fetchone()
        if row and row[0]!=metadata['controller_store_id']:raise ValueError('Controller store identity mismatch')
    return metadata


def restore_paired_recovery_snapshot(snapshot,controller_path,backend_home,*,key,workers_quiescent=False):
    import json
    import shutil
    import uuid
    if workers_quiescent is not True:raise ValueError('Both workers must be stopped before paired restore')
    snapshot,controller_path,backend_home=Path(snapshot),Path(controller_path),Path(backend_home)
    metadata=verify_paired_recovery_snapshot(snapshot)
    if controller_path.is_symlink() or backend_home.is_symlink():raise ValueError('Unsafe restore target')
    with closing(_quiescent_database(controller_path)):pass
    controller_owner=controller_path.stat();backend_owner=backend_home.stat()
    retained=backend_home.with_name(backend_home.name+'.before-restore-'+uuid.uuid4().hex)
    # Preserve the current stores and install the reconciliation fence before replacing either side.
    barrier=backend_home/metadata['recovery_directory']/'restore-required.json'
    barrier.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    _atomic_write(barrier,b'{"reason":"restore_required"}')
    os.replace(backend_home,retained)
    shutil.copytree(snapshot/'backend',backend_home,symlinks=False)
    for path in [backend_home,*backend_home.rglob('*')]:
        os.chown(path,backend_owner.st_uid,backend_owner.st_gid);path.chmod(0o700 if path.is_dir() else 0o600)
    barrier=backend_home/metadata['recovery_directory']/'restore-required.json'
    barrier.parent.mkdir(parents=True,exist_ok=True,mode=0o700);_atomic_write(barrier,b'{"reason":"restore_required"}')
    os.chown(barrier,backend_owner.st_uid,backend_owner.st_gid)
    serialized=_prepare_recovery_snapshot((snapshot/'controller.sqlite3').read_bytes(),controller_path,key)
    _atomic_write(controller_path,serialized);os.chown(controller_path,controller_owner.st_uid,controller_owner.st_gid)
    from .store import Store
    store=Store(controller_path)
    try:
        store.set_state('local_recovery_restore_required','1')
        store.set_state('paused','1')
    finally:store.close()
    os.chown(controller_path,controller_owner.st_uid,controller_owner.st_gid)
    return {'restored':True,'reconciliation_required':True,'retained_backend':str(retained)}
