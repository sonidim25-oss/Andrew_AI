import threading
import unittest

from andrewbot import workers


class ModelWorkerTests(unittest.TestCase):
    def test_wakes_coalesce_and_stop_prevents_another_action(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def action():
            calls.append(1)
            entered.set()
            release.wait(2)
        self.assertTrue(hasattr(workers, 'ModelWorker'), 'serialized worker is missing')
        worker = workers.ModelWorker(action, lambda: False)
        worker.start()
        self.addCleanup(lambda: worker.stop())
        worker.wake()
        self.assertTrue(entered.wait(1))
        for _ in range(100):
            worker.wake()
        self.assertEqual(worker.snapshot()['state'], 'running')
        self.assertEqual(calls, [1])
        self.assertFalse(worker.stop(timeout=0))
        release.set()
        self.assertTrue(worker.stop())
        self.assertEqual(calls, [1])
        self.assertEqual(worker.snapshot()['state'], 'stopped')

    def test_exception_is_sanitized_and_worker_can_be_woken_again(self):
        first, second = threading.Event(), threading.Event()
        calls = []
        def action():
            calls.append(1)
            if len(calls) == 1:
                first.set()
                raise RuntimeError('SECRET payload')
            second.set()
        self.assertTrue(hasattr(workers, 'ModelWorker'), 'serialized worker is missing')
        worker = workers.ModelWorker(action, lambda: False)
        worker.start()
        try:
            worker.wake()
            self.assertTrue(first.wait(1))
            worker.wake()
            self.assertTrue(second.wait(1))
            self.assertNotIn('SECRET', repr(worker.snapshot()))
        finally:
            self.assertTrue(worker.stop())
        self.assertEqual(worker.snapshot()['failures'], 1)
        self.assertIsNotNone(worker.snapshot()['last_completion_at'])

class DispatchWorkerTests(unittest.TestCase):
    def test_blocked_model_keeps_scheduler_and_system_reply_responsive(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock
        from andrewbot.dispatch import ControllerDispatch
        from andrewbot.store import Store
        from andrewbot.telegram import OwnerRateLimit
        entered, release, replied = threading.Event(), threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.db')
            controller, telegram, sessions = Mock(), Mock(), Mock()
            controller.flush_queued.return_value = None
            def handle(value, **kwargs):
                if value['message']['text'] == '/status':
                    return 'system health'
                entered.set()
                release.wait(5)
                return 'chat complete'
            controller.handle_update.side_effect = handle
            telegram.get_updates.return_value = []
            telegram.send_message.side_effect = lambda chat, text: replied.set() if text == 'system health' else None
            dispatch = ControllerDispatch(store, controller, telegram, sessions, OwnerRateLimit(), 17, True, True)
            def update(number, text):
                return {'update_id': number, 'message': {'text': text, 'from': {'id': 17},
                    'chat': {'id': 17, 'type': 'private'}}}
            store.enqueue_update(update(1, 'chat'))
            dispatch.start()
            try:
                # A synchronous step fails to return until the fake model times out.
                scheduler_returned = threading.Event()
                scheduler = threading.Thread(target=lambda: (dispatch.step(), scheduler_returned.set()))
                scheduler.start()
                self.assertTrue(entered.wait(2))
                self.assertTrue(scheduler_returned.wait(0.3), 'scheduler blocked on model')
                self.assertEqual(controller.check_scheduled_wake.call_count, 1)
                store.enqueue_update(update(2, '/status'))
                self.assertTrue(replied.wait(2))
                self.assertEqual(sessions.tick.call_count, 0)
                # Work has not been claimed just because a worker was notified.
                store.enqueue_update(update(3, 'later chat'))
                dispatch._model_worker.wake()
                self.assertIsNone(store.connection.execute(
                    'SELECT 1 FROM telegram_claims WHERE update_id=3').fetchone())
            finally:
                dispatch.should_stop = lambda: True
                release.set()
                scheduler.join(3)
                self.assertTrue(dispatch.stop())
                store.close()

    def test_chat_queued_and_autonomy_share_one_model_worker(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock
        from andrewbot.dispatch import ControllerDispatch
        from andrewbot.store import Store
        from andrewbot.telegram import OwnerRateLimit
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        lock = threading.Lock()
        calls, threads, active = [], [], [0, 0]
        def model_operation(kind):
            with lock:
                active[0] += 1
                active[1] = max(active)
                calls.append(kind)
                threads.append(threading.get_ident())
            if kind == 'chat':
                entered.set()
                release.wait(3)
            with lock:
                active[0] -= 1
            if kind == 'autonomy':
                finished.set()
            return None
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.db')
            controller, telegram, sessions = Mock(), Mock(), Mock()
            controller.handle_update.side_effect = lambda *args, **kwargs: model_operation('chat')
            controller.flush_queued.side_effect = lambda: model_operation('queued')
            sessions.tick.side_effect = lambda: model_operation('autonomy')
            telegram.get_updates.return_value = []
            store.enqueue_update({'update_id': 1, 'message': {'text': 'chat', 'from': {'id': 17},
                'chat': {'id': 17, 'type': 'private'}}})
            dispatch = ControllerDispatch(store, controller, telegram, sessions, OwnerRateLimit(), 17, True, True)
            dispatch.start()
            try:
                dispatch._updates_ready.set()
                dispatch.step()
                self.assertTrue(entered.wait(1))
                for _ in range(10):
                    dispatch._model_worker.wake()
                self.assertEqual(calls, ['chat'])
                release.set()
                self.assertTrue(finished.wait(1))
            finally:
                release.set()
                self.assertTrue(dispatch.stop())
                store.close()
            self.assertEqual(calls[:3], ['chat', 'queued', 'autonomy'])
            self.assertEqual(active[1], 1)
            self.assertEqual(len(set(threads)), 1)

class AdmissionTests(unittest.TestCase):
    def test_shutdown_during_flush_does_not_start_autonomy(self):
        from unittest.mock import Mock
        from andrewbot.dispatch import ControllerDispatch
        from andrewbot.telegram import OwnerRateLimit
        entered, release = threading.Event(), threading.Event()
        store, controller, telegram, sessions = Mock(), Mock(), Mock(), Mock()
        store.pending_updates.return_value = []
        telegram.get_updates.return_value = []
        def flush():
            entered.set()
            release.wait(2)
        controller.flush_queued.side_effect = flush
        dispatch = ControllerDispatch(store, controller, telegram, sessions, OwnerRateLimit(), 17, True, True)
        dispatch._model_worker.start()
        dispatch._model_worker.wake()
        self.assertTrue(entered.wait(1))
        self.assertFalse(dispatch.stop(timeout=0))
        release.set()
        self.assertTrue(dispatch.stop())
        sessions.tick.assert_not_called()

class DiaryWorkerTests(unittest.TestCase):
    def test_active_diary_sender_cannot_be_reauthorized_by_retry(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock
        from andrewbot.store import Store
        from andrewbot.diary import deliver_pending_diary
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.db')
            entry = store.create_diary_entry('2026-10-16', 'Today I tested delivery.', 'diary')
            telegram = Mock()
            telegram.send_message.side_effect = lambda *args: (entered.set(), release.wait(3))
            sender = threading.Thread(target=lambda: deliver_pending_diary(telegram, store, 17))
            sender.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(store.retry_uncertain_diary_delivery(entry),
                                 'active send was authorized for duplicate delivery')
                self.assertEqual(deliver_pending_diary(telegram, store, 17), 0)
            finally:
                release.set()
                sender.join(3)
            self.assertEqual(telegram.send_message.call_count, 1)
            self.assertEqual(store.diary_delivery_state(entry), 'delivered')
            store.close()

    def test_diary_worker_does_not_block_scheduler_or_close_shared_storage(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock
        from andrewbot.dispatch import ControllerDispatch
        from andrewbot.main import close_controller_resources
        from andrewbot.store import Store
        from andrewbot.telegram import OwnerRateLimit
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.db')
            controller, telegram, sessions = Mock(), Mock(), Mock()
            telegram.get_updates.return_value = []
            controller.flush_queued.return_value = None
            def diary(should_stop):
                entered.set()
                release.wait(4)
            dispatch = ControllerDispatch(store, controller, telegram, sessions, OwnerRateLimit(),
                17, True, True, diary_action=diary)
            dispatch.start()
            try:
                dispatch._updates_ready.set()
                dispatch.step()
                self.assertTrue(entered.wait(1))
                controller.check_scheduled_wake.assert_called_once()
                history, gateway = Mock(), Mock()
                self.assertFalse(close_controller_resources(dispatch, gateway, history, store))
                history.close.assert_not_called()
                self.assertEqual(store.connection.execute('SELECT 1').fetchone()[0], 1)
            finally:
                release.set()
                self.assertTrue(dispatch.stop())
                store.close()

    def test_diary_stop_prevents_claiming_another_entry(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock
        from andrewbot.diary import deliver_pending_diary
        from andrewbot.store import Store
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.db')
            first = store.create_diary_entry('2026-10-15', 'First day.')
            second = store.create_diary_entry('2026-10-16', 'Second day.')
            with store.connection:
                store.connection.execute('UPDATE diary_entries SET created_at=? WHERE entry_id=?',
                                         ('2026-10-15T00:00:00+00:00', first))
            stopped = threading.Event()
            telegram = Mock()
            telegram.send_message.side_effect = lambda *args: stopped.set()
            try:
                self.assertEqual(deliver_pending_diary(telegram, store, 17, should_stop=stopped.is_set), 1)
                self.assertEqual(store.diary_delivery_state(first), 'delivered')
                self.assertEqual(store.diary_delivery_state(second), 'pending')
                self.assertEqual(telegram.send_message.call_count, 1)
            finally:
                store.close()

    def test_diary_claim_survives_reopen_until_startup_recovery_and_stays_uncertain(self):
        import tempfile
        from pathlib import Path
        from andrewbot.store import Store
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            store = Store(path)
            entry = store.create_diary_entry('2026-10-16', 'Saved diary.')
            token = store.claim_diary_sender(entry)
            other = Store(path)
            try:
                self.assertIsNone(other.claim_diary_sender(entry))
                self.assertFalse(other.retry_uncertain_diary_delivery(entry))
                self.assertFalse(other.finish_diary_sender(entry, 'wrong-token', delivered=True))
                self.assertEqual(other.diary_delivery_state(entry), 'uncertain')
            finally:
                other.close()
                store.close()
            recovered = Store(path)
            try:
                self.assertFalse(recovered.retry_uncertain_diary_delivery(entry))
                recovered.recover_diary_claims()
                self.assertEqual(recovered.pending_diary_deliveries(), [])
                self.assertEqual(recovered.diary_delivery_state(entry), 'uncertain')
                self.assertTrue(recovered.retry_uncertain_diary_delivery(entry))
                self.assertFalse(recovered.finish_diary_sender(entry, token, delivered=True))
            finally:
                recovered.close()

class StopAdmissionRegressionTests(unittest.TestCase):
    def test_stop_during_inbox_read_does_not_claim_or_handle_input(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock, patch
        from andrewbot.dispatch import ControllerDispatch
        from andrewbot.store import Store
        from andrewbot.telegram import OwnerRateLimit
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.db')
            store.enqueue_update({'update_id': 1, 'message': {'text': 'saved', 'from': {'id': 17},
                'chat': {'id': 17, 'type': 'private'}}})
            controller = Mock()
            dispatch = ControllerDispatch(store, controller, Mock(), Mock(), OwnerRateLimit(), 17, True, True)
            original = store.pending_updates
            def pending():
                entered.set()
                release.wait(3)
                return original()
            try:
                with patch.object(store, 'pending_updates', side_effect=pending):
                    dispatch._model_worker.start()
                    dispatch._model_worker.wake()
                    self.assertTrue(entered.wait(1))
                    self.assertFalse(dispatch.stop(timeout=0))
                    release.set()
                    self.assertTrue(dispatch.stop())
                controller.handle_update.assert_not_called()
                self.assertEqual(len(store.pending_updates()), 1)
                self.assertEqual(store.connection.execute('SELECT count(*) FROM telegram_claims').fetchone()[0], 0)
            finally:
                release.set()
                dispatch.stop()
                store.close()

    def test_dispatch_stop_is_passed_into_diary_entry_admission(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock
        from andrewbot.diary import deliver_pending_diary
        from andrewbot.dispatch import ControllerDispatch
        from andrewbot.store import Store
        from andrewbot.telegram import OwnerRateLimit
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.db')
            first = store.create_diary_entry('2026-10-15', 'First day.')
            second = store.create_diary_entry('2026-10-16', 'Second day.')
            with store.connection:
                store.connection.execute('UPDATE diary_entries SET created_at=? WHERE entry_id=?',
                                         ('2026-10-15T00:00:00+00:00', first))
            telegram = Mock()
            telegram.send_message.side_effect = lambda *args: (entered.set(), release.wait(3))
            def diary(should_stop=lambda: False):
                return deliver_pending_diary(telegram, store, 17, should_stop=should_stop)
            dispatch = ControllerDispatch(store, Mock(), telegram, Mock(), OwnerRateLimit(), 17, False, False,
                                          diary_action=diary)
            try:
                dispatch._diary_worker.start()
                dispatch._diary_worker.wake()
                self.assertTrue(entered.wait(1))
                self.assertFalse(dispatch.stop(timeout=0))
                release.set()
                self.assertTrue(dispatch.stop())
                self.assertEqual(telegram.send_message.call_count, 1)
                self.assertEqual(store.diary_delivery_state(second), 'pending')
            finally:
                release.set()
                dispatch.stop()
                store.close()
