"""Durable Telegram dispatch, independent of the autonomous scheduler."""
import logging

from .telegram_menu import BUTTON_COMMANDS


class ControllerDispatch:
    def __init__(self, store, controller, telegram, sessions, limiter,
                 owner_id, autonomous_enabled, fallback_enabled, should_stop=lambda: False):
        self.store, self.controller, self.telegram = store, controller, telegram
        self.sessions, self.limiter = sessions, limiter
        self.owner_id = owner_id
        self.autonomous_enabled, self.fallback_enabled = autonomous_enabled, fallback_enabled
        self.should_stop = should_stop

    def process_updates(self):
        handled_chat = False
        for update_id, update in self.store.pending_updates():
            if self.should_stop():
                return
            message = update.get('message') or {}
            owner = ((message.get('from') or {}).get('id') == self.owner_id
                     and (message.get('chat') or {}).get('id') == self.owner_id
                     and (message.get('chat') or {}).get('type') == 'private')
            text = message.get('text') or ''
            command = BUTTON_COMMANDS.get(text, text).split(maxsplit=1)
            control = bool(command and command[0].split('@', 1)[0].lower()
                           in {'/pause', '/wake', '/status', '/budget'})
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
            # Commit the response and completion together before attempting
            # delivery. A Telegram failure must not repeat model/tool actions.
            self.store.complete_update(update_id, self.owner_id if owner else None, reply, uncertain)

    def deliver_reply(self):
        row = self.store.next_reply()
        if row is None:
            return
        self.store.attempt_reply(row['id'])
        text = row['text']
        if row['attempts']:
            text = 'Delivery retry; you may have already received this reply.\n\n' + text
        self.telegram.send_message(row['chat_id'], text)
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

        polled = self.guarded('Telegram polling', poll)
        if self.should_stop():
            return polled
        self.guarded('Owner request processing', self.process_updates)
        if self.should_stop():
            return polled
        if self.autonomous_enabled:
            self.guarded('Scheduled wake', self.controller.check_scheduled_wake)
        if self.fallback_enabled:
            self.guarded('Fallback wake', self.controller.check_fallback_wake)
        self.guarded('Telegram reply delivery', self.deliver_reply)
        if self.should_stop():
            return polled
        self.guarded('Queued messages', lambda: self.store.queue_reply(
            self.owner_id, self.controller.flush_queued()))
        if self.should_stop():
            return polled
        self.guarded('Autonomous session', self.sessions.tick)
        return polled
