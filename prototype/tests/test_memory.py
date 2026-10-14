import tempfile
import unittest
from pathlib import Path

from andrewbot.controller import Controller
from andrewbot.memory import MemoryService, normalize_memory_query, validate_memory_text
from andrewbot.store import Store


class MemoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "controller.sqlite3")
        self.memory = MemoryService(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_memory_text_rejects_empty_secret_and_reasoning_markers(self):
        for value in (
            "  ", "api_key=secret_value_123", "<private_reasoning>hidden</private_reasoning>",
            '{"password": "synthetic-not-a-real-password"}',
            '{"token": "synthetic-token-value"}',
            '{"api_key": "synthetic-api-key-value"}',
        ):
            with self.subTest(value=value), self.assertRaises(ValueError) as raised:
                validate_memory_text(value)
            self.assertNotIn(value, str(raised.exception))

    def test_overlong_write_is_rejected_without_storage(self):
        with self.assertRaises(ValueError):
            self.memory.add_experience("x" * 4001, "experience")
        self.assertEqual(self.store.search_memory_rows("x", limit=20), [])

    def test_query_normalization_handles_fts_operators_quotes_and_empty_text(self):
        self.assertEqual(normalize_memory_query('astronomy OR "music"'), '"astronomy" OR "music"')
        self.assertEqual(normalize_memory_query('" ( ) * : AND OR NOT '), "")

    def test_memory_search_is_bounded(self):
        for index in range(12):
            self.memory.add_experience(f"I observed astronomy detail {index}.", "experience")
        self.assertEqual(len(self.memory.search("astronomy")), 8)
        self.assertEqual(len(self.memory.search("astronomy", limit=20)), 12)

    def test_personality_edit_records_source_reason_and_references(self):
        event_id = self.store.add_activity("Andrew attended a public astronomy lecture")
        diary_id = self.store.create_diary_entry("2026-10-14", "I enjoyed the astronomy lecture.")
        revision_id = self.memory.propose_personality_edit(
            "I am interested in astronomy.", "The lecture increased my curiosity.", [event_id], [diary_id]
        )
        revision = self.store.current_personality_revision()
        self.assertEqual(revision["revision_id"], revision_id)
        self.assertEqual(revision["source"], "andrew")
        self.assertEqual(revision["reason"], "The lecture increased my curiosity.")
        self.assertEqual(revision["event_ids"], [event_id])
        self.assertEqual(revision["diary_ids"], [diary_id])

    def test_rejected_edit_is_audited_without_raw_content(self):
        sensitive = "api_key=secret_value_123"
        with self.assertRaises(ValueError):
            self.memory.propose_personality_edit(sensitive, "Invalid secret-like text")
        event = self.store.recent_activity(1)[0]["event"]
        self.assertIn("Memory edit rejected", event)
        self.assertNotIn(sensitive, event)
        self.assertEqual(self.store.personality_revisions(), [])

    def test_malformed_memory_writes_are_rejected_and_audited(self):
        controller = Controller(self.store, object(), 17, memory_service=self.memory)
        for arguments in (
            {"content": "example", "record_type": []},
            [],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                controller.execute_tool("memory_add_experience", arguments)
        events = [row["event"] for row in self.store.recent_activity(10)]
        self.assertGreaterEqual(sum("Memory edit rejected" in event for event in events), 2)

    def test_memory_tool_response_is_bounded_in_utf8_bytes(self):
        for index in range(8):
            self.memory.add_experience(f"astronomy {index} " + "🌌" * 1990, "experience")
        controller = Controller(self.store, object(), 17, memory_service=self.memory)
        result = controller.execute_tool("memory_search", {"query": "astronomy"})
        self.assertLessEqual(len(result.encode("utf-8")), 16_000)
        self.assertIn("truncated", result)

    def test_controller_routes_memory_writes_and_search_through_service(self):
        controller = Controller(self.store, object(), 17, memory_service=self.memory)
        saved = controller.execute_tool(
            "memory_add_experience", {"content": "I went to an astronomy lecture.", "record_type": "experience"}
        )
        self.assertIn("saved", saved.lower())
        result = controller.execute_tool("memory_search", {"query": "astronomy"})
        self.assertIn("astronomy lecture", result)

    def test_memory_tools_cannot_edit_owner_records(self):
        controller = Controller(self.store, object(), 17, memory_service=self.memory)
        with self.assertRaisesRegex(ValueError, "unsupported controller action"):
            controller.execute_tool("edit_founding_letter", {"content": "replacement"})
        with self.assertRaisesRegex(ValueError, "unsupported controller action"):
            controller.execute_tool("edit_safety_policy", {"content": "replacement"})


if __name__ == "__main__":
    unittest.main()
