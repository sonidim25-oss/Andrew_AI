import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from andrewbot.budget import budget_snapshot
from andrewbot.controller import Controller
from andrewbot.gateway import ModelGateway
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store

# UTC has crossed midnight while Toronto is still on the previous day.
INSTANT = datetime(2026, 10, 22, 2, 0, tzinfo=timezone.utc)


class ConfiguredTimezoneTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.store = self.open_store("utc.sqlite3", "UTC")

    def open_store(self, name, *args):
        store = Store(self.directory / name, *args)
        self.addCleanup(store.close)
        return store

    def insert_usage(self, store, cost):
        with store.connection:
            store.connection.execute(
                "INSERT INTO usage(month,day,cost_usd,input_tokens,output_tokens,created_at) VALUES(?,?,?,?,?,?)",
                ("legacy", "legacy", cost, 1, 1, INSTANT.isoformat(timespec="seconds")))

    def test_store_defaults_to_toronto_and_rejects_unknown_zone(self):
        self.assertEqual(self.open_store("default.sqlite3").timezone_name, "America/Toronto")
        with self.assertRaises(ZoneInfoNotFoundError):
            Store(self.directory / "invalid.sqlite3", "Not/AZone")

    def test_budget_day_and_usage_totals_follow_configured_zone(self):
        self.insert_usage(self.store, 0.25)
        snapshot = budget_snapshot(self.store, now=INSTANT)
        self.assertEqual((snapshot["day"], snapshot["daily_cost"]), ("2026-10-22", 0.25))
        toronto = self.open_store("toronto.sqlite3")
        self.insert_usage(toronto, 0.25)
        self.assertEqual(budget_snapshot(toronto, now=INSTANT)["day"], "2026-10-21")
        self.assertEqual(toronto.usage_totals("2026-10", "2026-10-22")["daily_cost"], 0.0)
        self.assertEqual(toronto.usage_totals("2026-10", "2026-10-21")["daily_cost"], 0.25)

    def test_gateway_periods_follow_configured_zone(self):
        gateway = ModelGateway(None, self.store, "token")
        with patch("andrewbot.gateway.datetime", wraps=datetime) as clock:
            clock.now.side_effect = lambda zone: INSTANT.astimezone(zone)
            self.assertEqual(gateway.periods(), ("2026-10", "2026-10-22"))

    def test_controller_defaults_to_store_zone(self):
        self.assertEqual(Controller(self.store, None, 17).timezone_name, "UTC")
        self.assertEqual(Controller(self.store, None, 17, timezone_name="Asia/Tokyo").timezone_name, "Asia/Tokyo")

    def test_personality_effective_date_uses_configured_zone(self):
        with patch("andrewbot.store.datetime", wraps=datetime) as clock:
            clock.now.side_effect = lambda zone=None: INSTANT.astimezone(zone or timezone.utc)
            revision_id = self.store.create_personality_revision("I enjoy astronomy.", "A durable interest.", "andrew")
        row = self.store.connection.execute(
            "SELECT effective_date FROM personality_revisions WHERE revision_id=?", (revision_id,)).fetchone()
        self.assertEqual(row["effective_date"], "2026-10-22")

    def test_incident_diary_date_uses_configured_zone(self):
        runner = SessionRunner(self.store, Controller(self.store, None, 17), None)
        self.assertTrue(runner._record_incident("session-a", "restart", INSTANT.isoformat()))
        dates = self.store.connection.execute(
            "SELECT local_date FROM diary_entries WHERE record_type='incident_summary'").fetchall()
        self.assertEqual([row[0] for row in dates], ["2026-10-22"])


if __name__ == "__main__":
    unittest.main()
