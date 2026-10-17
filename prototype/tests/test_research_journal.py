import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from andrewbot.controller import Controller
from andrewbot.memory import ResearchJournal
from andrewbot.memory_backup import create_encrypted_snapshot, restore_snapshot
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store


class JournalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'controller.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.journal = ResearchJournal(self.store)

    def save(self, **fields):
        return self.journal.execute('save', fields)

    def test_create_edit_link_and_delete_preserve_other_memories(self):
        memory = self.store.create_memory_record('interest', 'An existing interest.', 'andrew', [], [])
        first = self.save(question='Why does a fixture behave this way?', doubts='I do not know yet.', next_step='Read the second source.')
        child = self.save(question='Does the same observation hold elsewhere?', parent_id=first['record_id'])
        edited = self.save(id=first['record_id'], revision=1, findings='The first source describes a mechanism.',
                           sources=['https://example.org/source'])
        self.assertEqual(edited['revision'], 2)
        self.assertEqual(edited['doubts'], first['doubts'])
        with self.assertRaises(ValueError):
            self.save(id=first['record_id'], revision=1, findings='Stale edit')
        with self.assertRaises(ValueError):
            self.journal.execute('delete', {'id': first['record_id'], 'revision': 1})
        self.assertEqual(self.store.research_entry(child['record_id'])['revision'], 1)
        self.journal.execute('focus', {'id': first['record_id']})
        self.store.transition_state({'sleeping': '1'}, 'Andrew requested timed sleep', stop_note='Return when rested.')
        self.assertIsNotNone(self.store.research_last_checkpoint(first['record_id']))
        self.store.set_state('sleeping', '0')
        self.journal.execute('delete', {'id': first['record_id'], 'revision': 2})
        self.assertIsNone(self.store.research_entry(first['record_id']))
        self.assertIsNone(self.store.research_last_checkpoint(first['record_id']))
        child_after = self.store.research_entry(child['record_id'])
        self.assertIsNone(child_after['parent_id'])
        self.assertEqual(child_after['revision'], 2)
        self.assertIsNotNone(self.store.connection.execute('SELECT 1 FROM memory_records WHERE record_id=?', (memory,)).fetchone())
        serialized_events = json.dumps(self.store.research_observations())
        self.assertNotIn(first['question'], serialized_events)
        self.assertNotIn(first['next_step'], serialized_events)

    def test_concurrent_edits_require_fresh_revision(self):
        first = self.save(question='A shared question?')
        barrier = threading.Barrier(2)
        results = []
        def edit(value):
            barrier.wait()
            try:
                self.save(id=first['record_id'], revision=1, findings=value)
                results.append('saved')
            except ValueError:
                results.append('stale')
        workers = [threading.Thread(target=edit, args=(value,)) for value in ('first', 'second')]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertCountEqual(results, ['saved', 'stale'])
        self.assertEqual(self.store.research_entry(first['record_id'])['revision'], 2)

    def test_parent_cycle_is_rejected(self):
        first = self.save(question='First?')
        second = self.save(question='Second?', parent_id=first['record_id'])
        with self.assertRaises(ValueError):
            self.save(id=first['record_id'], revision=1, parent_id=second['record_id'])

    def test_rejects_sensitive_or_invalid_fields_without_writing(self):
        for fields in ({'question': 'password=not-for-memory'}, {'question': '<think>private</think>'},
                       {'question': ''}, {'question': 'x' * 501}, {'question': 'Q?', 'sources': ['https://user:pass@example.org']},
                       {'question': 'Q?', 'sources': ['file:///etc/passwd']}, {'question': 'Q?', 'status': []},
                       {'question': 'Q?', 'parent_id': '0' * 32}, {'question': 'Q?', 'source': 'owner'},
                       {'question': 'Q?', 'revision': True}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.save(**fields)
        self.assertEqual(self.journal.execute('list', {})['entries'], [])
        self.assertEqual(self.store.research_observations(), [])

    def test_pause_sleep_and_errors_block_mutations_but_allow_reading(self):
        first = self.save(question='Q?')
        for key, value in [('paused', '1'), ('sleeping', '1'), ('autonomy_error', 'timeout')]:
            self.store.set_state(key, value)
            with self.subTest(key=key):
                self.assertEqual(self.journal.execute('get', {'id': first['record_id']})['question'], 'Q?')
                for operation, args in [('save', {'question': 'Another?'}), ('focus', {'id': first['record_id']}),
                                        ('delete', {'id': first['record_id'], 'revision': 1})]:
                    with self.assertRaises(ValueError):
                        self.journal.execute(operation, args)
            self.store.set_state(key, '' if key == 'autonomy_error' else '0')

    def test_unicode_context_bounds_and_pagination_include_previous_focus(self):
        entries = [self.save(question=f'{index} Почему?' + '界' * 150, findings='界' * 1000, next_step='界' * 500)
                   for index in range(14)]
        self.journal.execute('focus', {'id': entries[-1]['record_id']})
        context = self.journal.wake_context()
        self.assertLessEqual(len(context.encode('utf-8')), 12_000)
        self.assertIn(entries[-1]['record_id'], context)
        self.assertIn('"more_available": true', context)
        page = self.journal.execute('list', {'limit': 3})
        found = page['entries'][:]
        while page['next_after'] is not None:
            page = self.journal.execute('list', {'after': page['next_after'], 'limit': 3})
            found.extend(page['entries'])
        self.assertEqual([row['record_id'] for row in found], [row['record_id'] for row in entries])
        self.assertNotIn('findings', found[0])
        self.assertEqual(self.journal.execute('get', {'id': entries[0]['record_id']})['findings'], '界' * 1000)

    def test_restart_and_encrypted_backup_restore_entries_links_and_stop_context(self):
        first = self.save(question='What should I revisit?', next_step='Compare the two examples.')
        self.journal.execute('focus', {'id': first['record_id']})
        self.store.transition_state({'sleeping': '1'}, 'Andrew requested timed sleep', stop_note='A natural stopping point.')
        key = b'x' * 32
        snapshot = create_encrypted_snapshot(self.store, self.path.parent / 'backups', key)
        self.assertNotIn(first['question'].encode(), snapshot.read_bytes())
        restored = self.path.parent / 'restored.sqlite3'
        restore_snapshot(snapshot, restored, key)
        self.store.close()
        self.store = Store(restored)
        self.journal = ResearchJournal(self.store)
        context = self.journal.wake_context()
        self.assertIn('Compare the two examples.', context)
        self.assertIn('A natural stopping point.', context)
        self.assertIn('voluntary_sleep', context)
        self.assertEqual(len(self.store.research_page()['entries']), 1)

    def test_closed_entries_leave_focus_and_can_be_reopened(self):
        first = self.save(question='Finished?')
        self.journal.execute('focus', {'id': first['record_id']})
        self.save(id=first['record_id'], revision=1, status='closed')
        self.assertNotIn(first['record_id'], self.journal.wake_context())
        self.assertEqual(self.store.get_state('research_focus_id', ''), '')
        with self.assertRaises(ValueError):
            self.journal.execute('focus', {'id': first['record_id']})
        reopened = self.save(id=first['record_id'], revision=2, status='open')
        self.assertEqual(reopened['revision'], 3)
        self.assertIn(first['record_id'], self.journal.wake_context())


class ResearchLifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'controller.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.now = datetime.now(timezone.utc)
        outer = self
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return outer.now.astimezone(tz) if tz else outer.now.replace(tzinfo=None)
        patcher = patch('andrewbot.controller.datetime', Clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []
        self.action = lambda: None
        class Agent:
            def autonomous_turn(self, prompt, timeout):
                outer.calls.append(prompt)
                outer.action()
        self.agent = Agent()
        self.controller = Controller(self.store, self.agent, 17, autonomous_enabled=True)
        self.runner = SessionRunner(self.store, self.controller, self.agent, enabled=True)

    def tool(self, name, **args):
        return self.controller.execute_tool(name, args)

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.controller.store = self.store
        self.runner = SessionRunner(self.store, self.controller, self.agent, enabled=True)
        self.runner.recover()

    def test_three_independent_wakes_resume_a_question_and_allow_sleep_without_progress(self):
        chosen = []
        artifact = self.path.parent / 'fixture-notes.txt'
        class FixtureRuntime:
            def execute(self, name, arguments):
                if name == 'shell.run':
                    artifact.write_text('A fixture artifact created through the controller boundary.')
                return 'Status: ok\nFixture tool returned; no live website was opened.'
        self.controller.tool_runtime = FixtureRuntime()
        def first_wake():
            entry = json.loads(self.tool('research.save', question='How does a fixture preserve state?',
                                         next_step='Compare another fixture after waking.'))
            chosen.append(entry['record_id'])
            self.tool('research.focus', id=chosen[0])
            self.tool('browser.open', url='https://example.org')
            self.tool('save_diary_entry', content='I checked a fixture and left a question to revisit.')
            self.tool('sleep', minutes=1, reason='I choose to return later.')
        self.action = first_wake
        self.store.set_state('session_start_requested', 'scheduled_wake')
        self.assertTrue(self.runner.tick(self.now))
        self.reopen()
        self.now = datetime.fromisoformat(self.store.get_state('sleep_until'))
        self.assertTrue(self.controller.check_scheduled_wake(self.now))
        def second_wake():
            self.assertIn('Compare another fixture after waking.', self.calls[-1])
            self.assertIn('I choose to return later.', self.calls[-1])
            self.tool('research.focus', id=chosen[0])
            self.tool('research.save', id=chosen[0], revision=1, findings='The fixture returned saved state.',
                      doubts='Does another process behave the same way?', next_step='Compare a second process.')
            self.tool('research.save', question='What changes across another process?', parent_id=chosen[0])
            self.tool('shell.run', code='fixture creates notes')
            self.tool('sleep', minutes=1)
        self.action = second_wake
        self.assertTrue(self.runner.tick(self.now))
        self.assertTrue(artifact.is_file())
        self.reopen()
        self.now = datetime.fromisoformat(self.store.get_state('sleep_until'))
        self.assertTrue(self.controller.check_scheduled_wake(self.now))
        self.action = lambda: self.tool('sleep', minutes=1)
        self.assertTrue(self.runner.tick(self.now))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.store.pending_message_count(17), 0)
        self.assertEqual(len(self.store.research_page()['entries']), 2)
        events = self.store.research_observations(limit=200)
        self.assertTrue(any(row['kind'] == 'topic_resumed' and row['origin'] == 'autonomous' for row in events))
        self.assertEqual(sum(row['kind'] == 'voluntary_sleep' for row in events), 3)
        self.assertTrue(any(row['tool_name'] == 'browser.open' and row['origin'] == 'autonomous' for row in events))
        self.assertTrue(any(row['tool_name'] == 'shell.run' and row['outcome'] == 'returned' for row in events))
        self.assertFalse(any(row['kind'] == 'technical_failure' for row in events))
        self.assertEqual(self.store.research_last_checkpoint(chosen[0])['next_step'], 'Compare a second process.')

    def test_failure_preserves_intention_and_is_not_a_sleep_decision(self):
        entry = json.loads(self.tool('research.save', question='An unfinished question?', next_step='Read another source.'))
        def fail():
            self.tool('research.focus', id=entry['record_id'])
            raise TimeoutError('private failure payload')
        self.action = fail
        self.store.set_state('session_start_requested', 'scheduled_wake')
        with self.assertLogs(level='ERROR'):
            self.assertFalse(self.runner.tick(self.now))
        checkpoint = self.store.research_last_checkpoint(entry['record_id'])
        self.assertEqual(checkpoint['stop_type'], 'technical_failure')
        self.assertEqual(checkpoint['next_step'], 'Read another source.')
        self.assertEqual(checkpoint['stop_note'], '')
        self.assertNotEqual(self.store.get_state('sleeping'), '1')
        self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(len(self.calls), 1)

    def test_owner_pause_during_a_tool_preserves_origin_and_the_continuation(self):
        entry = json.loads(self.tool('research.save', question='Continue this?', next_step='Read the comparison.'))
        outer = self
        class Runtime:
            def execute(self, name, arguments):
                outer.controller._command('/pause')
                return 'Fixture returned during owner pause.'
        self.controller.tool_runtime = Runtime()
        def work():
            self.tool('research.focus', id=entry['record_id'])
            self.tool('browser.open', url='https://example.org')
        self.action = work
        self.store.set_state('session_start_requested', 'scheduled_wake')
        self.assertFalse(self.runner.tick(self.now))
        checkpoint = self.store.research_last_checkpoint(entry['record_id'])
        self.assertEqual(checkpoint['stop_type'], 'owner_pause')
        self.assertEqual(checkpoint['next_step'], 'Read the comparison.')
        events = self.store.research_observations(limit=100)
        tool = next(row for row in events if row['tool_name'] == 'browser.open')
        self.assertEqual(tool['origin'], 'autonomous')
        self.assertTrue(tool['cycle_id'])
        self.assertTrue(any(row['kind'] == 'owner_pause' and row['origin'] == 'owner' for row in events))
        self.assertFalse(any(row['kind'] == 'voluntary_sleep' for row in events))
        self.assertEqual(self.store.get_state('paused'), '1')

    def test_sleep_needs_no_question_or_explanation_and_owner_report_is_local(self):
        self.tool('sleep', minutes=1)
        checkpoint = self.store.connection.execute('SELECT * FROM research_checkpoints ORDER BY id DESC LIMIT 1').fetchone()
        self.assertIsNone(checkpoint['entry_id'])
        self.assertEqual(checkpoint['next_step'], '')
        self.assertEqual(checkpoint['stop_note'], '')
        self.assertEqual(self.store.research_page()['entries'], [])
        self.assertIn('No saved questions', self.controller._command('/research'))
        self.assertIn('voluntary_sleep', self.controller._command('/research', '/research events'))
        self.assertEqual(self.calls, [])
