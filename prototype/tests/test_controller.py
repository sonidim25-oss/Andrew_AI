import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from andrewbot.controller import Controller
from andrewbot.configuration import OUTPUT_TOKEN_COST_ESTIMATE
from andrewbot.store import Store


class FakeModel:
    def __init__(self):
        self.calls = 0

    def reply(self, messages, timeout=120):
        self.calls += 1
        return "I remember our conversation.", {"prompt_tokens": 10, "completion_tokens": 5}


class GatewayAccountedModel(FakeModel):
    def reply(self, messages, timeout=120):
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
            owner_id=123456789,
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

    def test_runtime_tools_dispatch_only_to_injected_runtime(self):
        class ToolRuntimeFake:
            def __init__(self):
                self.calls = []

            def execute(self, name, arguments):
                self.calls.append((name, arguments))
                return "completed safely"

        runtime = ToolRuntimeFake()
        self.controller.tool_runtime = runtime
        self.assertEqual(self.controller.execute_tool("python.run", {"code": "print(1)"}), "completed safely")
        self.assertEqual(runtime.calls, [("python.run", {"code": "print(1)"})])

    def test_runtime_tools_are_unavailable_without_runtime(self):
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.controller.execute_tool("python.run", {"code": "print(1)"})

    def test_owner_history_commands_require_confirmation_for_full_delete(self):
        class History:
            def __init__(self):
                self.deleted = 0
            def search(self, query, limit=20):
                return [{"id": 4, "visited_at": "2026-10-14T00:00:00+00:00", "title": "Example", "url": "https://example.com/"}]
            def delete(self, visit_id):
                return visit_id == 4
            def delete_all(self):
                self.deleted += 1
                return 3
        history = History()
        self.controller.visit_history = history
        self.assertIn("https://example.com/", self.controller._command("/history", "/history example"))
        self.assertIn("confirm", self.controller._command("/history", "/history delete all"))
        self.assertEqual(history.deleted, 0)
        self.assertEqual(self.controller._command("/history", "/history confirm DELETE ALL HISTORY"), "Deleted 3 browser history visit(s).")
        self.assertEqual(history.deleted, 1)

    def test_history_query_excludes_the_full_command_token(self):
        history = Mock()
        history.search.return_value = []
        self.controller.visit_history = history
        cases = [
            ('/history', ''),
            ('/history example.com', 'example.com'),
            ('/history@AndrewBot', ''),
            ('/history@AndrewBot example.com', 'example.com'),
            ('/HISTORY@AndrewBot two search terms', 'two search terms'),
            ('/history@AndrewBot\t  example.com', 'example.com'),
            ('🌐 Browsing history', ''),
        ]
        for command, query in cases:
            with self.subTest(command=command):
                history.reset_mock()
                self.controller.handle_update(self.owner_message(command))
                history.search.assert_called_once_with(query, limit=20)
        self.assertEqual(self.model.calls, 0)

    def test_owner_chat_uses_model_and_persists_both_messages(self):
        result = self.controller.handle_update({
            "update_id": 2,
            "message": {"from": {"id": 123456789}, "chat": {"id": 123456789, "type": "private"}, "text": "hello"},
        })
        self.assertEqual(result, "I remember our conversation.")
        self.assertEqual([row["role"] for row in self.store.recent_messages(123456789)], ["user", "assistant"])

    @staticmethod
    def owner_message(text):
        return {"message": {"from": {"id": 123456789},
                            "chat": {"id": 123456789, "type": "private"}, "text": text}}

    def test_owner_chat_is_not_blocked_by_legacy_monthly_spending(self):
        month, day = self.controller._periods()
        self.store.record_usage(month, day, 100, 1, 1)
        reply = self.controller.handle_update(self.owner_message('hi'))
        self.assertEqual(reply, 'I remember our conversation.')
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(self.store.pending_message_count(123456789), 0)

    def test_failed_owner_message_requires_explicit_replay_and_survives_restart(self):
        self.controller.model = Mock()
        self.controller.model.reply.side_effect = RuntimeError("fixture response lost after action")
        reply = self.controller.handle_update(self.owner_message("original request"))
        self.assertIn("/replay", reply)
        self.assertIn("uncertain", reply)
        path = self.store.path
        self.store.close()
        self.store = Store(path)
        self.controller.store = self.store
        self.assertEqual(self.store.uncertain_message_count(123456789), 1)
        self.assertIsNone(self.controller.flush_queued())
        self.controller.model.reply.assert_called_once()
        self.controller.model.reply.side_effect = None
        self.controller.model.reply.return_value = ("received", {"accounted_by_gateway": True})
        followup = self.controller.handle_update(self.owner_message("independent follow-up"))
        self.assertIn('has not been sent', followup)
        self.controller.model.reply.assert_called_once()
        self.controller._command('/wake')
        self.assertEqual(self.controller.flush_queued(), 'received')
        submitted = self.controller.model.reply.call_args.args[0]
        self.assertIn('independent follow-up', submitted[0]['content'])
        self.assertNotIn('original request', submitted[0]['content'])
        self.assertEqual(self.store.uncertain_message_count(123456789), 1)
        self.assertIn("Requeued 1", self.controller._command('/replay'))
        self.assertEqual(self.controller.flush_queued(), "received")
        self.assertIn("original request", self.controller.model.reply.call_args.args[0][0]['content'])
        self.assertEqual(self.store.pending_message_count(123456789), 0)
        self.assertIsNone(self.controller.flush_queued())

    def test_interrupted_direct_message_is_held_on_startup_recovery(self):
        self.controller.model = Mock()
        self.controller.model.reply.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.controller.handle_update(self.owner_message("interrupted request"))
        path = self.store.path
        self.store.close()
        self.store = Store(path)
        self.controller.store = self.store
        self.assertEqual(self.store.recover_queued_claims(), 1)
        self.assertEqual(self.store.uncertain_message_count(123456789), 1)
        self.assertIsNone(self.controller.flush_queued())
        self.controller.model.reply.assert_called_once()

    def test_sleep_queues_owner_message_without_model_call(self):
        self.controller.handle_update({
            "update_id": 3,
            "message": {"from": {"id": 123456789}, "chat": {"id": 123456789, "type": "private"}, "text": "/sleep"},
        })
        self.controller.handle_update({
            "update_id": 4,
            "message": {"from": {"id": 123456789}, "chat": {"id": 123456789, "type": "private"}, "text": "remember this"},
        })
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(self.store.pending_message_count(123456789), 1)

    def test_wake_replays_queued_messages_only_after_sleep_ends(self):
        self.controller._command("/sleep", "/sleep")
        self.controller.handle_update({"message": {"from": {"id": 123456789}, "chat": {"id": 123456789, "type": "private"}, "text": "I saw a red kite."}})
        self.assertIsNone(self.controller.flush_queued())
        self.assertEqual(self.model.calls, 0)
        self.controller._command("/wake", "/wake")
        result = self.controller.flush_queued()
        self.assertEqual(result, "I remember our conversation.")
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(self.store.pending_message_count(123456789), 0)

    def test_timed_sleep_wakes_after_restart_without_model_call(self):
        self.store.set_state("sleeping", "1")
        self.store.set_state("sleep_until", "2026-10-13T09:59:00+00:00")
        woke = self.controller.check_scheduled_wake(now="2026-10-13T10:00:00+00:00")
        self.assertTrue(woke)
        self.assertEqual(self.store.get_state("sleeping"), "0")
        self.assertEqual(self.store.get_state("sleep_until"), "")
        self.assertEqual(self.model.calls, 0)

    def test_restart_retains_conversation(self):
        self.store.add_message(123456789, "user", "founding memory")
        path = self.store.path
        self.store.close()
        self.store = Store(path)
        self.assertEqual(self.store.recent_messages(123456789)[0]["content"], "founding memory")

    def test_legacy_monthly_reserve_does_not_block_owner_work(self):
        frozen = datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)
        with patch("andrewbot.budget.datetime") as clock:
            clock.now.return_value = frozen
            self.store.record_usage("2026-10", "2026-10-31", 8.5, 1, 1)
            allowed, reason = self.controller.budget_allows(estimated_cost_usd=0.002)
            self.assertTrue(allowed)
            self.assertEqual(reason, "ok")
            self.store.record_usage("2026-10", "2026-10-30", 0.5, 1, 1)
            allowed, reason = self.controller.budget_allows(estimated_cost_usd=0.001)
            self.assertTrue(allowed)
            self.assertEqual(reason, "ok")

    def test_fallback_wakes_indefinite_sleep_in_configured_timezone(self):
        self.controller.timezone_name = "America/Toronto"
        self.controller.fallback_wake_time = "09:00"
        self.store.set_state("sleeping", "1")
        self.store.set_state("sleep_until", "")
        self.controller.handle_update({"message": {"from": {"id": 123456789}, "chat": {"id": 123456789, "type": "private"}, "text": "queued note"}})
        self.assertEqual(self.model.calls, 0)
        woke = self.controller.check_fallback_wake(now="2026-10-14T13:00:00+00:00")
        self.assertTrue(woke)
        self.assertEqual(self.store.get_state("sleeping"), "0")
        self.controller.flush_queued()
        self.assertEqual(self.store.pending_message_count(123456789), 0)
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
        now_local = datetime(2026, 10, 14, 10, 0, tzinfo=ZoneInfo("America/Toronto"))

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now_local if tz is None else now_local.astimezone(tz)

        with patch("andrewbot.controller.datetime", FrozenDateTime):
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
        self.assertEqual(json.loads(result)["reasoning_usage"]["monthly_cost"], 0.012)
        self.assertIsNone(json.loads(result)["reasoning_usage"]["monthly_limit"])

    def test_controller_does_not_double_count_gateway_accounted_usage(self):
        self.controller.model = GatewayAccountedModel()
        self.controller.handle_update({"message": {"from": {"id": 123456789}, "chat": {"id": 123456789, "type": "private"}, "text": "hello"}})
        month, day = self.controller._periods()
        self.assertEqual(self.store.usage_totals(month, day)["monthly_cost"], 0.0)


if __name__ == "__main__":
    unittest.main()
