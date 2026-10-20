from backup_fixture import isolate_backup_adoption
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from andrewbot.appserver import AppServerClient
from andrewbot.controller import Controller
from andrewbot.memory import SessionHistory
from andrewbot.store import Store


class SessionHistoryTests(unittest.TestCase):
    def setUp(self):
        isolate_backup_adoption(self)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'controller.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.session_id = 'a' * 32
        self.history = SessionHistory(self.store, 17, session_id=self.session_id, conversation_id='fixture', turn_id='turn-1')

    def search(self, query, **options):
        return self.history.execute('session_search', {'query': query, **options})['results']

    def read(self, record_id, **options):
        return self.history.execute('session_read', {'id': record_id, **options})['record']

    def test_original_chat_is_searchable_separately_from_memory(self):
        message_id = self.store.add_message(17, 'user', 'Find sfc31.pdf and retain its checksum.')
        rows = self.search('sfc31.pdf')
        self.assertEqual(len(rows), 1)
        row = self.read(rows[0]['id'])
        self.assertEqual(row['content'], 'Find sfc31.pdf and retain its checksum.')
        self.assertEqual(row['source_message_id'], str(message_id))
        self.assertEqual(row['role'], 'user')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM memory_records').fetchone()[0], 0)

    def test_streaming_fragments_are_joined_and_survive_restart(self):
        self.history.capture({'kind': 'assistant_delta', 'source_id': 'reply', 'content': [{'type': 'text', 'text': 'The docu'}]})
        self.history.capture({'kind': 'assistant_delta', 'source_id': 'reply', 'content': [{'type': 'text', 'text': 'ment checksum is verified.'}]})
        self.history.capture({'kind': 'step_finished', 'complete': True})
        self.store.close()
        self.store = Store(self.path)
        self.history.store = self.store
        rows = self.search('document', session_id=self.session_id)
        self.assertEqual(len(rows), 1)
        row = self.read(rows[0]['id'])
        self.assertEqual(row['content'], 'The document checksum is verified.')
        self.assertEqual(row['complete'], 1)
        self.assertEqual(row['content_sha256'], hashlib.sha256(row['content'].encode()).hexdigest())
        self.assertEqual(row['conversation_id'], 'fixture')

    def test_failed_turn_keeps_partial_text_labelled(self):
        self.history.capture({'kind': 'assistant_delta', 'source_id': 'reply', 'content': 'Partial observation before disconnect.'})
        row = self.read(self.search('observation')[0]['id'])
        self.assertEqual(row['complete'], 0)

    def test_tool_output_keeps_source_reference_and_untrusted_notice(self):
        self.history.capture({'kind': 'tool_result', 'source_id': 'tool-call-1', 'tool_name': 'shell.run',
                              'content': 'Source PDF sfc31.pdf sha256 fixturechecksum.'})
        result = self.history.execute('session_search', {'query': 'fixturechecksum'})
        self.assertIn('untrusted', result['notice'])
        row = self.read(result['results'][0]['id'])
        self.assertEqual(row['role'], 'tool')
        self.assertEqual(row['tool_name'], 'shell.run')
        self.assertEqual(row['source_message_id'], 'tool:tool-call-1')

    def test_literal_search_cannot_escape_owner_or_session_filter(self):
        self.store.add_message(18, 'user', 'Private uniquecontent for another owner.')
        self.store.add_message(17, 'user', 'Visible uniquecontent for this owner.')
        rows = self.search('uniquecontent OR " OR 1=1 --')
        self.assertEqual(len(rows), 1)
        self.assertNotIn('Private', rows[0]['excerpt'])
        self.assertEqual(self.search('uniquecontent', session_id=self.session_id), [])
        other = SessionHistory(self.store, 18)
        hidden_id = other.execute('session_search', {'query': 'Private'})['results'][0]['id']
        with self.assertRaisesRegex(ValueError, 'not found'):
            self.read(hidden_id)

    def test_reader_paginates_without_losing_original_text(self):
        text = 'Страница PDF 🧭\n\x00' * 1200
        self.store.add_message(17, 'assistant', text)
        record_id = self.search('PDF')[0]['id']
        chunks, offset = [], 0
        while True:
            row = self.read(record_id, offset=offset, limit=733)
            chunks.append(row['content'])
            if row['next_offset'] is None:
                break
            offset = row['next_offset']
        self.assertEqual(''.join(chunks), text)

    def test_backfill_is_idempotent_and_does_not_invent_old_session_ids(self):
        with self.store.connection:
            self.store.connection.execute("INSERT INTO messages(owner_id,role,content,created_at) VALUES(17,'user','Historical telescope evidence','2026-10-01T00:00:00+00:00')")
        self.store.backfill_session_history()
        self.store.backfill_session_history()
        rows = self.search('telescope')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['session_id'], '')
        self.assertEqual(rows[0]['created_at'], '2026-10-01T00:00:00+00:00')

    def test_encrypted_backup_restores_archive_and_search_index(self):
        from andrewbot.memory_backup import create_encrypted_snapshot, restore_snapshot
        self.history.capture({'kind': 'tool_result', 'source_id': 'backup-call', 'tool_name': 'shell.run',
                              'content': 'Retained observatory evidence from a source document.'})
        key = bytes(range(32))
        snapshot = create_encrypted_snapshot(self.store, Path(self.directory.name) / 'backups', key)
        self.assertNotIn(b'observatory', snapshot.read_bytes())
        path = Path(self.directory.name) / 'restored.sqlite3'
        restore_snapshot(snapshot, path, key)
        restored = Store(path)
        try:
            history = SessionHistory(restored, 17)
            rows = history.execute('session_search', {'query': 'observatory'})['results']
            self.assertEqual(len(rows), 1)
            record = history.execute('session_read', {'id': rows[0]['id']})['record']
            self.assertIn('source document', record['content'])
            self.assertEqual(restored.get_state('restore_recovery_required'), '1')
        finally:
            restored.close()

    def test_credentials_and_private_tags_are_redacted_even_across_fragments(self):
        parts = ['Observation sk-', 'abcdefghijklmnop', ' <think>hidden private work', '</think> Public conclusion.']
        for part in parts:
            self.history.capture({'kind': 'assistant_delta', 'source_id': 'reply', 'content': part})
        self.history.capture({'kind': 'step_finished', 'complete': True})
        row = self.read(self.search('conclusion')[0]['id'])
        self.assertNotIn('abcdefghijklmnop', row['content'])
        self.assertNotIn('hidden private work', row['content'])
        self.assertEqual(row['redacted'], 1)
        self.assertEqual(self.search('abcdefghijklmnop'), [])

    def test_hidden_reasoning_parts_are_not_archived(self):
        self.history.capture({'kind': 'assistant_delta', 'source_id': 'reply', 'content': [
            {'type': 'thinking', 'text': 'Private hiddenmarker'}, {'type': 'text', 'text': 'Public observation.'}]})
        self.assertEqual(self.search('hiddenmarker'), [])
        self.assertEqual(len(self.search('observation')), 1)

    def test_completed_records_cannot_be_rewritten_by_replayed_fragment(self):
        self.history.capture({'kind': 'assistant_final', 'source_id': 'reply', 'content': 'Original evidence.'})
        self.history.capture({'kind': 'assistant_final', 'source_id': 'reply', 'content': 'Rewritten different claim.'})
        self.assertEqual(len(self.search('Original')), 1)
        self.assertEqual(self.search('Rewritten'), [])

    def test_history_tools_work_while_paused_and_do_not_call_a_model(self):
        model = Mock()
        controller = Controller(self.store, model, 17)
        self.store.add_message(17, 'user', 'Retained lunar evidence.')
        self.store.set_state('paused', '1')
        result = json.loads(controller.execute_tool('session_search', {'query': 'lunar'}))
        record_id = result['results'][0]['id']
        self.assertIn('lunar', controller.execute_tool('session_read', {'id': record_id}))
        model.reply.assert_not_called()

    def test_bad_search_arguments_are_rejected(self):
        for arguments in ({'query': 'x', 'limit': True}, {'query': 'x', 'owner_id': 18},
                          {'query': 'x', 'session_id': '../bad'}, {'query': ' '}, {'query': 'x' * 501}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.history.execute('session_search', arguments)

    def test_history_capture_failure_stops_before_sending_tool_response(self):
        client = AppServerClient.__new__(AppServerClient)
        class Connection:
            def __init__(self):
                self.sent = []
            def send_json(self, value):
                self.sent.append(value)
            def recv_json(self):
                return {'type': 'external_tool_call_request', 'request_id': 'request', 'tool_call_id': 'call',
                        'tool_name': 'check_budget', 'input': {}}
        client.connection = Connection()
        def fail(_event):
            raise OSError('fixture archive unavailable')
        client.history_sink = fail
        with self.assertRaises(OSError):
            client.send_user_turn({'agent_id': 'a', 'conversation_id': 'c'}, 'question', tool_handler=lambda *_: 'Fixture result')
        self.assertEqual([event['type'] for event in client.connection.sent], ['input'])
