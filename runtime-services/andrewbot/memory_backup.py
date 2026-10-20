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
