"""Local fixtures only: real dispatch, controller, store and Telegram API."""
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from andrewbot.controller import Controller
from andrewbot.dispatch import ControllerDispatch
from andrewbot.latency import LatencyMetrics
from andrewbot.store import Store
from andrewbot.telegram import OwnerRateLimit, TelegramAPI


class LatencyIntegrationTests(unittest.TestCase):
    def test_model_send_and_failed_send_are_attributed_without_extra_actions(self):
        for fail_send in (False, True):
            with self.subTest(fail_send=fail_send), tempfile.TemporaryDirectory() as directory:
                clock = [100.0]
                calls = []
                class Model:
                    def reply(self, messages, timeout=120):
                        calls.append('model')
                        clock[0] += 2
                        return 'fixture reply', {'cost': 0.001, 'prompt_tokens': 10, 'completion_tokens': 2}
                def opener(request, timeout):
                    method = request.full_url.rsplit('/', 1)[-1]
                    if method == 'sendMessage':
                        calls.append('send')
                        clock[0] += 7
                        if fail_send:
                            raise TimeoutError('private response uncertainty')
                    return io.BytesIO(json.dumps({'ok': True, 'result': []}).encode())
                metrics = LatencyMetrics(clock=lambda: clock[0])
                store = Store(Path(directory) / 'fixture.sqlite3')
                try:
                    api = TelegramAPI('fixture-token', opener=opener, metrics=metrics)
                    controller = Controller(store, Model(), 17)
                    dispatch = ControllerDispatch(store, controller, api, SimpleNamespace(tick=lambda: None),
                        OwnerRateLimit(), 17, False, False, metrics=metrics)
                    store.enqueue_update({'update_id': 1, 'message': {'from': {'id': 17},
                        'chat': {'id': 17, 'type': 'private'}, 'text': 'fixture input'}})
                    dispatch.step()
                    snapshot = metrics.snapshot()
                    self.assertEqual(snapshot['chat']['last_seconds'], 2)
                    self.assertEqual(snapshot['send']['last_seconds'], 7)
                    self.assertEqual(snapshot['send']['failures'], int(fail_send))
                    self.assertEqual(snapshot['poll']['count'], 1)
                    self.assertEqual(snapshot['inbox_wait']['count'], 1)
                    self.assertEqual(snapshot['outbox_wait']['count'], 1)
                    self.assertEqual(calls, ['model', 'send'])
                    self.assertEqual(store.pending_updates(), [])
                    self.assertEqual(store.connection.execute('SELECT count(*) FROM usage').fetchone()[0], 1)
                    self.assertEqual(store.connection.execute('SELECT sum(cost_usd) FROM usage').fetchone()[0], 0.001)
                    self.assertEqual(store.connection.execute('SELECT queued FROM messages WHERE role="user"').fetchone()[0], 0)
                    if fail_send:
                        self.assertEqual(store.next_reply()['attempts'], 1)
                        progress = store.connection.execute('SELECT * FROM telegram_delivery_progress').fetchone()
                        self.assertEqual((progress['next_chunk'], progress['chunk_attempts']), (0, 1))
                    else:
                        self.assertIsNone(store.next_reply())
                finally:
                    store.close()

    def test_real_caught_failures_do_not_report_successful_stages(self):
        from andrewbot.diary import deliver_pending_diary
        from andrewbot.sessions import SessionRunner
        from datetime import datetime, timezone
        for stage in ('chat', 'queued', 'autonomous', 'diary'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                class FailingModel:
                    def reply(self, messages, timeout=120):
                        raise TimeoutError('private provider content')
                    def autonomous_turn(self, prompt, timeout):
                        raise TimeoutError('private provider content')
                metrics = LatencyMetrics()
                store = Store(Path(directory) / 'fixture.sqlite3')
                try:
                    model = FailingModel()
                    controller = Controller(store, model, 17, autonomous_enabled=True)
                    if stage == 'chat':
                        action = lambda: controller.handle_update({'update_id': 1, 'message': {
                            'from': {'id': 17}, 'chat': {'id': 17, 'type': 'private'}, 'text': 'fixture'}})
                    elif stage == 'queued':
                        store.add_message(17, 'user', 'fixture', queued=True)
                        action = controller.flush_queued
                    elif stage == 'autonomous':
                        runner = SessionRunner(store, controller, model, enabled=True)
                        store.set_state('session_start_requested', 'owner_wake')
                        action = runner.tick
                    else:
                        store.create_diary_entry(datetime.now(timezone.utc).date().isoformat(), 'Fixture diary.')
                        def fail_open(request, timeout):
                            raise TimeoutError('private Telegram content')
                        api = TelegramAPI('fixture-token', opener=fail_open, metrics=metrics)
                        action = lambda: deliver_pending_diary(api, store, 17)
                    with metrics.measure(stage):
                        action()
                    row = metrics.snapshot()[stage]
                    self.assertEqual((row['count'], row['failures']), (1, 1))
                    self.assertIsNone(row['last_success_monotonic'])
                    self.assertEqual(sum(row['errors'].values()), 1)
                    self.assertNotIn('private', json.dumps(metrics.snapshot()))
                    self.assertEqual(store.connection.execute('SELECT count(*) FROM usage').fetchone()[0], 0)
                    if stage in ('chat', 'queued'):
                        self.assertEqual(store.connection.execute('SELECT queued FROM messages').fetchone()[0], 3)
                finally:
                    store.close()


    def test_autonomous_ledger_failure_is_recorded_and_idle_is_not_failure(self):
        from andrewbot.sessions import SessionRunner
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            metrics = LatencyMetrics()
            store = Store(Path(directory) / 'fixture.sqlite3')
            try:
                controller = Controller(store, object(), 17, autonomous_enabled=True)
                runner = SessionRunner(store, controller,
                    SimpleNamespace(autonomous_turn=lambda *args, **kwargs: None), enabled=True)
                with metrics.measure('autonomous'):
                    self.assertFalse(runner.tick())  # no requested work is healthy
                success = metrics.snapshot()['autonomous']['last_success_monotonic']
                store.set_state('session_start_requested', 'owner_wake')
                original = store.add_activity
                def record(event):
                    if event.startswith('Autonomous cycle completed'):
                        raise RuntimeError('private ledger error')
                    return original(event)
                with patch.object(store, 'add_activity', side_effect=record):
                    with metrics.measure('autonomous'):
                        self.assertFalse(runner.tick())
                row = metrics.snapshot()['autonomous']
                self.assertEqual((row['count'], row['failures']), (2, 1))
                self.assertEqual(row['errors']['runtime_error'], 1)
                self.assertEqual(row['last_success_monotonic'], success)
            finally:
                store.close()
