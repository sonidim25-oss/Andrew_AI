"""Proven-unsent provider exhaustion through the isolated pinned Letta runtime (legacy, non-journaled mode)."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'prototype'))
from andrewbot.appserver import AppServerClient, LettaAgent
from andrewbot.controller import Controller
from andrewbot.gateway import GatewayThread, ModelGateway, make_server
from andrewbot.model import ProviderRequestError
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store


@unittest.skipUnless(os.environ.get('ANDREW_LETTA_TEST_BINARY'), 'requires isolated pinned Letta harness')
class UnsentProviderExhaustionProtocolTests(unittest.TestCase):
    @patch('andrewbot.appserver.gmail_permissions', return_value={'enabled': False})
    def test_exhaustion_schedules_backoff_then_the_next_cycle_completes(self, _permissions):
        with tempfile.TemporaryDirectory(prefix='andrew-unsent-protocol-') as directory:
            root = Path(directory)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            environment = dict(os.environ, HOME=str(root), LETTA_HOME=str(root / 'settings'),
                               LETTA_LOCAL_BACKEND_DIR=str(root / 'backend'), DO_NOT_TRACK='1')
            store = Store(root / 'controller.sqlite3')
            self.addCleanup(store.close)
            state = {'reachable': False, 'calls': 0}

            class Provider:
                def complete(self, payload):
                    state['calls'] += 1
                    if not state['reachable']:
                        raise ProviderRequestError('network_error', request_sent=False)
                    return {'id': 'fixture', 'model': 'qwen/qwen3.8-flash',
                            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'Back online.'},
                                         'finish_reason': 'stop'}],
                            'usage': {'prompt_tokens': 20, 'completion_tokens': 3, 'cost': .000001}}

            gateway = GatewayThread(make_server(ModelGateway(Provider(), store, 'fixture-token'), port=0))
            gateway.start()
            self.addCleanup(gateway.close)
            with (root / 'server.log').open('w') as log:
                server = subprocess.Popen(['node', os.environ['ANDREW_LETTA_TEST_BINARY'], 'server',
                    '--backend', 'local', '--listen', f'ws://127.0.0.1:{port}', '--openai-api'],
                    cwd=root, env=environment, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 30
                    while True:
                        self.assertIsNone(server.poll(), 'fixture backend exited')
                        try:
                            client = AppServerClient(f'ws://127.0.0.1:{port}/ws', timeout=5)
                            break
                        except ConnectionRefusedError:
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(.1)
                    try:
                        client.request({'type': 'connect_provider', 'target': 'local', 'provider_id': 'openai-compatible',
                            'fields': {'apiKey': 'fixture-token', 'baseUrl': f'http://127.0.0.1:{gateway.server.server_port}/v1'}},
                            'connect_provider_response')
                        agent_id = client.create_agent({'name': 'Unsent fixture', 'system': 'Reply briefly.',
                            'model': 'openai-compatible/qwen/qwen3.8-flash',
                            'model_settings': {'provider_type': 'openai-compatible', 'max_tokens': 512,
                                               'parallel_tool_calls': False},
                            'include_base_tools': False, 'memory_blocks': []})
                    finally:
                        client.close()
                    agent = LettaAgent(store, 'fixture-token', url=f'ws://127.0.0.1:{port}/ws')
                    agent.agent_id = agent_id
                    controller = Controller(store, agent, 17, autonomous_enabled=True)
                    agent.tool_handler = controller.execute_tool
                    runner = SessionRunner(store, controller, agent, enabled=True)
                    now = datetime.now(timezone.utc)
                    controller._command('/wake')

                    self.assertFalse(runner.tick(now))
                    self.assertEqual(state['calls'], 16)
                    self.assertEqual(store.get_state('autonomy_error', ''), '')
                    self.assertEqual(store.get_state('session_start_requested'), 'provider_retry')
                    self.assertEqual(store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0], 0)
                    self.assertEqual(store.connection.execute(
                        "SELECT count(*) FROM provider_attempts WHERE outcome!='not_reached'").fetchone()[0], 0)

                    self.assertFalse(runner.tick(now + timedelta(seconds=30)))
                    self.assertEqual(state['calls'], 16)
                    state['reachable'] = True
                    self.assertTrue(runner.tick(now + timedelta(seconds=60)))
                    self.assertEqual(state['calls'], 17)
                    self.assertEqual(store.get_state('autonomy_error', ''), '')
                    self.assertEqual(store.connection.execute(
                        "SELECT status FROM cycles ORDER BY started_at").fetchall()[-1][0], 'complete')
                finally:
                    server.terminate()
                    server.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
