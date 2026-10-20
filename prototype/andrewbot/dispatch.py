"""Durable Telegram dispatch, independent of the autonomous scheduler."""
import logging
import threading
import time
from datetime import datetime, timezone

from .latency import LatencyMetrics, wall_age_seconds
from .workers import ModelWorker

from .telegram_menu import owner_control
from .telegram import deliver_saved_message


class ControllerDispatch:
    def __init__(self, store, controller, telegram, sessions, limiter,
                 owner_id, autonomous_enabled, fallback_enabled, should_stop=lambda: False,
                 metrics=None, utc_now=None, diary_action=None, media_worker=None, recovery_action=None):
        self.media_worker = media_worker
        self.store, self.controller, self.telegram = store, controller, telegram
        self.sessions, self.limiter = sessions, limiter
        self.owner_id = owner_id
        self.autonomous_enabled, self.fallback_enabled = autonomous_enabled, fallback_enabled
        self.metrics = metrics if metrics is not None else LatencyMetrics()
        self.utc_now = utc_now if utc_now is not None else lambda: datetime.now(timezone.utc)
        self.should_stop = should_stop
        self._poll_thread = None
        self._reply_thread = None
        self._system_reply_thread = None
        self._poll_stop = threading.Event()
        self._updates_ready = threading.Event()
        self._reply_ready = threading.Event()
        self._system_reply_ready = threading.Event()
        self._input_lock = threading.RLock()
        self._diary_worker = ModelWorker(
            lambda: diary_action(should_stop=lambda: self._poll_stop.is_set() or self.should_stop()),
            lambda: self._poll_stop.is_set() or self.should_stop()) if diary_action is not None else None
        self._recovery_worker = ModelWorker(recovery_action,
            lambda: self._poll_stop.is_set() or self.should_stop()) if recovery_action is not None else None
        self._model_worker = ModelWorker(self._model_pass, lambda: self._poll_stop.is_set() or self.should_stop())
        self.controller.health_snapshot = self.health_snapshot

    def health_snapshot(self):
        now = time.monotonic()
        metrics = self.metrics.snapshot()
        for row in metrics.values():
            last = row.pop('last_success_monotonic')
            row['last_success_age_seconds'] = None if last is None else max(0, now - last)
        result = {'model': self._model_worker.snapshot(),
                'diary': None if self._diary_worker is None else self._diary_worker.snapshot(),
                'metrics': metrics}
        if self._recovery_worker is not None:
            result['recovery'] = self._recovery_worker.snapshot()
        return result

    def start(self):
        """Poll and execute only local owner controls while model work is blocked."""
        if self._poll_thread is not None:
            raise RuntimeError('Telegram poller is already started')
        self._poll_thread = threading.Thread(target=self._poll_loop, name='andrew-owner-controls', daemon=True)
        self._reply_thread = threading.Thread(target=self._reply_loop, args=('normal',), name='andrew-replies', daemon=True)
        self._system_reply_thread = threading.Thread(target=self._reply_loop, args=('system',), name='andrew-system-replies', daemon=True)
        self._poll_thread.start()
        self._reply_thread.start()
        self._system_reply_thread.start()
        self._model_worker.start()
        if self._recovery_worker is not None:
            self._recovery_worker.start()
        if self.media_worker is not None:
            self.media_worker.start()
        if self._diary_worker is not None:
            self._diary_worker.start()

    def stop(self, timeout=2):
        self._poll_stop.set()
        self._updates_ready.set()
        self._reply_ready.set()
        self._system_reply_ready.set()
        deadline = time.monotonic() + max(0, timeout)
        # Close every worker's admission before any join can consume the
        # shared deadline. In-flight calls retain storage until they settle.
        self._model_worker.request_stop()
        if self._recovery_worker is not None:
            self._recovery_worker.request_stop()
        if self._diary_worker is not None:
            self._diary_worker.request_stop()
        if self.media_worker is not None:
            self.media_worker.request_stop()
        model_stopped = self._model_worker.stop(timeout=max(0, deadline - time.monotonic()))
        diary_stopped = self._diary_worker is None or self._diary_worker.stop(timeout=max(0, deadline - time.monotonic()))
        media_stopped = self.media_worker is None or self.media_worker.stop(timeout=max(0, deadline - time.monotonic()))
        recovery_stopped = self._recovery_worker is None or self._recovery_worker.stop(timeout=max(0, deadline - time.monotonic()))
        workers = (self._poll_thread, self._reply_thread, self._system_reply_thread)
        for worker in workers:
            if worker is not None:
                worker.join(timeout=max(0, deadline - time.monotonic()))
        return model_stopped and diary_stopped and media_stopped and recovery_stopped and all(worker is None or not worker.is_alive() for worker in workers)

    def _reply_loop(self, lane):
        ready = self._system_reply_ready if lane == 'system' else self._reply_ready
        while not self._poll_stop.is_set() and not self.should_stop():
            ready.wait(1)
            ready.clear()
            # Drain the lane, so coalesced notifications do not add one second
            # per reply. Claims and part attempts remain durable between calls.
            while not self._poll_stop.is_set() and not self.should_stop():
                try:
                    if not self._deliver_reply(lane):
                        break
                except Exception as error:
                    logging.warning('Telegram reply delivery failed (%s)', type(error).__name__)
                    break

    def _owner_control(self, update, owner_id):
        """Route local commands and time-entry replies without starting model work."""
        prompt_after = (int(self.store.get_state('telegram_wake_prompt_after_update_id', '-1'))
                        if self.store.get_state('telegram_wake_prompt_until', '') else None)
        return owner_control(update, owner_id, prompt_after)

    def _poll_loop(self):
        while not self._poll_stop.is_set() and not self.should_stop():
            # Saved owner controls must run even when the next network poll
            # blocks or fails. The main thread leaves these to this worker.
            self.guarded('Saved owner controls', lambda: self.process_updates(controls_only=True))
            if self._poll_stop.is_set() or self.should_stop():
                return
            try:
                offset = self.store.last_update_id()
                updates = self._run_stage('poll', lambda: self.telegram.get_updates(
                    offset=None if offset is None else offset + 1, poll_timeout=1))
                if self._poll_stop.is_set() or self.should_stop():
                    return
                for update in updates:
                    self.store.enqueue_update(update)
                # One ordered durable queue covers new and previously saved
                # commands, including sleep and wake-time entry.
                self.process_updates(controls_only=True)
                if updates:
                    self._updates_ready.set()
            except Exception as error:
                logging.warning('Telegram control polling failed (%s)', type(error).__name__)
                self._poll_stop.wait(1)

    def process_updates(self, controls_only=False, updates=None):
        handled_chat = False
        if controls_only and updates is None:
            # Re-query after each command: /wake_at can make the next plain
            # message a local time response before a later /wake executes.
            for _ in range(50):
                with self._input_lock:
                    pending = self.store.pending_owner_controls(self.owner_id)
                    if not pending or self._poll_stop.is_set() or self.should_stop():
                        return
                    self.process_updates(controls_only=True, updates=pending[:1])
            return
        if updates is None:
            updates = self.store.pending_updates()
        for update_id, update in updates:
            if self._poll_stop.is_set() or self.should_stop():
                return
            with self._input_lock:
                if self._poll_stop.is_set() or self.should_stop():
                    return
                message = update.get('message') or {}
                owner = ((message.get('from') or {}).get('id') == self.owner_id
                         and (message.get('chat') or {}).get('id') == self.owner_id
                         and (message.get('chat') or {}).get('type') == 'private')
                control = self._owner_control(update, self.owner_id)
                if controls_only and not control:
                    continue
                if not controls_only and self._poll_thread is not None and control:
                    continue
                if owner and not control and (handled_chat or not self.limiter.allow()):
                    continue
                if not self.store.claim_update(update_id):
                    continue
                if control:
                    self._handle_claimed_update(update_id, update, owner, control=True)
                    continue
                if owner:
                    handled_chat = True
            # Release the routing lock before any model or external tool work.
            self._handle_claimed_update(update_id, update, owner, control=False)

    def _handle_claimed_update(self, update_id, update, owner, control):
        if self._poll_stop.is_set() or self.should_stop():
            return
        uncertain = False
        try:
            age = wall_age_seconds(self.store.inbox_received_at(update_id), self.utc_now())
            if age is not None:
                self.metrics.observe('inbox_wait', age)
            with self.metrics.measure('control' if control else 'chat'):
                if self._poll_thread is not None and not control:
                    reply = self.controller.handle_update(update, allow_time_entry=False)
                else:
                    reply = self.controller.handle_update(update)
            if control and self.store.get_state('telegram_wake_prompt_until', ''):
                self.store.set_state('telegram_wake_prompt_after_update_id', str(update_id))
        except Exception as error:
            uncertain = True
            logging.warning('Owner request outcome uncertain (%s)', type(error).__name__)
            reply = 'The request outcome is uncertain. It was not automatically repeated; please check the result before asking again.'
        if self._poll_stop.is_set():
            return
        self.store.complete_update(update_id, self.owner_id if owner else None, reply, uncertain,
                                   lane='system' if control else 'normal')
        (self._system_reply_ready if control else self._reply_ready).set()

    def deliver_reply(self, lane=None):
        if lane is not None:
            return self._deliver_reply(lane)
        system = self._deliver_reply('system')
        normal = self._deliver_reply('normal')
        return system or normal

    def _deliver_reply(self, lane='normal'):
        if self._poll_stop.is_set() or self.should_stop():
            return False
        row = self.store.claim_reply(lane)
        if row is None:
            return False
        try:
            age = self.store.reply_age_seconds(row['id'], self.utc_now())
            if age is not None:
                self.metrics.observe('outbox_wait', age)
            complete = deliver_saved_message(self.telegram, self.store, 'reply', row['id'], row['chat_id'],
                                             row['text'], row['attempts'] > 0,
                                             should_stop=lambda: self._poll_stop.is_set() or self.should_stop())
            if complete:
                self.store.finish_reply(row['id'], row['claim_token'])
            else:
                self.store.release_reply(row['id'], row['claim_token'])
        except Exception:
            self.store.hold_reply(row['id'], row['claim_token'])
            raise
        return True

    def _run_stage(self, stage, action):
        with self.metrics.measure(stage):
            return action()

    @staticmethod
    def guarded(name, action):
        try:
            action()
            return True
        except Exception as error:
            # Remote errors can contain URLs, tokens or message bodies.
            logging.warning('%s failed (%s)', name, type(error).__name__)
            return False

    def step(self):
        def poll():
            offset = self.store.last_update_id()
            updates = self._run_stage('poll', lambda: self.telegram.get_updates(
                offset=None if offset is None else offset + 1))
            for update in updates:
                self.store.enqueue_update(update)

        if self._poll_thread is None:
            polled = self.guarded('Telegram polling', poll)
        else:
            self._updates_ready.wait(1)
            self._updates_ready.clear()
            polled = True
        if self._poll_stop.is_set() or self.should_stop():
            return polled
        if self._poll_thread is None:
            self.guarded('Owner request processing', self.process_updates)
        if self._poll_stop.is_set() or self.should_stop():
            return polled
        if self.autonomous_enabled:
            self.guarded('Scheduled wake', lambda: self._run_stage('schedule', self.controller.check_scheduled_wake))
        if self.fallback_enabled:
            self.guarded('Fallback wake', lambda: self._run_stage('schedule', self.controller.check_fallback_wake))
        if self._poll_thread is None:
            self.guarded('Telegram reply delivery', self.deliver_reply)
        else:
            self._reply_ready.set()
        if self._poll_stop.is_set() or self.should_stop():
            return polled
        if self._poll_thread is not None:
            self._model_worker.wake()
            if self._recovery_worker is not None:
                self._recovery_worker.wake()
            if self._diary_worker is not None:
                self._diary_worker.wake()
            return polled
        self._finish_model_pass()
        return polled

    def _model_pass(self):
        self.guarded('Owner request processing', self.process_updates)
        if not self._poll_stop.is_set() and not self.should_stop():
            self._finish_model_pass()

    def _finish_model_pass(self):
        if self._poll_stop.is_set() or self.should_stop():
            return
        recovery=getattr(self.sessions.agent,'local_recovery',None) if hasattr(self.sessions,'agent') else None
        if recovery is not None and recovery.enabled is True and recovery.has_pending():
            self.guarded('Journaled model recovery',recovery.reconcile_one)
            return
        self.guarded('Queued messages', lambda: self._run_stage('queued', lambda: self.store.queue_reply(
            self.owner_id, self.controller.flush_queued())))
        self._reply_ready.set()
        if self._poll_stop.is_set() or self.should_stop():
            return
        self.guarded('Autonomous session', lambda: self._run_stage('autonomous', self.sessions.tick))
