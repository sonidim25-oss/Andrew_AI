import tempfile
import unittest
from pathlib import Path

from andrewbot.diary import incident_summary
from andrewbot.store import Store


class IncidentSummaryTests(unittest.TestCase):
    def test_latest_failure_is_not_replaced_with_old_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'fixture.sqlite3')
            try:
                store.add_activity('Session failed (category=timeout); automatic model calls stopped')
                for event in ('Old checkpoint one', 'Old checkpoint two', 'Old checkpoint three',
                              'Current owner wake', 'Current model request started',
                              'Letta model gateway response reached output token limit',
                              'Autonomous cycle failed (stage=agent_turn, category=output_token_limit)'):
                    store.add_activity(event)
                summary = incident_summary('fixture', 'provider_error', store.recent_activity(8))
                self.assertIn('category=output_token_limit', summary)
                self.assertIn('gateway response reached output token limit', summary)
                self.assertNotIn('category=timeout', summary)
                self.assertNotIn('Old checkpoint', summary)
            finally:
                store.close()
