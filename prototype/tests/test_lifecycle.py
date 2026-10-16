import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from andrewbot.controller import Controller
from andrewbot.diary import deliver_pending_diary
from andrewbot.gateway import ModelGateway
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "controller.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.calls = []
        outer = self

        class Agent:
            def autonomous_turn(self, prompt, timeout):
                outer.calls.append((prompt, timeout))
                if outer.action:
                    outer.action()

        self.agent = Agent()
        self.action = None
        self.controller = Controller(self.store, self.agent, 17, autonomous_enabled=True, fallback_enabled=True)
        self.runner = SessionRunner(self.store, self.controller, self.agent, enabled=True)
        self.now = datetime(2026, 10, 14, 13, tzinfo=timezone.utc)

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.controller.store = self.store
        self.runner = SessionRunner(self.store, self.controller, self.agent, enabled=True)

    def test_daily_fallback_starts_idle_lifecycle_once(self):
        self.assertTrue(self.controller.check_fallback_wake(self.now))
        self.assertTrue(self.runner.tick(self.now))
        session = self.runner.active_session_id
        self.assertFalse(self.controller.check_fallback_wake(self.now))
        self.assertEqual(self.runner.active_session_id, session)
        self.assertEqual(len(self.calls), 1)

    def test_fallback_does_not_repeat_after_clock_moves_back_a_day(self):
        self.assertTrue(self.controller.check_fallback_wake(self.now))
        self.store.set_state("session_start_requested", "")
        self.assertFalse(self.controller.check_fallback_wake(self.now - timedelta(days=1)))
        self.assertFalse(self.controller.check_fallback_wake(self.now))
        self.assertTrue(self.controller.check_fallback_wake(self.now + timedelta(days=1)))

    def test_dst_gap_and_fold_resolve_to_valid_future_instants(self):
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("America/Toronto")
        spring = datetime(2026, 3, 8, 1, 59, tzinfo=zone)
        target = self.controller._next_local_wake(spring, 2, 30, zone)
        self.assertEqual(target.astimezone(timezone.utc), datetime(2026, 3, 8, 7, tzinfo=timezone.utc))
        between = datetime(2026, 11, 1, 5, 45, tzinfo=timezone.utc).astimezone(zone)
        target = self.controller._next_local_wake(between, 1, 30, zone)
        self.assertEqual(target.astimezone(timezone.utc), datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))

    def test_paused_wakes_preserve_pause_deadline_and_daily_checkpoint(self):
        self.store.transition_state({"paused": "1", "sleeping": "1", "sleep_until": self.now.isoformat()}, "pause test")
        self.assertFalse(self.controller.check_scheduled_wake(self.now))
        self.assertFalse(self.controller.check_fallback_wake(self.now))
        self.assertEqual(self.store.get_state("sleep_until"), self.now.isoformat())
        self.assertIsNone(self.store.get_state("fallback_wake_day"))
        with self.assertRaises(ValueError):
            self.controller.execute_tool("continue_activity", {})
        self.assertEqual(self.store.get_state("paused"), "1")
        self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(self.calls, [])

    def test_pause_then_manual_wake_starts_a_fresh_autonomous_session(self):
        previous_session = self.runner.start("scheduled_wake", self.now)
        self.controller._command("/pause")
        self.controller._command("/wake")
        self.assertTrue(self.runner.tick(self.now))
        self.assertEqual(len(self.calls), 1)
        self.assertNotEqual(self.store.get_state("active_session_id"), previous_session)

    def test_late_agent_action_cannot_cancel_committed_sleep(self):
        self.controller.execute_tool("sleep", {"minutes": 1})
        deadline = self.store.get_state("sleep_until")
        with self.assertRaises(ValueError):
            self.controller.execute_tool("continue_activity", {})
        self.assertEqual(self.store.get_state("sleeping"), "1")
        self.assertEqual(self.store.get_state("sleep_until"), deadline)

    def test_confirmed_diary_delivery_is_not_repeated_after_restart(self):
        entry_id = self.store.create_diary_entry("2026-10-14", "I am keeping a dated account of my day.")
        sends = []
        class Telegram:
            def send_message(self, owner, text):
                sends.append((owner, text))
        self.assertEqual(deliver_pending_diary(Telegram(), self.store, 17), 1)
        self.reopen()
        self.assertEqual(deliver_pending_diary(Telegram(), self.store, 17), 0)
        self.assertEqual(len(sends), 1)
        self.assertEqual(self.store.diary_delivery_state(entry_id), "delivered")

    def test_active_session_cannot_sleep_before_diary_is_saved(self):
        self.runner.start("scheduled_wake", self.now)
        for name, arguments in (("sleep", {"minutes": 1}), ("wake_at", {"time": "09:00"})):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "diary"):
                self.controller.execute_tool(name, arguments)
        self.assertNotEqual(self.store.get_state("sleeping"), "1")

    def test_diary_then_sleep_survives_restart_and_wakes_once(self):
        self.runner.start("scheduled_wake", self.now)
        self.controller.execute_tool("save_diary_entry", {"content": "I explored a small idea and will return after resting."})
        entry = self.store.diary_entry()
        self.controller.execute_tool("sleep", {"minutes": 1})
        due = datetime.fromisoformat(self.store.get_state("sleep_until"))
        self.runner.stop("agent_sleep")
        self.reopen()
        self.runner.recover()
        self.assertFalse(self.runner.tick(due - timedelta(seconds=1)))
        self.assertFalse(self.controller.check_scheduled_wake(due - timedelta(seconds=1)))
        self.assertTrue(self.controller.check_scheduled_wake(due))
        self.assertFalse(self.controller.check_scheduled_wake(due))
        self.assertTrue(self.runner.tick(due))
        self.assertEqual(self.store.diary_entry()["entry_id"], entry["entry_id"])

    def test_uncertain_diary_delivery_is_not_automatically_repeated(self):
        entry_id = self.store.create_diary_entry("2026-10-14", "I am checking how my diary is stored.")
        sends = []

        class Telegram:
            def send_message(self, owner, text):
                sends.append((owner, text))
                raise TimeoutError()

        self.assertEqual(deliver_pending_diary(Telegram(), self.store, 17), 0)
        self.reopen()
        self.assertEqual(self.store.diary_delivery_state(entry_id), "uncertain")
        self.assertEqual(deliver_pending_diary(Telegram(), self.store, 17), 0)
        self.assertEqual(len(sends), 1)

    def test_restart_interrupts_cycle_preserves_reservation_and_rests(self):
        session = self.runner.start("scheduled_wake", self.now)
        self.store.begin_cycle(session, "unfinished", "2026-10-14")
        self.store.reserve_usage("uncertain", "2026-10", "2026-10-14", 0.01)
        self.reopen()
        self.runner.recover()
        self.runner.recover()
        self.assertEqual(self.store.get_state("active_session_status"), "interrupted")
        self.assertEqual(self.store.get_state("sleeping"), "1")
        self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(self.calls, [])
        row = self.store.connection.execute("SELECT status FROM cycles WHERE cycle_id='unfinished'").fetchone()
        self.assertEqual(row[0], "interrupted")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM budget_reservations").fetchone()[0], 1)
        self.assertEqual(len(self.store.pending_diary_deliveries()), 1)
        self.assertEqual(self.store.pending_diary_deliveries()[0]["record_type"], "incident_summary")

    def test_cycle_and_time_limits_block_provider_calls(self):
        session = self.runner.start("scheduled_wake", self.now)
        for cycle in range(150):
            self.store.begin_cycle(session, str(cycle), "2026-10-14")
            self.store.finish_cycle(str(cycle), "complete")
        self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(self.store.get_state("active_session_status"), "cycle_limit")
        self.store.set_state("sleeping", "0")
        self.runner.start("scheduled_wake", self.now + timedelta(days=1))
        self.assertFalse(self.runner.tick(self.now + timedelta(days=1, minutes=180)))
        self.assertEqual(self.store.get_state("active_session_status"), "time_limit")
        self.assertEqual(self.calls, [])

    def test_provider_failure_rests_and_records_incident_without_retry(self):
        def fail():
            raise RuntimeError("provider unavailable")
        self.action = fail
        self.store.set_state("session_start_requested", "scheduled_wake")
        self.assertFalse(self.runner.tick(self.now))
        self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.get_state("sleeping"), "1")
        self.assertEqual(self.store.pending_diary_deliveries()[0]["record_type"], "incident_summary")

    def test_sleep_and_pause_block_gateway_before_provider_and_reservation(self):
        class Provider:
            def complete(self, payload):
                raise AssertionError("provider must not be called")
        gateway = ModelGateway(Provider(), self.store, "test-token")
        for key in ("sleeping", "paused"):
            with self.subTest(key=key):
                self.store.set_state(key, "1")
                status, _ = gateway.complete("Bearer test-token", {"model": gateway.model_id, "messages": [{"role": "user", "content": "test"}]})
                self.assertEqual(status, 423)
                self.store.set_state(key, "0")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM budget_reservations").fetchone()[0], 0)

    def test_timeout_diagnostic_excludes_exception_text_and_does_not_retry(self):
        def fail():
            raise TimeoutError("private provider payload and secret-token")
        self.action = fail
        self.store.set_state("session_start_requested", "scheduled_wake")
        with self.assertLogs(level="ERROR") as logs:
            self.assertFalse(self.runner.tick(self.now))
        self.assertIn("stage=agent_turn, category=timeout", " ".join(logs.output))
        incident = self.store.pending_diary_deliveries()[0]["content"]
        self.assertIn("stage=agent_turn, category=timeout", incident)
        self.assertNotIn("secret-token", incident + " ".join(logs.output))
        self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(len(self.calls), 1)

    def test_database_failure_is_logged_even_when_activity_storage_fails(self):
        import sqlite3
        self.runner.start("scheduled_wake", self.now)
        with patch.object(self.store, "begin_cycle", side_effect=sqlite3.OperationalError("secret-token")), \
                patch.object(self.store, "add_activity", side_effect=sqlite3.OperationalError("secret-token")), \
                self.assertLogs(level="ERROR") as logs:
            self.assertFalse(self.runner.tick(self.now))
        self.assertIn("stage=begin_cycle, category=database_error", " ".join(logs.output))
        self.assertNotIn("secret-token", " ".join(logs.output))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.get_state("sleeping"), "1")

    def test_finish_cycle_failure_is_distinguished_from_agent_failure(self):
        import sqlite3
        finish = self.store.finish_cycle
        def fail_completion(cycle_id, status):
            if status == "complete":
                raise sqlite3.OperationalError("database unavailable")
            return finish(cycle_id, status)
        self.store.set_state("session_start_requested", "scheduled_wake")
        with patch.object(self.store, "finish_cycle", side_effect=fail_completion), self.assertLogs(level="ERROR") as logs:
            self.assertFalse(self.runner.tick(self.now))
        self.assertIn("stage=finish_cycle, category=database_error", " ".join(logs.output))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.get_state("sleeping"), "1")

    def test_budget_exhaustion_blocks_session(self):
        self.store.record_usage("2026-10", "2026-10-14", 9, 1, 1)
        self.assertIsNone(self.runner.start("scheduled_wake", self.now))
        self.assertEqual(self.calls, [])

    def test_old_session_stop_cannot_overwrite_a_new_owner_wake(self):
        self.runner.start('scheduled_wake', self.now)
        self.controller._command('/pause')
        self.controller._command('/wake')
        self.runner.stop('provider_error', rest=True)
        self.assertEqual(self.store.get_state('active_session_status'), 'owner_wake')
        self.assertEqual(self.store.get_state('session_start_requested'), 'owner_wake')
        self.assertEqual(self.store.get_state('sleeping'), '0')

    def test_failed_admission_does_not_discard_a_concurrent_owner_wake(self):
        self.store.set_state('session_start_requested', 'owner_wake')
        def deny_start(trigger, current):
            self.controller._command('/wake')
            return None
        with patch.object(self.runner, 'start', side_effect=deny_start):
            self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(self.store.get_state('session_start_requested'), 'owner_wake')


class LifecyclePolicyTests(unittest.TestCase):
    def policy(self, environment):
        from andrewbot.lifecycle_policy import load_lifecycle_policy
        return load_lifecycle_policy(environment)

    def test_opt_in_is_required_and_fallback_depends_on_autonomy(self):
        self.assertFalse(self.policy({}).autonomous_enabled)
        self.assertFalse(self.policy({"ANDREW_FALLBACK_WAKE_ENABLED": "true"}).fallback_enabled)
        policy = self.policy({"ANDREW_AUTONOMOUS_ENABLED": "1", "ANDREW_FALLBACK_WAKE_ENABLED": "1"})
        self.assertTrue(policy.autonomous_enabled)
        self.assertTrue(policy.fallback_enabled)

    def test_release_gate_cannot_be_overridden_by_environment(self):
        with patch("andrewbot.lifecycle_policy.TEXT_MEMORY_LIFECYCLE_READY", False):
            with self.assertRaises(ValueError):
                self.policy({"ANDREW_AUTONOMOUS_ENABLED": "1"})

    def test_external_runtime_is_rejected_for_this_autonomous_release(self):
        with self.assertRaises(ValueError):
            self.policy({"ANDREW_AUTONOMOUS_ENABLED": "1", "ANDREW_ISOLATED_RUNTIME_ENABLED": "1"})

    def test_budget_values_cannot_weaken_release_limits(self):
        for key, value in (("ANDREW_MONTHLY_LIMIT_USD", "11"), ("ANDREW_MONTHLY_RESERVE_USD", "0"),
                           ("ANDREW_DAILY_TARGET_USD", "1"), ("ANDREW_DAILY_TARGET_USD", "nan"),
                           ("ANDREW_MONTHLY_LIMIT_USD", "inf"), ("ANDREW_MONTHLY_LIMIT_USD", "-1")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.policy({"ANDREW_AUTONOMOUS_ENABLED": "1", key: value})

    def test_lower_budget_is_shared_with_controller_and_gateway(self):
        policy = self.policy({"ANDREW_MONTHLY_LIMIT_USD": "5", "ANDREW_DAILY_TARGET_USD": "0.10"})
        self.assertEqual(policy.budget_arguments(), {"monthly_limit_usd": 5.0, "monthly_reserve_usd": 1.0, "daily_target_usd": 0.10})
