import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from andrewbot.latency import LatencyMetrics


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class LatencyTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.metrics = LatencyMetrics(clock=self.clock)

    def test_duration_and_success_timestamp(self):
        with self.metrics.measure('chat'):
            self.clock.now += 2
        row = self.metrics.snapshot()['chat']
        self.assertEqual((row['count'], row['failures']), (1, 0))
        for key in ('total_seconds', 'last_seconds', 'max_seconds'):
            self.assertEqual(row[key], 2)
        self.assertEqual(row['last_success_monotonic'], 102)
        self.assertEqual(row['active_count'], 0)
        self.assertIsNone(row['oldest_active_seconds'])

    def test_failure_propagates_without_retaining_exception(self):
        failure = RuntimeError('private token and message')
        with self.assertRaises(RuntimeError) as caught:
            with self.metrics.measure('send'):
                self.clock.now += 3
                raise failure
        self.assertIs(caught.exception, failure)
        row = self.metrics.snapshot()['send']
        self.assertEqual((row['count'], row['failures'], row['last_seconds']), (1, 1, 3))
        self.assertEqual(row['active_count'], 0)
        self.assertIsNone(row['last_success_monotonic'])
        self.assertNotIn('private', json.dumps(self.metrics.snapshot()))

    def test_nested_measurements_and_snapshot_are_independent(self):
        with self.metrics.measure('chat'):
            self.clock.now += 1
            with self.metrics.measure('chat'):
                self.clock.now += 2
                row = self.metrics.snapshot()['chat']
                self.assertEqual((row['active_count'], row['oldest_active_seconds']), (2, 3))
            self.assertEqual(self.metrics.snapshot()['chat']['active_count'], 1)
        self.assertEqual(self.metrics.snapshot()['chat']['total_seconds'], 5)

    def test_concurrent_snapshot_does_not_wait_for_measured_work(self):
        entered, release = threading.Event(), threading.Event()
        def work():
            with self.metrics.measure('autonomous'):
                entered.set()
                self.assertTrue(release.wait(2))
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(work)
            try:
                self.assertTrue(entered.wait(2))
                row = pool.submit(self.metrics.snapshot).result(timeout=1)['autonomous']
                self.assertEqual(row['active_count'], 1)
            finally:
                release.set()
            future.result(timeout=2)

    def test_observations_have_bounded_history_and_detached_snapshots(self):
        shape = self.metrics.snapshot()
        for _ in range(10000):
            self.metrics.observe('inbox_wait', 2)
        snapshot = self.metrics.snapshot()
        self.assertEqual(set(shape), set(snapshot))
        self.assertEqual(snapshot['inbox_wait']['count'], 10000)
        self.assertEqual(snapshot['inbox_wait']['total_seconds'], 20000)
        self.assertEqual(snapshot['inbox_wait']['clock'], 'wall_age')
        snapshot['inbox_wait']['count'] = -1
        self.assertEqual(self.metrics.snapshot()['inbox_wait']['count'], 10000)
        self.assertLess(len(json.dumps(self.metrics.snapshot())), 6000)

    def test_invalid_stage_and_duration_are_rejected(self):
        with self.assertRaises(ValueError):
            with self.metrics.measure('user supplied text'):
                pass
        for value in (-1, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                self.metrics.observe('send', value)
        with self.assertRaises(ValueError):
            self.metrics.observe('secret', 1)


class WallAgeTests(unittest.TestCase):
    def test_utc_age_is_restart_safe_clamped_and_validated(self):
        from datetime import datetime, timezone
        from andrewbot.latency import wall_age_seconds
        now = datetime(2026, 10, 16, 12, 0, 10, tzinfo=timezone.utc)
        self.assertEqual(wall_age_seconds('2026-10-16T12:00:00+00:00', now), 10)
        self.assertEqual(wall_age_seconds('2026-10-16T08:00:00-04:00', now), 10)
        self.assertEqual(wall_age_seconds('2026-10-16T12:00:20+00:00', now), 0)
        for value in (None, '', 'private malformed content', '2026-10-16T12:00:00'):
            self.assertIsNone(wall_age_seconds(value, now))


class CaughtFailureTests(unittest.TestCase):
    def test_caught_error_is_failed_without_advancing_last_success(self):
        from andrewbot.latency import record_failure
        clock = FakeClock()
        metrics = LatencyMetrics(clock=clock)
        record_failure(RuntimeError('outside measurement'))
        with metrics.measure('chat'):
            pass
        with metrics.measure('chat'):
            try:
                raise TimeoutError('private details')
            except TimeoutError as error:
                record_failure(error)
                record_failure(error)  # one failed operation, not two
            clock.now += 2
        row = metrics.snapshot()['chat']
        self.assertEqual((row['count'], row['failures']), (2, 1))
        self.assertEqual(row['last_success_monotonic'], 100)
        self.assertEqual(row['errors']['timeout'], 1)
        self.assertNotIn('private', json.dumps(metrics.snapshot()))

    def test_nested_failures_belong_to_the_correct_collector(self):
        from andrewbot.latency import record_failure
        outer, inner = LatencyMetrics(), LatencyMetrics()
        with outer.measure('diary'):
            try:
                with inner.measure('send'):
                    raise ValueError('private')
            except ValueError as error:
                record_failure(error)
        self.assertEqual(outer.snapshot()['diary']['errors']['invalid_value'], 1)
        self.assertEqual(inner.snapshot()['send']['errors']['invalid_value'], 1)
        row = outer.snapshot()['diary']
        row['errors']['invalid_value'] = 999
        self.assertEqual(outer.snapshot()['diary']['errors']['invalid_value'], 1)

    def test_custom_error_names_are_not_retained(self):
        private_error = type('private_message_secret', (Exception,), {})
        metrics = LatencyMetrics()
        with self.assertRaises(private_error):
            with metrics.measure('chat'):
                raise private_error('private body')
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot['chat']['errors']['unexpected_error'], 1)
        self.assertNotIn('private', json.dumps(snapshot))
