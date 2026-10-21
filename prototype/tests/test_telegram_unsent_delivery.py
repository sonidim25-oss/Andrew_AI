import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from andrewbot.dispatch import ControllerDispatch
from andrewbot.store import Store
from andrewbot.telegram import TelegramAPI, TelegramNotSent, OwnerRateLimit, deliver_saved_message


RETRY_NOTICE = 'Delivery retry; you may have already received this part.\n\n'
TOKEN = 'fixture-secret-token'


class ProvenUnsentClassificationTests(unittest.TestCase):
    def call_with(self, error):
        api = TelegramAPI(TOKEN, opener=Mock(side_effect=error))
        with self.assertRaises(RuntimeError) as raised:
            api.call('sendMessage', {'chat_id': 17, 'text': 'hello'})
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        return raised.exception

    def http_error(self, code):
        return HTTPError(f'https://api.telegram.org/bot{TOKEN}/sendMessage', code, 'fixture', {}, None)

    def test_name_resolution_and_refused_connections_prove_nothing_was_sent(self):
        for error in (URLError(socket.gaierror(-3, 'Temporary failure in name resolution')),
                      URLError(ConnectionRefusedError(111, 'Connection refused')),
                      ConnectionRefusedError(111, 'Connection refused'),
                      self.http_error(429)):
            with self.subTest(error=type(error).__name__):
                self.assertIsInstance(self.call_with(error), TelegramNotSent)

    def test_ambiguous_failures_remain_uncertain(self):
        for error in (URLError(TimeoutError('timed out')), TimeoutError('timed out'),
                      URLError(ConnectionResetError(104, 'reset')), ConnectionResetError(104, 'reset'),
                      self.http_error(500), self.http_error(502), self.http_error(400)):
            with self.subTest(error=repr(error)):
                self.assertNotIsInstance(self.call_with(error), TelegramNotSent)

    def test_unsent_later_piece_of_a_long_text_is_not_reported_as_unsent(self):
        api = TelegramAPI(TOKEN)
        sent = []
        def call(method, payload):
            if sent:
                raise TelegramNotSent('Telegram API was not reached')
            sent.append(payload['text'])
            return {'message_id': 1, 'chat': {'id': payload['chat_id']}, 'text': payload['text']}
        api.call = call
        with self.assertRaises(RuntimeError) as raised:
            api.send_message(17, 'x' * 5000)
        self.assertNotIsInstance(raised.exception, TelegramNotSent)


class UnsentReplyDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'state.db')
        self.addCleanup(lambda: self.store.close())
        self.api = TelegramAPI('fixture-token')
        self.calls, self.acknowledged, self.failures = [], [], {}
        def call(method, payload):
            self.calls.append(payload['text'])
            failure = self.failures.get(len(self.calls))
            if failure is not None:
                raise failure
            self.acknowledged.append(payload['text'])
            return {'message_id': len(self.calls), 'chat': {'id': payload['chat_id']}, 'text': payload['text']}
        self.api.call = call
        self.dispatch = ControllerDispatch(self.store, Mock(), self.api, Mock(), OwnerRateLimit(), 17, False, False)

    def progress(self):
        row = self.store.connection.execute('SELECT next_chunk,chunk_attempts,chunk_token FROM telegram_delivery_progress').fetchone()
        return tuple(row)

    def test_unsent_reply_is_requeued_and_delivered_once_without_retry_notice(self):
        self.store.queue_reply(17, 'Fixture reply.')
        self.failures[1] = TelegramNotSent('Telegram API was not reached')
        with self.assertRaises(TelegramNotSent):
            self.dispatch.deliver_reply()
        row = self.store.next_reply()
        self.assertEqual((row['delivery_state'], row['attempts'], row['claim_token']), ('pending', 0, None))
        self.assertEqual(self.progress(), (0, 0, ''))
        self.assertTrue(self.dispatch.deliver_reply())
        self.assertEqual(self.acknowledged, ['Fixture reply.'])
        self.assertIsNone(self.store.next_reply())

    def test_unsent_later_part_resumes_without_repeating_acknowledged_parts(self):
        text = ('A complete reply. 🙂\n' * 600) + 'The ending is preserved.'
        self.store.queue_reply(17, text)
        self.failures[2] = TelegramNotSent('Telegram API was not reached')
        with self.assertRaises(TelegramNotSent):
            self.dispatch.deliver_reply()
        self.assertEqual(self.store.next_reply()['delivery_state'], 'pending')
        self.assertEqual(self.progress(), (1, 0, ''))
        self.assertTrue(self.dispatch.deliver_reply())
        self.assertIsNone(self.store.next_reply())
        self.assertEqual(self.calls.count(self.acknowledged[0]), 1)
        self.assertFalse(any(text.startswith(RETRY_NOTICE) for text in self.acknowledged))
        self.assertEqual(''.join(self.acknowledged), text)

    def test_reply_survives_restart_after_unsent_failure(self):
        self.store.queue_reply(17, 'Fixture reply.')
        self.failures[1] = TelegramNotSent('Telegram API was not reached')
        with self.assertRaises(TelegramNotSent):
            self.dispatch.deliver_reply()
        self.store.recover_reply_claims()
        self.assertEqual(self.store.next_reply()['delivery_state'], 'pending')

    def test_ambiguous_failure_still_holds_the_reply(self):
        self.store.queue_reply(17, 'Fixture reply.')
        self.failures[1] = RuntimeError('Telegram API request failed')
        with self.assertRaises(RuntimeError):
            self.dispatch.deliver_reply()
        self.assertEqual(self.store.next_reply()['delivery_state'], 'held')
        self.assertFalse(self.dispatch.deliver_reply())
        self.assertEqual(len(self.calls), 1)

    def test_revocation_refuses_a_changed_admission_and_the_reply_is_held(self):
        self.store.queue_reply(17, 'Fixture reply.')
        self.failures[1] = TelegramNotSent('Telegram API was not reached')
        with patch.object(self.store, 'revoke_unsent_telegram_chunk', side_effect=ValueError('changed')):
            with self.assertRaises(ValueError):
                self.dispatch.deliver_reply()
        self.assertEqual(self.store.next_reply()['delivery_state'], 'held')
        self.assertEqual(self.progress()[1], 1)

    def test_revocation_requires_the_current_admission_token(self):
        deliver_saved_message_context = {}
        original = self.store.telegram_chunk_context
        def capture(*args):
            deliver_saved_message_context.update(original(*args))
            return original(*args)
        self.failures[1] = RuntimeError('Telegram API request failed')
        with patch.object(self.store, 'telegram_chunk_context', side_effect=capture):
            with self.assertRaises(RuntimeError):
                deliver_saved_message(self.api, self.store, 'reply', 7, 17, 'Fixture reply.')
        stale = dict(deliver_saved_message_context, chunk_token='stale')
        with self.assertRaises(ValueError):
            self.store.revoke_unsent_telegram_chunk(stale)
        self.assertEqual(self.progress()[1], 1)


class UnsentReplyBackoffTests(unittest.TestCase):
    def test_reply_lane_backs_off_exponentially_and_resets_after_success(self):
        dispatch = ControllerDispatch(Mock(), Mock(), Mock(), Mock(), OwnerRateLimit(), 17, False, False)
        waits = []
        stop = Mock()
        stop.is_set.side_effect = lambda: len(waits) >= 9
        stop.wait.side_effect = lambda delay: waits.append(delay)
        dispatch._poll_stop = stop
        dispatch._reply_ready = Mock()
        outcomes = iter([TelegramNotSent('x')] * 8 + [True, False, TelegramNotSent('x')])
        def deliver(lane):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        dispatch._deliver_reply = deliver
        with self.assertLogs(level='WARNING'):
            dispatch._reply_loop('normal')
        self.assertEqual(waits, [1, 2, 4, 8, 16, 32, 60, 60, 1])


if __name__ == '__main__':
    unittest.main()
