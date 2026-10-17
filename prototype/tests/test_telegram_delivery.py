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
            return {'message_id': len(self.calls), 'chat': {'id': payload['chat_id']}, 'text': payload['text']}
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
        self.store.recover_diary_claims()
        self.assertTrue(self.store.retry_uncertain_diary_delivery(entry_id))
        self.assertEqual(deliver_pending_diary(self.api, self.store, 17), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.diary_delivery_state(entry_id), 'delivered')

    def test_long_reply_holds_uncertain_part_without_repeating_model_or_send(self):
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
        self.assertEqual(self.store.next_reply()['delivery_state'], 'held')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls.count(first), 1)
        self.assertEqual(self.store.next_reply()['text'], text)
        controller.handle_update.assert_not_called()
        progress = self.store.connection.execute('SELECT * FROM telegram_delivery_progress').fetchone()
        self.assertEqual((progress['next_chunk'], progress['chunk_attempts']), (1, 1))

    def test_final_reply_ack_is_not_repeated_after_finish_failure(self):
        self.store.queue_reply(17, 'Fixture reply.')
        dispatch = ControllerDispatch(self.store, Mock(), self.api, Mock(), OwnerRateLimit(), 17, False, False)
        with patch.object(self.store, 'finish_reply', side_effect=RuntimeError('fixture crash')):
            with self.assertRaises(RuntimeError):
                dispatch.deliver_reply()
        self.assertEqual(len(self.calls), 1)
        self.reopen()
        self.store.recover_reply_claims()
        dispatch.store = self.store
        dispatch.deliver_reply()
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.store.next_reply())

    def test_generic_sender_refuses_a_previously_attempted_unacknowledged_part(self):
        from andrewbot.telegram import deliver_saved_message
        self.fail_at = 1
        with self.assertRaises(TimeoutError):
            deliver_saved_message(self.api, self.store, 'reply', 7, 17, 'Fixture reply.')
        self.assertFalse(deliver_saved_message(self.api, self.store, 'reply', 7, 17, 'Fixture reply.'))
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
