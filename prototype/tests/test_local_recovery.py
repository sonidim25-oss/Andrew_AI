from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from andrewbot.controller import Controller
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store
from andrewbot.recovery import RecoveryCoordinator
from andrewbot.recovery_records import RecoveryRecords,digest


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'controller.sqlite3');self.addCleanup(self.store.close)
        self.controller=Controller(self.store,Mock(),17,autonomous_enabled=True)
        runner=SessionRunner(self.store,self.controller,self.controller.model,enabled=True)
        session=runner.start('owner_wake',datetime.now(timezone.utc))
        self.store.begin_cycle(session,'cycle-fixture','2026-10-18');self.store.checkpoint_cycle_turn('cycle-fixture','model_started')
        runtime={'agent_id':'agent-fixture','conversation_id':'conversation-fixture'}
        self.store.record_model_turn_receipt(session,'cycle-fixture',runtime,{'kind':'prepared','request_id':'consumer-fixture','client_message_id':'input-fixture'})
        self.binding={'store_id':'backend-store',**runtime,'session_id':session,'cycle_id':'cycle-fixture','client_message_id':'input-fixture',
            'consumer_id':'consumer-fixture','input_sha':'a'*64,'owner_revision':'0','config_sha':'b'*64}
        self.store.set_state('local_recovery_config_sha','b'*64)
        self.records=RecoveryRecords(self.store);self.scope=self.records.admit_scope(self.binding)
        self.client=Mock()
        self.client.capabilities.return_value={'contract':'local_durable_recovery_v1','version':1,'store_id':'backend-store'}
        state={'status':'found','binding':self.binding,'revision':7,'phase':'continuation_ready','state':'interrupted'}
        self.client.inspect.return_value=state;self.client.status.return_value=state
        self.coordinator=RecoveryCoordinator(self.store,self.controller,lambda:self.client,enabled=True,config_sha='b'*64)

    def test_disabled_startup_does_not_connect_or_resume(self):
        factory=Mock(side_effect=AssertionError('Unexpected transport'))
        coordinator=RecoveryCoordinator(self.store,self.controller,factory,enabled=False)
        self.assertEqual(coordinator.inspect_pending(),[]);self.assertFalse(coordinator.reconcile_one());factory.assert_not_called()

    def test_pause_sleep_restore_and_revision_block_before_claim(self):
        for key,value in [('paused','1'),('sleeping','1'),('restore_recovery_required','1'),('local_recovery_restore_required','1'),('owner_control_revision','1')]:
            previous=self.store.get_state(key,'0');self.store.set_state(key,value)
            self.assertFalse(self.coordinator.reconcile_one())
            self.assertEqual(self.store.get_state(key),value)
            self.store.set_state(key,previous)
        self.client.claim.assert_not_called();self.client.resume.assert_not_called()
        self.assertEqual(len(self.coordinator._pending()),1)

    def test_pending_owner_input_preempts_recovery_and_stays_queued(self):
        message=self.store.add_message(17,'user','Owner input has priority',queued=True)
        self.assertFalse(self.coordinator.reconcile_one());self.client.claim.assert_not_called()
        self.assertEqual(self.store.connection.execute('SELECT queued FROM messages WHERE id=?',(message,)).fetchone()[0],1)

    def test_unknown_outcome_keeps_reservation_and_original_lost_attempt(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE recovery_attempts SET backend_attempt_id='original-lost-attempt'")
            self.store.connection.execute('INSERT INTO budget_reservations VALUES(?,?,?,?,?)',('original-reservation','2026-10','2026-10-18',.01,self.store.now()))
        self.client.status.return_value={**self.client.inspect.return_value,'phase':'provider_admitted'}
        self.assertFalse(self.coordinator.reconcile_one())
        self.client.status.assert_called_once_with(self.binding,'original-lost-attempt')
        self.client.claim.assert_not_called();self.client.resume.assert_not_called()
        self.assertEqual(self.store.connection.execute('SELECT reservation_id FROM budget_reservations').fetchone()[0],'original-reservation')
        replacement=self.records.admit_scope(self.binding)
        self.assertEqual(self.store.connection.execute('SELECT backend_attempt_id FROM recovery_attempts WHERE admission_id=?',(replacement['admission_id'],)).fetchone()[0],'original-lost-attempt')

    def test_inspection_has_no_mutations_or_private_payloads(self):
        before='\n'.join(self.store.connection.iterdump())
        result=self.coordinator.inspect_pending()
        self.assertEqual(before,'\n'.join(self.store.connection.iterdump()))
        self.assertNotIn(self.scope['scope_token'],json.dumps(result));self.assertNotIn('token_sha',json.dumps(result))
        self.client.claim.assert_not_called();self.client.resume.assert_not_called()
