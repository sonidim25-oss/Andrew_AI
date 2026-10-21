"""Retry admission keeps SQLite lifecycle guards locked through its writes."""
import sqlite3
import unittest
from datetime import timedelta
from unittest.mock import patch

from andrewbot.store import Store
from prototype.tests import test_unsent_provider_retry as retry_fixture


class RetryTransactionTests(unittest.TestCase):
    def setUp(self):
        # Reuse the real gateway/session fixture, without repeating its test suite.
        self.fixture = retry_fixture.UnsentProviderRetryTests('test_backoff_survives_restart')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.peer = Store(self.fixture.path)
        self.peer.connection.execute('PRAGMA busy_timeout=0')
        self.addCleanup(self.peer.close)

    def assert_owner_write_blocked(self):
        try:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                self.peer.transition_owner_control({'paused': '1'}, 'concurrent pause')
        finally:
            self.peer.connection.rollback()

    def test_scheduling_locks_owner_controls_before_reading_proof(self):
        prove = self.store._unsent_cycle_proven
        checks = []

        def prove_while_locked(*args):
            result = prove(*args)
            self.assertTrue(result)
            self.assert_owner_write_blocked()
            checks.append(True)
            return result

        with patch.object(self.store, '_unsent_cycle_proven', side_effect=prove_while_locked):
            self.fixture.fail_first_cycle()
        self.assertEqual(checks, [True])
        self.fixture.assert_scheduled()
        self.peer.transition_owner_control({'paused': '1'}, 'pause after scheduling')
        self.assertFalse(self.fixture.runner.tick(self.fixture.now + timedelta(seconds=60)))
        self.assertEqual(self.fixture.provider.calls, 16)

    def test_due_retry_locks_owner_controls_through_session_admission(self):
        self.fixture.fail_first_cycle()
        pending = self.store.pending_owner_input
        checks = []

        def pending_while_locked(*args):
            result = pending(*args)
            self.assert_owner_write_blocked()
            checks.append(True)
            return result

        with patch.object(self.store, 'pending_owner_input', side_effect=pending_while_locked):
            started = self.store.start_provider_retry_session(
                'retry-session', self.fixture.now + timedelta(seconds=60), 17)
        self.assertTrue(started)
        self.assertEqual(checks, [True])
        self.assertEqual(self.store.get_state('active_session_id'), 'retry-session')
        self.peer.transition_owner_control({'paused': '1'}, 'pause after admission')
        self.assertEqual(self.store.get_state('paused'), '1')
