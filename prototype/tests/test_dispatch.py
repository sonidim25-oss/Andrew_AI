import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from andrewbot.dispatch import ControllerDispatch
from andrewbot.store import Store
from andrewbot.telegram import OwnerRateLimit


def update(number=1, text='hello'):
    return {'update_id': number, 'message': {'from': {'id': 17},
            'chat': {'id': 17, 'type': 'private'}, 'text': text}}


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.db'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.controller = Mock()
        self.controller.handle_update.return_value = 'reply'
        self.controller.flush_queued.return_value = None
        self.telegram = Mock()
        self.telegram.get_updates.return_value = []
        self.sessions = Mock()
        self.dispatch = ControllerDispatch(self.store, self.controller, self.telegram,
            self.sessions, OwnerRateLimit(), 17, True, True)

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.dispatch.store = self.store

    def test_delivery_failure_and_restart_do_not_repeat_actions(self):
        self.store.enqueue_update(update())
        self.telegram.send_message.side_effect = RuntimeError('offline')
        self.dispatch.step()
        self.assertEqual(self.controller.handle_update.call_count, 1)
        self.assertEqual(self.store.pending_updates(), [])
        self.assertIsNotNone(self.store.next_reply())
        self.reopen()
        self.telegram.send_message.side_effect = None
        self.dispatch.step()
        self.assertEqual(self.controller.handle_update.call_count, 1)
        self.assertIsNone(self.store.next_reply())
        self.assertIn('Delivery retry', self.telegram.send_message.call_args.args[1])

    def test_poll_failure_does_not_stall_saved_updates_wakes_or_session(self):
        self.store.enqueue_update(update())
        self.telegram.get_updates.side_effect = RuntimeError('offline')
        self.assertFalse(self.dispatch.step())
        self.controller.handle_update.assert_called_once()
        self.controller.check_scheduled_wake.assert_called_once()
        self.controller.check_fallback_wake.assert_called_once()
        self.sessions.tick.assert_called_once()

    def test_crash_during_action_is_not_automatically_replayed(self):
        self.store.enqueue_update(update())
        self.assertTrue(self.store.claim_update(1))
        self.reopen()
        self.assertEqual(self.store.recover_update_claims(17), 1)
        self.assertEqual(self.store.recover_update_claims(17), 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM telegram_uncertain').fetchone()[0], 1)
        self.dispatch.step()
        self.controller.handle_update.assert_not_called()
        self.assertIn('uncertain', self.telegram.send_message.call_args.args[1])

    def test_exception_after_action_does_not_rerun_it(self):
        self.store.enqueue_update(update())
        self.controller.handle_update.side_effect = RuntimeError('after side effect')
        self.dispatch.step()
        self.dispatch.step()
        self.assertEqual(self.controller.handle_update.call_count, 1)

    def test_rate_limit_defers_chat_but_allows_owner_pause(self):
        self.dispatch.limiter = Mock()
        self.dispatch.limiter.allow.return_value = False
        self.store.enqueue_update(update(1))
        self.store.enqueue_update(update(2, '/pause'))
        self.dispatch.step()
        self.assertEqual(self.controller.handle_update.call_args.args[0]['update_id'], 2)
        self.assertEqual([row[0] for row in self.store.pending_updates()], [1])

    def test_failed_wake_check_does_not_block_session_or_delivery(self):
        self.controller.check_scheduled_wake.side_effect = RuntimeError('bad wake')
        self.store.queue_reply(17, 'saved reply')
        self.dispatch.step()
        self.sessions.tick.assert_called_once()
        self.telegram.send_message.assert_called_once_with(17, 'saved reply')

    def test_stop_during_poll_starts_no_new_model_work(self):
        self.telegram.get_updates.side_effect = lambda **kwargs: setattr(self.dispatch, 'should_stop', lambda: True) or []
        self.store.enqueue_update(update())
        self.dispatch.step()
        self.sessions.tick.assert_not_called()
        self.controller.handle_update.assert_not_called()
        self.controller.flush_queued.assert_not_called()

    def test_chat_backlog_cannot_starve_pause_or_scheduler(self):
        for number in range(1, 10):
            self.store.enqueue_update(update(number))
        self.store.enqueue_update(update(10, '/pause'))
        self.dispatch.step()
        self.assertEqual([call.args[0]['update_id'] for call in self.controller.handle_update.call_args_list], [1, 10])
        self.sessions.tick.assert_called_once()


class QueueBatchTests(unittest.TestCase):
    def test_batches_claim_complete_messages_without_losing_the_older_ones(self):
        from andrewbot.controller import Controller
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.db')
            self.addCleanup(store.close)
            model = Mock()
            model.reply.return_value = ('read', {'accounted_by_gateway': True})
            controller = Controller(store, model, 17)
            messages = [str(i) + '🙂' * 3000 for i in range(10)]
            for message in messages:
                store.add_message(17, 'user', message, queued=True)
            for _ in range(4):
                controller.flush_queued()
            forwarded = '\n'.join(call.args[0][0]['content'] for call in model.reply.call_args_list)
            for message in messages:
                self.assertEqual(forwarded.count(message), 1)
            self.assertEqual(store.queued_messages(17), [])
