"""Controller mapping, fenced runner admission and callback-free receipt import."""

import hashlib
import json
import logging
import secrets
import sqlite3

from .recovery_identity import canonical, digest
from .recovery_records import RecoveryRecords
from .runner_format import FORMATTER, RunnerProtocolError, format_runner_response, prepare_runner_request
from .runner_protocol import (
    CONTRACT, VERSION, TOOLS, DispatchGrant, OperationIdentity, ReceiptEvidence, _uuid,
    validate_evidence, validate_grant, validate_identity,
)
from .runner_socket import RunnerTransportError, validate_envelope


PREGRANT_RESULT = canonical({'status': 'error', 'executed': False, 'error': {
    'code': 'runner_not_executed',
    'message': 'Runner communication failed before command admission; the command was not executed.'}})


def _failure_category(error):
    if isinstance(error, RunnerTransportError):
        return error.category
    if isinstance(error, OSError):
        return 'io_error'
    if isinstance(error, sqlite3.Error):
        return 'storage_error'
    if isinstance(error, RunnerProtocolError):
        return 'protocol_error'
    from .recovery import RecoveryInterrupted
    if isinstance(error, RecoveryInterrupted):
        return error.category
    return 'authority_or_state_error' if isinstance(error, ValueError) else 'unexpected_error'


def _failure():
    return RunnerProtocolError('runner result recovery held')


def initialize_schema(store):
    connection = store.connection
    definition = connection.execute("SELECT sql FROM sqlite_master WHERE name='cycle_tool_attempts'").fetchone()[0]
    if "'not_executed'" not in definition:
        if connection.in_transaction:
            raise _failure()
        # Do not rename the original table: SQLite would rewrite foreign keys
        # in external receipts to the temporary name. Replace it transactionally.
        connection.execute('PRAGMA foreign_keys=OFF')
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('''CREATE TABLE runner_cycle_tool_migration(
                attempt_id TEXT PRIMARY KEY,cycle_id TEXT NOT NULL REFERENCES cycles(cycle_id),
                tool_name TEXT NOT NULL,outcome TEXT NOT NULL
                    CHECK(outcome IN ('started','returned','uncertain','not_executed')),
                started_at TEXT NOT NULL,finished_at TEXT)''')
            connection.execute('INSERT INTO runner_cycle_tool_migration SELECT * FROM cycle_tool_attempts')
            connection.execute('DROP TABLE cycle_tool_attempts')
            connection.execute('ALTER TABLE runner_cycle_tool_migration RENAME TO cycle_tool_attempts')
            connection.execute('CREATE INDEX cycle_tool_attempts_cycle ON cycle_tool_attempts(cycle_id,outcome)')
            if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise _failure()
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute('PRAGMA foreign_keys=ON')
    connection.executescript('''
        CREATE TABLE IF NOT EXISTS runner_operations(
            operation_id TEXT PRIMARY KEY,admission_id TEXT NOT NULL UNIQUE REFERENCES recovery_effects(admission_id),
            legacy_attempt_id TEXT NOT NULL UNIQUE REFERENCES cycle_tool_attempts(attempt_id),
            binding_sha TEXT NOT NULL,identity_json TEXT NOT NULL,identity_sha TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('prepared','admitted','cancelled','imported')),
            created_at TEXT NOT NULL,admitted_at TEXT,imported_at TEXT);
        CREATE TRIGGER IF NOT EXISTS immutable_runner_mapping BEFORE UPDATE OF
            operation_id,admission_id,legacy_attempt_id,binding_sha,identity_json,identity_sha ON runner_operations
            BEGIN SELECT RAISE(ABORT,'immutable runner mapping'); END;
        CREATE TRIGGER IF NOT EXISTS retain_runner_mapping BEFORE DELETE ON runner_operations
            BEGIN SELECT RAISE(ABORT,'runner mapping retention required'); END;
        CREATE TABLE IF NOT EXISTS runner_imports(
            operation_id TEXT PRIMARY KEY REFERENCES runner_operations(operation_id),
            evidence_sha TEXT NOT NULL,evidence_json TEXT NOT NULL,response_sha TEXT NOT NULL,
            result_sha TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS immutable_runner_import BEFORE UPDATE ON runner_imports
            BEGIN SELECT RAISE(ABORT,'immutable runner import'); END;
        CREATE TRIGGER IF NOT EXISTS retain_runner_import BEFORE DELETE ON runner_imports
            BEGIN SELECT RAISE(ABORT,'runner import retention required'); END;
        CREATE TABLE IF NOT EXISTS runner_research_results(
            attempt_id TEXT PRIMARY KEY REFERENCES cycle_tool_attempts(attempt_id),
            context_json TEXT NOT NULL,event_id INTEGER NOT NULL UNIQUE REFERENCES research_events(id));
        CREATE TRIGGER IF NOT EXISTS immutable_runner_research BEFORE UPDATE ON runner_research_results
            BEGIN SELECT RAISE(ABORT,'immutable runner research'); END;
        CREATE TRIGGER IF NOT EXISTS retain_runner_research BEFORE DELETE ON runner_research_results
            BEGIN SELECT RAISE(ABORT,'runner research retention required'); END;
    ''')


def _mapping(store, identity):
    row = store.connection.execute('SELECT * FROM runner_operations WHERE operation_id=?', (identity.operation_id,)).fetchone()
    effect = store.connection.execute('SELECT * FROM recovery_effects WHERE admission_id=?', (identity.admission_id,)).fetchone()
    legacy = store.connection.execute('SELECT * FROM cycle_tool_attempts WHERE attempt_id=?', (identity.legacy_attempt_id,)).fetchone()
    if (row is None or row['identity_json'] != canonical(identity.to_dict())
            or row['identity_sha'] != digest(identity.to_dict()) or row['binding_sha'] != identity.binding_sha
            or row['admission_id'] != identity.admission_id or row['legacy_attempt_id'] != identity.legacy_attempt_id
            or effect is None or effect['kind'] != 'tool' or effect['reservation_id'] is not None or effect['effect_id'] != identity.effect_id
            or effect['binding_sha'] != identity.binding_sha or effect['request_sha'] != identity.request_sha
            or legacy is None or legacy['cycle_id'] != identity.binding['cycle_id']
            or legacy['tool_name'] != identity.tool_name):
        raise _failure()
    scope = store.connection.execute('SELECT origin,binding_sha FROM recovery_attempts WHERE admission_id=?', (effect['scope_id'],)).fetchone()
    cycle = store.connection.execute('SELECT session_id FROM cycles WHERE cycle_id=?', (identity.binding['cycle_id'],)).fetchone()
    if (scope is None or scope['origin'] != 'autonomous' or scope['binding_sha'] != identity.binding_sha
            or cycle is None or cycle['session_id'] != identity.binding['session_id']):
        raise _failure()
    return row, effect, legacy


def prepared_cancellation_proven(store, admission_id):
    """Only a locally fenced pre-grant mapping can extend owner-yield evidence."""
    try:
        row = store.connection.execute('SELECT * FROM runner_operations WHERE admission_id=?', (admission_id,)).fetchone()
        if row is None:
            return False
        identity = validate_identity(json.loads(row['identity_json']))
        row, effect, legacy = _mapping(store, identity)
        return (row['state'] == 'cancelled' and row['admitted_at'] is None
                and effect['state'] == 'cancelled' and effect['reservation_id'] is None
                and legacy['outcome'] == 'not_executed'
                and store.connection.execute('SELECT 1 FROM recovery_results WHERE admission_id=?', (admission_id,)).fetchone() is None)
    except (ValueError, TypeError, sqlite3.Error):
        return False


def validate_pregrant_rejection(store, admission_id, result_sha, result, usage):
    """Validate a controller cancellation receipt, not a receiver execution result."""
    marker = usage.get('runner_pregrant_rejection') if isinstance(usage, dict) else None
    if (not isinstance(usage, dict) or set(usage) != {'runner_pregrant_rejection'} or not isinstance(marker, dict)
            or set(marker) != {'contract', 'operation_id', 'identity_sha', 'category'}
            or marker['contract'] != 'runner_not_executed_v1'
            or marker['category'] not in {'io_error', 'deadline_exceeded'}
            or result != PREGRANT_RESULT or result_sha != digest(result)):
        raise _failure()
    saved = store.connection.execute('SELECT identity_json FROM runner_operations WHERE admission_id=?', (admission_id,)).fetchone()
    if saved is None:
        raise _failure()
    identity = validate_identity(json.loads(saved['identity_json']))
    row, effect, legacy = _mapping(store, identity)
    if (identity.admission_id != admission_id
            or marker['operation_id'] != identity.operation_id or marker['identity_sha'] != digest(identity.to_dict())
            or row['state'] != 'cancelled' or row['admitted_at'] is not None
            or effect['state'] != 'confirmed' or legacy['outcome'] != 'not_executed'
            or legacy['finished_at'] is None
            or identity.controller_store_id != store.get_state('local_recovery_controller_store_id', '')
            or identity.runner_store_id != store.get_state('runner_receipt_store_id', '')
            or store.connection.execute('SELECT 1 FROM runner_imports WHERE operation_id=?', (identity.operation_id,)).fetchone()):
        raise _failure()


def cancel_prepared_mapping_locked(store, admission_id):
    """Caller owns the outer transaction; never commit a partial cancellation."""
    if not store.connection.in_transaction:
        raise _failure()
    row = store.connection.execute('SELECT identity_json FROM runner_operations WHERE admission_id=?', (admission_id,)).fetchone()
    if row is None:
        return False
    identity = validate_identity(json.loads(row['identity_json']))
    row, effect, legacy = _mapping(store, identity)
    if (row['state'] != 'prepared' or row['admitted_at'] is not None or effect['state'] != 'prepared'
            or legacy['outcome'] != 'started'):
        raise _failure()
    store.connection.execute("UPDATE recovery_effects SET state='cancelled' WHERE admission_id=?", (identity.admission_id,))
    store.connection.execute("UPDATE cycle_tool_attempts SET outcome='not_executed',finished_at=? WHERE attempt_id=?",
                             (store.now(), identity.legacy_attempt_id))
    store.connection.execute("UPDATE runner_operations SET state='cancelled' WHERE operation_id=?", (identity.operation_id,))
    return True


def _receipt_metadata(identity, evidence):
    return dict(contract=CONTRACT, version=VERSION, operation_id=identity.operation_id,
                controller_store_id=identity.controller_store_id, runner_store_id=identity.runner_store_id,
                identity=identity.to_dict(), identity_sha=digest(identity.to_dict()), response_sha=evidence.response_sha,
                formatter=FORMATTER, cleanup_verified=True)


def confirmed_runner_metadata(store, admission_id, result_sha, result, usage):
    row = store.connection.execute('''SELECT o.identity_json,i.evidence_json,i.evidence_sha,i.result_sha,i.response_sha
        FROM runner_operations o JOIN runner_imports i USING(operation_id) WHERE o.admission_id=?''', (admission_id,)).fetchone()
    if row is None:
        raise _failure()
    identity = validate_identity(json.loads(row['identity_json']))
    operation, effect, legacy = _mapping(store, identity)
    if (identity.controller_store_id != store.get_state('local_recovery_controller_store_id', '')
            or identity.runner_store_id != store.get_state('runner_receipt_store_id', '')):
        raise _failure()
    evidence = validate_evidence(json.loads(row['evidence_json']), identity)
    metadata = _receipt_metadata(identity, evidence)
    research = store.connection.execute('''SELECT r.context_json,e.entry_id,e.session_id,e.cycle_id,e.tool_name,e.outcome,e.kind
        FROM runner_research_results r JOIN research_events e ON e.id=r.event_id WHERE r.attempt_id=?''',
                                         (identity.legacy_attempt_id,)).fetchone()
    if (operation['state'] != 'imported' or operation['admitted_at'] is None or effect['state'] != 'confirmed' or legacy['outcome'] != 'returned'
            or not evidence.importable or row['evidence_sha'] != digest(evidence.to_dict())
            or row['response_sha'] != evidence.response_sha
            or row['result_sha'] != result_sha or digest(result) != result_sha
            or result != format_runner_response(evidence.to_dict()['response'])
            or usage != {'runner_receipt': metadata} or research is None
            or research['context_json'] != canonical(identity.to_dict()['research_context'])
            or research['entry_id'] != identity.research_context['research_id']
            or research['session_id'] != identity.binding['session_id'] or research['cycle_id'] != identity.binding['cycle_id']
            or research['tool_name'] != identity.tool_name or research['outcome'] != 'returned' or research['kind'] != 'tool_used'):
        raise _failure()
    return metadata


class RunnerRecoveryAdapter:
    def __init__(self, store, runner_client, *, expected_runner_uid, runner_store_id):
        if type(expected_runner_uid) is not int or expected_runner_uid < 0 or not _uuid(runner_store_id):
            raise _failure()
        self.store, self.client = store, runner_client
        self.expected_runner_uid, self.runner_store_id = expected_runner_uid, runner_store_id
        self.records = RecoveryRecords(store)

    def _pins(self):
        controller = self.store.get_state('local_recovery_controller_store_id', '')
        if not _uuid(controller) or self.store.get_state('runner_receipt_store_id', '') != self.runner_store_id:
            raise _failure()
        return controller

    def _enabled(self):
        self._pins()
        if (self.store.get_state('runner_receipts_enabled', '0') != '1'
                or self.store.get_state('runner_receipt_backend_capability', '0') != '1'):
            raise _failure()

    def prepare(self, metadata, name, arguments):
        request = prepare_runner_request(name, arguments)
        with self.store._lock, self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            self._enabled()
            effect = self.records._validate_dispatch(metadata, {'name': name, 'arguments': arguments}, {'tool'})
            origin = self.store.connection.execute('SELECT origin FROM recovery_attempts WHERE admission_id=?',
                                                   (metadata['claim']['admission_id'],)).fetchone()
            if origin is None or origin['origin'] != 'autonomous':
                raise _failure()
            old = self.store.connection.execute('SELECT identity_json FROM runner_operations WHERE admission_id=?',
                                                (effect['admission_id'],)).fetchone()
            if old is not None:
                identity = validate_identity(json.loads(old['identity_json']))
                _mapping(self.store, identity)
                if (identity.tool_name != name or identity.worker_request_sha != digest(request)
                        or identity.controller_store_id != self._pins() or identity.runner_store_id != self.runner_store_id):
                    raise _failure()
                return identity
            if effect['state'] != 'prepared':
                raise _failure()  # Legacy admitted operations cannot be retrofitted.
            binding = metadata['binding']
            if not self.store.connection.execute("SELECT 1 FROM cycles WHERE cycle_id=? AND session_id=? AND status='running'",
                    (binding['cycle_id'], binding['session_id'])).fetchone():
                raise _failure()
            research_id = self.store._focused_research_id()
            research = self.store.research_entry(research_id) if research_id else None
            context = dict(session_id=binding['session_id'], cycle_id=binding['cycle_id'], research_id=research_id,
                           research_revision=research['revision'] if research else 0)
            attempt_id = secrets.token_hex(16)
            identity = validate_identity(dict(contract=CONTRACT, version=VERSION, controller_store_id=self._pins(),
                runner_store_id=self.runner_store_id, binding=dict(binding), binding_sha=digest(binding),
                admission_id=effect['admission_id'], effect_id=effect['effect_id'], request_sha=effect['request_sha'],
                operation_id=secrets.token_hex(32), legacy_attempt_id=attempt_id, tool_name=name,
                worker_request_sha=digest(request), formatter=FORMATTER, research_context=context))
            self.store.connection.execute('INSERT INTO cycle_tool_attempts VALUES(?,?,?,?,?,NULL)',
                (attempt_id, binding['cycle_id'], name, 'started', self.store.now()))
            self.store.connection.execute('''INSERT INTO runner_operations(operation_id,admission_id,legacy_attempt_id,
                binding_sha,identity_json,identity_sha,state,created_at) VALUES(?,?,?,?,?,?,'prepared',?)''', (
                identity.operation_id, identity.admission_id, attempt_id, identity.binding_sha,
                canonical(identity.to_dict()), digest(identity.to_dict()), self.store.now()))
            self.store.connection.execute('UPDATE recovery_attempts SET untrusted=1 WHERE binding_sha=?', (identity.binding_sha,))
        return identity

    def grant(self, metadata, identity, ready):
        identity = validate_identity(identity.to_dict())
        if validate_envelope(ready) != 'ready' or ready['operation_sha'] != digest(identity.to_dict()):
            raise _failure()
        with self.store._lock, self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            self._enabled()
            row, effect, legacy = _mapping(self.store, identity)
            if identity.controller_store_id != self._pins() or identity.runner_store_id != self.runner_store_id:
                raise _failure()
            if row['state'] != 'prepared' or effect['state'] != 'prepared' or legacy['outcome'] != 'started':
                raise _failure()
            if metadata['admission_id'] != identity.admission_id or digest(metadata['binding']) != identity.binding_sha:
                raise _failure()
            self.records._validate_dispatch_sha(metadata, identity.request_sha, {'tool'})
            from .recovery import check_recovery_outcomes
            check_recovery_outcomes(self.store, dict(identity.binding), ignore_runner_attempt_id=identity.legacy_attempt_id)
            grant = validate_grant(dict(operation_sha=ready['operation_sha'], challenge=ready['challenge'],
                scope_id=metadata['claim']['admission_id'], fence=metadata['claim']['fence'],
                boot_id=metadata['claim']['boot_id'], expires_monotonic_ms=metadata['claim']['expires_monotonic_ms']))
            self.records._begin_tool_sha_locked(metadata, identity.request_sha)
            self.store.connection.execute("UPDATE runner_operations SET state='admitted',admitted_at=? WHERE operation_id=?",
                                           (self.store.now(), identity.operation_id))
            self._fault('grant_before_commit')
        self._fault('grant_after_commit')
        return grant

    def cancel_prepared(self, identity):
        identity = validate_identity(identity.to_dict())
        with self.store._lock, self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            row, effect, legacy = _mapping(self.store, identity)
            if row['state'] == 'cancelled' and prepared_cancellation_proven(self.store, identity.admission_id):
                return
            if (row['state'] != 'prepared' or row['admitted_at'] is not None or effect['state'] != 'prepared'
                    or legacy['outcome'] != 'started'):
                raise _failure()
            cancel_prepared_mapping_locked(self.store, identity.admission_id)

    def _confirm_pregrant_rejection(self, metadata, identity, category):
        """Only the live failure handler may confirm a just-fenced cancellation."""
        if category not in {'io_error', 'deadline_exceeded'}:
            raise _failure()
        with self.store._lock, self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            self._enabled()
            self.records._validate_dispatch_sha(metadata, identity.request_sha, {'tool'})
            if not prepared_cancellation_proven(self.store, identity.admission_id):
                raise _failure()
            if not self.store.connection.execute("SELECT 1 FROM cycles WHERE cycle_id=? AND session_id=? AND status='running'",
                    (identity.binding['cycle_id'], identity.binding['session_id'])).fetchone():
                raise _failure()
            from .recovery import check_recovery_outcomes
            check_recovery_outcomes(self.store, dict(identity.binding))
            usage = {'runner_pregrant_rejection': {'contract': 'runner_not_executed_v1',
                'operation_id': identity.operation_id, 'identity_sha': digest(identity.to_dict()), 'category': category}}
            self.records._confirm(identity.admission_id, canonical(PREGRANT_RESULT), usage)
            validate_pregrant_rejection(self.store, identity.admission_id, digest(PREGRANT_RESULT), PREGRANT_RESULT, usage)
        return PREGRANT_RESULT

    def execute(self, metadata, name, arguments):
        request = prepare_runner_request(name, arguments)
        identity = self.prepare(metadata, name, arguments)
        from .memory import note_untrusted_memory_source
        note_untrusted_memory_source()
        with self.store._lock:
            row, _, _ = _mapping(self.store, identity)
            if row['state'] == 'imported':
                return self.records.effect_result(identity.admission_id)['result']
            if row['state'] == 'cancelled':
                proof = self.records.effect_result(identity.admission_id)
                if proof['state'] == 'confirmed' and isinstance(proof.get('usage'), dict) and 'runner_pregrant_rejection' in proof['usage']:
                    return proof['result']
                raise _failure()
            if row['state'] != 'prepared':
                raise _failure()
        stage = 'capabilities'
        try:
            caps = self.client.receipt_capabilities(expected_runner_uid=self.expected_runner_uid)
            if (caps.get('contract') != CONTRACT or type(caps.get('version')) is not int or caps['version'] != VERSION
                    or caps.get('controller_store_id') != identity.controller_store_id or caps.get('runner_store_id') != self.runner_store_id
                    or caps.get('formatter') != FORMATTER or caps.get('execution_enabled') is not True):
                raise _failure()
            def grant(ready):
                nonlocal stage
                stage = 'grant'
                value = self.grant(metadata, identity, ready)
                stage = 'handshake'
                return value
            stage = 'handshake'
            evidence = self.client.execute_receipted(request, identity, expected_runner_uid=self.expected_runner_uid,
                                                     grant_factory=grant)
            stage = 'import'
            return self.import_result(identity, evidence)
        except Exception as error:
            with self.store._lock:
                row, _, _ = _mapping(self.store, identity)
            category = _failure_category(error)
            logging.warning('Runner recovery failure (stage=%s, category=%s, admission=%s)', stage, category, row['state'])
            if row['state'] == 'prepared':
                self.cancel_prepared(identity)
                if isinstance(error, (OSError, RunnerTransportError)):
                    try:
                        return self._confirm_pregrant_rejection(metadata, identity, category)
                    except Exception as refusal:
                        logging.warning('Runner non-execution receipt withheld (category=%s)', _failure_category(refusal))
            elif row['state'] == 'admitted':
                with self.store._lock, self.store.connection:
                    self.store.connection.execute("UPDATE cycle_tool_attempts SET outcome='uncertain',finished_at=? WHERE attempt_id=? AND outcome='started'",
                                                   (self.store.now(), identity.legacy_attempt_id))
            raise _failure() from None

    def import_result(self, identity, evidence):
        identity = validate_identity(identity.to_dict())
        if identity.controller_store_id != self._pins() or identity.runner_store_id != self.runner_store_id:
            raise _failure()
        evidence = validate_evidence(evidence.to_dict() if isinstance(evidence, ReceiptEvidence) else evidence, identity)
        if not evidence.importable:
            raise _failure()
        text = format_runner_response(evidence.to_dict()['response'])
        encoded = canonical(text); result_sha = hashlib.sha256(encoded.encode()).hexdigest()
        evidence_json = canonical(evidence.to_dict()); evidence_sha = digest(evidence.to_dict())
        with self.store._lock, self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            if identity.controller_store_id != self._pins() or identity.runner_store_id != self.runner_store_id:
                raise _failure()
            row, effect, legacy = _mapping(self.store, identity)
            previous = self.store.connection.execute('SELECT * FROM runner_imports WHERE operation_id=?', (identity.operation_id,)).fetchone()
            if previous is not None:
                if previous['evidence_json'] != evidence_json or previous['evidence_sha'] != evidence_sha:
                    raise _failure()
                proof = self.records.effect_result(identity.admission_id)
                if proof.get('runner_receipt') != _receipt_metadata(identity, evidence) or proof.get('result') != text:
                    raise _failure()
                return text
            if (row['state'] != 'admitted' or row['admitted_at'] is None or effect['state'] != 'admitted'
                    or legacy['outcome'] not in {'started', 'uncertain'}):
                raise _failure()
            self.store.connection.execute('INSERT INTO runner_imports VALUES(?,?,?,?,?,?)', (
                identity.operation_id, evidence_sha, evidence_json, evidence.response_sha, result_sha, self.store.now()))
            self.records._confirm(identity.admission_id, encoded, {'runner_receipt': _receipt_metadata(identity, evidence)})
            self.store.connection.execute("UPDATE cycle_tool_attempts SET outcome='returned',finished_at=? WHERE attempt_id=?",
                                           (self.store.now(), identity.legacy_attempt_id))
            self.store.record_runner_research_result(identity.legacy_attempt_id, identity.to_dict()['research_context'], outcome='returned')
            self.store.connection.execute('UPDATE recovery_attempts SET untrusted=1 WHERE binding_sha=?', (identity.binding_sha,))
            self.store.connection.execute("UPDATE runner_operations SET state='imported',imported_at=? WHERE operation_id=?",
                                           (self.store.now(), identity.operation_id))
            self._fault('import_before_commit')
        self._fault('import_after_commit')
        return text

    def _fault(self, point):
        """Isolated subprocess commit-boundary test hook."""


def reconcile_pending_runner_result(store, runner_client, binding, *, expected_runner_uid):
    with store._lock:
        row = store.connection.execute("SELECT identity_json FROM runner_operations WHERE binding_sha=? AND state='admitted' ORDER BY rowid LIMIT 1",
                                       (digest(binding),)).fetchone()
    if row is None:
        return False
    try:
        identity = validate_identity(json.loads(row['identity_json']))
        adapter = RunnerRecoveryAdapter(store, runner_client, expected_runner_uid=expected_runner_uid,
                                        runner_store_id=store.get_state('runner_receipt_store_id', ''))
        evidence = runner_client.lookup_receipt(identity, expected_runner_uid=expected_runner_uid, timeout_seconds=5)
        adapter.import_result(identity, evidence)
        return True
    except Exception:
        return False
