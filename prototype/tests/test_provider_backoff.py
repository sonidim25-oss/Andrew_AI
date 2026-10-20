"""Rejections authorize only their exact retry, never an unknown newer attempt."""
import time
import unittest
from unittest.mock import patch

from test_recovery_dispatch import RecoveryDispatchTests
from andrewbot.model import ProviderRequestError
from andrewbot.recovery import RecoveryCoordinator, RecoveryInterrupted
from andrewbot.recovery_records import RecoveryRecords
from andrewbot.store import Store


class ProviderBackoffTests(unittest.TestCase):
    setUp=RecoveryDispatchTests.setUp
    metadata=RecoveryDispatchTests.metadata

    def test_confirmed_journaled_provider_can_resume_with_unrelated_legacy_costs(self):
        metadata=self.metadata()
        self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],200)
        self.store.reserve_usage('provider-'+'a'*64,'2026-10','2026-10-17',.01)
        coordinator=RecoveryCoordinator(self.store,self.controller,enabled=True,config_sha='b'*64)
        coordinator._check_outcomes(self.binding)
        unknown=self.metadata(effect_id='unknown-new-provider')
        self.provider.complete.side_effect=TimeoutError()
        self.gateway.complete('Bearer fixture-token',self.payload,recovery=unknown)
        with self.assertRaises(RecoveryInterrupted):coordinator._check_outcomes(self.binding)
    def reject(self, *, delay=20):
        metadata=self.metadata()
        self.provider.complete.side_effect=ProviderRequestError('http_error',429,retry_after=delay)
        self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],429)
        proof=self.records.effect_result(metadata['admission_id'])
        self.assertEqual(proof['state'],'rejected')
        return metadata,proof

    def test_rate_limit_survives_controller_restart_and_settles_once(self):
        metadata,proof=self.reject()
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM usage').fetchone()[0],0)
        self.store.close();self.store=Store(self.path);self.gateway.store=self.store
        self.records=RecoveryRecords(self.store)
        self.assertEqual(self.records.effect_result(metadata['admission_id']),proof)
        self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],429)
        self.assertEqual(self.provider.complete.call_count,1)
        self.provider.complete.side_effect=None
        with patch('time.time',return_value=proof['retry_at_ms']/1000+1):
            self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],200)
        self.assertEqual(self.provider.complete.call_count,2)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM usage').fetchone()[0],1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM recovery_provider_rejections').fetchone()[0],1)

    def test_lost_retry_cannot_use_prior_rejection_to_replay(self):
        metadata,proof=self.reject()
        self.provider.complete.side_effect=TimeoutError('PRIVATE')
        with patch('time.time',return_value=proof['retry_at_ms']/1000+1):
            for _ in range(2):
                self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],422)
        self.assertEqual(self.provider.complete.call_count,2)
        self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'],'uncertain')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],1)

    def test_rejection_can_transfer_to_new_scope_but_old_scope_cannot_retry(self):
        metadata,proof=self.reject()
        scope=self.records.admit_scope(self.binding)
        claim={**self.claim,'admission_id':scope['admission_id'],'fence':2}
        admission=self.records.admit_effect(self.binding,claim,metadata['effect'],scope['scope_token'])
        current={**admission,'binding':self.binding,'claim':claim,'scope_token':scope['scope_token']}
        self.provider.complete.side_effect=None
        with patch('time.time',return_value=proof['retry_at_ms']/1000+1):
            self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],423)
            self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=current)[0],200)
        self.assertEqual(self.provider.complete.call_count,2)

    def test_pause_revision_pending_owner_and_expired_lease_block_retry(self):
        metadata,proof=self.reject()
        with patch('time.time',return_value=proof['retry_at_ms']/1000+1):
            for key,value in [('paused','1'),('sleeping','1'),('owner_control_revision','1'),('local_recovery_restore_required','1')]:
                old=self.store.get_state(key,'0');self.store.set_state(key,value)
                self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],423)
                self.store.set_state(key,old)
            message=self.store.add_message(17,'user','Stop and answer me',queued=True)
            self.store.set_state('local_recovery_owner_id','17')
            self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],423)
            with self.store.connection:self.store.connection.execute('UPDATE messages SET queued=0 WHERE id=?',(message,))
            expired={**metadata,'claim':{**metadata['claim'],'expires_monotonic_ms':0}}
            self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=expired)[0],423)
        self.assertEqual(self.provider.complete.call_count,1)

    def test_backoff_increases_and_never_shortens_provider_delay(self):
        metadata,first=self.reject(delay=900)
        self.assertGreaterEqual(first['retry_at_ms']-first['rejected_at_ms'],900000)
        with patch('time.time',return_value=first['retry_at_ms']/1000+1):
            self.provider.complete.side_effect=ProviderRequestError('http_error',429)
            self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],429)
        second=self.records.effect_result(metadata['admission_id'])
        self.assertEqual(second['attempt'],2)
        self.assertEqual(second['retry_at_ms']-second['rejected_at_ms'],10000)

    def test_legacy_costs_do_not_block_proven_rejection_but_unknown_retry_does(self):
        metadata,proof=self.reject()
        self.store.reserve_usage('provider-'+'a'*64,'2026-10','2026-10-17',.01)
        coordinator=RecoveryCoordinator(self.store,self.controller,enabled=True,config_sha='b'*64)
        coordinator._check_outcomes(self.binding)
        self.provider.complete.side_effect=TimeoutError()
        with patch('time.time',return_value=proof['retry_at_ms']/1000+1):
            self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)
        with self.assertRaises(RecoveryInterrupted):coordinator._check_outcomes(self.binding)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],2)

    def test_status_exposes_retry_time_without_clearing_pause(self):
        self.reject()
        status=self.controller._command('/status')
        self.assertIn('HTTP 429',status)
        self.assertIn('retry eligible at',status)

    def test_restore_preserves_rejections_and_refuses_conflicting_evidence(self):
        from andrewbot.memory_backup import _preserve_newer_ledgers
        old=Store(self.path.with_name('old.db'));self.addCleanup(old.close)
        metadata,proof=self.reject()
        self.assertTrue(_preserve_newer_ledgers(old,self.path))
        self.assertEqual(RecoveryRecords(old).effect_result(metadata['admission_id']),proof)
        with self.store.connection:
            self.store.connection.execute('UPDATE recovery_provider_rejections SET retry_at_ms=retry_at_ms+1')
        with self.assertRaisesRegex(ValueError,'rejection receipts conflict'):_preserve_newer_ledgers(old,self.path)


del RecoveryDispatchTests
