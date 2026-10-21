"""Worker stop-check failures must not strand durable work or bypass admission."""
import threading
import unittest
from unittest.mock import patch

from andrewbot.workers import ModelWorker


class WorkerStopCheckTests(unittest.TestCase):
    def test_failed_stop_check_preserves_wake_and_recovers_without_restart(self):
        checked, allowed, acted = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def should_stop():
            checked.set()
            if not allowed.is_set():
                raise RuntimeError('SECRET guard failure')
            return False

        worker = ModelWorker(lambda: (calls.append(1), acted.set()), should_stop)
        with patch('threading.excepthook'), self.assertLogs(level='WARNING') as logs:
            worker.start()
            try:
                worker.wake()
                self.assertTrue(checked.wait(1))
                # Joining waits for the original thread to die on the broken code.
                worker._thread.join(0.05)
                self.assertTrue(worker._thread.is_alive(), 'stop check killed the worker')
                self.assertEqual(worker.snapshot()['state'], 'blocked')
                self.assertEqual(calls, [])
                allowed.set()
                self.assertTrue(acted.wait(2), 'pending wake was lost')
            finally:
                self.assertTrue(worker.stop())
        self.assertEqual(calls, [1])
        self.assertGreaterEqual(worker.snapshot()['failures'], 1)
        self.assertNotIn('SECRET', str(logs.output))

    def test_second_admission_check_failure_does_not_consume_wake(self):
        check_calls = []
        failed, allowed, acted = threading.Event(), threading.Event(), threading.Event()

        def should_stop():
            check_calls.append(1)
            if len(check_calls) >= 2 and not allowed.is_set():
                failed.set()
                raise RuntimeError('guard unavailable')
            return False

        worker = ModelWorker(acted.set, should_stop)
        with patch('threading.excepthook'), patch('andrewbot.workers.logging.warning'):
            worker.start()
            try:
                worker.wake()
                self.assertTrue(failed.wait(1))
                self.assertFalse(acted.is_set())
                allowed.set()
                self.assertTrue(acted.wait(2), 'wake consumed before admission succeeded')
            finally:
                self.assertTrue(worker.stop())

    def test_stop_interrupts_guard_backoff_and_never_runs_action(self):
        failed = threading.Event()
        actions = []

        def should_stop():
            failed.set()
            raise RuntimeError('guard unavailable')

        worker = ModelWorker(lambda: actions.append(1), should_stop)
        with patch('threading.excepthook'), patch('andrewbot.workers.logging.warning'):
            worker.start()
            worker.wake()
            self.assertTrue(failed.wait(1))
            self.assertTrue(worker.stop(timeout=0.5))
        self.assertEqual(actions, [])
        self.assertEqual(worker.snapshot()['state'], 'stopped')

    def test_worker_reports_stopped_when_stop_predicate_ends_thread(self):
        worker = ModelWorker(lambda: self.fail('must not run'), lambda: True)
        worker.start()
        try:
            worker._thread.join(1)
            self.assertEqual(worker.snapshot()['state'], 'stopped')
        finally:
            worker.stop()

    def test_owner_stop_after_guard_failure_prevents_pending_action(self):
        failed, allow_check = threading.Event(), threading.Event()
        calls = []

        def should_stop():
            failed.set()
            if not allow_check.is_set():
                raise RuntimeError('guard unavailable')
            return True

        worker = ModelWorker(lambda: calls.append(1), should_stop)
        with patch('threading.excepthook'), patch('andrewbot.workers.logging.warning'):
            worker.start()
            try:
                worker.wake()
                self.assertTrue(failed.wait(1))
                allow_check.set()
                worker._thread.join(2)
                self.assertFalse(worker._thread.is_alive())
                self.assertEqual(worker.snapshot()['state'], 'stopped')
                self.assertEqual(calls, [])
            finally:
                worker.stop()

    def test_guard_backoff_does_not_repeat_completed_action(self):
        acted, guard_failed = threading.Event(), threading.Event()
        actions = []

        def should_stop():
            if acted.is_set():
                guard_failed.set()
                raise RuntimeError('guard unavailable')
            return False

        worker = ModelWorker(lambda: (actions.append(1), acted.set()), should_stop)
        with patch('threading.excepthook'), patch('andrewbot.workers.logging.warning'):
            worker.start()
            try:
                worker.wake()
                self.assertTrue(guard_failed.wait(1))
                self.assertTrue(worker._thread.is_alive())
                self.assertEqual(actions, [1])
            finally:
                self.assertTrue(worker.stop())

    def test_idle_guard_recovery_clears_blocked_status_without_a_wake(self):
        failed, allowed, recovered = threading.Event(), threading.Event(), threading.Event()
        actions = []

        def should_stop():
            if not allowed.is_set():
                failed.set()
                raise RuntimeError('guard unavailable')
            recovered.set()
            return False

        worker = ModelWorker(lambda: actions.append(1), should_stop)
        with patch('threading.excepthook'), patch('andrewbot.workers.logging.warning'):
            worker.start()
            try:
                self.assertTrue(failed.wait(1))
                allowed.set()
                self.assertTrue(recovered.wait(2))
                # The successful check enters the normal idle wait.
                worker._thread.join(0.05)
                self.assertEqual(worker.snapshot()['state'], 'idle')
                self.assertEqual(actions, [])
            finally:
                self.assertTrue(worker.stop())
