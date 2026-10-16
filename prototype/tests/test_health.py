from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from andrewbot.controller import Controller
from andrewbot.dispatch import ControllerDispatch
from andrewbot.store import Store
from andrewbot.telegram import OwnerRateLimit


def update(owner=17):
    return {'update_id': 1, 'message': {'from': {'id': owner},
        'chat': {'id': owner, 'type': 'private'}, 'text': '/health'}}


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'fixture.db')
        self.addCleanup(self.store.close)
        self.model = Mock()
        self.controller = Controller(self.store, self.model, 17)

    def test_empty_health_is_local_and_reports_unavailable_ages(self):
        reply = self.controller.handle_update(update())
        self.assertIn('Health', reply)
        self.assertIn('unavailable', reply)
        self.assertIn('Queued messages: 0', reply)
        self.model.reply.assert_not_called()

    def test_nonowner_cannot_request_health_snapshot(self):
        reply = self.controller.handle_update(update(99))
        self.assertIsNone(reply)
        self.model.reply.assert_not_called()

    def test_health_counts_saved_work_without_exposing_content(self):
        now = datetime.now(timezone.utc)
        self.store.add_message(17, 'user', 'SECRET INPUT', queued=True)
        self.store.queue_reply(17, 'SECRET REPLY')
        self.store.set_state('autonomy_error', 'SECRET ERROR')
        self.store.set_state('paused', '1')
        with self.store.connection:
            self.store.connection.execute('UPDATE telegram_outbox SET created_at=?',
                                          ((now - timedelta(seconds=20)).isoformat(),))
        dispatch = ControllerDispatch(self.store, self.controller, Mock(), Mock(), OwnerRateLimit(),
                                       17, False, False)
        reply = self.controller.handle_update(update())
        self.assertIn('Queued messages: 1', reply)
        self.assertIn('normal: pending=1', reply)
        self.assertIn('Model: idle', reply)
        self.assertIn('paused=yes', reply)
        self.assertNotIn('SECRET', reply)
        self.model.reply.assert_not_called()
        snapshot = self.store.health_snapshot(17, now=now)
        self.assertEqual(snapshot['outbox']['normal']['oldest_seconds'], 20)
        with self.store.connection:
            self.store.connection.execute('UPDATE telegram_outbox SET created_at=NULL')
        self.assertIsNone(self.store.health_snapshot(17)['outbox']['normal']['oldest_seconds'])

    def test_health_reply_uses_system_lane(self):
        dispatch = ControllerDispatch(self.store, self.controller, Mock(), Mock(), OwnerRateLimit(),
                                       17, False, False)
        self.store.enqueue_update(update())
        dispatch.process_updates(controls_only=True)
        reply = self.store.next_reply()
        self.assertEqual(reply['lane'], 'system')
        self.assertIn('Health', reply['text'])
