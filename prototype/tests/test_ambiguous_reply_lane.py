"""Ambiguous messages stay held while the real reply loop serves later work."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from andrewbot.dispatch import ControllerDispatch
from andrewbot.store import Store
from andrewbot.telegram import OwnerRateLimit


class AmbiguousReplyLaneTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / 'state.db'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.calls = []
        self.fail_at = 1
        self.telegram = Mock()
        self.telegram.send_message.side_effect = self.send
        self.dispatch = ControllerDispatch(self.store, Mock(), self.telegram,
                                          Mock(), OwnerRateLimit(), 17, False, False)

    def send(self, chat_id, text):
        self.calls.append(text)
        if len(self.calls) == self.fail_at:
            raise TimeoutError('delivery may have succeeded; confirmation lost')
        return {'message_id': len(self.calls), 'chat': {'id': chat_id}, 'text': text}

    def run_lane(self, lane):
        # Drive actual outer-loop iterations without wall-clock waits. The
        # third iteration stops a broken lane too, so regressions cannot hang.
        ready = Mock()
        iterations = []
        def wait(_timeout):
            iterations.append(1)
            if len(iterations) == 3:
                self.dispatch._poll_stop.set()
        ready.wait.side_effect = wait
        setattr(self.dispatch, '_system_reply_ready' if lane == 'system' else '_reply_ready', ready)
        self.dispatch._reply_loop(lane)
        self.dispatch._poll_stop.clear()

    def assert_only_held_reply(self, text, lane='normal'):
        rows = self.store.connection.execute('SELECT * FROM telegram_outbox ORDER BY id').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['text'], rows[0]['lane'], rows[0]['delivery_state'], rows[0]['claim_token']),
                         (text, lane, 'held', None))

    def test_timeout_does_not_stall_either_reply_lane(self):
        for lane in ('normal', 'system'):
            with self.subTest(lane=lane):
                self.store.queue_reply(17, 'Uncertain ' + lane, lane=lane)
                self.store.queue_reply(17, 'Later ' + lane, lane=lane)
                self.fail_at = len(self.calls) + 1
                with self.assertLogs(level='WARNING'):
                    self.run_lane(lane)
                self.assertEqual(self.calls[-2:], ['Uncertain ' + lane, 'Later ' + lane])
                rows = self.store.connection.execute(
                    'SELECT delivery_state,claim_token FROM telegram_outbox WHERE lane=?', (lane,)).fetchall()
                self.assertEqual([tuple(row) for row in rows], [('held', None)])

    def test_partial_message_timeout_does_not_stall_or_repeat_any_chunk(self):
        self.fail_at = 2
        text = 'x' * 5000
        self.store.queue_reply(17, text)
        self.store.queue_reply(17, 'Later reply')
        with self.assertLogs(level='WARNING'):
            self.run_lane('normal')
        self.assertEqual(self.calls, ['x' * 3800, 'x' * 1200, 'Later reply'])
        self.assert_only_held_reply(text)

    def test_restart_preserves_held_message_and_delivers_later_reply(self):
        self.store.queue_reply(17, 'Uncertain reply')
        with self.assertRaises(TimeoutError):
            self.dispatch._deliver_reply('normal')
        self.store.queue_reply(17, 'Later reply')
        self.store.close()
        self.store = Store(self.path)
        self.store.recover_reply_claims()
        self.dispatch.store = self.store
        self.run_lane('normal')
        self.assertEqual(self.calls, ['Uncertain reply', 'Later reply'])
        self.assert_only_held_reply('Uncertain reply')
