import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from andrewbot.browser_history import HistoryCapacityError, VisitHistory, sanitize_visit


class BrowserHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "history"
        self.key = b"h" * 32

    def test_history_supports_control_thread_reads_and_deletes_during_recording(self):
        history = VisitHistory(self.directory, self.key, max_bytes=1_000_000)
        self.addCleanup(history.close)
        with ThreadPoolExecutor(max_workers=3) as workers:
            records = [workers.submit(history.record, f'https://example.com/{index}', f'Visit {index}')
                       for index in range(12)]
            reads = [workers.submit(history.search, 'example') for _ in range(4)]
            ids = [result.result() for result in records]
            for result in reads:
                result.result()
            self.assertEqual(len(history.search('example')), 12)
            self.assertTrue(workers.submit(history.delete, ids[0]).result())
            self.assertEqual(workers.submit(history.delete_all).result(), 11)
        self.assertEqual(history.search(''), [])

    def test_encrypted_persistent_sanitized_search_and_delete(self):
        history = VisitHistory(self.directory, self.key, max_bytes=1_000_000)
        visit_id = history.record("https://example.com/path?q=private#fragment", "Example", datetime.now(timezone.utc))
        history.close()
        raw = (self.directory / "history.sqlite3").read_bytes()
        self.assertNotIn(b"private", raw)
        self.assertNotIn(b"Example", raw)
        history = VisitHistory(self.directory, self.key, max_bytes=1_000_000)
        rows = history.search("example")
        self.assertEqual(rows[0]["url"], "https://example.com/path")
        self.assertNotIn("q=private", rows[0]["url"])
        self.assertTrue(history.delete(visit_id))
        self.assertEqual(history.search("example"), [])
        history.close()

    def test_invalid_scheme_and_field_limits(self):
        with self.assertRaises(ValueError):
            sanitize_visit("file:///etc/passwd", "x")
        url, title = sanitize_visit("https://example.com/" + "a" * 3000 + "?secret=1", "t" * 600)
        self.assertLessEqual(len(url), 2048)
        self.assertEqual(len(title), 512)

    def test_authentication_failure_does_not_return_corrupt_row(self):
        history = VisitHistory(self.directory, self.key, max_bytes=1_000_000)
        history.record("https://example.com/", "safe", datetime.now(timezone.utc))
        history.close()
        history = VisitHistory(self.directory, b"x" * 32, max_bytes=1_000_000)
        with self.assertRaises(ValueError):
            history.search("example")
        history.close()

    def test_delete_all(self):
        history = VisitHistory(self.directory, self.key, max_bytes=1_000_000)
        history.record("https://example.com/", "a", datetime.now(timezone.utc))
        self.assertEqual(history.delete_all(), 1)
        self.assertEqual(history.search(""), [])
        history.close()

    def test_capacity_failure_never_evicts_previous_visits(self):
        history = VisitHistory(self.directory, self.key, max_bytes=1_000_000)
        first = history.record("https://example.com/first", "first", datetime.now(timezone.utc))
        history.max_bytes = history._allocated_bytes()
        failed = False
        for index in range(30):
            try:
                history.record("https://example.com/" + str(index) + "x" * 1500, "second" * 80,
                               datetime.now(timezone.utc))
            except HistoryCapacityError:
                failed = True
                break
        self.assertTrue(failed)
        rows = history.search("")
        self.assertIn(first, [row["id"] for row in rows])
        history.close()


if __name__ == "__main__":
    unittest.main()
