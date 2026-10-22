import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from andrewbot.gateway import ModelGateway
from andrewbot.model import OpenRouterModel, ProviderRequestError
from andrewbot.store import Store

RESPONSE = {'id': 'synthetic', 'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 5, 'completion_tokens': 1, 'cost': 0.000001}}


def refused_port():
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class LocalServer:
    """Records every request that actually reaches the synthetic provider."""

    def __init__(self, redirect_to=None, tunnel_close=False):
        self.received = []
        self.accepted = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_CONNECT(self):
                owner.received.append(('CONNECT', self.path))
                self.close_connection = True

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                owner.received.append(('POST', json.loads(body)))
                owner.accepted.set()
                if redirect_to:
                    self.send_response(302)
                    self.send_header('Location', redirect_to)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                output = json.dumps(RESPONSE).encode()
                try:
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(output)))
                    self.end_headers()
                    self.wfile.write(output)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}/provider'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def posts(self):
        return [body for method, body in self.received if method == 'POST']


class ProviderTransportEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'ledger.sqlite3')
        self.addCleanup(self.store.close)
        self.provider = OpenRouterModel('synthetic-key', timeout=5)
        self.gateway = ModelGateway(self.provider, self.store, 'synthetic-token')
        self.payload = {'model': self.gateway.model_id, 'messages': [{'role': 'user', 'content': 'transport fixture'}]}
        environment = patch.dict(os.environ, {'NO_PROXY': '*', 'no_proxy': '*'})
        environment.start()
        self.addCleanup(environment.stop)

    def provider_error(self):
        with self.assertRaises(ProviderRequestError) as caught:
            self.provider.complete(self.payload)
        return caught.exception

    def reservations(self):
        return self.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0]

    def test_send_failure_after_body_received_stays_uncertain_without_second_dispatch(self):
        original = http.client.HTTPConnection.send
        state = {'injected': False}
        with LocalServer() as server:
            def send_then_fail(connection, data):
                original(connection, data)
                if not state['injected'] and isinstance(data, bytes) and data.startswith(b'{'):
                    state['injected'] = True
                    self.assertTrue(server.accepted.wait(2))
                    raise ConnectionResetError('synthetic failure after body transmission')
            self.provider.endpoint = server.url
            with patch.object(http.client.HTTPConnection, 'send', send_then_fail), self.assertLogs(level='WARNING'):
                status, result = self.gateway.complete('Bearer synthetic-token', self.payload)
            self.assertEqual((status, result['error']['code']), (422, 'provider_attempt_uncertain'))
            self.assertEqual(self.reservations(), 1)
            self.assertEqual(self.gateway.complete('Bearer synthetic-token', self.payload)[0], 422)
            self.assertEqual(len(server.posts()), 1)

    def test_refused_connection_is_proven_not_sent_and_retry_dispatches_once(self):
        self.provider.endpoint = f'http://127.0.0.1:{refused_port()}/provider'
        error = self.provider_error()
        self.assertEqual((error.safe_category, error.request_sent), ('network_error', False))
        with self.assertLogs(level='WARNING'):
            status, result = self.gateway.complete('Bearer synthetic-token', self.payload)
        self.assertEqual((status, result['error']['code']), (503, 'provider_not_reached'))
        self.assertEqual(self.reservations(), 0)
        with LocalServer() as server:
            self.provider.endpoint = server.url
            self.assertEqual(self.gateway.complete('Bearer synthetic-token', self.payload)[0], 200)
            self.assertEqual(len(server.posts()), 1)

    def test_connect_phase_dns_and_timeout_failures_are_proven_not_sent(self):
        self.provider.endpoint = 'http://provider.invalid/provider'
        for failure, category in ((socket.gaierror('synthetic dns'), 'network_error'),
                                  (TimeoutError('synthetic connect timeout'), 'timeout')):
            with self.subTest(category=category), patch('socket.create_connection', side_effect=failure):
                error = self.provider_error()
                self.assertEqual((error.safe_category, error.request_sent), (category, False))

    def test_redirect_then_connect_failure_stays_uncertain(self):
        with LocalServer(redirect_to=f'http://127.0.0.1:{refused_port()}/elsewhere') as server:
            self.provider.endpoint = server.url
            error = self.provider_error()
            self.assertEqual(error.safe_category, 'network_error')
            self.assertTrue(error.request_sent)
            with self.assertLogs(level='WARNING'):
                self.assertEqual(self.gateway.complete('Bearer synthetic-token', self.payload)[0], 422)
            self.assertEqual(self.reservations(), 1)
            self.assertEqual(len(server.posts()), 2)

    def test_unreachable_proxy_is_proven_not_sent(self):
        proxy = f'http://127.0.0.1:{refused_port()}'
        with patch.dict(os.environ, {'HTTPS_PROXY': proxy, 'https_proxy': proxy, 'NO_PROXY': '', 'no_proxy': ''}):
            error = self.provider_error()
        self.assertEqual((error.safe_category, error.request_sent), ('network_error', False))

    def test_proxy_tunnel_failure_after_connect_request_stays_uncertain(self):
        with LocalServer() as proxy:
            address = proxy.url.rsplit('/', 1)[0]
            with patch.dict(os.environ, {'HTTPS_PROXY': address, 'https_proxy': address, 'NO_PROXY': '', 'no_proxy': ''}):
                error = self.provider_error()
            self.assertEqual([method for method, _ in proxy.received], ['CONNECT'])
        self.assertEqual(error.safe_category, 'network_error')
        self.assertTrue(error.request_sent)

    def recovery_fixture(self):
        from prototype.tests.test_recovery_dispatch import RecoveryDispatchTests
        fixture = RecoveryDispatchTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture, ModelGateway(self.provider, fixture.store, 'fixture-token')

    def test_recovery_mode_backs_off_proven_unsent_failure_then_dispatches_once(self):
        fixture, gateway = self.recovery_fixture()
        self.provider.endpoint = f'http://127.0.0.1:{refused_port()}/provider'
        metadata = fixture.metadata()
        with self.assertLogs(level='WARNING'):
            status, result = gateway.complete('Bearer fixture-token', fixture.payload, recovery=metadata)
        self.assertEqual((status, result['error']['code']), (503, 'provider_not_reached'))
        proof = fixture.records.effect_result(metadata['admission_id'])
        self.assertEqual((proof['state'], proof['reason'], proof['http_status']), ('rejected', 'provider_not_reached', None))
        ledger = fixture.store.connection
        self.assertEqual(ledger.execute('SELECT count(*) FROM budget_reservations').fetchone()[0], 0)
        self.assertEqual(ledger.execute('SELECT count(*) FROM usage').fetchone()[0], 0)
        with LocalServer() as server:
            self.provider.endpoint = server.url
            self.assertEqual(gateway.complete('Bearer fixture-token', fixture.payload, recovery=metadata)[0], 503)
            self.assertEqual(server.posts(), [])
            with patch('time.time', return_value=proof['retry_at_ms'] / 1000 + 1):
                self.assertEqual(gateway.complete('Bearer fixture-token', fixture.payload, recovery=metadata)[0], 200)
            self.assertEqual(len(server.posts()), 1)
        self.assertEqual(ledger.execute('SELECT count(*) FROM usage').fetchone()[0], 1)
        self.assertEqual(fixture.records.effect_result(metadata['admission_id'])['state'], 'confirmed')

    def test_recovery_mode_keeps_maybe_sent_failure_held(self):
        fixture, gateway = self.recovery_fixture()
        original = http.client.HTTPConnection.send
        with LocalServer() as server:
            def send_then_fail(connection, data):
                original(connection, data)
                if isinstance(data, bytes) and data.startswith(b'{'):
                    self.assertTrue(server.accepted.wait(2))
                    raise ConnectionResetError('synthetic failure after body transmission')
            self.provider.endpoint = server.url
            metadata = fixture.metadata()
            with patch.object(http.client.HTTPConnection, 'send', send_then_fail), self.assertLogs(level='WARNING'):
                status, result = gateway.complete('Bearer fixture-token', fixture.payload, recovery=metadata)
            self.assertEqual((status, result['error']['code']), (422, 'provider_attempt_uncertain'))
            self.assertEqual(fixture.records.effect_result(metadata['admission_id'])['state'], 'uncertain')
            self.assertEqual(fixture.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0], 1)
            self.assertEqual(gateway.complete('Bearer fixture-token', fixture.payload, recovery=metadata)[0], 422)
            self.assertEqual(len(server.posts()), 1)


if __name__ == '__main__':
    unittest.main()
