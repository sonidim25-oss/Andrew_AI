import json
import time
from collections import deque
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramAPI:
    def __init__(self, token, opener=None, timeout=35):
        self.token = token
        self.opener = opener or urlopen
        self.timeout = timeout

    def call(self, method, payload):
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
        text = text[:4000]
        return self.call("sendMessage", {"chat_id": chat_id, "text": text})

    def get_me(self):
        return self.call("getMe", {})

    def get_updates(self, offset=None):
        payload = {"timeout": 25, "limit": 50, "allowed_updates": ["message"]}
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
