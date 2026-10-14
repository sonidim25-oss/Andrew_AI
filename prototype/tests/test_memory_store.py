import tempfile
import sqlite3
import unittest
from pathlib import Path

from andrewbot.store import Store


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "controller.sqlite3"
        self.store = Store(self.database_path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_memory_schema_migrates_existing_database(self):
        self.store.close()
        self.database_path.unlink()
        connection = sqlite3.connect(self.database_path)
        connection.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, created_at TEXT NOT NULL, queued INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE activity (id INTEGER PRIMARY KEY, event TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE diary_entries (
                entry_id TEXT PRIMARY KEY, local_date TEXT NOT NULL, created_at TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '', content TEXT NOT NULL,
                record_type TEXT NOT NULL CHECK(record_type IN ('diary','incident_summary')),
                delivery_state TEXT NOT NULL DEFAULT 'pending' CHECK(delivery_state IN ('pending','delivered','uncertain')),
                delivery_attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE diary_event_refs (
                entry_id TEXT NOT NULL REFERENCES diary_entries(entry_id),
                activity_id INTEGER NOT NULL REFERENCES activity(id), PRIMARY KEY(entry_id, activity_id)
            );
            INSERT INTO activity VALUES (1, 'pre-migration activity', '2026-10-14T00:00:00+00:00');
            INSERT INTO messages VALUES (1, 17, 'user', 'pre-migration message', '2026-10-14T00:00:00+00:00', 0);
            INSERT INTO diary_entries VALUES ('diary-old', '2026-10-14', '2026-10-14T00:00:00+00:00', '',
                'I noticed the autumn air was cool.', 'diary', 'pending', 0);
            INSERT INTO diary_event_refs VALUES ('diary-old', 1);
        """)
        connection.commit()
        connection.close()
        self.store = Store(self.database_path)

        self.assertEqual(self.store.recent_messages(17)[0]["content"], "pre-migration message")
        self.assertEqual(self.store.diary_entry("2026-10-14")["entry_id"], "diary-old")
        events = [row["event"] for row in self.store.recent_activity(10)]
        self.assertIn("pre-migration activity", events)
        self.assertEqual(self.store.personality_revisions(), [])

    def test_personality_revision_is_append_only_and_ordered(self):
        first = self.store.create_personality_revision(
            "I am curious about astronomy.", "Repeated interest in the night sky.", "andrew"
        )
        second = self.store.create_personality_revision(
            "I am curious about astronomy and music.", "Music became a new interest.", "andrew"
        )

        revisions = self.store.personality_revisions()
        self.assertEqual([row["revision_id"] for row in revisions], [second, first])
        self.assertEqual(revisions[1]["content"], "I am curious about astronomy.")
        self.assertEqual(self.store.current_personality_revision()["revision_id"], second)

    def test_memory_records_validate_event_and_diary_references_atomically(self):
        event_id = self.store.add_activity("Andrew read about astronomy")
        diary_id = self.store.create_diary_entry("2026-10-14", "I read about astronomy.")
        record_id = self.store.create_memory_record(
            "experience", "I read about astronomy.", "andrew", [event_id], [diary_id]
        )
        self.assertEqual(self.store.get_memory_record(record_id)["event_ids"], [event_id])
        self.assertEqual(self.store.get_memory_record(record_id)["diary_ids"], [diary_id])

        with self.assertRaises(ValueError):
            self.store.create_memory_record(
                "experience", "A record with an unknown source.", "andrew", [999999], [diary_id]
            )
        self.assertIsNone(self.store.get_memory_record("unknown-record-id"))
        self.assertEqual(len(self.store.search_memory_rows("astronomy", limit=20)), 1)

    def test_memory_search_returns_bounded_ranked_rows(self):
        matching = self.store.create_memory_record(
            "interest", "I enjoy astronomy and telescope observations.", "andrew"
        )
        self.store.create_memory_record("experience", "I cooked a vegetable soup.", "andrew")

        rows = self.store.search_memory_rows("astronomy", limit=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["record_id"], matching)
        self.assertEqual(rows[0]["record_type"], "interest")

    def test_memory_writes_create_audit_events_without_content(self):
        private_text = "I value curiosity about astronomy."
        record_id = self.store.create_personality_revision(
            private_text, "This became important over time.", "andrew"
        )

        event = self.store.recent_activity(1)[0]["event"]
        self.assertIn(record_id, event)
        self.assertNotIn(private_text, event)
        self.assertNotIn("important over time", event)


if __name__ == "__main__":
    unittest.main()
