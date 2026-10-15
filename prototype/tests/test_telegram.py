import unittest
from urllib.error import HTTPError, URLError

from andrewbot.telegram import TelegramAPI


class TelegramAPITests(unittest.TestCase):
    def test_emoji_cannot_exceed_telegram_utf16_limit(self):
        from unittest.mock import Mock
        api = TelegramAPI('fixture-token')
        api.call = Mock()
        api.send_message(17, '🙂' * 3000)
        text = api.call.call_args.args[1]['text']
        self.assertEqual(len(text.encode('utf-16-le')), 8000)
        self.assertNotIn('\ufffd', text)

    def test_request_errors_do_not_expose_bot_token(self):
        token = "123456789:synthetic-secret-token"

        def fail(request, timeout):
            raise URLError(f"connection failed for {request.full_url}")

        api = TelegramAPI(token, opener=fail)
        with self.assertRaises(RuntimeError) as caught:
            api.call("getMe", {})
        self.assertNotIn(token, str(caught.exception))

    def test_http_errors_report_only_status_not_token_url(self):
        token = "123456:synthetic-secret-token"

        def reject(request, timeout):
            raise HTTPError(request.full_url, 401, "unauthorized", {}, None)

        api = TelegramAPI(token, opener=reject)
        with self.assertRaisesRegex(RuntimeError, "HTTP status 401") as caught:
            api.call("getMe", {})
        self.assertNotIn(token, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
