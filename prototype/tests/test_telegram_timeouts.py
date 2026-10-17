import json
import unittest
from unittest.mock import Mock

from andrewbot.telegram import TelegramAPI


class TelegramTimeoutTests(unittest.TestCase):
    def api(self, **kwargs):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({'ok': True, 'result': {'message_id': 1, 'chat': {'id': 17}, 'text': 'hello'}}).encode()
        opener = Mock(return_value=response)
        return TelegramAPI('TEST', opener=opener, **kwargs), opener

    def test_interactive_wait_is_five_seconds(self):
        api, opener = self.api()
        api.get_me()
        self.assertEqual(opener.call_args.kwargs['timeout'], 5)
        api.send_message(17, 'hello')
        self.assertEqual(opener.call_args.kwargs['timeout'], 5)

    def test_poll_wait_exceeds_requested_long_poll_by_five_seconds(self):
        api, opener = self.api()
        for requested in (25, 1, 0):
            api.get_updates(poll_timeout=requested)
            self.assertEqual(opener.call_args.kwargs['timeout'], requested + 5)

    def test_explicit_timeout_remains_an_override(self):
        api, opener = self.api(timeout=12)
        api.get_me()
        self.assertEqual(opener.call_args.kwargs['timeout'], 12)
        api.get_updates(poll_timeout=1)
        self.assertEqual(opener.call_args.kwargs['timeout'], 12)

    def test_invalid_poll_duration_is_rejected_before_network(self):
        api, opener = self.api()
        for invalid in (-1, True, '25', float('inf'), float('nan')):
            with self.subTest(value=invalid):
                with self.assertRaises(ValueError):
                    api.get_updates(poll_timeout=invalid)
        opener.assert_not_called()
