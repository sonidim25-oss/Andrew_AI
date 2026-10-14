import tempfile
import unittest
from pathlib import Path

from andrewbot.configuration import load_model_config
from andrewbot.gateway import ModelGateway
from andrewbot.memory import MemoryService
from andrewbot.store import Store


class ContinuityProvider:
    def complete(self, payload):
        return {
            "id": "continuity-test",
            "choices": [{"message": {"role": "assistant", "content": "Continuity preserved."}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3, "cost": 0.000001},
        }


class MemoryContinuityTests(unittest.TestCase):
    def test_memory_retrieval_and_revision_history_survive_restart_and_model_change(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "controller.sqlite3"
            first_store = Store(database_path)
            event_id = first_store.add_activity("Andrew read about astronomy")
            diary_id = first_store.create_diary_entry(
                "2026-10-14", "I enjoyed learning about distant galaxies.", event_ids=[event_id]
            )
            first_memory = MemoryService(first_store)
            record_id = first_memory.add_experience(
                "I enjoy astronomy and learning about galaxies.", "interest", [event_id], [diary_id]
            )
            first_revision = first_memory.propose_personality_edit(
                "I am curious about astronomy.", "I kept returning to the topic.", [event_id], [diary_id]
            )
            second_revision = first_memory.propose_personality_edit(
                "I am curious about astronomy and long-term space exploration.",
                "A later interest grew from the earlier one.", [event_id], [diary_id],
            )
            first_store.close()

            changed_config = load_model_config({
                "ANDREW_MODEL_ID": "google/gemini-2.5-flash",
                "ANDREW_MODEL_INPUT_USD_PER_MILLION": "0.25",
                "ANDREW_MODEL_OUTPUT_USD_PER_MILLION": "1.5",
            })
            store = Store(database_path)
            try:
                memory = MemoryService(store)
                gateway = ModelGateway(
                    ContinuityProvider(), store, "gateway-test-token", model_config=changed_config,
                    memory_service=memory,
                )
                self.assertEqual(gateway.model_id, "google/gemini-2.5-flash")
                self.assertTrue(
                    memory.current_personality_context().startswith(
                        f"Current personality revision {second_revision} "
                    )
                )
                revisions = store.personality_revisions()
                self.assertEqual([row["revision_id"] for row in revisions], [second_revision, first_revision])
                rows = memory.search("astronomy galaxies", limit=20)
                self.assertEqual(rows[0]["record_id"], record_id)
                self.assertEqual(rows[0]["diary_ids"], [diary_id])
                self.assertEqual(store.diary_entry("2026-10-14")["entry_id"], diary_id)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
