"""Exercise conversation, autonomy and error transitions through the real controller/store."""
import tempfile
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from andrewbot.controller import Controller
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store


class ConversationLifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'controller.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.now = datetime(2026, 10, 16, 14, tzinfo=timezone.utc)
        outer = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return outer.now.astimezone(tz) if tz else outer.now.replace(tzinfo=None)

        clock_patch = patch('andrewbot.controller.datetime', Clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.reply_calls = []
        self.autonomous_calls = []
        self.reply_action = None
        self.autonomous_action = None

        class Agent:
            def reply(self, messages, timeout=120):
                outer.reply_calls.append(messages)
                if outer.reply_action:
                    outer.reply_action()
                return 'A fixture reply.', {'accounted_by_gateway': True}

            def autonomous_turn(self, prompt, timeout):
                outer.autonomous_calls.append(prompt)
                if outer.autonomous_action:
                    outer.autonomous_action()

        self.agent = Agent()
        self.controller = Controller(self.store, self.agent, 17, autonomous_enabled=True, fallback_enabled=True)
        self.runner = SessionRunner(self.store, self.controller, self.agent, enabled=True)

    def message(self, text='Hello', update_id=1, owner_id=17):
        return {'update_id': update_id, 'message': {'from': {'id': owner_id},
                'chat': {'id': owner_id, 'type': 'private'}, 'text': text}}

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.controller.store = self.store
        self.runner = SessionRunner(self.store, self.controller, self.agent, enabled=True)
        self.runner.recover()

    def test_answer_waits_without_calls_then_resumes_after_restart(self):
        self.controller._command('/wake')
        self.assertEqual(self.controller.handle_update(self.message()), 'A fixture reply.')
        self.assertEqual(self.store.get_state('sleeping'), '0')
        self.assertIn('waiting for your follow-up', self.controller._command('/status'))
        self.reopen()
        self.assertFalse(self.runner.tick(self.now))
        self.now += timedelta(seconds=299)
        self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(len(self.reply_calls), 1)
        self.assertEqual(self.autonomous_calls, [])
        self.now += timedelta(seconds=1)
        self.assertTrue(self.runner.tick(self.now))
        self.assertIn('own interests or projects', self.autonomous_calls[0])
        self.assertIn('working independently', self.controller._command('/status'))

    def test_follow_up_extends_quiet_window_and_preserves_active_session(self):
        session = self.runner.start('owner_wake', self.now)
        self.controller.handle_update(self.message())
        self.now += timedelta(minutes=4)
        self.controller.handle_update(self.message('Follow-up', 2))
        self.now += timedelta(minutes=1)
        self.assertFalse(self.runner.tick(self.now))
        self.now += timedelta(minutes=4)
        self.assertTrue(self.runner.tick(self.now))
        self.assertEqual(self.runner.active_session_id, session)
        self.assertEqual(len(self.reply_calls), 2)
        self.assertEqual(len(self.autonomous_calls), 1)

    def test_restart_during_idle_conversation_preserves_waiting_session(self):
        session = self.runner.start('owner_wake', self.now)
        self.controller.handle_update(self.message())
        self.reopen()
        self.assertEqual(self.runner.active_session_id, session)
        self.assertFalse(self.runner.tick(self.now))
        self.assertIsNone(self.store.next_reply())
        self.now += timedelta(minutes=5)
        self.assertTrue(self.runner.tick(self.now))
        self.assertEqual(self.runner.active_session_id, session)

    def test_owner_input_beyond_first_inbox_page_prevents_independent_turn(self):
        self.controller._command('/wake')
        for update_id in range(1, 52):
            self.store.enqueue_update(self.message('noise', update_id, owner_id=99))
        self.store.enqueue_update(self.message('Owner priority', 52))
        self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(self.autonomous_calls, [])
        update = self.message('Owner priority', 52)
        self.assertTrue(self.store.claim_update(52))
        reply = self.controller.handle_update(update)
        self.store.complete_update(52, 17, reply)
        self.assertFalse(self.runner.tick(self.now))
        self.now += timedelta(minutes=5)
        self.assertTrue(self.runner.tick(self.now))
        self.assertEqual(len(self.reply_calls), 1)

    def test_message_arriving_during_independent_work_blocks_sleep(self):
        def receive_then_try_sleep():
            self.store.enqueue_update(self.message('I have a follow-up'))
            self.store.create_diary_entry('2026-10-16', 'I checked one small idea.')
            with self.assertRaisesRegex(ValueError, 'Stay available'):
                self.controller.execute_tool('sleep', {'minutes': 1})
        self.autonomous_action = receive_then_try_sleep
        self.controller._command('/wake')
        self.assertTrue(self.runner.tick(self.now))
        self.assertFalse(self.runner.tick(self.now))
        self.assertNotEqual(self.store.get_state('sleeping'), '1')
        self.assertEqual(len(self.autonomous_calls), 1)

    def test_voluntary_sleep_requires_quiet_and_saves_wake_notice_once(self):
        self.runner.start('owner_wake', self.now)
        self.store.create_diary_entry('2026-10-16', 'I checked one small idea.')
        self.controller.handle_update(self.message())
        for tool, args in [('sleep', {'minutes': 10}), ('wake_at', {'time': '09:00'})]:
            with self.subTest(tool=tool), self.assertRaisesRegex(ValueError, 'Stay available'):
                self.controller.execute_tool(tool, args)
        self.assertIsNone(self.store.next_reply())
        self.now += timedelta(minutes=5)
        self.autonomous_action = lambda: self.controller.execute_tool('sleep', {'minutes': 10})
        self.assertTrue(self.runner.tick(self.now))
        self.assertEqual(self.store.get_state('active_session_status'), 'agent_sleep')
        self.assertEqual(self.store.get_state('sleeping'), '1')
        notice = self.store.next_reply()
        self.assertIn('choosing to sleep', notice['text'])
        self.assertIn(self.store.get_state('sleep_until'), notice['text'])
        self.reopen()
        self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(self.store.next_reply()['id'], notice['id'])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM telegram_outbox').fetchone()[0], 1)

    def test_local_wake_choice_announces_date_time_and_zone(self):
        self.controller.execute_tool('wake_at', {'time': '09:00'})
        notice = self.store.next_reply()['text']
        self.assertIn('2026-10-17 09:00 EDT', notice)
        self.assertEqual(datetime.fromisoformat(self.store.get_state('sleep_until')),
                         datetime(2026, 10, 17, 13, tzinfo=timezone.utc))

    def test_failure_latches_error_not_sleep_and_notifies_once_across_restart(self):
        def fail():
            raise TimeoutError('private provider payload')
        self.autonomous_action = fail
        self.controller._command('/wake')
        with self.assertLogs(level='ERROR'):
            self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(self.store.get_state('sleeping'), '0')
        self.assertEqual(self.store.get_state('active_session_status'), 'error')
        self.assertEqual(self.store.get_state('autonomy_error'), 'timeout')
        notice = self.store.next_reply()
        self.assertIn('My session failed', notice['text'])
        self.assertIn('messages are saved', notice['text'])
        self.assertNotIn('private provider payload', notice['text'])
        self.assertNotIn('sleeping', self.controller._command('/status'))
        self.reopen()
        self.now += timedelta(days=1)
        self.assertFalse(self.controller.check_fallback_wake(self.now))
        self.assertFalse(self.controller.check_scheduled_wake(self.now))
        self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(len(self.autonomous_calls), 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM telegram_outbox').fetchone()[0], 1)
        self.assertEqual(self.store.next_reply()['id'], notice['id'])
        self.autonomous_action = None
        self.controller._command('/wake')
        self.assertTrue(self.runner.tick(self.now))

    def test_queued_failure_reports_saved_input_and_never_blindly_replays(self):
        self.controller._command('/sleep')
        self.controller.handle_update(self.message('Do one action'))
        self.controller._command('/wake')
        def fail():
            raise TimeoutError('uncertain result')
        self.reply_action = fail
        reply = self.controller.flush_queued()
        self.assertIn('My session failed', reply)
        self.assertIn('/replay', reply)
        self.assertEqual(self.store.uncertain_message_count(17), 1)
        self.assertFalse(self.runner.tick(self.now))

        self.assertIsNone(self.controller.flush_queued())
        self.controller._command('/wake')
        self.assertIsNone(self.controller.flush_queued())
        self.assertEqual(len(self.reply_calls), 1)
        self.controller._command('/replay')
        self.reply_action = None
        self.assertEqual(self.controller.flush_queued(), 'A fixture reply.')
        self.assertEqual(len(self.reply_calls), 2)
        self.assertEqual(self.store.pending_message_count(17), 0)
        self.assertFalse(self.runner.tick(self.now))

    def test_owner_response_ledger_failure_stops_autonomy_and_preserves_input(self):
        self.controller._command('/wake')
        original = self.store.add_message
        with patch.object(self.store, 'add_message', wraps=self.store.add_message) as save:
            def fail_assistant(owner_id, role, content, **kwargs):
                if role == 'assistant':
                    raise sqlite3.OperationalError('private database detail')
                return original(owner_id, role, content, **kwargs)
            save.side_effect = fail_assistant
            reply = self.controller.handle_update(self.message())
        self.assertIn('My session failed', reply)
        self.assertNotIn('private database detail', reply)
        self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(self.store.get_state('sleeping'), '0')
        self.assertEqual(self.store.uncertain_message_count(17), 1)

    def test_atomic_agent_continue_cannot_override_concurrent_owner_sleep(self):
        original = self.store.pending_message_count
        def owner_sleeps(owner_id):
            self.controller._command('/sleep')
            return original(owner_id)
        with patch.object(self.store, 'pending_message_count', side_effect=owner_sleeps):
            with self.assertRaises(ValueError):
                self.controller.execute_tool('continue_activity', {})
        self.assertEqual(self.store.get_state('sleeping'), '1')
        self.assertEqual(self.store.get_state('session_start_requested'), '')

    def test_explicit_owner_timed_sleep_can_schedule_recovery_without_replay(self):
        self.store.record_session_failure('timeout', 17)
        self.controller._command('/sleep', '/sleep 10')
        self.assertFalse(self.runner.tick(self.now))
        self.now += timedelta(minutes=10)
        self.assertTrue(self.controller.check_scheduled_wake(self.now))
        self.assertTrue(self.runner.tick(self.now))

    def test_owner_commands_win_over_late_reply_or_failure(self):
        for command in ('/pause', '/sleep'):
            for fail in (False, True):
                with self.subTest(command=command, fail=fail):
                    self.controller._command('/wake')
                    def owner_command():
                        self.controller._command(command)
                        if fail:
                            raise TimeoutError('old response')
                    self.reply_action = owner_command
                    self.controller.handle_update(self.message())
                    self.assertEqual(self.store.get_state('paused' if command == '/pause' else 'sleeping'), '1')
                    self.assertEqual(self.store.get_state('session_start_requested'), '')
                    self.assertEqual(self.store.get_state('autonomy_error'), '')
                    self.now += timedelta(minutes=6)
                    self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(self.autonomous_calls, [])

    def test_session_continues_after_old_time_limit_without_inventing_sleep(self):
        self.controller._command('/wake')
        session = self.runner.start('owner_wake', self.now)
        self.now += timedelta(minutes=181)
        self.assertTrue(self.runner.tick(self.now))
        self.assertEqual(self.runner.active_session_id, session)
        self.assertEqual(self.store.get_state('sleeping'), '0')
        self.assertEqual(len(self.autonomous_calls), 1)
        self.assertIsNone(self.store.next_reply())
