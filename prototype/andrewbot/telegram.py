import json
import time
import uuid
from collections import deque
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from .telegram_menu import command_keyboard
from .latency import LatencyMetrics


class TelegramMediaError(RuntimeError):
    """Fixed diagnostics only: Telegram URLs contain the bot credential."""
    CATEGORIES = frozenset({'http_error', 'api_rejected', 'timeout',
                            'network_error', 'invalid_response', 'unexpected_error'})

    def __init__(self, category, http_status=None):
        self.category = category if category in self.CATEGORIES else 'unexpected_error'
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        detail = self.category + (f', HTTP {self.http_status}' if self.http_status is not None else '')
        super().__init__('Telegram media delivery outcome uncertain (' + detail + ')')


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


RETRY_NOTICE = 'Delivery retry; you may have already received this part.\n\n'


def validate_sent_text(result, chat_id, text):
    if (not isinstance(result, dict) or type(result.get('message_id')) is not int
            or result['message_id'] <= 0 or not isinstance(result.get('chat'), dict)
            or type(result['chat'].get('id')) is not int or result['chat']['id'] != chat_id
            or result.get('text') != text):
        raise RuntimeError('Telegram send confirmation is invalid; outcome remains uncertain')
    return result['message_id']


def validate_sent_media(result, chat_id, mime):
    if (not isinstance(result, dict) or type(result.get('message_id')) is not int
            or result['message_id'] <= 0 or not isinstance(result.get('chat'), dict)
            or type(result['chat'].get('id')) is not int or result['chat']['id'] != chat_id):
        raise TelegramMediaError('invalid_response')
    if mime == 'audio/mpeg':
        files = [result.get('voice')]
    elif mime in {'image/png', 'image/jpeg'}:
        files = result.get('photo')
    else:
        raise TelegramMediaError('invalid_response')
    if not isinstance(files, list) or not 1 <= len(files) <= 32:
        raise TelegramMediaError('invalid_response')
    for file in files:
        if not isinstance(file, dict) or any(not isinstance(file.get(key), str)
                or not 1 <= len(file[key]) <= 1024 for key in ('file_id', 'file_unique_id')):
            raise TelegramMediaError('invalid_response')
    return result['message_id'], files[-1]['file_unique_id']


def deliver_saved_message(telegram, store, kind, record_id, chat_id, text,
                          previously_attempted=False, should_stop=lambda: False):
    progress = store.prepare_telegram_delivery(kind, record_id, text, previously_attempted)
    store.apply_telegram_receipts(kind, record_id, chat_id)
    progress = store.prepare_telegram_delivery(kind, record_id, text, previously_attempted)
    chunks = split_message(progress['content'])
    for index in range(progress['next_chunk'], len(chunks)):
        if should_stop():
            return False
        attempts = store.attempt_telegram_chunk(kind, record_id, index)
        if attempts is None:
            return False
        prefix = RETRY_NOTICE if attempts else ''
        context = store.telegram_chunk_context(kind, record_id, index)
        result = telegram.send_message(chat_id, prefix + chunks[index])
        validate_sent_text(result, chat_id, prefix + chunks[index])
        store.save_telegram_receipt(context, chat_id, prefix + chunks[index], result)
        # Persist each acknowledgement. An unacknowledged attempt is held;
        # only an explicit owner diary retry can authorize another attempt.
        store.acknowledge_telegram_chunk(kind, record_id, index)
    return True


class TelegramAPI:
    def __init__(self, token, opener=None, timeout=None, metrics=None):
        self.token = token
        self.opener = opener or urlopen
        self.timeout = timeout
        self.metrics = metrics if metrics is not None else LatencyMetrics()

    def call(self, method, payload):
        if method == 'sendMessage':
            with self.metrics.measure('send'):
                result = self._call(method, payload)
                validate_sent_text(result, payload['chat_id'], payload['text'])
                return result
        return self._call(method, payload)

    def _call(self, method, payload):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        request = Request(url, data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            timeout = self.timeout if self.timeout is not None else (payload['timeout'] + 5 if method == 'getUpdates' else 5)
            with self.opener(request, timeout=timeout) as response:
                data = json.loads(response.read())
        except HTTPError as error:
            # HTTPError URLs contain the bot token, so expose only the status.
            raise RuntimeError(f"Telegram API returned HTTP status {error.code}") from None
        except (URLError, TimeoutError, ValueError, OSError):
            # Telegram embeds the bot token in the API path; never propagate the exception text.
            raise RuntimeError("Telegram API request failed") from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise RuntimeError("Telegram API returned an unsuccessful response")
        return data.get("result")

    def send_message(self, chat_id, text):
        result = None
        for chunk in split_message(text, limit=4000):
            result = self.call("sendMessage", {"chat_id": chat_id, "text": chunk,
                                              "reply_markup": command_keyboard()})
            validate_sent_text(result, chat_id, chunk)
        return result

    def send_media(self, chat_id, data, mime, caption=None):
        if mime not in {'audio/mpeg', 'image/png', 'image/jpeg'} or not isinstance(data, bytes) or not 0 < len(data) <= 8 * 1024 * 1024:
            raise ValueError('Invalid Telegram media')
        voice = mime == 'audio/mpeg'
        result = self._upload('sendVoice' if voice else 'sendPhoto',
                            {'chat_id': str(chat_id), **({'caption': caption[:1000]} if caption else {})},
                            'voice' if voice else 'photo', data, mime)
        validate_sent_media(result, chat_id, mime)
        return result

    def set_profile_photo(self, jpeg):
        if not isinstance(jpeg, bytes) or not jpeg.startswith(b'\xff\xd8') or len(jpeg) > 2 * 1024 * 1024:
            raise ValueError('Invalid avatar image')
        return self._upload('setMyProfilePhoto', {'photo': json.dumps({'type': 'static', 'photo': 'attach://avatar'})},
                            'avatar', jpeg, 'image/jpeg')

    def _upload(self, method, fields, field, data, mime):
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in fields.items():
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
        suffix = {'audio/mpeg': 'mp3', 'image/png': 'png', 'image/jpeg': 'jpg'}[mime]
        parts.extend([(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="media.{suffix}"\r\nContent-Type: {mime}\r\n\r\n').encode(),
                      data, f'\r\n--{boundary}--\r\n'.encode()])
        request = Request(f'https://api.telegram.org/bot{self.token}/{method}', data=b''.join(parts),
                          headers={'Content-Type': 'multipart/form-data; boundary=' + boundary}, method='POST')
        try:
            with self.metrics.measure('media_send'):
                with self.opener(request, timeout=self.timeout if self.timeout is not None else 30) as response:
                    raw = response.read(1024 * 1024 + 1)
                    if len(raw) > 1024 * 1024:
                        raise TelegramMediaError('invalid_response')
                    result = json.loads(raw)
            if not isinstance(result, dict):
                raise TelegramMediaError('invalid_response')
            if result.get('ok') is False:
                raise TelegramMediaError('api_rejected', result.get('error_code'))
            if result.get('ok') is not True or 'result' not in result:
                raise TelegramMediaError('invalid_response')
            value = result['result']
            if method in {'sendVoice', 'sendPhoto'}:
                if not isinstance(value, dict) or type(value.get('message_id')) is not int or value['message_id'] <= 0:
                    raise TelegramMediaError('invalid_response')
            elif method == 'setMyProfilePhoto' and value is not True:
                raise TelegramMediaError('invalid_response')
            return value
        except TelegramMediaError:
            raise
        except HTTPError as error:
            status = error.code
            error.close()
            raise TelegramMediaError('http_error', status) from None
        except URLError as error:
            category = 'timeout' if isinstance(error.reason, TimeoutError) else 'network_error'
            raise TelegramMediaError(category) from None
        except TimeoutError:
            raise TelegramMediaError('timeout') from None
        except OSError:
            raise TelegramMediaError('network_error') from None
        except (ValueError, UnicodeError):
            raise TelegramMediaError('invalid_response') from None
        except Exception:
            raise TelegramMediaError('unexpected_error') from None

    def get_me(self):
        return self.call("getMe", {})

    def get_updates(self, offset=None, poll_timeout=25):
        if type(poll_timeout) is not int or not 0 <= poll_timeout <= 50:
            raise ValueError("Poll duration must be an integer from 0 to 50 seconds")
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
