from datetime import datetime, timedelta, timezone
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from andrewbot.diary import deliver_pending_diary
from andrewbot.store import Store
from andrewbot.telegram import TelegramAPI, TelegramNotSent, deliver_saved_message


class DiaryUnsentRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'controller.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
        self.clock = patch.object(Store, 'now', side_effect=lambda: self.now.isoformat(timespec='seconds'))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.entry = self.store.create_diary_entry('2026-10-21', 'Today I worked on a small experiment.')
        self.calls = []
        self.sent = []
        self.failures = {}
        self.api = TelegramAPI('fixture-token')
        def call(method, payload):
            self.calls.append(payload['text'])
            error = self.failures.get(len(self.calls))
            if error:
                raise error
            self.sent.append(payload['text'])
            return dict(message_id=len(self.calls), chat={'id': payload['chat_id']}, text=payload['text'])
        self.api.call = call

    def send(self):
        return deliver_pending_diary(self.api, self.store, 17)

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.store.recover_diary_claims()

    def test_real_dns_failure_is_retried_only_when_due_without_retry_notice(self):
        original = TelegramAPI('fixture-token', opener=lambda *a, **k: (_ for _ in ()).throw(
            URLError(socket.gaierror(-3, 'fixture DNS failure'))))
        self.assertEqual(deliver_pending_diary(original, self.store, 17), 0)
        self.assertEqual(self.store.diary_delivery_state(self.entry), 'pending')
        self.assertEqual(self.send(), 0)
        self.assertEqual(self.calls, [])
        self.now += timedelta(seconds=1)
        self.assertEqual(self.send(), 1)
        self.assertEqual(self.store.diary_delivery_state(self.entry), 'delivered')
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn('Delivery retry', self.sent[0])
        self.assertNotIn('uncertain', self.sent[0])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM diary_delivery_retries').fetchone()[0], 0)

    def test_backoff_survives_restart_and_doubles_to_sixty_seconds(self):
        for index, delay in enumerate((1, 2, 4, 8, 16, 32, 60, 60), start=1):
            self.failures[index] = TelegramNotSent('fixture not sent')
            self.assertEqual(self.send(), 0)
            self.assertEqual(len(self.calls), index)
            self.reopen()
            self.assertEqual(self.send(), 0)
            self.assertEqual(len(self.calls), index)
            self.now += timedelta(seconds=delay - .1)
            self.assertEqual(self.send(), 0)
            self.assertEqual(len(self.calls), index)
            self.now += timedelta(seconds=.1)
        self.assertEqual(self.send(), 1)
        self.assertEqual(len(self.sent), 1)

    def test_unsent_second_part_keeps_first_part_receipt_and_resumes_once(self):
        self.store.connection.execute('UPDATE diary_entries SET content=? WHERE entry_id=?',
                                     ('Today I reflected. ' + '🙂' * 2100, self.entry))
        self.store.connection.commit()
        self.failures[2] = TelegramNotSent('fixture not sent')
        self.assertEqual(self.send(), 0)
        first = self.sent[0]
        self.reopen()
        self.now += timedelta(seconds=1)
        self.assertEqual(self.send(), 1)
        self.assertEqual(self.sent.count(first), 1)
        self.assertEqual(len(self.sent), 2)
        self.assertFalse(any('Delivery retry' in text for text in self.sent))

    def test_ambiguous_failure_remains_held_after_cooldown_and_restart(self):
        self.failures[1] = TelegramNotSent('fixture not sent')
        self.send()
        self.now += timedelta(seconds=1)
        self.failures[2] = TimeoutError('fixture acknowledgement lost')
        self.send()
        self.assertEqual(self.store.diary_delivery_state(self.entry), 'uncertain')
        self.reopen()
        self.now += timedelta(days=1)
        self.assertEqual(self.send(), 0)
        self.assertEqual(len(self.calls), 2)

    def test_failed_revocation_keeps_uncertainty_and_never_retries(self):
        self.failures[1] = TelegramNotSent('fixture not sent')
        with patch.object(self.store, 'revoke_unsent_telegram_chunk', side_effect=ValueError('changed')):
            self.send()
        self.assertEqual(self.store.diary_delivery_state(self.entry), 'uncertain')
        self.now += timedelta(days=1)
        self.assertEqual(self.send(), 0)
        self.assertEqual(len(self.calls), 1)

    def test_two_store_connections_cannot_claim_same_due_retry(self):
        self.failures[1] = TelegramNotSent('fixture not sent')
        self.send()
        self.now += timedelta(seconds=1)
        peer = Store(self.path)
        self.addCleanup(peer.close)
        token = self.store.claim_diary_sender(self.entry)
        self.assertIsNotNone(token)
        self.assertIsNone(peer.claim_diary_sender(self.entry))

    def test_shutdown_does_not_attempt_or_consume_due_retry(self):
        self.failures[1] = TelegramNotSent('fixture not sent')
        self.send()
        self.now += timedelta(seconds=1)
        self.assertEqual(deliver_pending_diary(self.api, self.store, 17, should_stop=lambda: True), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.send(), 1)

    def test_restart_after_revocation_commit_releases_sender_without_losing_safe_retry(self):
        token = self.store.claim_diary_sender(self.entry)
        self.assertIsNotNone(token)
        self.failures[1] = TelegramNotSent('fixture not sent')
        with self.assertRaises(TelegramNotSent):
            deliver_saved_message(self.api, self.store, 'diary', self.entry, 17, 'saved exact text', diary_sender_token=token)
        # Simulate termination before finish_diary_sender releases its claim.
        self.assertEqual(self.store.diary_delivery_state(self.entry), 'pending')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM diary_delivery_claims').fetchone()[0], 1)
        self.reopen()
        self.assertEqual(self.send(), 0)
        self.now += timedelta(seconds=1)
        self.assertEqual(self.send(), 1)
        self.assertEqual(self.sent, ['saved exact text'])

    def test_retry_persistence_failure_rolls_back_revocation_and_keeps_the_entry_held(self):
        self.store.connection.executescript('''CREATE TRIGGER fail_retry BEFORE INSERT ON diary_delivery_retries
            BEGIN SELECT RAISE(ABORT,'fixture persistence failure'); END;''')
        self.failures[1] = TelegramNotSent('fixture not sent')
        self.send()
        self.assertEqual(self.store.diary_delivery_state(self.entry), 'uncertain')
        self.assertEqual(self.store.connection.execute('SELECT chunk_attempts FROM telegram_delivery_progress').fetchone()[0], 1)
        self.now += timedelta(days=1)
        self.assertEqual(self.send(), 0)
        self.assertEqual(len(self.calls), 1)

    def test_changed_sender_claim_cannot_requeue_the_stale_send(self):
        original = self.api.call
        def changed_sender(method, payload):
            with self.store.connection:
                self.store.connection.execute("UPDATE diary_delivery_claims SET token='new-sender' WHERE entry_id=?", (self.entry,))
            return original(method, payload)
        self.api.call = changed_sender
        self.failures[1] = TelegramNotSent('fixture not sent')
        self.send()
        self.assertEqual(self.store.diary_delivery_state(self.entry), 'uncertain')
        self.assertEqual(self.store.connection.execute('SELECT chunk_attempts FROM telegram_delivery_progress').fetchone()[0], 1)
        self.reopen()
        self.now += timedelta(days=1)
        self.assertEqual(self.send(), 0)
        self.assertEqual(len(self.calls), 1)
