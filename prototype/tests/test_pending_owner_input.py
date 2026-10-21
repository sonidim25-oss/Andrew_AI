import json
from pathlib import Path
import tempfile
import unittest

from andrewbot.store import Store


class PendingOwnerInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'controller.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def update(self, update_id, text='hello', owner=17, **fields):
        message = dict(text=text, **{'from': {'id': owner}},
                       chat={'id': owner, 'type': 'private'})
        message.update(fields)
        return {'update_id': update_id, 'message': message}

    def insert_many(self, updates):
        with self.store.connection:
            self.store.connection.executemany('INSERT INTO telegram_inbox VALUES(?,?,?)',
                [(item['update_id'], json.dumps(item), self.store.now()) for item in updates])

    def assert_bounded_check(self, expected):
        # Deterministic SQLite instruction bound, independent of machine speed.
        # The inbox may be arbitrarily large; these cases need only a tiny prefix.
        self.store.connection.set_progress_handler(lambda: 1, 10000)
        try:
            self.assertEqual(self.store.pending_owner_input(17), expected)
        finally:
            self.store.connection.set_progress_handler(None, 0)

    def test_unrelated_inbox_backlog_does_not_require_a_full_scan(self):
        self.insert_many([self.update(index, owner=99) for index in range(5000)])
        self.assert_bounded_check(False)

    def test_first_owner_message_does_not_materialize_entire_matching_inbox(self):
        self.insert_many([self.update(0)] + [self.update(index, '/status') for index in range(1, 2000)])
        self.assert_bounded_check(True)

    def test_unqueued_message_history_does_not_require_a_full_scan(self):
        with self.store.connection:
            self.store.connection.executemany('INSERT INTO messages(owner_id,role,content,created_at,queued) VALUES(?,?,?,?,?)',
                [(17, 'user', 'old delivered message', self.store.now(), 0)] * 2000)
        self.assert_bounded_check(False)

    def test_local_commands_do_not_hide_model_input_beyond_first_page(self):
        self.insert_many([self.update(index, '/status') for index in range(100)] + [self.update(100)])
        self.assertTrue(self.store.pending_owner_input(17))
        self.assertTrue(self.store.claim_update(100))
        self.assertFalse(self.store.pending_owner_input(17))

    def test_forwarded_nonprivate_other_owner_and_empty_text_are_not_pending(self):
        self.insert_many([
            self.update(1, forward_origin=None), self.update(2, forward_from={}),
            self.update(3, forward_from_chat={}), self.update(4, forward_sender_name='name'),
            self.update(5, chat={'id': 17, 'type': 'group'}), self.update(6, owner=99),
            self.update(7, ' \t\n'), self.update(8, '/unknown'), self.update(9, '📊 Status'),
        ])
        self.assertFalse(self.store.pending_owner_input(17))

    def test_prompt_boundary_and_media_commands_keep_existing_routing(self):
        self.store.set_state('telegram_wake_prompt_until', 'active')
        self.store.set_state('telegram_wake_prompt_after_update_id', '10')
        self.insert_many([self.update(11, '09:00')])
        self.assertFalse(self.store.pending_owner_input(17))
        self.insert_many([self.update(12, '/voice_reply hello')])
        self.assertTrue(self.store.pending_owner_input(17))
        self.store.claim_update(12)
        self.insert_many([self.update(9, 'before prompt')])
        self.assertTrue(self.store.pending_owner_input(17))

    def test_queued_and_processing_inputs_count_but_uncertain_messages_do_not(self):
        for queued, expected in [(0, False), (1, True), (2, True), (3, False)]:
            with self.subTest(queued=queued), self.store.connection:
                self.store.connection.execute('DELETE FROM messages')
                self.store.connection.execute('INSERT INTO messages(owner_id,role,content,created_at,queued) VALUES(?,?,?,?,?)',
                                             (17, 'user', 'fixture', self.store.now(), queued))
                self.assertEqual(self.store.pending_owner_input(17), expected)

    def test_other_connection_inputs_are_visible_without_reopening(self):
        peer = Store(self.path)
        self.addCleanup(peer.close)
        self.assertFalse(self.store.pending_owner_input(17))
        peer.enqueue_update(self.update(1))
        self.assertTrue(self.store.pending_owner_input(17))
        peer.claim_update(1)
        self.assertFalse(self.store.pending_owner_input(17))

    def test_existing_database_gets_indexes_on_reopen_without_losing_inbox(self):
        self.insert_many([self.update(index, owner=99) for index in range(5000)] + [self.update(5000)])
        with self.store.connection:
            self.store.connection.execute('DROP INDEX telegram_inbox_owner')
            self.store.connection.execute('DROP INDEX messages_pending_owner')
        self.store.close()
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM telegram_inbox').fetchone()[0], 5001)
        self.assert_bounded_check(True)
