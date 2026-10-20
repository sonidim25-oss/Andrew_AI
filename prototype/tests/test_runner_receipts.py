import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from andrewbot.runner_protocol import RunnerProtocolError, validate_identity
from andrewbot.runner_receipts import RunnerReceiptStore
from test_runner_protocol import identity_document

KEY = b'K' * 32
CONTROLLER = identity_document()['controller_store_id']


class RunnerReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'receipts'
        self.store = RunnerReceiptStore(self.root, KEY, expected_uid=os.getuid())
        self.runner_id = self.store.provision(controller_store_id=CONTROLLER)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def identity(self, operation='a', **changes):
        document = identity_document()
        document.update(runner_store_id=self.runner_id, operation_id=operation * 64,
                        admission_id=operation * 64, legacy_attempt_id='attempt-' + operation,
                        **changes)
        return validate_identity(document)

    def response(self, **changes):
        return dict(status='ok', stdout='PRIVATE-RESULT-42', stderr='', artifacts=[], safe_error=None, **changes)

    def test_commit_reopen_lookup_and_duplicate_prepare_never_admits_again(self):
        identity = self.identity()
        self.assertEqual(self.store.lookup(identity).state, 'missing')
        self.assertTrue(self.store.prepare(identity).created)
        self.assertEqual(self.store.lookup(identity).state, 'prepared')
        self.store.admit(identity)
        saved = self.store.finish(identity, self.response(), cleanup_verified=True)
        self.assertTrue(saved.importable)
        self.assertFalse(self.store.prepare(identity).created)
        with self.assertRaises(RunnerProtocolError):
            self.store.admit(identity)
        self.store.close()
        self.store = RunnerReceiptStore(self.root, KEY, expected_uid=os.getuid())
        self.assertEqual(self.store.lookup(identity), saved)
        self.assertEqual(self.store.capabilities()['runner_store_id'], self.runner_id)
        self.assertEqual(self.store.finish(identity, self.response(), cleanup_verified=True), saved)
        altered = self.response(); altered['stdout'] = 'changed'
        with self.assertRaises(RunnerProtocolError):
            self.store.finish(identity, altered, cleanup_verified=True)

    def test_changed_identity_or_store_refuses(self):
        identity = self.identity()
        self.store.prepare(identity)
        for changes in [dict(request_sha='0' * 64), dict(worker_request_sha='0' * 64),
                        dict(runner_store_id='21234567-1234-4321-8123-123456789abc')]:
            doc = identity.to_dict(); doc.update(changes)
            with self.assertRaises(RunnerProtocolError):
                self.store.prepare(validate_identity(doc))
        other = self.identity('b').to_dict(); other['legacy_attempt_id'] = identity.legacy_attempt_id
        with self.assertRaises(RunnerProtocolError):
            self.store.prepare(validate_identity(other))

    def test_prepare_physically_reserves_result_slot_and_finish_replaces_it(self):
        before=self.store.allocated_bytes;identity=self.identity();self.store.prepare(identity)
        self.assertGreaterEqual(self.store.allocated_bytes-before,64*1024)
        row=self.store.connection.execute('SELECT length(reservation) FROM receipts').fetchone()
        self.assertEqual(row[0],64*1024)
        self.store.admit(identity);self.store.finish(identity,self.response(),cleanup_verified=True)
        row=self.store.connection.execute('SELECT length(reservation),length(ciphertext) FROM receipts').fetchone()
        self.assertEqual(row[0],0);self.assertGreater(row[1],16)
        self.assertTrue(self.store.lookup(identity).importable)

    def test_prior_ledger_upgrade_allocates_unresolved_slots_without_losing_identity(self):
        identity=self.identity();self.store.prepare(identity)
        with self.store.connection:self.store.connection.execute('ALTER TABLE receipts DROP COLUMN reservation')
        self.store.close();self.store=RunnerReceiptStore(self.root,KEY,expected_uid=os.getuid())
        self.assertEqual(self.store.lookup(identity).state,'prepared')
        self.assertEqual(self.store.connection.execute('SELECT length(reservation) FROM receipts').fetchone()[0],65536)
        self.store.admit(identity);self.store.finish(identity,self.response(),cleanup_verified=True)
        self.store.close();self.store=RunnerReceiptStore(self.root,KEY,expected_uid=os.getuid(),capacity_bytes=1)
        self.assertTrue(self.store.lookup(identity).importable)
        self.assertFalse(self.store.capabilities()['capacity_available'])

    def test_capacity_blocks_prepare_but_not_lookup_or_finish(self):
        identity = self.identity()
        self.store.prepare(identity)
        self.store.max_records = 1
        self.assertFalse(self.store.prepare(identity).created)
        with self.assertRaises(RunnerProtocolError):
            self.store.prepare(self.identity('b'))
        self.store.admit(identity)
        self.store.finish(identity, self.response(), cleanup_verified=True)
        self.assertTrue(self.store.lookup(identity).importable)
        self.store.max_records = 50000
        self.store.capacity_bytes = self.store.allocated_bytes
        with self.assertRaises(RunnerProtocolError):
            self.store.prepare(self.identity('c'))
        self.assertTrue(self.store.lookup(identity).importable)

    def test_no_plaintext_output_or_key_and_permissions(self):
        identity = self.identity()
        self.store.prepare(identity); self.store.admit(identity)
        self.store.finish(identity, self.response(), cleanup_verified=True)
        self.store.checkpoint()
        for path in self.root.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for sentinel in [b'PRIVATE-RESULT-42', KEY, b'print(42)']:
                self.assertNotIn(sentinel, path.read_bytes())
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)

    def test_cleanup_failure_and_error_response_not_importable(self):
        identity = self.identity()
        self.store.prepare(identity); self.store.admit(identity)
        evidence = self.store.finish(identity, self.response(), cleanup_verified=False)
        self.assertEqual(evidence.state, 'uncertain')
        self.assertFalse(evidence.importable)
        self.store.close()
        self.store = RunnerReceiptStore(self.root, KEY, expected_uid=os.getuid())
        self.assertEqual(self.store.lookup(identity), evidence)
        other = self.identity('b')
        self.store.prepare(other); self.store.admit(other)
        error = self.response(); error.update(status='error', safe_error='execution_failed')
        self.assertFalse(self.store.finish(other, error, cleanup_verified=True).importable)

    def test_unfinished_admission_on_restart_remains_unknown_and_cannot_finish(self):
        identity = self.identity()
        self.store.prepare(identity); self.store.admit(identity)
        self.store.close()
        self.store = RunnerReceiptStore(self.root, KEY, expected_uid=os.getuid())
        self.assertEqual(self.store.lookup(identity).state, 'uncertain')
        self.assertFalse(self.store.prepare(identity).created)
        with self.assertRaises(RunnerProtocolError):
            self.store.finish(identity, self.response(), cleanup_verified=True)

    def test_wrong_key_uid_and_unsafe_files_refuse_startup(self):
        self.store.close()
        for key, uid in [(b'X' * 32, os.getuid()), (KEY, os.getuid() + 1)]:
            with self.assertRaises(RunnerProtocolError):
                RunnerReceiptStore(self.root, key, expected_uid=uid)
        database = self.root / 'receipts.sqlite3'
        old = database.with_suffix('.saved'); database.rename(old)
        database.symlink_to(old)
        with self.assertRaises(RunnerProtocolError):
            RunnerReceiptStore(self.root, KEY, expected_uid=os.getuid())
        database.unlink(); os.mkfifo(database, 0o600)
        with self.assertRaises(RunnerProtocolError):
            RunnerReceiptStore(self.root, KEY, expected_uid=os.getuid())

    def test_missing_or_corrupt_identity_never_reprovisions(self):
        self.store.close()
        identity_path = self.root / 'identity.json'
        original = identity_path.read_bytes()
        for replacement in [None, b'{}', b'not json']:
            if replacement is None:
                identity_path.unlink()
            else:
                identity_path.write_bytes(replacement)
            with self.assertRaises(RunnerProtocolError):
                RunnerReceiptStore(self.root, KEY, expected_uid=os.getuid())
            identity_path.write_bytes(original); identity_path.chmod(0o600)

    def test_owner_lock_blocks_second_process(self):
        script = '''import os,sys
from pathlib import Path
from andrewbot.runner_receipts import RunnerReceiptStore
from andrewbot.runner_protocol import RunnerProtocolError
try: RunnerReceiptStore(Path(sys.argv[1]), b'K'*32, expected_uid=os.getuid())
except RunnerProtocolError: sys.exit(23)
sys.exit(99)
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.root)], capture_output=True)
        self.assertEqual(result.returncode, 23, result.stderr)

    def test_faults_before_and_after_durable_commits(self):
        for point in ['admission_before_commit', 'admission_after_commit',
                      'result_before_commit', 'result_after_commit']:
            with self.subTest(point=point):
                identity = self.identity('abcd'[['admission_before_commit', 'admission_after_commit',
                                               'result_before_commit', 'result_after_commit'].index(point)])
                self.store.prepare(identity)
                self.store.close()
                script = '''import json,os,sys
from pathlib import Path
from andrewbot.runner_protocol import validate_identity
from andrewbot.runner_receipts import RunnerReceiptStore
s=RunnerReceiptStore(Path(sys.argv[1]), b'K'*32, expected_uid=os.getuid())
i=validate_identity(json.loads(sys.argv[2])); point=sys.argv[3]
if point.startswith('result'):
    s.admit(i)
s._fault=lambda name: os._exit(71) if name==point else None
if point.startswith('admission'): s.admit(i)
else: s.finish(i, dict(status='ok',stdout='saved',stderr='',artifacts=[],safe_error=None), cleanup_verified=True)
'''
                result = subprocess.run([sys.executable, '-c', script, str(self.root),
                                         json.dumps(identity.to_dict()), point], capture_output=True)
                self.assertEqual(result.returncode, 71, result.stderr)
                self.store = RunnerReceiptStore(self.root, KEY, expected_uid=os.getuid())
                evidence = self.store.lookup(identity)
                expected = 'prepared' if point == 'admission_before_commit' else (
                    'result' if point == 'result_after_commit' else 'uncertain')
                self.assertEqual(evidence.state, expected)
                self.assertEqual(evidence.importable, point == 'result_after_commit')

    def test_failed_result_commit_never_returns_success(self):
        identity = self.identity(); self.store.prepare(identity); self.store.admit(identity)
        def fail(point):
            if point == 'result_before_commit':
                raise sqlite3.OperationalError('disk full PRIVATE')
        self.store._fault = fail
        with self.assertRaises(RunnerProtocolError) as error:
            self.store.finish(identity, self.response(), cleanup_verified=True)
        self.assertNotIn('PRIVATE', str(error.exception))
        self.assertEqual(self.store.lookup(identity).state, 'admitted')

    def test_corrupt_ciphertext_and_cleanup_metadata_cannot_authorize_success(self):
        identity = self.identity(); self.store.prepare(identity); self.store.admit(identity)
        self.store.finish(identity, self.response(), cleanup_verified=False)
        with self.store.connection:
            self.store.connection.execute('UPDATE receipts SET cleanup_verified=1')
        with self.assertRaises(RunnerProtocolError):
            self.store.lookup(identity)
        with self.store.connection:
            self.store.connection.execute('UPDATE receipts SET cleanup_verified=0,ciphertext=?', (b'x' * 32,))
        with self.assertRaises(RunnerProtocolError):
            self.store.lookup(identity)

    def test_unresolved_records_cannot_be_deleted_or_identity_updated(self):
        identity = self.identity(); self.store.prepare(identity)
        with self.assertRaises(sqlite3.IntegrityError), self.store.connection:
            self.store.connection.execute('DELETE FROM receipts')
        with self.assertRaises(sqlite3.IntegrityError), self.store.connection:
            self.store.connection.execute("UPDATE receipts SET identity_sha='changed'")
        self.assertEqual(self.store.lookup(identity).state, 'prepared')

    def test_changed_database_path_or_permissions_refuses_lookup(self):
        identity = self.identity(); self.store.prepare(identity)
        database = self.root / 'receipts.sqlite3'
        database.chmod(0o644)
        with self.assertRaises(RunnerProtocolError):
            self.store.lookup(identity)
        database.chmod(0o600)
        database.rename(self.root / 'retained.sqlite3')
        database.touch(mode=0o600)
        with self.assertRaises(RunnerProtocolError):
            self.store.lookup(identity)

    def test_receipt_root_and_parent_symlinks_are_refused(self):
        alias = Path(self.temp.name) / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(RunnerProtocolError):
            RunnerReceiptStore(alias, KEY, expected_uid=os.getuid())
        with self.assertRaises(RunnerProtocolError):
            RunnerReceiptStore(alias / 'child', KEY, expected_uid=os.getuid())

    def test_failed_admission_commit_never_returns_admitted(self):
        identity = self.identity(); self.store.prepare(identity)
        def fail(point):
            if point == 'admission_before_commit':
                raise sqlite3.OperationalError('disk full PRIVATE')
        self.store._fault = fail
        with self.assertRaises(RunnerProtocolError):
            self.store.admit(identity)
        self.assertEqual(self.store.lookup(identity).state, 'prepared')

    def test_lookup_and_capabilities_remain_available_during_result_commit(self):
        identity = self.identity(); self.store.prepare(identity); self.store.admit(identity)
        entered, release = threading.Event(), threading.Event()
        errors = []
        def fault(point):
            if point == 'result_before_commit':
                entered.set()
                release.wait(3)
        self.store._fault = fault
        def finish():
            try:
                self.store.finish(identity, self.response(), cleanup_verified=True)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=finish)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            # The result is still uncommitted. Lookup must see only admission,
            # without waiting for the writer or exposing its pending response.
            started = __import__('time').monotonic()
            self.assertEqual(self.store.lookup(identity).state, 'admitted')
            self.assertEqual(self.store.capabilities()['records'], 1)
            self.assertLess(__import__('time').monotonic() - started, 1)
        finally:
            release.set(); thread.join(4)
        self.assertFalse(errors)
        self.assertTrue(self.store.lookup(identity).importable)
