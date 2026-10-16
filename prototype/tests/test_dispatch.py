import tempfile
import unittest
import threading
import queue
import time
from pathlib import Path
from unittest.mock import Mock

from andrewbot.dispatch import ControllerDispatch
from andrewbot.store import Store
from andrewbot.telegram import OwnerRateLimit
from andrewbot.controller import Controller
from andrewbot.sessions import SessionRunner
from andrewbot.appserver import LettaTurnCancelled


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

    def test_persisted_pause_is_found_beyond_the_normal_inbox_page(self):
        for number in range(1, 101):
            self.store.enqueue_update(update(number))
        self.store.enqueue_update(update(101, '/pause'))
        self.dispatch.process_updates(controls_only=True)
        self.assertEqual(self.controller.handle_update.call_args.args[0]['update_id'], 101)

    def test_background_control_path_rejects_forwarded_and_nonowner_commands(self):
        forwarded = update(1, '/pause')
        forwarded['message']['forward_origin'] = {'type': 'hidden_user'}
        stranger = update(2, '/pause')
        stranger['message']['from']['id'] = 99
        for item in (forwarded, stranger):
            self.store.enqueue_update(item)
        self.dispatch.process_updates(controls_only=True)
        self.controller.handle_update.assert_not_called()
        self.assertEqual(len(self.store.pending_updates()), 2)


class ConcurrentOwnerControlsTests(unittest.TestCase):
    def run_control(self, controls):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.sqlite3')
            entered, cancelled, release, notified = (threading.Event() for _ in range(4))
            inbox = queue.Queue()
            replies = []
            class Telegram:
                def get_updates(self, **kwargs):
                    try:
                        return inbox.get(timeout=0.05)
                    except queue.Empty:
                        return []
                def send_message(self, owner, text):
                    replies.append(text)
                    notified.set()
            class Agent:
                def autonomous_turn(self, prompt, timeout):
                    entered.set()
                    release.wait(3)
                    if cancelled.is_set():
                        raise LettaTurnCancelled('Owner control')
                def cancel_active_turn(self):
                    cancelled.set()
                    release.set()
            agent = Agent()
            controller = Controller(store, agent, 17, autonomous_enabled=True)
            runner = SessionRunner(store, controller, agent, enabled=True)
            store.set_state('session_start_requested', 'owner_wake')
            dispatch = ControllerDispatch(store, controller, Telegram(), runner, OwnerRateLimit(), 17, True, False)
            dispatch.start()
            dispatch._updates_ready.set()
            worker = threading.Thread(target=dispatch.step)
            try:
                worker.start()
                self.assertTrue(entered.wait(1))
                inbox.put([update(index + 1, command) for index, command in enumerate(controls)])
                self.assertTrue(notified.wait(1), 'Owner controls waited for the blocked turn')
                if '/pause' in controls:
                    self.assertTrue(cancelled.wait(1))
                    worker.join(1)
                    self.assertFalse(worker.is_alive())
                    expected = 'owner_wake' if controls[-1] == '/wake' else 'paused'
                    deadline = time.monotonic() + 1
                    while store.get_state('active_session_status') != expected and time.monotonic() < deadline:
                        time.sleep(0.005)
                    self.assertEqual(store.get_state('active_session_status'), expected)
                    self.assertEqual(store.get_state('paused', '0'), '0' if expected == 'owner_wake' else '1')
                    if expected == 'owner_wake':
                        self.assertEqual(store.get_state('session_start_requested'), 'owner_wake')
                    self.assertEqual(store.connection.execute('SELECT status FROM cycles').fetchone()[0], 'stopped')
                    self.assertEqual(store.pending_diary_deliveries(), [])
                else:
                    self.assertFalse(cancelled.is_set())
                    self.assertTrue(worker.is_alive())
                return replies
            finally:
                release.set()
                worker.join(3)
                dispatch.stop()
                store.close()

    def test_pause_is_processed_and_turn_cancelled_while_model_is_blocked(self):
        replies = self.run_control(['/pause'])
        self.assertTrue(any('Paused' in reply for reply in replies))

    def test_status_remains_available_without_cancelling_active_work(self):
        self.run_control(['/status'])

    def test_pause_then_wake_preserves_new_wake_request(self):
        self.run_control(['/pause', '/wake'])


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
