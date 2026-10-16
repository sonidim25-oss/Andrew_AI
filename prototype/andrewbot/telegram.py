import json
import time
from collections import deque
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from .telegram_menu import command_keyboard
from .latency import LatencyMetrics


def split_message(text, limit=3800):
    """Split without losing Unicode characters; leave room for a retry notice."""
    chunks, current, used = [], [], 0
    for character in text:
        units = 2 if ord(character) > 0xffff else 1
        if used + units > limit:
            chunks.append(''.join(current))
            current, used = [], 0
        current.append(character)
        used += units
    if current or not chunks:
        chunks.append(''.join(current))
    return chunks


def deliver_saved_message(telegram, store, kind, record_id, chat_id, text,
                          previously_attempted=False, should_stop=lambda: False):
    progress = store.prepare_telegram_delivery(kind, record_id, text, previously_attempted)
    chunks = split_message(progress['content'])
    for index in range(progress['next_chunk'], len(chunks)):
        if should_stop():
            return False
        attempts = store.attempt_telegram_chunk(kind, record_id, index)
        if attempts is None:
            return False
        prefix = 'Delivery retry; you may have already received this part.\n\n' if attempts else ''
        telegram.send_message(chat_id, prefix + chunks[index])
        # Persist each acknowledgement. An unacknowledged attempt is held;
        # only an explicit owner diary retry can authorize another attempt.
        store.acknowledge_telegram_chunk(kind, record_id, index)
    return True


class TelegramAPI:
    def __init__(self, token, opener=None, timeout=35, metrics=None):
        self.token = token
        self.opener = opener or urlopen
        self.timeout = timeout
        self.metrics = metrics if metrics is not None else LatencyMetrics()

    def call(self, method, payload):
        if method == 'sendMessage':
            with self.metrics.measure('send'):
                return self._call(method, payload)
        return self._call(method, payload)

    def _call(self, method, payload):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        request = Request(url, data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self.opener(request, timeout=self.timeout) as response:
                data = json.loads(response.read())
        except HTTPError as error:
            # HTTPError URLs contain the bot token, so expose only the status.
            raise RuntimeError(f"Telegram API returned HTTP status {error.code}") from None
        except (URLError, TimeoutError, ValueError, OSError):
            # Telegram embeds the bot token in the API path; never propagate the exception text.
            raise RuntimeError("Telegram API request failed") from None
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError("Telegram API returned an unsuccessful response")
        return data.get("result")

    def send_message(self, chat_id, text):
        result = None
        for chunk in split_message(text, limit=4000):
            result = self.call("sendMessage", {"chat_id": chat_id, "text": chunk,
                                              "reply_markup": command_keyboard()})
        return result

    def get_me(self):
        return self.call("getMe", {})

    def get_updates(self, offset=None, poll_timeout=25):
        payload = {"timeout": poll_timeout, "limit": 50, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload) or []


class OwnerRateLimit:
    def __init__(self, maximum=15, window_seconds=60):
        self.maximum = maximum
        self.window_seconds = window_seconds
        self.timestamps = deque()

    def allow(self, now=None):
        now = time.monotonic() if now is None else now
        while self.timestamps and now - self.timestamps[0] >= self.window_seconds:
            self.timestamps.popleft()
        if len(self.timestamps) >= self.maximum:
            return False
        self.timestamps.append(now)
        return True
