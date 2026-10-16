"""Durable Telegram dispatch, independent of the autonomous scheduler."""
import logging
import threading

from .telegram_menu import BUTTON_COMMANDS


class ControllerDispatch:
    def __init__(self, store, controller, telegram, sessions, limiter,
                 owner_id, autonomous_enabled, fallback_enabled, should_stop=lambda: False):
        self.store, self.controller, self.telegram = store, controller, telegram
        self.sessions, self.limiter = sessions, limiter
        self.owner_id = owner_id
        self.autonomous_enabled, self.fallback_enabled = autonomous_enabled, fallback_enabled
        self.should_stop = should_stop
        self._poll_thread = None
        self._reply_thread = None
        self._poll_stop = threading.Event()
        self._updates_ready = threading.Event()
        self._reply_ready = threading.Event()
        self._delivery_lock = threading.Lock()

    def start(self):
        """Poll and execute only local owner controls while model work is blocked."""
        if self._poll_thread is not None:
            raise RuntimeError('Telegram poller is already started')
        self._poll_thread = threading.Thread(target=self._poll_loop, name='andrew-owner-controls', daemon=True)
        self._reply_thread = threading.Thread(target=self._reply_loop, name='andrew-replies', daemon=True)
        self._poll_thread.start()
        self._reply_thread.start()

    def stop(self):
        self._poll_stop.set()
        self._updates_ready.set()
        self._reply_ready.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2)
        if self._reply_thread is not None:
            self._reply_thread.join(timeout=2)

    def _reply_loop(self):
        while not self._poll_stop.is_set() and not self.should_stop():
            self._reply_ready.wait(1)
            self._reply_ready.clear()
            if self._poll_stop.is_set() or self.should_stop():
                return
            self.guarded('Telegram reply delivery', self.deliver_reply)

    @staticmethod
    def _owner_control(update, owner_id):
        message = update.get('message') or {}
        text = message.get('text') or ''
        if not isinstance(text, str):
            return False
        command = BUTTON_COMMANDS.get(text.strip(), text.strip()).split(maxsplit=1)
        return bool((message.get('from') or {}).get('id') == owner_id
                    and (message.get('chat') or {}).get('id') == owner_id
                    and (message.get('chat') or {}).get('type') == 'private'
                    and not any(key in message for key in ('forward_origin', 'forward_from', 'forward_from_chat', 'forward_sender_name'))
                    and command and command[0].split('@', 1)[0].lower() in {'/pause', '/wake', '/status', '/budget'})

    def _poll_loop(self):
        while not self._poll_stop.is_set() and not self.should_stop():
            # Saved owner controls must run even when the next network poll
            # blocks or fails. The main thread leaves these to this worker.
            self.guarded('Saved owner controls', lambda: self.process_updates(controls_only=True))
            if self._poll_stop.is_set() or self.should_stop():
                return
            try:
                offset = self.store.last_update_id()
                updates = self.telegram.get_updates(offset=None if offset is None else offset + 1, poll_timeout=1)
                if self._poll_stop.is_set() or self.should_stop():
                    return
                received = [update for update in updates if self.store.enqueue_update(update)]
                # Handle new controls directly even if normal chat has filled
                # the durable inbox's first page. Claims prevent duplicate work.
                self.process_updates(controls_only=True, updates=[(u['update_id'], u) for u in received])
                self.process_updates(controls_only=True)
                if updates:
                    self._updates_ready.set()
            except Exception as error:
                logging.warning('Telegram control polling failed (%s)', type(error).__name__)
                self._poll_stop.wait(1)

    def process_updates(self, controls_only=False, updates=None):
        handled_chat = False
        if updates is None:
            buttons = [text for text, command in BUTTON_COMMANDS.items() if command in {'/pause', '/wake', '/status', '/budget'}]
            updates = self.store.pending_owner_controls(self.owner_id, buttons) if controls_only else self.store.pending_updates()
        for update_id, update in updates:
            if self.should_stop():
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
                # Defer instead of discarding saved owner messages. Pause and
                # status remain available even when the chat is busy.
                continue
            if not self.store.claim_update(update_id):
                continue
            if owner and not control:
                handled_chat = True
            uncertain = False
            try:
                reply = self.controller.handle_update(update)
            except Exception as error:
                uncertain = True
                logging.warning('Owner request outcome uncertain (%s)', type(error).__name__)
                reply = 'The request outcome is uncertain. It was not automatically repeated; please check the result before asking again.'
            if self._poll_stop.is_set():
                return
            # Commit the response and completion together before attempting
            # delivery. A Telegram failure must not repeat model/tool actions.
            self.store.complete_update(update_id, self.owner_id if owner else None, reply, uncertain)
            self._reply_ready.set()

    def deliver_reply(self):
        with self._delivery_lock:
            self._deliver_reply()

    def _deliver_reply(self):
        row = self.store.next_reply()
        if row is None:
            return
        self.store.attempt_reply(row['id'])
        text = row['text']
        if row['attempts']:
            text = 'Delivery retry; you may have already received this reply.\n\n' + text
        self.telegram.send_message(row['chat_id'], text)
        if not self._poll_stop.is_set():
            self.store.delivered_reply(row['id'])

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
            for update in self.telegram.get_updates(offset=None if offset is None else offset + 1):
                self.store.enqueue_update(update)

        if self._poll_thread is None:
            polled = self.guarded('Telegram polling', poll)
        else:
            self._updates_ready.wait(1)
            self._updates_ready.clear()
            polled = True
        if self.should_stop():
            return polled
        self.guarded('Owner request processing', self.process_updates)
        if self.should_stop():
            return polled
        if self.autonomous_enabled:
            self.guarded('Scheduled wake', self.controller.check_scheduled_wake)
        if self.fallback_enabled:
            self.guarded('Fallback wake', self.controller.check_fallback_wake)
        if self._poll_thread is None:
            self.guarded('Telegram reply delivery', self.deliver_reply)
        else:
            self._reply_ready.set()
        if self.should_stop():
            return polled
        self.guarded('Queued messages', lambda: self.store.queue_reply(
            self.owner_id, self.controller.flush_queued()))
        self._reply_ready.set()
        if self.should_stop():
            return polled
        self.guarded('Autonomous session', self.sessions.tick)
        return polled
