"""Media confirmations survive local crashes without another upload or generation."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock, patch

from test_media import MediaCase
from telegram_fixture import sent_media
from andrewbot.media import MediaService, MediaWorker
from andrewbot.telegram import TelegramMediaError, validate_sent_media


class MediaReceiptTests(MediaCase):
    def ready(self, key='one'):
        job = self.media.submit('voice', 'Private media text', key, owner_requested=True)
        self.media.claim()
        self.media.complete(job, b'ID3' + bytes(40), 'audio/mpeg', .001)
        return self.media.claim_delivery()

    def test_committed_receipt_survives_reopen_and_prevents_resend(self):
        row = self.ready()
        self.media.save_delivery_receipt(row, sent_media(42, row['mime'], 91))
        self.store.close()
        from andrewbot.store import Store
        self.store = Store(Path(self.tmp.name) / 'controller.sqlite3')
        self.media = MediaService(self.store, Path(self.tmp.name) / 'media', 42, self.media.policy)
        self.media.recover()
        self.assertEqual(self.media.job(row['id'])['delivery'], 'delivered')
        self.assertEqual(self.media.job(row['id'])['telegram_message_id'], 91)
        self.assertIsNone(self.media.claim_delivery())
        provider, telegram = Mock(), Mock()
        MediaWorker(self.media, provider, telegram).step()
        provider.generate.assert_not_called()
        telegram.send_media.assert_not_called()
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM telegram_outbox').fetchone()[0], 0)
        proof = dict(self.store.connection.execute('SELECT * FROM media_delivery_receipts').fetchone())
        self.assertNotIn('Private media text', json.dumps(proof))
        self.assertEqual(len(proof['file_identity_sha']), 64)

    def test_process_exit_after_receipt_commit_before_acknowledgement(self):
        row = self.ready()
        code = '''import json,os,sys
from andrewbot.store import Store
from andrewbot.media import MediaService,MediaPolicy
s=Store(sys.argv[1]);m=MediaService(s,sys.argv[2],42,MediaPolicy(enabled=True))
m.save_delivery_receipt(json.loads(sys.argv[3]),json.loads(sys.argv[4]))
os._exit(19)
'''
        result = subprocess.run([sys.executable, '-c', code, str(Path(self.tmp.name) / 'controller.sqlite3'),
            str(Path(self.tmp.name) / 'media'), json.dumps(row), json.dumps(sent_media(42, row['mime'], 92))],
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])), capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertEqual(self.media.job(row['id'])['delivery'], 'sending')
        self.assertEqual(self.media.reconcile_deliveries(startup=True), 1)
        self.assertEqual(self.media.job(row['id'])['telegram_message_id'], 92)

    def test_runtime_reconciliation_leaves_active_sender_alone_but_settles_held_proof(self):
        row = self.ready()
        self.media.save_delivery_receipt(row, sent_media(42, row['mime']))
        self.assertEqual(self.media.reconcile_deliveries(), 0)
        self.assertEqual(self.media.job(row['id'])['delivery'], 'sending')
        with self.store.connection:
            self.store.connection.execute("UPDATE media_jobs SET delivery='uncertain',error='delivery_interrupted'")
        self.assertEqual(self.media.reconcile_deliveries(), 1)
        self.assertEqual(self.media.job(row['id'])['delivery'], 'delivered')
        self.assertIsNone(self.media.job(row['id'])['error'])

    def test_receipt_does_not_settle_changed_identity_content_or_recipient(self):
        row = self.ready()
        self.media.save_delivery_receipt(row, sent_media(42, row['mime']))
        for field, replacement in (('delivery_attempt', 'different'), ('delivery_chat_id', 99),
                ('artifact_sha', 'different'), ('digest', 'different'), ('mime', 'image/png'), ('kind', 'image')):
            with self.subTest(field=field):
                with self.store.connection:
                    self.store.connection.execute('UPDATE media_jobs SET ' + field + '=? WHERE id=?', (replacement, row['id']))
                self.assertEqual(self.media.reconcile_deliveries(startup=True), 0)
                with self.store.connection:
                    self.store.connection.execute('UPDATE media_jobs SET ' + field + '=? WHERE id=?', (row[field], row['id']))
        other = MediaService(self.store, self.media.artifacts, 99, self.media.policy)
        self.assertEqual(other.reconcile_deliveries(startup=True), 0)
        self.assertEqual(self.media.reconcile_deliveries(startup=True), 1)

    def test_late_response_cannot_settle_a_different_attempt(self):
        old = self.ready()
        with self.store.connection:
            self.store.connection.execute("UPDATE media_jobs SET delivery='pending'")
        new = self.media.claim_delivery()
        self.assertNotEqual(old['delivery_attempt'], new['delivery_attempt'])
        with self.assertRaises(ValueError):
            self.media.save_delivery_receipt(old, sent_media(42, old['mime']))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM media_delivery_receipts').fetchone()[0], 0)
        self.media.recover()
        self.assertEqual(self.media.job(old['id'])['delivery'], 'uncertain')

    def test_unknown_response_and_failed_receipt_storage_stay_held_without_retry(self):
        for failure in ('timeout', 'database'):
            with self.subTest(failure=failure):
                job = self.media.submit('voice', 'Keep held', failure, owner_requested=True)
                provider, telegram = Mock(), Mock()
                provider.generate.return_value = (b'ID3' + bytes(40), 'audio/mpeg', .001)
                telegram.send_media.return_value = sent_media(42, 'audio/mpeg')
                if failure == 'timeout':
                    telegram.send_media.side_effect = TimeoutError('Private response')
                    worker = MediaWorker(self.media, provider, telegram)
                    worker.step()
                else:
                    worker = MediaWorker(self.media, provider, telegram)
                    with patch.object(self.media, 'save_delivery_receipt', side_effect=OSError('Private storage error')):
                        worker.step()
                self.assertEqual(self.media.job(job)['delivery'], 'uncertain')
                worker.step()
                self.media.recover()
                self.assertEqual(telegram.send_media.call_count, 1)
                self.assertEqual(provider.generate.call_count, 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM media_delivery_receipts').fetchone()[0], 0)

    def test_finalization_failure_consumes_positive_receipt_without_uncertain_notice(self):
        row = self.ready()
        # Return this already admitted job to the unsent queue to exercise the worker.
        with self.store.connection:
            self.store.connection.execute("UPDATE media_jobs SET delivery='pending'")
        telegram, provider = Mock(), Mock()
        telegram.send_media.return_value = sent_media(42, row['mime'])
        with patch.object(self.media, 'acknowledge', side_effect=OSError('local finalization failed')):
            MediaWorker(self.media, provider, telegram).step()
        self.assertEqual(self.media.job(row['id'])['delivery'], 'delivered')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM telegram_outbox').fetchone()[0], 0)
        self.assertEqual(telegram.send_media.call_count, 1)

    def test_restore_halt_preserves_receipt_and_existing_unknown_state(self):
        row = self.ready()
        self.media.save_delivery_receipt(row, sent_media(42, row['mime']))
        self.store.set_state('restore_recovery_required', '1')
        self.assertEqual(self.media.reconcile_deliveries(startup=True), 0)
        self.assertEqual(self.media.job(row['id'])['delivery'], 'sending')

    def test_legacy_sending_without_receipt_remains_uncertain(self):
        row = self.ready()
        with self.store.connection:
            self.store.connection.execute("UPDATE media_jobs SET delivery_attempt='',delivery_chat_id=NULL")
        self.media.recover()
        self.assertEqual(self.media.job(row['id'])['delivery'], 'uncertain')
        self.assertIsNone(self.media.claim_delivery())
        with self.assertRaises(ValueError):
            self.media.acknowledge(row['id'], 1)

    def test_response_requires_positive_id_expected_chat_and_expected_media(self):
        voice = sent_media(42, 'audio/mpeg')
        photo = sent_media(42, 'image/png')
        self.assertTrue(validate_sent_media(voice, 42, 'audio/mpeg'))
        self.assertTrue(validate_sent_media(photo, 42, 'image/png'))
        for response in ({'message_id': 1}, voice | {'message_id': True}, voice | {'message_id': 0},
                voice | {'chat': {'id': 99}}, voice | {'voice': {}}, photo,
                voice | {'voice': {'file_id': 'PRIVATE', 'file_unique_id': ''}}):
            with self.subTest(response=response):
                with self.assertRaises(TelegramMediaError) as caught:
                    validate_sent_media(response, 42, 'audio/mpeg')
                self.assertNotIn('PRIVATE', str(caught.exception))
