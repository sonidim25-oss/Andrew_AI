import json
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from andrewbot.model import OpenRouterModel
from andrewbot.configuration import load_model_config


class OpenRouterModelTests(unittest.TestCase):
    def test_http_error_reports_status_without_api_key(self):
        key = "sk-or-v1-synthetic-secret"

        def reject(request, timeout):
            raise HTTPError(request.full_url, 401, "unauthorized", {}, None)

        model = OpenRouterModel(key)
        import urllib.request
        original = urllib.request.urlopen
        try:
            urllib.request.urlopen = reject
            with self.assertRaisesRegex(RuntimeError, "HTTP status 401") as caught:
                model.reply([])
        finally:
            urllib.request.urlopen = original
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

        with patch("andrewbot.model.urllib.request.urlopen", return_value=Response()) as mocked:
            model = OpenRouterModel("synthetic-key", model_config=config)
            model.complete({"model": "old/model", "messages": [{"role": "user", "content": "hello"}]})
        request = mocked.call_args.args[0]
        self.assertEqual(json.loads(request.data)["model"], "google/gemini-2.5-flash")


if __name__ == "__main__":
    unittest.main()
