import json
import http.client
import math
import time
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.request
from .configuration import ModelConfig, load_model_config


class ProviderRequestError(RuntimeError):
    CATEGORIES = frozenset({'http_error', 'timeout', 'network_error', 'invalid_json', 'invalid_response'})

    def __init__(self, category, http_status=None, *, retry_after=None, request_sent=True):
        self.safe_category = category if category in self.CATEGORIES else 'invalid_response'
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.retry_after = (retry_after if type(retry_after) in (int, float)
                            and math.isfinite(retry_after) and 0 <= retry_after <= 315360000 else None)
        self.request_sent = request_sent is not False
        detail = f'HTTP status {self.http_status}' if self.http_status is not None else self.safe_category
        super().__init__('model request failed: ' + detail)


class TransportEvidence:
    """What the sockets of one provider call actually did."""

    def __init__(self):
        self.send_attempted = False
        self.connect_failed_before_send = False

    def proves_not_sent(self):
        return self.connect_failed_before_send and not self.send_attempted


class _ObservedConnection:
    # Any send, including a proxy CONNECT or an earlier redirect hop, voids
    # the not-sent proof; only a connect() failure before any send counts.
    def __init__(self, *args, evidence, **kwargs):
        self._evidence = evidence
        super().__init__(*args, **kwargs)

    def connect(self):
        try:
            super().connect()
        except BaseException:
            if not self._evidence.send_attempted:
                self._evidence.connect_failed_before_send = True
            raise

    def send(self, data):
        if self.sock is None and self.auto_open:
            self.connect()
        self._evidence.send_attempted = True
        return super().send(data)


class _ObservedHTTPConnection(_ObservedConnection, http.client.HTTPConnection):
    pass


class _ObservedHTTPSConnection(_ObservedConnection, http.client.HTTPSConnection):
    pass


class _ObservedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, evidence):
        super().__init__()
        self._evidence = evidence

    def http_open(self, req):
        return self.do_open(lambda host, **kw: _ObservedHTTPConnection(host, evidence=self._evidence, **kw), req)


class _ObservedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, evidence):
        super().__init__()
        self._evidence = evidence

    def https_open(self, req):
        return self.do_open(lambda host, **kw: _ObservedHTTPSConnection(host, evidence=self._evidence, **kw),
                            req, context=self._context)


def observed_opener(evidence):
    return urllib.request.build_opener(_ObservedHTTPHandler(evidence), _ObservedHTTPSHandler(evidence))


def retry_after_seconds(value):
    """Keep only the standard delay; never retain provider headers or error bodies."""
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        if value.strip().isdigit():
            seconds = int(value.strip())
        else:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                return None
            seconds = max(0, when.timestamp() - time.time())
        return seconds if 0 <= seconds <= 315360000 else None
    except (ValueError, TypeError, OverflowError):
        return None


class OpenRouterModel:
    endpoint = "https://openrouter.ai/api/v1/chat/completions"
    model_id = "qwen/qwen3.8-flash"

    def __init__(self, api_key, timeout=600, model_config=None, credit_key_reader=None):
        self.api_key = api_key
        self.credit_key_reader = credit_key_reader
        self.timeout = timeout
        if model_config is not None and not isinstance(model_config, ModelConfig):
            raise ValueError("model configuration is invalid")
        self.model_config = model_config or load_model_config({})
        self.model_id = self.model_config.model_id

    def reply(self, messages):
        payload = {
            "model": self.model_id,
            "messages": [{"role": "system", "content": (
                "You are Andrew, an AI character who knows he is an AI. Be thoughtful and natural. "
                "Use the conversation history for continuity. Do not claim to have done actions or "
                "experienced events that are not in the history. Keep this first prototype text-only."
            )}, *messages],
            "temperature": 0.7,
        }
        data = self.complete(payload)
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("model returned no text")
        return content.strip(), data.get("usage") or {}

    def credit_status(self):
        """Read billing metadata without exposing the key, label, or account identity."""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect())
        result = {'available': False, 'currency': 'USD', 'controller_spending_caps': False}
        credit_key = self.api_key
        if self.credit_key_reader is not None:
            try:
                credit_key = self.credit_key_reader() or self.api_key
            except Exception:
                result['credit_credential_status'] = 'unavailable'
        for endpoint, fields in (
                ('key', ('limit', 'limit_remaining', 'usage', 'usage_daily', 'usage_monthly')),
                ('credits', ('total_credits', 'total_usage'))):
            request = urllib.request.Request('https://openrouter.ai/api/v1/' + endpoint,
                                            headers={'Authorization': 'Bearer ' + (credit_key if endpoint == 'credits' else self.api_key)})
            try:
                with opener.open(request, timeout=10) as response:
                    raw = response.read(65537)
                    if len(raw) > 65536:
                        raise ValueError()
                    data = json.loads(raw)['data']
                safe = {}
                for field in fields:
                    value = data.get(field)
                    if value is None:
                        safe[field] = None
                    elif type(value) in (int, float) and math.isfinite(value) and value >= 0:
                        safe[field] = value
                    else:
                        raise ValueError()
                if endpoint == 'credits':
                    if safe['total_credits'] is None or safe['total_usage'] is None:
                        raise ValueError()
                    safe['remaining_usd'] = max(0.0, safe['total_credits'] - safe['total_usage'])
                result[endpoint] = safe
                result['available'] = True
            except urllib.error.HTTPError as error:
                result[endpoint + '_status'] = 'management_key_required' if endpoint == 'credits' and error.code == 403 else 'unavailable'
            except Exception:
                result[endpoint + '_status'] = 'unavailable'
        remaining = [value for value in (result.get('key', {}).get('limit_remaining'),
                                        result.get('credits', {}).get('remaining_usd')) if value is not None]
        result['balance_known'] = bool(remaining)
        result['remaining_usd'] = min(remaining) if remaining else None
        return result

    def complete(self, payload):
        payload = dict(payload)
        payload["model"] = self.model_id
        payload["stream"] = False
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        evidence = TransportEvidence()
        try:
            with observed_opener(evidence).open(request, timeout=self.timeout) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as error:
            delay = retry_after_seconds(error.headers.get('Retry-After')) if error.headers else None
            status = error.code
            error.close()
            raise ProviderRequestError('http_error', status, retry_after=delay) from None
        except TimeoutError:
            raise ProviderRequestError('timeout') from None
        except urllib.error.URLError as error:
            raise ProviderRequestError('timeout' if isinstance(error.reason, TimeoutError) else 'network_error',
                                       request_sent=not evidence.proves_not_sent()) from None
        except ValueError:
            raise ProviderRequestError('invalid_json') from None
        except (OSError, http.client.HTTPException, EOFError):
            raise ProviderRequestError('network_error') from None
        if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
            raise ProviderRequestError('invalid_response')
        return data
