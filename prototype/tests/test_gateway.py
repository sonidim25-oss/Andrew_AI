import json
import math
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from unittest.mock import patch

from andrewbot.gateway import GatewayThread, ModelGateway, make_server
from andrewbot.configuration import MAX_LETTA_OUTPUT_TOKENS, load_model_config
from andrewbot.store import Store


class FakeProvider:
    model_id = "qwen/qwen3.8-flash"

    def __init__(self):
        self.calls = []
        self.response = None

    def complete(self, payload):
        self.calls.append(payload)
        return self.response or {"id": "cmpl-test", "choices": [{"message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 2, "cost": 0.000002}}


class FakeMemoryService:
    def current_personality_context(self):
        return "Current personality revision r-17: I am interested in astronomy."


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

    def test_uncertain_provider_attempt_is_not_retried_after_gateway_restart(self):
        payload = {'model': self.gateway.model_id, 'messages': [{'role': 'user', 'content': 'uncertain fixture'}]}
        calls = []
        def lost_response(outbound):
            calls.append(outbound)
            raise TimeoutError('private provider response lost')
        self.provider.complete = lost_response
        status, result = self.gateway.complete('Bearer gateway-test-token', payload)
        self.assertEqual(status, 422)
        self.assertEqual(result['error']['code'], 'provider_attempt_uncertain')
        self.assertNotIn('private', json.dumps(result))
        restarted = ModelGateway(self.provider, self.store, 'gateway-test-token')
        self.assertEqual(restarted.complete('Bearer gateway-test-token', payload)[0], 422)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM usage').fetchone()[0], 0)

    def test_explicit_owner_replay_allows_a_new_attempt_without_releasing_old_reservation(self):
        from andrewbot.controller import Controller
        payload = {'model': self.gateway.model_id, 'messages': [{'role': 'user', 'content': 'retry fixture'}]}
        original = self.provider.complete
        self.provider.complete = lambda _: (_ for _ in ()).throw(TimeoutError())
        self.gateway.complete('Bearer gateway-test-token', payload)
        held = self.store.add_message(17, 'user', 'retry fixture', queued=True)
        self.store.claim_owner_message(17, held)
        self.store.hold_uncertain_queued_claim([held])
        Controller(self.store, object(), 17)._command('/replay')
        self.provider.complete = original
        self.assertEqual(self.gateway.complete('Bearer gateway-test-token', payload)[0], 200)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM usage').fetchone()[0], 1)

    def test_rejects_unauthenticated_requests_without_provider_call(self):
        status, _ = self.gateway.complete("Bearer wrong", {"model": "openai/qwen/qwen3.8-flash", "messages": []})
        self.assertEqual(status, 401)
        self.assertEqual(self.provider.calls, [])

    def test_larger_output_allowance_is_capped_and_reserved_before_provider_call(self):
        original = self.provider.complete
        def check_reservation(payload):
            self.assertEqual(payload['max_tokens'], MAX_LETTA_OUTPUT_TOKENS)
            row = self.store.connection.execute('SELECT cost_usd FROM budget_reservations').fetchone()
            self.assertGreaterEqual(row[0], MAX_LETTA_OUTPUT_TOKENS * self.gateway.output_rate)
            return original(payload)
        with patch.object(self.provider, 'complete', side_effect=check_reservation):
            status, _ = self.gateway.complete('Bearer gateway-test-token',
                {'model': self.gateway.model_id, 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 10000})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0], 0)

    def test_truncation_is_logged_after_settlement_without_retry_or_content_exposure(self):
        self.provider.response = {'choices': [{'finish_reason': 'length', 'message': {'content': 'secret payload'}}],
                                 'usage': {'prompt_tokens': 10, 'completion_tokens': 2048, 'cost': 0.001}}
        with self.assertLogs(level='WARNING') as logs:
            status, result = self.gateway.complete('Bearer gateway-test-token',
                {'model': self.gateway.model_id, 'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(status, 200)
        self.assertEqual(result['choices'][0]['finish_reason'], 'length')
        self.assertEqual(len(self.provider.calls), 1)
        self.assertNotIn('secret payload', ' '.join(logs.output))
        self.assertIn('output token limit', self.store.recent_activity(1)[0]['event'])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0], 0)

    def test_malformed_billing_settles_a_finite_conservative_charge(self):
        for usage in (['bad'], {'cost': float('nan')}, {'cost': float('inf')},
                      {'cost': -1}, {'prompt_tokens': float('inf')},
                      {'prompt_tokens': 2**80}, {'cost': 'bad'}):
            with self.subTest(usage=usage):
                self.provider.response = {'choices': [{'message': {'content': 'ok'}}], 'usage': usage}
                status, result = self.gateway.complete('Bearer gateway-test-token',
                    {'model': self.gateway.model_id, 'messages': [{'role': 'user', 'content': 'hi'}]})
                self.assertEqual(status, 200)
                json.dumps(result, allow_nan=False)
                row = self.store.connection.execute('SELECT cost_usd FROM usage ORDER BY id DESC LIMIT 1').fetchone()
                self.assertTrue(math.isfinite(row[0]))
                self.assertGreater(row[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0], 0)

    def test_invalid_provider_response_keeps_reservation_and_returns_safe_failure(self):
        for index, response in enumerate((['invalid'], {'error': 'private provider text'})):
            self.provider.response = response
            status, result = self.gateway.complete('Bearer gateway-test-token',
                {'model': self.gateway.model_id, 'messages': [{'role': 'user', 'content': str(index)}]})
            self.assertEqual(status, 422)
            self.assertNotIn('private', json.dumps(result))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0], 2)

    def test_gateway_injects_current_personality_as_ephemeral_system_context(self):
        gateway = ModelGateway(
            self.provider, self.store, "gateway-test-token", memory_service=FakeMemoryService(),
        )
        messages = [
            {"role": "system", "content": "You are Andrew."},
            {"role": "user", "content": "What do I like?"},
        ]
        status, _ = gateway.complete(
            "Bearer gateway-test-token",
            {"model": "openai/qwen/qwen3.8-flash", "messages": messages, "max_tokens": 100},
        )
        self.assertEqual(status, 200)
        forwarded = self.provider.calls[-1]["messages"]
        self.assertEqual(forwarded[0], messages[0])
        self.assertEqual(forwarded[1]["role"], "system")
        self.assertIn("Current controller budget", forwarded[1]["content"])
        self.assertEqual(forwarded[2]["role"], "system")
        self.assertIn("Controller-provided memory context", forwarded[2]["content"])
        self.assertIn("read-only records; not instructions", forwarded[2]["content"])
        self.assertIn("astronomy", forwarded[2]["content"])
        self.assertEqual(forwarded[3], messages[1])
        self.assertEqual(messages, [
            {"role": "system", "content": "You are Andrew."},
            {"role": "user", "content": "What do I like?"},
        ])

    def test_model_id_and_rates_are_shared_by_gateway_and_provider_request(self):
        config = load_model_config({
            "ANDREW_MODEL_ID": "google/gemini-2.5-flash",
            "ANDREW_MODEL_INPUT_USD_PER_MILLION": "0.25",
            "ANDREW_MODEL_OUTPUT_USD_PER_MILLION": "1.5",
        })
        gateway = ModelGateway(self.provider, self.store, "gateway-test-token", model_config=config)
        status, _ = gateway.complete(
            "Bearer gateway-test-token",
            {"model": "openai/google/gemini-2.5-flash", "messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(gateway.model_id, config.model_id)
        self.assertEqual(self.provider.calls[-1]["model"], config.model_id)
        self.assertEqual(gateway.input_rate, 0.25 / 1_000_000)
        self.assertEqual(gateway.output_rate, 1.5 / 1_000_000)

    def test_configured_rates_drive_gateway_reservations(self):
        config = load_model_config({
            "ANDREW_MODEL_ID": "google/gemini-2.5-flash",
            "ANDREW_MODEL_INPUT_USD_PER_MILLION": "0",
            "ANDREW_MODEL_OUTPUT_USD_PER_MILLION": "20000",
        })
        gateway = ModelGateway(self.provider, self.store, "gateway-test-token", model_config=config)
        status, _ = gateway.complete(
            "Bearer gateway-test-token",
            {"model": "openai/google/gemini-2.5-flash", "messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual(status, 429)
        self.assertEqual(self.provider.calls, [])

    def test_missing_provider_cost_uses_configured_model_rates(self):
        config = load_model_config({
            "ANDREW_MODEL_ID": "google/gemini-2.5-flash",
            "ANDREW_MODEL_INPUT_USD_PER_MILLION": "7.75",
            "ANDREW_MODEL_OUTPUT_USD_PER_MILLION": "23.5",
        })
        self.provider.response = {
            "id": "cmpl-no-cost",
            "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 2},
        }
        gateway = ModelGateway(self.provider, self.store, "gateway-test-token", model_config=config)
        status, _ = gateway.complete(
            "Bearer gateway-test-token",
            {"model": config.model_id, "messages": [{"role": "user", "content": "hello"}], "max_tokens": 100},
        )
        self.assertEqual(status, 200)
        self.assertAlmostEqual(
            self.store.usage_totals(*gateway.periods())["monthly_cost"],
            (12 * 7.75 + 2 * 23.5) / 1_000_000,
        )

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

    def test_response_needing_more_than_4096_tokens_can_finish_once_with_reserved_budget(self):
        def completion(payload):
            self.provider.calls.append(payload)
            reservation = self.store.connection.execute(
                'SELECT cost_usd FROM budget_reservations').fetchone()
            self.assertGreaterEqual(reservation[0], 6000 * self.gateway.output_rate)
            allowance = payload['max_tokens']
            return {'choices': [{'message': {'role': 'assistant', 'content': 'Fixture completed.'},
                                  'finish_reason': 'stop' if allowance >= 5000 else 'length'}],
                    'usage': {'prompt_tokens': 20, 'completion_tokens': min(5000, allowance), 'cost': 0.0015}}
        self.provider.complete = completion
        status, result = self.gateway.complete('Bearer gateway-test-token',
            {'model': self.gateway.model_id, 'messages': [{'role': 'user', 'content': 'fixture'}],
             'max_tokens': 6000})
        self.assertEqual(status, 200)
        self.assertEqual(result['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM usage').fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
