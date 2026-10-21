import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from andrewbot.appserver import LettaTurnError
from andrewbot.controller import Controller
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store


class SessionFailureLoggingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / 'controller.sqlite3')
        self.addCleanup(self.store.close)

        class Agent:
            def autonomous_turn(self, prompt, timeout=None):
                raise LettaTurnError('turn_failed')

        self.controller = Controller(self.store, Agent(), 17, autonomous_enabled=True)
        self.runner = SessionRunner(self.store, self.controller, Agent(), enabled=True)

    def test_bookkeeping_failures_are_logged_without_detail_and_session_still_stops(self):
        self.controller._command('/wake')
        failure = RuntimeError('PRIVATE credential text')
        with patch.object(self.store, 'finish_cycle', side_effect=failure), \
                patch.object(self.store, 'add_activity', side_effect=failure), \
                patch.object(self.runner, '_record_incident', side_effect=failure), \
                self.assertLogs(level='WARNING') as logs:
            self.assertFalse(self.runner.tick(datetime.now(timezone.utc)))
        output = '\n'.join(logs.output)
        for message in ('Failed cycle status could not be persisted',
                        'Cycle failure activity could not be persisted',
                        'Cycle failure incident summary could not be persisted'):
            self.assertIn(message, output)
        self.assertNotIn('PRIVATE', output)
        self.assertEqual(self.store.get_state('autonomy_error'), 'turn_failed')


if __name__ == '__main__':
    unittest.main()
