"""Only the exact live recovery callback may use the interrupted-state exception."""
import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from prototype.tests import test_recovery_dispatch as fixture
from andrewbot.store import Store


class InterruptedToolAuthorityTests(unittest.TestCase):
    setUp = fixture.RecoveryDispatchTests.setUp
    metadata = fixture.RecoveryDispatchTests.metadata

    def run_local(self, name, args, callback=None):
        meta = self.metadata('tool', effect_id=name, payload={'name': name, 'arguments': args})
        self.store.set_state('autonomy_error', 'local_recovery_interrupted')
        result = self.dispatch.run_tool(meta, name, args, callback or (lambda: self.controller.execute_tool(name, args)))
        self.assertEqual(self.records.effect_result(meta['admission_id'])['state'], 'confirmed')
        self.assertEqual(self.store.get_state('autonomy_error'), 'local_recovery_interrupted')
        return result

    def test_save_focus_delete_are_available_only_inside_the_live_scope(self):
        saved = json.loads(self.run_local('research.save', {'question': 'Synthetic question'}))
        entry = saved['record_id']
        self.run_local('research.focus', {'id': entry})
        self.run_local('research.delete', {'id': entry, 'revision': saved['revision']})
        self.assertEqual(self.store.research_page()['entries'], [])
        with self.assertRaises(ValueError):
            self.controller.execute_tool('research.save', {'question': 'Outside scope'})

    def test_continue_is_confirmed_without_clearing_the_recovery_error(self):
        self.run_local('continue_activity', {})
        self.assertEqual(self.store.get_state('session_start_requested'), 'agent_continue')
        with self.assertRaises(ValueError):
            self.controller.execute_tool('continue_activity', {})

    def test_sleep_and_wake_at_confirm_a_terminal_stop(self):
        day = datetime.now(ZoneInfo('America/Toronto')).date().isoformat()
        self.store.create_diary_entry(day, 'Synthetic diary.')
        self.run_local('sleep', {'minutes': 1})
        self.assertEqual(self.store.get_state('sleeping'), '1')
        self.store.set_state('sleeping', '0')
        self.run_local('wake_at', {'time': '12:30'})
        self.assertEqual(self.store.get_state('sleeping'), '1')

    def test_authority_does_not_leak_to_another_store_or_another_tool(self):
        other = Store(self.path.parent / 'other.sqlite3')
        self.addCleanup(other.close)
        other.set_state('autonomy_error', 'local_recovery_interrupted')
        def callback():
            with self.assertRaises(ValueError):
                other.save_research_entry({'question': 'Wrong store'})
            with self.assertRaises(ValueError):
                self.store.continue_activity_state()
            return self.controller.execute_tool('research.save', {'question': 'Authorized'})
        self.run_local('research.save', {'question': 'Authorized'}, callback)

    def test_owner_change_during_callback_invalidates_the_exception(self):
        def callback():
            self.store.transition_owner_control({}, 'new owner revision')
            return self.controller.execute_tool('research.save', {'question': 'Stale'})
        with self.assertRaises(ValueError):
            self.run_local('research.save', {'question': 'Stale'}, callback)
        self.assertEqual(self.store.research_page()['entries'], [])

    def test_callback_failure_does_not_leave_authority_or_an_open_transaction(self):
        def callback():
            raise RuntimeError('Synthetic callback failure')
        with self.assertRaises(RuntimeError):
            self.run_local('research.save', {'question': 'Not saved'}, callback)
        self.assertFalse(self.store.connection.in_transaction)
        with self.assertRaises(ValueError):
            self.controller.execute_tool('research.save', {'question': 'Outside scope'})

    def test_scope_rechecks_restore_fences_at_the_write(self):
        def callback():
            self.store.set_state('local_recovery_restore_required', '1')
            return self.controller.execute_tool('research.save', {'question': 'Blocked'})
        with self.assertRaises(ValueError):
            self.run_local('research.save', {'question': 'Blocked'}, callback)
        self.assertEqual(self.store.research_page()['entries'], [])

    def test_research_save_does_not_authorize_another_research_operation(self):
        def callback():
            with self.assertRaises(ValueError):
                self.store.focus_research_entry('missing-entry')
            return self.controller.execute_tool('research.save', {'question': 'Authorized'})
        self.run_local('research.save', {'question': 'Authorized'}, callback)

    def test_owner_pause_and_sleep_during_callback_still_block(self):
        for flag in ('paused', 'sleeping'):
            with self.subTest(flag=flag):
                args = {'question': 'Blocked ' + flag}
                meta = self.metadata('tool', effect_id=flag, payload={'name': 'research.save', 'arguments': args})
                self.store.set_state('autonomy_error', 'local_recovery_interrupted')
                def callback():
                    self.store.set_state(flag, '1')
                    return self.controller.execute_tool('research.save', args)
                with self.assertRaises(ValueError):
                    self.dispatch.run_tool(meta, 'research.save', args, callback)
                self.store.set_state(flag, '0')
        self.assertEqual(self.store.research_page()['entries'], [])
