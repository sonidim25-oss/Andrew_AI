import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from andrewbot.controller import Controller
from andrewbot.store import Store


class FakeModel:
    def __init__(self):
        self.calls = 0

    def reply(self, messages):
        self.calls += 1
        return "I remember our conversation.", {"prompt_tokens": 10, "completion_tokens": 5}


class GatewayAccountedModel(FakeModel):
    def reply(self, messages):
        self.calls += 1
        return "A Letta reply.", {"accounted_by_gateway": True, "cost": 0.25}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.model = FakeModel()
        self.controller = Controller(
            store=self.store,
            model=self.model,
            owner_id=352635376,
            monthly_limit_usd=10.0,
            monthly_reserve_usd=1.0,
            daily_target_usd=0.30,
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_ignores_non_owner_updates(self):
        result = self.controller.handle_update({
            "update_id": 1,
            "message": {"from": {"id": 99}, "chat": {"id": 99, "type": "private"}, "text": "hello"},
        })
        self.assertIsNone(result)
        self.assertEqual(self.model.calls, 0)

    def test_owner_chat_uses_model_and_persists_both_messages(self):
        result = self.controller.handle_update({
            "update_id": 2,
            "message": {"from": {"id": 352635376}, "chat": {"id": 352635376, "type": "private"}, "text": "hello"},
        })
        self.assertEqual(result, "I remember our conversation.")
        self.assertEqual([row["role"] for row in self.store.recent_messages(352635376)], ["user", "assistant"])

    def test_sleep_queues_owner_message_without_model_call(self):
        self.controller.handle_update({
            "update_id": 3,
            "message": {"from": {"id": 352635376}, "chat": {"id": 352635376, "type": "private"}, "text": "/sleep"},
        })
        self.controller.handle_update({
            "update_id": 4,
            "message": {"from": {"id": 352635376}, "chat": {"id": 352635376, "type": "private"}, "text": "remember this"},
        })
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(self.store.pending_message_count(352635376), 1)

    def test_wake_replays_queued_messages_only_after_sleep_ends(self):
        self.controller._command("/sleep", "/sleep")
        self.controller.handle_update({"message": {"from": {"id": 352635376}, "chat": {"id": 352635376, "type": "private"}, "text": "I saw a red kite."}})
        self.assertIsNone(self.controller.flush_queued())
        self.assertEqual(self.model.calls, 0)
        self.controller._command("/wake", "/wake")
        result = self.controller.flush_queued()
        self.assertEqual(result, "I remember our conversation.")
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(self.store.pending_message_count(352635376), 0)

    def test_timed_sleep_wakes_after_restart_without_model_call(self):
        self.store.set_state("sleeping", "1")
        self.store.set_state("sleep_until", "2026-10-13T09:59:00+00:00")
        woke = self.controller.check_scheduled_wake(now="2026-10-13T10:00:00+00:00")
        self.assertTrue(woke)
        self.assertEqual(self.store.get_state("sleeping"), "0")
        self.assertEqual(self.store.get_state("sleep_until"), "")
        self.assertEqual(self.model.calls, 0)

    def test_restart_retains_conversation(self):
        self.store.add_message(352635376, "user", "founding memory")
        path = self.store.path
        self.store.close()
        self.store = Store(path)
        self.assertEqual(self.store.recent_messages(352635376)[0]["content"], "founding memory")

    def test_budget_gate_preserves_reserve_and_daily_target(self):
        with patch.object(Controller, "_periods", return_value=("2026-10", "2026-10-14")):
            self.store.record_usage("2026-10", "2026-10-14", 0.299, 1, 1)
            allowed, reason = self.controller.budget_allows(estimated_cost_usd=0.002)
            self.assertFalse(allowed)
            self.assertEqual(reason, "daily_target")
            self.store.record_usage("2026-10", "2026-10-13", 8.701, 1, 1)
            allowed, reason = self.controller.budget_allows(estimated_cost_usd=0.001)
            self.assertFalse(allowed)
            self.assertEqual(reason, "monthly_reserve")

    def test_fallback_wakes_indefinite_sleep_in_configured_timezone(self):
        self.controller.timezone_name = "America/Toronto"
        self.controller.fallback_wake_time = "09:00"
        self.store.set_state("sleeping", "1")
        self.store.set_state("sleep_until", "")
        self.controller.handle_update({"message": {"from": {"id": 352635376}, "chat": {"id": 352635376, "type": "private"}, "text": "queued note"}})
        self.assertEqual(self.model.calls, 0)
        woke = self.controller.check_fallback_wake(now="2026-10-14T13:00:00+00:00")
        self.assertTrue(woke)
        self.assertEqual(self.store.get_state("sleeping"), "0")
        self.controller.flush_queued()
        self.assertEqual(self.store.pending_message_count(352635376), 0)
        self.assertEqual(self.model.calls, 1)

    def test_scheduled_sleep_is_not_overridden_by_daily_fallback(self):
        self.controller.timezone_name = "America/Toronto"
        self.controller.fallback_wake_time = "09:00"
        self.store.set_state("sleeping", "1")
        self.store.set_state("sleep_until", "2026-10-15T13:00:00+00:00")
        self.assertFalse(self.controller.check_fallback_wake(now="2026-10-14T13:00:00+00:00"))
        self.assertEqual(self.store.get_state("sleeping"), "1")

    def test_wake_at_schedules_next_local_occurrence(self):
        self.controller.timezone_name = "America/Toronto"
        with patch("andrewbot.controller.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 14, 10, 0, tzinfo=ZoneInfo("America/Toronto"))
            clock.fromisoformat = datetime.fromisoformat
            reply = self.controller._command("/wake_at", "/wake_at 09:30")
        self.assertIn("09:30", reply)
        self.assertEqual(self.store.get_state("sleeping"), "1")
        self.assertEqual(self.store.get_state("sleep_until"), "2026-10-15T13:30:00+00:00")

    def test_agent_sleep_tool_persists_a_bounded_wake_request(self):
        result = self.controller.execute_tool("sleep", {"minutes": 25})
        self.assertIn("sleep", result.lower())
        self.assertEqual(self.store.get_state("sleeping"), "1")
        self.assertTrue(self.store.get_state("sleep_until"))

    def test_agent_cannot_schedule_unbounded_or_invalid_sleep(self):
        with self.assertRaises(ValueError):
            self.controller.execute_tool("sleep", {"minutes": 1441})
        with self.assertRaises(ValueError):
            self.controller.execute_tool("unsupported", {})

    def test_agent_budget_tool_reports_live_ledger(self):
        self.store.record_usage(*self.controller._periods(), 0.012, 20, 5)
        result = self.controller.execute_tool("check_budget", {})
        self.assertIn("$0.0120", result)
        self.assertIn("$10.00", result)

    def test_controller_does_not_double_count_gateway_accounted_usage(self):
        self.controller.model = GatewayAccountedModel()
        self.controller.handle_update({"message": {"from": {"id": 352635376}, "chat": {"id": 352635376, "type": "private"}, "text": "hello"}})
        month, day = self.controller._periods()
        self.assertEqual(self.store.usage_totals(month, day)["monthly_cost"], 0.0)


if __name__ == "__main__":
    unittest.main()
