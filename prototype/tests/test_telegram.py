import unittest
from urllib.error import HTTPError, URLError

from andrewbot.telegram import TelegramAPI


class TelegramAPITests(unittest.TestCase):
    def test_emoji_cannot_exceed_telegram_utf16_limit(self):
        from unittest.mock import Mock
        api = TelegramAPI('fixture-token')
        api.call = Mock()
        api.send_message(17, '🙂' * 3000)
        chunks = [call.args[1]['text'] for call in api.call.call_args_list]
        self.assertEqual(''.join(chunks), '🙂' * 3000)
        for text in chunks:
            self.assertLessEqual(len(text.encode('utf-16-le')), 8000)
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


class TelegramLatencyTests(unittest.TestCase):
    def test_send_timing_records_each_part_without_poll_double_counting(self):
        import io
        import json
        from andrewbot.latency import LatencyMetrics
        clock = [10.0]
        calls = []
        def opener(request, timeout):
            calls.append((request.full_url.rsplit('/', 1)[-1], timeout))
            clock[0] += 4
            return io.BytesIO(json.dumps({'ok': True, 'result': []}).encode())
        metrics = LatencyMetrics(clock=lambda: clock[0])
        api = TelegramAPI('fixture-token', opener=opener, metrics=metrics)
        api.send_message(17, 'a' * 4500)
        api.get_updates(poll_timeout=1)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot['send']['count'], 2)
        self.assertEqual(snapshot['send']['total_seconds'], 8)
        self.assertEqual(snapshot['poll']['count'], 0)
        self.assertEqual(calls, [('sendMessage', 35), ('sendMessage', 35), ('getUpdates', 35)])

    def test_send_failures_are_timed_and_remain_redacted(self):
        import io
        import json
        from andrewbot.latency import LatencyMetrics
        for kind in ('timeout', 'malformed', 'http'):
            with self.subTest(kind=kind):
                clock = [0.0]
                metrics = LatencyMetrics(clock=lambda: clock[0])
                def opener(request, timeout):
                    clock[0] += 5
                    if kind == 'timeout':
                        raise TimeoutError('private-token private-body')
                    if kind == 'http':
                        raise HTTPError(request.full_url, 429, 'private-body', {}, None)
                    return io.BytesIO(b'private-body invalid JSON')
                api = TelegramAPI('private-token', opener=opener, metrics=metrics)
                with self.assertRaises(RuntimeError) as caught:
                    api.send_message(17, 'private-body')
                row = metrics.snapshot()['send']
                self.assertEqual((row['count'], row['failures'], row['last_seconds']), (1, 1, 5))
                self.assertEqual(row['active_count'], 0)
                self.assertNotIn('private', str(caught.exception))
                self.assertNotIn('private', json.dumps(metrics.snapshot()))
