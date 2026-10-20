import unittest
from urllib.error import HTTPError

from andrewbot.model import OpenRouterModel


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


if __name__ == "__main__":
    unittest.main()
