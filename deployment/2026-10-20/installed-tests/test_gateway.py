import json
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from unittest.mock import patch

from andrewbot.gateway import GatewayThread, ModelGateway, make_server
from andrewbot.store import Store


class FakeProvider:
    model_id = "qwen/qwen3.8-flash"

    def __init__(self):
        self.calls = []
        self.response = None

    def complete(self, payload):
        self.calls.append(payload)
        return self.response or {"id": "cmpl-test", "choices": [{"message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 2, "cost": 0.000002}}


class ModelGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "ledger.sqlite3")
        self.provider = FakeProvider()
        self.gateway = ModelGateway(self.provider, self.store, "gateway-test-token", daily_target_usd=0.30, monthly_limit_usd=10, monthly_reserve_usd=1)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_forwards_only_authorized_model_and_records_provider_usage(self):
        status, result = self.gateway.complete("Bearer gateway-test-token", {"model": "openai/qwen/qwen3.8-flash", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 100})
        self.assertEqual(status, 200)
        self.assertEqual(result["choices"][0]["message"]["content"], "Hello")
        self.assertEqual(self.provider.calls[0]["model"], "qwen/qwen3.8-flash")
        self.assertEqual(self.store.usage_totals(*self.gateway.periods())["monthly_cost"], 0.000002)

    def test_rejects_unauthenticated_requests_without_provider_call(self):
        status, _ = self.gateway.complete("Bearer wrong", {"model": "openai/qwen/qwen3.8-flash", "messages": []})
        self.assertEqual(status, 401)
        self.assertEqual(self.provider.calls, [])

    def test_enforces_monthly_reserve_before_forwarding(self):
        month, day = self.gateway.periods()
        self.store.record_usage(month, day, 9.0, 1, 1)
        status, _ = self.gateway.complete("Bearer gateway-test-token", {"model": "openai/qwen/qwen3.8-flash", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(status, 429)
        self.assertEqual(self.provider.calls, [])

    def test_openai_compatible_http_route_supports_models_and_streaming(self):
        server = make_server(self.gateway, port=0)
        thread = GatewayThread(server)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}/v1"
        try:
            request = Request(base + "/models", headers={"Authorization": "Bearer gateway-test-token"})
            with urlopen(request, timeout=2) as response:
                models = json.load(response)
            self.assertEqual(models["data"][0]["id"], "qwen/qwen3.8-flash")

            payload = {"model": "openai/qwen/qwen3.8-flash", "messages": [{"role": "user", "content": "hello"}], "stream": True}
            request = Request(base + "/chat/completions", data=json.dumps(payload).encode(), headers={"Authorization": "Bearer gateway-test-token", "Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=2) as response:
                self.assertIn("text/event-stream", response.headers.get("Content-Type", ""))
                chunks = []
                for line in response:
                    chunks.append(line.decode())
                    if line.strip() == b"data: [DONE]":
                        break
                stream = "".join(chunks)
            self.assertIn('"content":"Hello"', stream)
            self.assertIn("[DONE]", stream)

            self.provider.response = {
                "id": "cmpl-tool",
                "choices": [{"message": {"role": "assistant", "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "sleep", "arguments": json.dumps({"minutes": 5})},
                }]}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 2, "cost": 0.000002},
            }
            request = Request(base + "/chat/completions", data=json.dumps(payload).encode(), headers={"Authorization": "Bearer gateway-test-token", "Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=2) as response:
                tool_stream = []
                for line in response:
                    tool_stream.append(line.decode())
                    if line.strip() == b"data: [DONE]":
                        break
            self.assertIn('"name":"sleep"', "".join(tool_stream))
            self.assertIn('"index":0', "".join(tool_stream))
        finally:
            thread.close()


if __name__ == "__main__":
    unittest.main()
