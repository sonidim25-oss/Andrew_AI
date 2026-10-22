import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from andrewbot.controller import Controller
from andrewbot.store import Store
from andrewbot.tool_errors import ToolRejected


class DiaryTimezoneTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "controller.sqlite3")
        self.addCleanup(self.store.close)
        self.store.set_state("active_session_id", "timezone-test")
        self.controller = Controller(self.store, None, 17,
                                     autonomous_enabled=True, timezone_name="UTC")
        # UTC has crossed midnight while Toronto is still on the previous day.
        instant = datetime(2026, 10, 22, 0, 30, tzinfo=timezone.utc)
        clock_patch = patch("andrewbot.controller.datetime", wraps=datetime)
        clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        clock.now.side_effect = lambda zone: instant.astimezone(zone)

    def test_save_uses_configured_date(self):
        self.controller._execute_tool("save_diary_entry", {"content": "I reviewed my work today."})
        self.assertIsNotNone(self.store.diary_entry("2026-10-22"))
        self.assertIsNone(self.store.diary_entry("2026-10-21"))

    def test_configured_day_diary_allows_sleep(self):
        self.store.create_diary_entry("2026-10-22", "I reviewed my work today.",
                                      session_id="timezone-test")
        self.controller._execute_tool("sleep", {"minutes": 30})
        self.assertEqual(self.store.get_state("sleeping"), "1")

    def test_previous_toronto_day_does_not_allow_sleep(self):
        self.store.create_diary_entry("2026-10-21", "I reviewed my work today.",
                                      session_id="timezone-test")
        with self.assertRaisesRegex(ToolRejected, "save today's diary"):
            self.controller._execute_tool("sleep", {"minutes": 30})
        self.assertNotEqual(self.store.get_state("sleeping", "0"), "1")
