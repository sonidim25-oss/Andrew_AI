"""Private encrypted receiver receipts. Lookup never dispatches a worker."""

import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .recovery_identity import canonical, digest
from .runner_format import FORMATTER, MAX_RESPONSE_BYTES, RunnerProtocolError, validate_runner_response
from .runner_protocol import (
    CONTRACT, VERSION, OperationIdentity, ReceiptEvidence, _uuid,
    validate_evidence, validate_identity,
)

RESERVATION_BYTES = 64 * 1024
# Keep headroom for a bounded response transaction's rollback journal and
# B-tree/index updates, in addition to each unresolved operation's reservation.
JOURNAL_HEADROOM_BYTES = 128 * 1024


@dataclass(frozen=True)
class PrepareResult:
    created: bool
    evidence: ReceiptEvidence


def _failure():
    return RunnerProtocolError('runner receipt evidence unavailable')


class RunnerReceiptStore:
    def __init__(self, root: Path, key: bytes, *, expected_uid: int,
                 capacity_bytes: int = 268435456, max_records: int = 50000):
        self.root = Path(root).absolute()
        self.connection = None
        self._lock_fd = None
        self._mutex = threading.RLock()
        self._identity = None
        self._paths = {}
        self.capacity_bytes = capacity_bytes
        self.max_records = max_records
        self.expected_uid = expected_uid
        if (not isinstance(key, bytes) or len(key) != 32 or type(expected_uid) is not int
                or expected_uid != os.getuid() or type(capacity_bytes) is not int or capacity_bytes <= 0
                or type(max_records) is not int or max_records <= 0):
            raise _failure()
        self._cipher = AESGCM(key)
        self._key_fingerprint = hashlib.sha256(key).hexdigest()
        try:
            for parent in reversed((self.root, *self.root.parents)):
                if parent.exists() or parent.is_symlink():
                    if stat.S_ISLNK(parent.lstat().st_mode) or not parent.is_dir():
                        raise _failure()
            if not self.root.exists():
                self.root.mkdir(mode=0o700)
                self._sync_directory(self.root.parent)
            info = self.root.lstat()
            if info.st_uid != expected_uid or stat.S_IMODE(info.st_mode) != 0o700:
                raise _failure()
            self._root_identity = (info.st_dev, info.st_ino)
            lock_path = self.root / 'owner.lock'
            if lock_path.exists() or lock_path.is_symlink():
                self._safe_file(lock_path)
            self._lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            self._safe_file(lock_path)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._paths[lock_path] = self._file_identity(lock_path)
            identity_path, db_path = self.root / 'identity.json', self.root / 'receipts.sqlite3'
            have_identity = identity_path.exists() or identity_path.is_symlink()
            have_database = db_path.exists() or db_path.is_symlink()
            if have_identity != have_database:
                raise _failure()
            if have_identity:
                self._safe_file(identity_path); self._safe_file(db_path)
                if identity_path.stat().st_size > 8192:
                    raise _failure()
                self._identity = json.loads(identity_path.read_text())
                expected = {'contract', 'version', 'controller_store_id', 'runner_store_id', 'key_fingerprint'}
                if (not isinstance(self._identity, dict) or set(self._identity) != expected
                        or self._identity['contract'] != CONTRACT
                        or type(self._identity['version']) is not int or self._identity['version'] != VERSION
                        or not _uuid(self._identity['controller_store_id'])
                        or not _uuid(self._identity['runner_store_id'])
                        or self._identity['key_fingerprint'] != self._key_fingerprint):
                    raise _failure()
                self._open_database()
                if self.connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise _failure()
                rows = self.connection.execute('SELECT value FROM metadata WHERE key=?', ('identity',)).fetchall()
                if len(rows) != 1 or rows[0][0] != canonical(self._identity):
                    raise _failure()
                with self.connection:
                    self.connection.execute('BEGIN IMMEDIATE')
                    if 'reservation' not in {r[1] for r in self.connection.execute('PRAGMA table_info(receipts)')}:
                        self.connection.execute("ALTER TABLE receipts ADD COLUMN reservation BLOB NOT NULL DEFAULT X''")
                        self.connection.execute('UPDATE receipts SET reservation=zeroblob(?) WHERE ciphertext IS NULL',(RESERVATION_BYTES,))
                        if self._capacity()[1]>self.capacity_bytes:raise _failure()
                    if self.connection.execute('SELECT 1 FROM receipts WHERE length(reservation)!=CASE WHEN ciphertext IS NULL THEN ? ELSE 0 END LIMIT 1',(RESERVATION_BYTES,)).fetchone():raise _failure()
                    self.connection.execute("UPDATE receipts SET state='uncertain' WHERE state='admitted'")
            elif not have_identity and any(path != lock_path for path in self.root.iterdir()):
                raise _failure()
        except Exception:
            self.close()
            raise _failure() from None

    @staticmethod
    def _sync_directory(directory):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _safe_file(self, path):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.expected_uid
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise _failure()
        return info

    def _file_identity(self, path):
        info = self._safe_file(path)
        return info.st_dev, info.st_ino

    def _check_paths(self):
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != self.expected_uid or (info.st_dev, info.st_ino) != self._root_identity
                or any(self._file_identity(path) != original for path, original in self._paths.items())):
            raise _failure()
        for suffix in ('-journal', '-wal', '-shm'):
            path = self.root / ('receipts.sqlite3' + suffix)
            if path.exists() or path.is_symlink():
                self._safe_file(path)

    def _open_database(self):
        database = self.root / 'receipts.sqlite3'
        self._check_paths()
        self.connection = sqlite3.connect(database, timeout=5, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        if self.connection.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower() != 'delete':
            raise _failure()
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.execute('PRAGMA foreign_keys=ON')
        for path in (database, self.root / 'identity.json'):
            self._paths[path] = self._file_identity(path)

    @contextmanager
    def _transaction(self):
        with self._mutex:
            try:
                self._check_paths()
                if self.connection is None:
                    raise _failure()
                with self.connection:
                    yield
            except Exception:
                raise _failure() from None

    @contextmanager
    def _reader(self):
        """Read committed evidence without waiting on the execution-owner lock."""
        connection = None
        try:
            self._check_paths()
            if self.connection is None:
                raise _failure()
            database = self.root / 'receipts.sqlite3'
            connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=0.2)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            yield connection
        except Exception:
            raise _failure() from None
        finally:
            if connection is not None:
                connection.close()

    def provision(self, *, controller_store_id: str) -> str:
        with self._mutex:
            if self._identity is not None or not _uuid(controller_store_id) or self._lock_fd is None:
                raise _failure()
            identity = dict(contract=CONTRACT, version=VERSION, controller_store_id=controller_store_id,
                            runner_store_id=str(uuid.uuid4()), key_fingerprint=self._key_fingerprint)
            try:
                self._check_paths()
                # Exclusive creation deliberately leaves incomplete provisioning
                # visible on failure; ordinary startup never repairs/replaces it.
                path = self.root / 'identity.json'
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                with os.fdopen(fd, 'wb') as output:
                    output.write(canonical(identity).encode()); output.flush(); os.fsync(output.fileno())
                self._sync_directory(self.root)
                database = self.root / 'receipts.sqlite3'
                fd = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                os.fsync(fd); os.close(fd)
                self._identity = identity
                self._open_database()
                self.connection.executescript('''
                    CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE receipts(
                        operation_id TEXT PRIMARY KEY, admission_id TEXT NOT NULL UNIQUE,
                        legacy_attempt_id TEXT NOT NULL UNIQUE,
                        identity_json TEXT NOT NULL, identity_sha TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('prepared','admitted','uncertain','result')),
                        prepared_at INTEGER NOT NULL, admitted_at INTEGER, finished_at INTEGER,
                        response_sha TEXT, nonce BLOB, ciphertext BLOB,
                        cleanup_verified INTEGER NOT NULL DEFAULT 0 CHECK(cleanup_verified IN (0,1)),
                        reservation BLOB NOT NULL DEFAULT X'');
                    CREATE TRIGGER immutable_receipt_identity BEFORE UPDATE OF
                        operation_id,admission_id,legacy_attempt_id,identity_json,identity_sha ON receipts
                        BEGIN SELECT RAISE(ABORT,'immutable receipt identity'); END;
                    CREATE TRIGGER retain_receipts BEFORE DELETE ON receipts
                        BEGIN SELECT RAISE(ABORT,'receipt retention required'); END;
                ''')
                with self.connection:
                    self.connection.execute('INSERT INTO metadata VALUES(?,?)', ('identity', canonical(identity)))
                self._sync_directory(self.root)
                return identity['runner_store_id']
            except Exception:
                self.close()
                raise _failure() from None

    def _identity_check(self, identity):
        if not isinstance(identity, OperationIdentity) or self._identity is None:
            raise _failure()
        identity = validate_identity(identity.to_dict())
        if (identity.runner_store_id != self._identity['runner_store_id']
                or identity.controller_store_id != self._identity['controller_store_id']):
            raise _failure()
        return identity

    def _row(self, identity, connection=None):
        row = (connection or self.connection).execute(
            'SELECT * FROM receipts WHERE operation_id=?', (identity.operation_id,)).fetchone()
        if row is not None and (row['identity_sha'] != digest(identity.to_dict())
                                or row['identity_json'] != canonical(identity.to_dict())):
            raise _failure()
        return row

    @staticmethod
    def _aad(identity, response_sha, cleanup_verified):
        return canonical(dict(contract=CONTRACT, identity=identity.to_dict(),
                              response_sha=response_sha, cleanup_verified=cleanup_verified)).encode()

    def _evidence(self, identity, row):
        response = None
        if row is not None and row['ciphertext'] is not None:
            try:
                if (not isinstance(row['nonce'], bytes) or len(row['nonce']) != 12
                        or not isinstance(row['ciphertext'], bytes)
                        or not 16 <= len(row['ciphertext']) <= MAX_RESPONSE_BYTES + 16):
                    raise _failure()
                raw = self._cipher.decrypt(row['nonce'], row['ciphertext'], self._aad(
                    identity, row['response_sha'], bool(row['cleanup_verified'])))
                response = json.loads(raw.decode('utf-8'))
            except (InvalidTag, ValueError, TypeError, UnicodeError):
                raise _failure() from None
        elif row is not None and (row['nonce'] is not None or row['response_sha'] is not None):
            raise _failure()
        return validate_evidence(dict(
            contract=CONTRACT, version=VERSION, identity=identity.to_dict(), identity_sha=digest(identity.to_dict()),
            state=row['state'] if row is not None else 'missing', response=response,
            response_sha=row['response_sha'] if row is not None else None,
            cleanup_verified=bool(row['cleanup_verified']) if row is not None else False), identity)

    @property
    def allocated_bytes(self):
        if self.connection is None:
            raise _failure()
        return self._allocated(self.connection)

    @staticmethod
    def _allocated(connection):
        return (connection.execute('PRAGMA page_count').fetchone()[0]
                * connection.execute('PRAGMA page_size').fetchone()[0])

    def _capacity(self, connection=None):
        connection = connection or self.connection
        count = connection.execute('SELECT count(*) FROM receipts').fetchone()[0]
        metadata_bytes = 0
        for filename in ('identity.json', 'owner.lock'):
            info = self._safe_file(self.root / filename)
            metadata_bytes += max(info.st_size, info.st_blocks * 512)
        return count, (self._allocated(connection) + metadata_bytes + JOURNAL_HEADROOM_BYTES)

    def _assert_dispatch(self):
        if (self.root / 'restore-required.json').exists():
            raise _failure()

    def prepare(self, identity):
        self._assert_dispatch()
        identity = self._identity_check(identity)
        with self._transaction():
            old = self._row(identity)
            if old is not None:
                return PrepareResult(False, self._evidence(identity, old))
            count, allocated = self._capacity()
            if count >= self.max_records or allocated + RESERVATION_BYTES > self.capacity_bytes:
                raise _failure()
            self.connection.execute('''INSERT INTO receipts(
                operation_id,admission_id,legacy_attempt_id,identity_json,identity_sha,state,prepared_at,reservation)
                VALUES(?,?,?,?,?,'prepared',?,zeroblob(?))''', (
                identity.operation_id, identity.admission_id, identity.legacy_attempt_id,
                canonical(identity.to_dict()), digest(identity.to_dict()), time.time_ns(), RESERVATION_BYTES))
            if self._capacity()[1] > self.capacity_bytes:
                raise _failure()
            evidence = self._evidence(identity, self._row(identity))
        return PrepareResult(True, evidence)

    def admit(self, identity):
        self._assert_dispatch()
        identity = self._identity_check(identity)
        with self._transaction():
            row = self._row(identity)
            if row is None or row['state'] != 'prepared':
                raise _failure()
            self.connection.execute("UPDATE receipts SET state='admitted',admitted_at=? WHERE operation_id=?",
                                    (time.time_ns(), identity.operation_id))
            evidence = self._evidence(identity, self._row(identity))
            self._fault('admission_before_commit')
        self._fault('admission_after_commit')
        return evidence

    def finish(self, identity, response, *, cleanup_verified):
        identity = self._identity_check(identity)
        if type(cleanup_verified) is not bool:
            raise _failure()
        response = validate_runner_response(response)
        if response['visit'] is not None:
            raise _failure()
        response_sha = digest(response)
        with self._transaction():
            row = self._row(identity)
            if row is None:
                raise _failure()
            if row['ciphertext'] is not None:
                evidence = self._evidence(identity, row)
                if evidence.response_sha != response_sha or evidence.cleanup_verified != cleanup_verified:
                    raise _failure()
                return evidence
            if row['state'] != 'admitted':
                raise _failure()
            nonce = secrets.token_bytes(12)
            ciphertext = self._cipher.encrypt(nonce, canonical(response).encode(),
                                              self._aad(identity, response_sha, cleanup_verified))
            self.connection.execute('''UPDATE receipts SET state=?,finished_at=?,response_sha=?,nonce=?,
                ciphertext=?,cleanup_verified=?,reservation=X'' WHERE operation_id=?''', (
                'result' if cleanup_verified else 'uncertain', time.time_ns(), response_sha, nonce,
                ciphertext, int(cleanup_verified), identity.operation_id))
            if self._capacity()[1] > self.capacity_bytes:
                raise _failure()
            evidence = self._evidence(identity, self._row(identity))
            self._fault('result_before_commit')
        self._fault('result_after_commit')
        return evidence

    def mark_uncertain(self, identity):
        identity = self._identity_check(identity)
        with self._transaction():
            row = self._row(identity)
            if row is None or row['state'] not in {'admitted', 'uncertain'}:
                raise _failure()
            self.connection.execute("UPDATE receipts SET state='uncertain' WHERE operation_id=?", (identity.operation_id,))

    def lookup(self, identity):
        identity = self._identity_check(identity)
        with self._reader() as connection:
            return self._evidence(identity, self._row(identity, connection))

    def capabilities(self):
        with self._reader() as connection:
            count, used = self._capacity(connection)
            return dict(contract=CONTRACT, version=VERSION, runner_store_id=self._identity['runner_store_id'],
                        controller_store_id=self._identity['controller_store_id'], formatter=FORMATTER,
                        records=count, capacity_available=(count < self.max_records
                                                          and used + RESERVATION_BYTES <= self.capacity_bytes))

    def checkpoint(self):
        with self._transaction():
            fd = os.open(self.root / 'receipts.sqlite3', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            self._sync_directory(self.root)

    def _fault(self, point):
        """Commit boundary hook for isolated subprocess crash tests."""

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
