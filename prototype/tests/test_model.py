import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from andrewbot.model import OpenRouterModel, ProviderRequestError, retry_after_seconds
from andrewbot.configuration import load_model_config


class OpenRouterModelTests(unittest.TestCase):
    def test_rate_limit_preserves_only_standard_retry_delay(self):
        failure=HTTPError('fixed',429,'private body',{'Retry-After':'37','Private':'secret'},None)
        with patch('urllib.request.OpenerDirector.open',side_effect=failure):
            with self.assertRaises(ProviderRequestError) as caught:
                OpenRouterModel('private-key').complete({'messages':[]})
        self.assertEqual(caught.exception.http_status,429)
        self.assertEqual(caught.exception.retry_after,37)
        self.assertNotIn('private',str(caught.exception))

    def test_retry_after_date_and_invalid_values(self):
        with patch('andrewbot.model.time.time',return_value=0):
            self.assertEqual(retry_after_seconds('Thu, 01 Jan 1970 00:01:00 GMT'),60)
        for value in (None,'secret','-5','NaN','Infinity','9'*129):
            self.assertIsNone(retry_after_seconds(value))

    def test_provider_failures_keep_only_safe_category_and_http_status(self):
        import http.client
        cases = [(HTTPError('fixed', 503, 'private provider body', {}, None), 'http_error', 503),
                 (TimeoutError('private timeout'), 'timeout', None),
                 (URLError(TimeoutError('private nested timeout')), 'timeout', None),
                 (URLError('private connection details'), 'network_error', None),
                 (http.client.IncompleteRead(b'private partial response'), 'network_error', None)]
        for failure, category, status in cases:
            with self.subTest(category=category), patch('urllib.request.OpenerDirector.open', side_effect=failure):
                with self.assertRaises(ProviderRequestError) as caught:
                    OpenRouterModel('private-key').complete({'messages': []})
                self.assertEqual(caught.exception.safe_category, category)
                self.assertEqual(caught.exception.http_status, status)
                self.assertTrue(caught.exception.request_sent)
                self.assertNotIn('private', str(caught.exception))

    def test_http_error_reports_status_without_api_key(self):
        key = "sk-or-v1-synthetic-secret"

        def reject(request, timeout):
            raise HTTPError(request.full_url, 401, "unauthorized", {}, None)

        model = OpenRouterModel(key)
        with patch('urllib.request.OpenerDirector.open', side_effect=reject):
            with self.assertRaisesRegex(RuntimeError, "HTTP status 401") as caught:
                model.reply([])
        self.assertNotIn(key, str(caught.exception))

    def test_configured_model_id_is_sent_to_openrouter(self):
        config = load_model_config({"ANDREW_MODEL_ID": "google/gemini-2.5-flash", "ANDREW_MODEL_INPUT_USD_PER_MILLION": "0.15", "ANDREW_MODEL_OUTPUT_USD_PER_MILLION": "0.47"})

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"choices":[]}'

        with patch("urllib.request.OpenerDirector.open", return_value=Response()) as mocked:
            model = OpenRouterModel("synthetic-key", model_config=config)
            model.complete({"model": "old/model", "messages": [{"role": "user", "content": "hello"}]})
        request = mocked.call_args.args[0]
        self.assertEqual(json.loads(request.data)["model"], "google/gemini-2.5-flash")


if __name__ == "__main__":
    unittest.main()

class ProviderTimeoutTests(unittest.TestCase):
    def test_default_provider_timeout_accommodates_long_compaction_and_can_be_overridden(self):
        self.assertEqual(OpenRouterModel('fixture').timeout,600)
        self.assertEqual(OpenRouterModel('fixture',timeout=17).timeout,17)
