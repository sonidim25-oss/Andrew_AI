import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from andrewbot.diary import deliver_pending_diary, format_diary_entry
from andrewbot.dispatch import ControllerDispatch
from andrewbot.store import Store
from andrewbot.telegram import TelegramAPI, OwnerRateLimit


RETRY_NOTICE = 'Delivery retry; you may have already received this part.\n\n'


class MultipartDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.db'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.api = TelegramAPI('fixture-token')
        self.calls, self.acknowledged = [], []
        self.fail_at = None
        def call(method, payload):
            self.calls.append(payload['text'])
            if len(self.calls) == self.fail_at:
                raise TimeoutError('fixture acknowledgement lost')
            self.acknowledged.append(payload['text'])
            return {'message_id': len(self.calls)}
        self.api.call = call

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)

    def test_valid_unicode_diary_is_delivered_completely(self):
        content = 'Today I reflected. ' + '🙂' * 2100 + '\nTomorrow I will check my memory.'
        entry_id = self.store.create_diary_entry('2026-10-16', content)
        rendered = format_diary_entry(self.store.diary_entry('2026-10-16'))
        self.assertEqual(deliver_pending_diary(self.api, self.store, 17), 1)
        self.assertGreater(len(self.calls), 1)
        self.assertEqual(''.join(self.acknowledged), rendered)
        self.assertEqual(self.store.diary_delivery_state(entry_id), 'delivered')
        for text in self.calls:
            self.assertLessEqual(len(text.encode('utf-16-le')), 8000)

    def test_partial_diary_requires_retry_and_resumes_after_restart(self):
        content = 'Today I reflected. ' + '🙂' * 2100 + '\nTomorrow I will check my memory.'
        entry_id = self.store.create_diary_entry('2026-10-16', content)
        rendered = format_diary_entry(self.store.diary_entry('2026-10-16'))
        self.fail_at = 2
        self.assertEqual(deliver_pending_diary(self.api, self.store, 17), 0)
        self.assertEqual(self.store.diary_delivery_state(entry_id), 'uncertain')
        first = self.acknowledged[0]
        self.reopen()
        self.assertEqual(deliver_pending_diary(self.api, self.store, 17), 0)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.store.retry_uncertain_diary_delivery(entry_id))
        self.assertEqual(deliver_pending_diary(self.api, self.store, 17), 1)
        self.assertEqual(self.calls.count(first), 1, 'An acknowledged part was repeated')
        self.assertTrue(self.calls[-1].startswith(RETRY_NOTICE))
        recovered = ''.join(text.removeprefix(RETRY_NOTICE) for text in self.acknowledged)
        self.assertEqual(recovered, rendered)
        self.assertEqual(self.store.diary_delivery_state(entry_id), 'delivered')
        self.assertEqual(self.store.diary_entry('2026-10-16')['content'], content)

    def test_restart_after_last_ack_does_not_resend_completed_diary_parts(self):
        entry_id = self.store.create_diary_entry('2026-10-16', 'I wrote a complete account of my day.')
        with patch.object(self.store, 'mark_diary_delivered', side_effect=RuntimeError('fixture crash')):
            with self.assertRaises(RuntimeError):
                deliver_pending_diary(self.api, self.store, 17)
        self.assertEqual(len(self.calls), 1)
        self.reopen()
        self.assertTrue(self.store.retry_uncertain_diary_delivery(entry_id))
        self.assertEqual(deliver_pending_diary(self.api, self.store, 17), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.diary_delivery_state(entry_id), 'delivered')

    def test_long_reply_resumes_without_repeating_acknowledged_parts_or_model_work(self):
        text = ('A complete reply. 🙂\n' * 600) + 'The ending is preserved.'
        self.store.queue_reply(17, text)
        self.fail_at = 2
        controller = Mock()
        dispatch = ControllerDispatch(self.store, controller, self.api, Mock(), OwnerRateLimit(), 17, False, False)
        with self.assertRaises(TimeoutError):
            dispatch.deliver_reply()
        first = self.acknowledged[0]
        self.assertIsNotNone(self.store.next_reply())
        self.reopen()
        dispatch.store = self.store
        dispatch.deliver_reply()
        self.assertIsNone(self.store.next_reply())
        self.assertEqual(self.calls.count(first), 1)
        self.assertTrue(self.calls[2].startswith(RETRY_NOTICE))
        self.assertFalse(self.calls[-1].startswith(RETRY_NOTICE))
        self.assertEqual(''.join(part.removeprefix(RETRY_NOTICE) for part in self.acknowledged), text)
        for part in self.calls:
            self.assertLessEqual(len(part.encode('utf-16-le')), 8000)
        controller.handle_update.assert_not_called()
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM telegram_delivery_progress').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
