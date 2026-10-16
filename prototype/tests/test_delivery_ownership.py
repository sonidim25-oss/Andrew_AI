import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from andrewbot.store import Store


class DeliveryOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fixture.db'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)

    def test_claim_without_network_attempt_is_pending_after_restart(self):
        self.store.queue_reply(17, 'fixture')
        first = self.store.claim_reply('normal')
        self.assertIsNone(self.store.claim_reply('normal'))
        self.assertFalse(self.store.finish_reply(first['id'], 'wrong-token'))
        self.assertFalse(self.store.hold_reply(first['id'], 'wrong-token'))
        self.reopen()
        self.store.recover_reply_claims()
        second = self.store.claim_reply('normal')
        self.assertEqual(second['id'], first['id'])
        self.assertNotEqual(second['claim_token'], first['claim_token'])
        self.assertFalse(self.store.hold_reply(first['id'], first['claim_token']))

    def test_lost_ack_is_held_and_never_claimed_again(self):
        self.store.queue_reply(17, 'fixture')
        row = self.store.claim_reply('normal')
        self.store.prepare_telegram_delivery('reply', row['id'], row['text'])
        self.store.attempt_telegram_chunk('reply', row['id'], 0)
        self.reopen()
        self.store.recover_reply_claims()
        self.assertIsNone(self.store.claim_reply('normal'))
        self.assertEqual(self.store.next_reply()['delivery_state'], 'held')
        self.assertEqual(self.store.next_reply()['text'], 'fixture')

    def test_final_ack_is_finished_locally_after_restart(self):
        self.store.queue_reply(17, 'fixture')
        row = self.store.claim_reply('normal')
        self.store.prepare_telegram_delivery('reply', row['id'], row['text'])
        self.store.attempt_telegram_chunk('reply', row['id'], 0)
        self.store.acknowledge_telegram_chunk('reply', row['id'], 0)
        self.reopen()
        self.store.recover_reply_claims()
        self.assertIsNone(self.store.next_reply())
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM telegram_delivery_progress').fetchone()[0], 0)

    def test_legacy_attempt_without_progress_is_conservatively_held(self):
        self.store.add_message(17, 'user', 'saved fixture', queued=True)
        memory_id = self.store.create_memory_record('experience', 'saved memory', 'owner')
        self.store.record_usage('2026-10', '2026-10-16', 0.02, 100, 20)
        self.store.close()
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TABLE telegram_outbox')
            db.execute('CREATE TABLE telegram_outbox(id INTEGER PRIMARY KEY,chat_id INTEGER NOT NULL,text TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0)')
            db.execute("INSERT INTO telegram_outbox VALUES(7,17,'fixture',1)")
        self.store = Store(self.path)
        self.store.recover_reply_claims()
        row = self.store.next_reply()
        self.assertEqual((row['id'], row['lane'], row['delivery_state'], row['attempts']), (7, 'normal', 'held', 1))
        self.assertIsNone(self.store.claim_reply('normal'))
        self.assertEqual(self.store.connection.execute('SELECT cost_usd,input_tokens,output_tokens FROM usage').fetchone()[:], (0.02, 100, 20))
        self.assertEqual(self.store.connection.execute('SELECT content,queued FROM messages').fetchone()[:], ('saved fixture', 1))
        self.assertEqual(self.store.connection.execute('SELECT content FROM memory_records WHERE record_id=?', (memory_id,)).fetchone()[0], 'saved memory')

    def test_two_connections_cannot_claim_the_same_reply(self):
        self.store.queue_reply(17, 'fixture')
        other = Store(self.path)
        try:
            barrier = threading.Barrier(2)
            def claim(store):
                barrier.wait(timeout=2)
                return store.claim_reply('normal')
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(claim, store) for store in (self.store, other)]
                results = [future.result(timeout=3) for future in futures]
            self.assertEqual(sum(row is not None for row in results), 1)
        finally:
            other.close()

    def test_each_lane_is_fifo_and_validation_precedes_mutation(self):
        for lane, text in (('normal', 'first normal'), ('system', 'first system'), ('normal', 'second normal')):
            self.store.queue_reply(17, text, lane=lane)
        for lane, expected in (('normal', 'first normal'), ('system', 'first system'), ('normal', 'second normal')):
            row = self.store.claim_reply(lane)
            self.assertEqual(row['text'], expected)
            self.store.prepare_telegram_delivery('reply', row['id'], row['text'])
            self.store.attempt_telegram_chunk('reply', row['id'], 0)
            self.store.acknowledge_telegram_chunk('reply', row['id'], 0)
            self.assertTrue(self.store.finish_reply(row['id'], row['claim_token']))
        with self.assertRaises(ValueError):
            self.store.queue_reply(17, 'invalid', lane='untrusted')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM telegram_outbox').fetchone()[0], 0)

    def test_lane_has_only_one_active_sender_even_with_multiple_pending_rows(self):
        self.store.queue_reply(17, 'first')
        self.store.queue_reply(17, 'second')
        self.store.queue_reply(17, 'system', lane='system')
        self.assertEqual(self.store.claim_reply('normal')['text'], 'first')
        self.assertIsNone(self.store.claim_reply('normal'))
        self.assertEqual(self.store.claim_reply('system')['text'], 'system')

    def test_acknowledged_prefix_can_resume_only_the_definitively_unsent_suffix(self):
        from andrewbot.telegram import deliver_saved_message
        from types import SimpleNamespace
        text = 'x' * 5000
        self.store.queue_reply(17, text)
        row = self.store.claim_reply('normal')
        self.store.prepare_telegram_delivery('reply', row['id'], text)
        self.store.attempt_telegram_chunk('reply', row['id'], 0)
        self.store.acknowledge_telegram_chunk('reply', row['id'], 0)
        self.reopen()
        self.store.recover_reply_claims()
        row = self.store.claim_reply('normal')
        sent = []
        api = SimpleNamespace(send_message=lambda owner, part: sent.append(part))
        self.assertTrue(deliver_saved_message(api, self.store, 'reply', row['id'], 17, text))
        self.assertEqual(sent, ['x' * 1200])
        self.assertTrue(self.store.finish_reply(row['id'], row['claim_token']))

    def test_shutdown_before_any_part_does_not_hold_unsent_work(self):
        from andrewbot.telegram import deliver_saved_message
        from types import SimpleNamespace
        self.store.queue_reply(17, 'fixture')
        row = self.store.claim_reply('normal')
        sent = []
        self.assertFalse(deliver_saved_message(SimpleNamespace(send_message=lambda *args: sent.append(args)),
            self.store, 'reply', row['id'], 17, row['text'], should_stop=lambda: True))
        self.assertEqual(sent, [])
        self.assertTrue(self.store.release_reply(row['id'], row['claim_token']))
        self.assertIsNotNone(self.store.claim_reply('normal'))

    def test_legacy_progress_migration_preserves_acknowledged_prefix_and_uncertainty(self):
        self.store.record_usage('2026-10', '2026-10-16', 0.03, 200, 30)
        self.store.close()
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TABLE telegram_outbox')
            db.execute('DROP TABLE telegram_delivery_progress')
            db.execute('CREATE TABLE telegram_outbox(id INTEGER PRIMARY KEY,chat_id INTEGER NOT NULL,text TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE telegram_delivery_progress(kind TEXT NOT NULL,record_id TEXT NOT NULL,content TEXT NOT NULL,next_chunk INTEGER NOT NULL DEFAULT 0,chunk_attempts INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(kind,record_id))')
            db.execute('INSERT INTO telegram_outbox VALUES(7,17,?,1)', ('x' * 5000,))
            db.execute("INSERT INTO telegram_delivery_progress VALUES('reply','7',?,1,1)", ('x' * 5000,))
        self.store = Store(self.path)
        self.store.recover_reply_claims()
        self.assertIsNone(self.store.claim_reply('normal'))
        progress = self.store.connection.execute('SELECT * FROM telegram_delivery_progress').fetchone()
        self.assertEqual((progress['next_chunk'], progress['chunk_attempts'], progress['retry_notice']), (1, 1, 0))
        self.assertEqual(progress['content'], 'x' * 5000)
        self.assertEqual(self.store.next_reply()['delivery_state'], 'held')
        self.assertEqual(self.store.connection.execute('SELECT cost_usd,input_tokens,output_tokens FROM usage').fetchone()[:], (0.03, 200, 30))
