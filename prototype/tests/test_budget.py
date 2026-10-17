import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from andrewbot.budget import budget_snapshot
from andrewbot.store import Store


class AdvisoryDailyBudgetTests(unittest.TestCase):
    def test_first_day_overspend_does_not_reduce_monthly_allowance_to_daily_target(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'controller.sqlite3')
            self.addCleanup(store.close)
            with patch.object(store, 'now', return_value='2026-10-01T16:00:00+00:00'):
                store.record_usage('2026-10', '2026-10-01', .5, 1, 1)
            snapshot = budget_snapshot(store, datetime(2026, 10, 1, 16, tzinfo=timezone.utc))
            self.assertAlmostEqual(snapshot['remaining_ordinary'], 8.5)
            self.assertEqual(snapshot['daily_target'], .3)
            self.assertFalse(snapshot['daily_target_enforced'])
            self.assertAlmostEqual(snapshot['daily_target_overage'], .2)

    def test_monthly_reserve_and_uncertain_reservations_still_stop_spending(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'controller.sqlite3')
            self.addCleanup(store.close)
            with patch.object(store, 'now', return_value='2026-10-01T16:00:00+00:00'):
                store.record_usage('2026-10', '2026-10-01', 8.5, 1, 1)
            store.reserve_usage('uncertain', '2026-10', '2026-10-01', .5)
            now = datetime(2026, 10, 1, 16, tzinfo=timezone.utc)
            self.assertEqual(budget_snapshot(store, now)['remaining_ordinary'], 0)
            november = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)
            self.assertEqual(budget_snapshot(store, november)['remaining_ordinary'], 9)
