import os
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
        for key in ('letta_agent_id', 'letta_conversation_id'):
            row = current.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
            old = restored.get_state(key)
            if row and old and row[0] != old:
                raise ValueError('Restore ledger belongs to another conversation; manual reconciliation required')
        latest = {row[0]: tuple(row) for row in current.execute('SELECT * FROM usage')}
        if any(latest.get(row['id']) != tuple(row) for row in restored.connection.execute('SELECT * FROM usage')):
            raise ValueError('Restore ledger histories conflict; manual reconciliation required')
        tables = ('usage', 'budget_reservations', 'media_jobs', 'media_avatar_proposals', 'updates')
        available = {row[0] for row in current.execute("SELECT name FROM sqlite_master WHERE type='table'")}
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
                        restored.connection.executemany('INSERT INTO ' + name + ' VALUES(' + marks + ')', rows)
            # Do not fetch already-processed Telegram inputs again after
            # releasing recovery. Any newer pending inputs are quarantined too.
            for name in ('telegram_inbox', 'telegram_uncertain'):
                if name in available:
                    rows = current.execute('SELECT * FROM ' + name).fetchall()
                    restored.connection.executemany('INSERT OR IGNORE INTO telegram_uncertain VALUES(?,?,?)', rows)
                    restored.connection.executemany('INSERT OR IGNORE INTO updates VALUES(?)', [(r[0],) for r in rows])
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
