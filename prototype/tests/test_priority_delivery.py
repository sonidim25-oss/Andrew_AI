from telegram_fixture import sent_text
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest

from andrewbot.controller import Controller
from andrewbot.dispatch import ControllerDispatch
from andrewbot.latency import LatencyMetrics
from andrewbot.store import Store
from andrewbot.telegram import OwnerRateLimit, TelegramAPI


def control(number=1, text='/status', owner=17):
    return {'update_id': number, 'message': {'from': {'id': owner},
        'chat': {'id': owner, 'type': 'private'}, 'text': text}}


class PriorityDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'fixture.db')
        self.addCleanup(self.store.close)
        self.metrics = LatencyMetrics()
        self.entered, self.release, self.system_sent = (threading.Event() for _ in range(3))
        self.inbox = queue.Queue()
        self.sent = []
        def opener(request, timeout):
            method = request.full_url.rsplit('/', 1)[-1]
            payload = json.loads(request.data)
            if method == 'getUpdates':
                try:
                    updates = self.inbox.get(timeout=0.02)
                except queue.Empty:
                    updates = []
                return io.BytesIO(json.dumps({'ok': True, 'result': updates}).encode())
            if payload['text'] == 'ordinary fixture':
                self.entered.set()
                if not self.release.wait(5):
                    raise TimeoutError('blocked fixture')
            self.sent.append(payload['text'])
            return io.BytesIO(json.dumps({'ok':True,'result':sent_text(payload['chat_id'],payload['text'])}).encode())
        system_sent = self.system_sent
        class FixtureTelegram(TelegramAPI):
            def send_message(self, chat_id, text):
                result = super().send_message(chat_id, text)
                if text != 'ordinary fixture':
                    system_sent.set()
                return result
        api = FixtureTelegram('fixture-token', opener=opener, metrics=self.metrics)
        class NoModel:
            def reply(self, messages):
                raise AssertionError('local controls must not invoke the model')
        self.controller = Controller(self.store, NoModel(), 17)
        self.dispatch = ControllerDispatch(self.store, self.controller, api, SimpleNamespace(tick=lambda: False),
            OwnerRateLimit(), 17, False, False, metrics=self.metrics)

    def start_blocked_send(self):
        self.store.queue_reply(17, 'ordinary fixture')
        self.dispatch.start()
        self.dispatch._reply_ready.set()
        self.assertTrue(self.entered.wait(1))

    def test_system_reply_overtakes_a_blocked_normal_send(self):
        try:
            self.start_blocked_send()
            self.inbox.put([control()])
            self.assertTrue(self.system_sent.wait(1), 'system reply waited for normal delivery')
            self.assertFalse(self.release.is_set())
            snapshot = self.metrics.snapshot()
            self.assertEqual(snapshot['send']['active_count'], 1)
            self.assertEqual(snapshot['send']['count'], 1)
            self.assertEqual(snapshot['control']['count'], 1)
            self.assertEqual(self.store.next_reply()['delivery_state'], 'sending')
        finally:
            self.release.set()
            self.dispatch.stop()

    def test_health_reports_blocked_sender_without_waiting_for_it(self):
        try:
            self.start_blocked_send()
            self.inbox.put([control(text='/health')])
            self.assertTrue(self.system_sent.wait(1))
            self.assertFalse(self.release.is_set())
            reply = self.sent[0]
            self.assertIn('Health (local snapshots)', reply)
            self.assertIn('normal: pending=0, sending=1, held=0', reply)
            self.assertNotIn('ordinary fixture', reply)
            self.assertEqual(self.store.connection.execute('SELECT count(*) FROM usage').fetchone()[0], 0)
        finally:
            self.release.set()
            self.dispatch.stop()

    def test_only_authenticated_local_controls_use_system_lane(self):
        values = [control(1), control(2, owner=99), control(3)]
        values[-1]['message']['forward_origin'] = {'type': 'user'}
        for value in values:
            self.store.enqueue_update(value)
        self.dispatch.process_updates()
        rows = self.store.connection.execute('SELECT lane FROM telegram_outbox').fetchall()
        self.assertEqual([row['lane'] for row in rows], ['system'])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM usage').fetchone()[0], 0)

    def test_shutdown_does_not_close_resources_used_by_a_blocked_sender(self):
        from andrewbot.main import close_controller_resources
        gateway_closed, history_closed = [], []
        stop = self.dispatch.stop
        try:
            self.start_blocked_send()
            self.dispatch.stop = lambda: stop(timeout=0.01)
            self.assertFalse(close_controller_resources(self.dispatch,
                SimpleNamespace(close=lambda: gateway_closed.append(True)),
                SimpleNamespace(close=lambda: history_closed.append(True)), self.store))
            self.assertEqual(gateway_closed, [True])
            self.assertEqual(history_closed, [])
            self.assertIsNotNone(self.store.next_reply())  # connection is still open
        finally:
            self.release.set()
            self.dispatch.stop = stop
            stop()

    def test_startup_recovery_does_not_let_other_connections_steal_a_live_claim(self):
        # Opening another connection is not itself a recovery operation.
        self.store.queue_reply(17, 'fixture')
        row = self.store.claim_reply('normal')
        other = Store(self.store.path)
        try:
            self.assertIsNone(other.claim_reply('normal'))
            self.assertFalse(other.hold_reply(row['id'], 'wrong-token'))
        finally:
            other.close()
