import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "prototype"))
from andrewbot.controller import Controller
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store


class CommissioningRetryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "controller.sqlite3")
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store, object(), 17, autonomous_enabled=True, fallback_enabled=True)
        spec = importlib.util.spec_from_file_location("lifecycle_probe", PROJECT / "scripts/verify-live-lifecycle.py")
        self.probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.probe)
        self.now = datetime(2026, 10, 14, 15, tzinfo=timezone.utc)

    def test_retry_recovers_session_and_preserves_cycle_and_spend_ledgers(self):
        runner = SessionRunner(self.store, self.controller, object(), enabled=True)
        session = runner.start("scheduled_wake", self.now)
        self.store.begin_cycle(session, "old-cycle", "2026-10-14")
        self.store.finish_cycle("old-cycle", "complete")
        self.store.record_usage("2026-10", "2026-10-14", 0.001, 10, 5)
        self.store.set_state("fallback_wake_day", "2026-10-14")
        runner.recover()
        clock_now = [self.now]
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock_now[0].astimezone(tz) if tz else clock_now[0]
        def advance(seconds):
            clock_now[0] += timedelta(seconds=seconds)
        with patch.object(self.probe, "datetime", Clock), patch("andrewbot.controller.datetime", Clock), patch.object(self.probe.time, "sleep", side_effect=advance):
            trigger = self.probe.prepare_wake(self.controller, self.now, retry=True)
        self.assertEqual(trigger, "scheduled_wake")
        self.assertEqual(self.store.get_state("session_start_requested"), "scheduled_wake")
        self.assertEqual(self.store.get_state("fallback_wake_day"), "2026-10-14")
        self.assertEqual(self.store.cycle_count("2026-10-14"), 1)
        self.assertEqual(self.probe.ledger(self.store)[0][1], 0.001)
        self.assertGreaterEqual(clock_now[0], self.now + timedelta(minutes=1))

    def test_retry_cannot_override_owner_pause_or_existing_timed_sleep(self):
        for state in ({"paused": "1"}, {"sleeping": "1", "sleep_until": "2026-10-15T13:00:00+00:00"}):
            with self.subTest(state=state):
                self.store.transition_state({"paused": "0", "sleeping": "0", "sleep_until": "", "fallback_wake_day": "2026-10-14", **state}, "test")
                with self.assertRaises(self.probe.ValidationFailure):
                    self.probe.prepare_wake(self.controller, self.now, retry=True)
                for key, value in state.items():
                    self.assertEqual(self.store.get_state(key), value)

    def test_remote_failure_logging_includes_type_without_remote_details(self):
        with patch.object(self.probe.syslog, "syslog") as journal:
            message = self.probe.failure_message(RuntimeError("sensitive-provider-detail"))
        self.assertEqual(message, "Live lifecycle validation failed (RuntimeError)")
        self.assertNotIn("sensitive-provider-detail", str(journal.call_args))

    def test_local_check_name_is_logged_for_operator_diagnosis(self):
        with patch.object(self.probe.syslog, "syslog") as journal:
            message = self.probe.failure_message(self.probe.ValidationFailure("Scheduled wake did not fire"))
        self.assertEqual(message, "Live lifecycle validation failed: Scheduled wake did not fire")
        self.assertIn(message, str(journal.call_args))
