import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from andrewbot.appserver import LettaAgent
from andrewbot.controller import Controller
from andrewbot.memory import MemoryService
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store


class FoundingLetterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        self.addCleanup(self.store.close)
        self.letter = 'An owner-authored introduction for the new agent.'

    def test_archive_is_exact_searchable_immutable_and_idempotent(self):
        letter = self.store.ensure_founding_letter(self.letter)
        self.assertEqual(self.store.get_memory_record(letter['record_id'])['content'], self.letter)
        self.assertEqual(MemoryService(self.store).search('introduction')[0]['source'], 'owner')
        self.assertEqual(self.store.ensure_founding_letter('Different default'), letter)
        for operation in ('UPDATE memory_records SET content="changed" WHERE record_id=?',
                          'DELETE FROM memory_records WHERE record_id=?'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.connection.execute(operation, (letter['record_id'],))
            self.store.connection.rollback()
        self.assertEqual(self.store.get_memory_record(letter['record_id'])['content'], self.letter)

    def test_legacy_false_delivery_flag_is_not_trusted(self):
        self.store.set_state('founding_letter_delivered', '1')
        self.store.ensure_founding_letter(self.letter)
        self.assertEqual(self.store.get_state('founding_letter_delivered'), '0')

    def test_existing_owner_import_and_uncertain_queue_are_preserved(self):
        digest = hashlib.sha256(self.letter.encode()).hexdigest()
        record_id = 'owner-letter-' + digest
        # Reproduce the older data-only owner import without changing its text.
        self.store.connection.execute('INSERT INTO memory_records VALUES(?,?,?,?,?)',
                                      (record_id, 'experience', self.store.now(), self.letter, 'owner'))
        self.store.connection.execute('INSERT INTO memory_fts VALUES(?,?,?,?,?)',
                                      (record_id, 'experience', self.store.now(), self.letter, 'owner'))
        self.store.connection.commit()
        letter = self.store.ensure_founding_letter('Code default must not replace owner text')
        message_id = self.store.queue_founding_letter(17, letter)
        self.store.connection.execute('UPDATE messages SET queued=3 WHERE id=?', (message_id,))
        self.store.connection.commit()
        self.assertEqual(self.store.queue_founding_letter(17, letter), message_id)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute('SELECT queued FROM messages').fetchone()[0], 3)
        self.assertEqual(letter['content'], self.letter)

    def test_receipt_requires_persisted_user_text_and_supports_pagination(self):
        letter = self.store.ensure_founding_letter(self.letter)
        message_id = self.store.queue_founding_letter(17, letter)
        agent = LettaAgent(self.store, 'fixture-token', owner_id=17)
        agent.conversation_id = 'fixture-conversation'
        client = Mock()
        client.request.side_effect = [
            {'messages': [{'message_type': 'assistant_message', 'content': self.letter}],
             'has_more': True, 'next_before': 'older'},
            {'messages': [{'message_type': 'user_message', 'content': [{'type': 'text', 'text': 'Queued:\n' + self.letter}]}]},
        ]
        self.assertTrue(agent._verify_founding_letter(client, letter))
        self.assertEqual(client.request.call_args.args[0]['query']['before'], 'older')
        self.assertEqual(self.store.get_state('founding_letter_delivered'), '1')
        self.assertEqual(self.store.connection.execute('SELECT queued FROM messages WHERE id=?', (message_id,)).fetchone()[0], 0)

    def test_missing_receipt_does_not_set_delivered(self):
        letter = self.store.ensure_founding_letter(self.letter)
        agent = LettaAgent(self.store, 'fixture-token', owner_id=17)
        client = Mock()
        client.request.return_value = {'messages': []}
        self.assertFalse(agent._verify_founding_letter(client, letter))
        self.assertEqual(self.store.get_state('founding_letter_delivered'), '0')

    def test_status_distinguishes_archiving_from_verified_receipt(self):
        letter = self.store.ensure_founding_letter(self.letter)
        controller = Controller(self.store, Mock(), 17)
        self.assertIn('Owner letter: delivery pending', controller._command('/status'))
        self.store.confirm_founding_letter_delivery(letter)
        self.assertIn('Owner letter: receipt verified', controller._command('/status'))

    def test_autonomy_waits_until_letter_receipt_is_confirmed(self):
        letter = self.store.ensure_founding_letter(self.letter)
        self.store.set_state('session_start_requested', 'owner_wake')
        agent = Mock()
        controller = Controller(self.store, agent, 17, autonomous_enabled=True)
        runner = SessionRunner(self.store, controller, agent, enabled=True)
        self.assertFalse(runner.tick())
        agent.autonomous_turn.assert_not_called()
        self.store.confirm_founding_letter_delivery(letter)
        self.assertTrue(runner.tick())
        agent.autonomous_turn.assert_called_once()

    def test_initialization_archives_and_queues_without_claiming_delivery(self):
        store = self.store
        class Client:
            created = []
            def __init__(self, *args, **kwargs):
                pass
            def ensure_provider(self, token):
                pass
            def create_agent(self, body):
                self.created.append(body)
                return 'fixture-agent'
            def request(self, command, response_type):
                if command['type'] == 'agent_list':
                    return {'agents': []}
                if command['type'] == 'conversation_messages_list':
                    return {'messages': []}
                return {}
            def start_runtime(self, agent_id, conversation_id):
                return {'agent_id': agent_id, 'conversation_id': 'fixture-conversation'}
            def close(self):
                pass
        with patch('andrewbot.appserver.AppServerClient', Client), patch('andrewbot.appserver.gmail_permissions', return_value={'enabled': False}):
            agent = LettaAgent(store, 'fixture-token', owner_id=17)
            agent.founding_letter = self.letter
            agent.initialize()
            agent.initialize()
        self.assertNotIn('initial_message_sequence', Client.created[0])
        self.assertEqual(store.get_state('founding_letter_delivered'), '0')
        self.assertEqual(len(store.queued_messages(17)), 1)
