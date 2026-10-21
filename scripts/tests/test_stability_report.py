import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'prototype'))
from andrewbot.appserver import LettaTurnError
from andrewbot.controller import Controller
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store

spec = importlib.util.spec_from_file_location('stability_report', ROOT / 'scripts/stability-report.py')
stability = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stability)


class StabilityReportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'controller.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.outcomes = []
        outer = self

        class Agent:
            def autonomous_turn(self, prompt, timeout=None):
                outcome = outer.outcomes.pop(0)
                if outcome:
                    raise outcome
                return 'done', {}

        self.controller = Controller(self.store, Agent(), 17, autonomous_enabled=True)
        self.runner = SessionRunner(self.store, self.controller, Agent(), enabled=True)

    def run_report(self, *extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            stability.main(['--database', str(self.path), '--days', '3', *extra])
        return output.getvalue()

    def test_counts_completed_cycles_owner_stops_and_categories_from_real_paths(self):
        self.outcomes = [None, LettaTurnError('turn_failed')]
        self.controller._command('/wake')
        self.assertTrue(self.runner.tick(datetime.now(timezone.utc)))
        self.assertFalse(self.runner.tick(datetime.now(timezone.utc)))
        self.store.add_activity('Journaled model turn held (category=tool_uncertain)')
        self.controller._command('/wake')
        self.store.close()
        before = self.path.read_bytes()
        result = json.loads(self.run_report('--json'))
        self.assertEqual(self.path.read_bytes(), before)
        today = result['days'][-1]
        self.assertEqual(today['cycles'], {'complete': 1, 'failed': 1})
        self.assertEqual(today['session_failures'], {'turn_failed': 1})
        self.assertEqual(today['journaled_holds'], {'tool_uncertain': 1})
        self.assertEqual(today['stops_needing_owner'], 2)
        self.assertEqual(today['owner_wakes'], 2)
        self.assertEqual(today['sessions_started']['owner_wake'], 1)
        self.assertEqual(result['totals']['failure_categories'], {'turn_failed': 1, 'tool_uncertain': 1})
        self.assertEqual(result['current']['autonomy_error'], '')
        self.assertEqual(len(result['days']), 3)

    def test_text_output_and_current_error_without_content(self):
        self.store.add_message(17, 'user', 'PRIVATE owner text', queued=True)
        self.store.record_session_failure('provider_attempt_uncertain', 17)
        self.store.close()
        text = self.run_report()
        self.assertIn('provider_attempt_uncertain×1', text)
        self.assertIn('"autonomy_error": "provider_attempt_uncertain"', text)
        self.assertNotIn('PRIVATE', text)

    def test_parsed_activity_phrases_still_exist_in_controller_source(self):
        source = ''.join(path.read_text() for path in (ROOT / 'prototype/andrewbot').glob('*.py'))
        for phrase in ('Autonomous session started (', 'Session failed (category=', '); automatic model calls stopped',
                       'Journaled model turn held (category=', 'Provider unreachable before any request was sent; autonomous retry',
                       'Owner woke Andrew', 'Owner explicitly authorized uncertain message retry'):
            self.assertIn(phrase, source)


if __name__ == '__main__':
    unittest.main()
