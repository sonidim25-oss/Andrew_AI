from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import Mock

from andrewbot.controller import Controller
from andrewbot.recovery_records import RecoveryRecords, digest
from andrewbot.runner_protocol import CONTRACT, VERSION, validate_evidence
from andrewbot.runner_format import FORMATTER
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store

CONTROLLER_ID = '01234567-1234-4321-8123-123456789abc'
RUNNER_ID = '11234567-1234-4321-8123-123456789abc'


class EvidenceRunner:
    def __init__(self, store):
        self.store = store
        self.calls = 0
        self.lookups = 0
        self.receipt = None
        self.before_grant = lambda: None
        self.lose_reply = False
        self.capability_changes = {}

    def receipt_capabilities(self, *, expected_runner_uid):
        assert not self.store.connection.in_transaction
        assert self.store.connection.execute('SELECT 1 FROM runner_operations').fetchone()
        result = dict(contract=CONTRACT, version=VERSION, formatter=FORMATTER,
                    controller_store_id=CONTROLLER_ID, runner_store_id=RUNNER_ID,
                    execution_enabled=True, capacity_available=True, records=0)
        result.update(self.capability_changes)
        return result

    def execute_receipted(self, request, identity, *, expected_runner_uid, grant_factory, cancel_event=None):
        assert not self.store.connection.in_transaction
        self.before_grant()
        grant_factory(dict(contract=CONTRACT, version=VERSION, action='ready',
                           challenge='1' * 64, operation_sha=digest(identity.to_dict())))
        assert not self.store.connection.in_transaction
        self.calls += 1
        response = dict(status='ok', stdout='saved exact result', stderr='', artifacts=[], safe_error=None, visit=None)
        self.receipt = validate_evidence(dict(contract=CONTRACT, version=VERSION, identity=identity.to_dict(),
            identity_sha=digest(identity.to_dict()), state='result', response=response,
            response_sha=digest(response), cleanup_verified=True), identity)
        if self.lose_reply:
            raise TimeoutError('PRIVATE transport detail')
        return self.receipt

    def lookup_receipt(self, identity, *, expected_runner_uid, timeout_seconds=5):
        assert not self.store.connection.in_transaction
        assert timeout_seconds == 5
        self.lookups += 1
        return self.receipt


class RunnerRecoveryFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.db'
        self.store = Store(self.path); self.addCleanup(lambda: self.store.close())
        self.controller = Controller(self.store, Mock(), 17, autonomous_enabled=True)
        session = SessionRunner(self.store, self.controller, self.controller.model, enabled=True).start(
            'owner_wake', datetime.now(timezone.utc))
        self.store.begin_cycle(session, 'cycle-fixture', '2026-10-19')
        self.store.checkpoint_cycle_turn('cycle-fixture', 'model_started')
        runtime = dict(agent_id='agent-fixture', conversation_id='conversation-fixture')
        self.store.record_model_turn_receipt(session, 'cycle-fixture', runtime,
            dict(kind='prepared', request_id='consumer-fixture', client_message_id='input-fixture'))
        self.binding = dict(store_id='backend-store', **runtime, session_id=session, cycle_id='cycle-fixture',
                            client_message_id='input-fixture', consumer_id='consumer-fixture',
                            input_sha='a'*64, config_sha='b'*64, owner_revision='0')
        for key, value in dict(local_recovery_config_sha='b'*64, local_recovery_owner_id='17',
            local_recovery_controller_store_id=CONTROLLER_ID, runner_receipt_store_id=RUNNER_ID,
            runner_receipts_enabled='1', runner_receipt_backend_capability='1').items():
            self.store.set_state(key, value)
        self.records = RecoveryRecords(self.store); self.scope = self.records.admit_scope(self.binding)
        self.claim = dict(admission_id=self.scope['admission_id'], fence=1, claim_id='claim-fixture',
                          expires_monotonic_ms=time.monotonic()*1000+15000,
                          boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        self.name, self.arguments = 'python.run', {'code': 'print(42)'}
        self.effect = dict(kind='tool', effect_id='t-'+'d'*64,
                           request_sha=digest(dict(name=self.name, arguments=self.arguments)))
        authorization = self.records.admit_effect(self.binding, self.claim, self.effect, self.scope['scope_token'])
        self.metadata = dict(binding=self.binding, claim=self.claim, effect=self.effect,
                             scope_token=self.scope['scope_token'], **authorization)
        self.client = EvidenceRunner(self.store)
        from andrewbot.runner_recovery import RunnerRecoveryAdapter
        self.adapter = RunnerRecoveryAdapter(self.store, self.client, expected_runner_uid=os.getuid(), runner_store_id=RUNNER_ID)
        self.controller.runner_recovery = self.adapter

    def operation(self):
        row = self.store.connection.execute('SELECT * FROM runner_operations').fetchone()
        return dict(row) if row else None
