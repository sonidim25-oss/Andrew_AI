"""Saved-message work through isolated Letta; optional real >120-second proof."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'prototype'))
from andrewbot.appserver import AppServerClient, LettaAgent
from andrewbot.controller import Controller
from andrewbot.gateway import GatewayThread, ModelGateway, make_server
from andrewbot.store import Store


@unittest.skipUnless(os.environ.get('ANDREW_LETTA_TEST_BINARY'), 'requires isolated pinned Letta harness')
class ScheduledQueueProtocolTests(unittest.TestCase):
    @patch('andrewbot.appserver.gmail_permissions', return_value={'enabled': False})
    def test_scheduled_queue_completes_multiple_model_and_tool_rounds(self, _permissions):
        # Set to 43 for three real requests spanning the former 120-second limit.
        delay = float(os.environ.get('ANDREW_QUEUE_STEP_DELAY', '0'))
        with tempfile.TemporaryDirectory(prefix='andrew-queue-protocol-') as directory:
            root = Path(directory)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            environment = dict(os.environ, HOME=str(root), LETTA_HOME=str(root / 'settings'),
                               LETTA_LOCAL_BACKEND_DIR=str(root / 'backend'), DO_NOT_TRACK='1')
            store = Store(root / 'controller.sqlite3')
            self.addCleanup(store.close)
            calls = []

            class Provider:
                def complete(self, payload):
                    calls.append(payload)
                    time.sleep(delay)
                    if len(calls) <= 2:
                        name = ('check_budget', 'recent_activity')[len(calls) - 1]
                        message = {'role': 'assistant', 'content': None, 'tool_calls': [
                            {'id': f'fixture-{len(calls)}', 'type': 'function',
                             'function': {'name': name, 'arguments': '{}'}}]}
                        reason = 'tool_calls'
                    else:
                        message = {'role': 'assistant', 'content': 'Saved work completed.'}
                        reason = 'stop'
                    return {'id': f'fixture-{len(calls)}', 'model': 'qwen/qwen3.8-flash',
                            'choices': [{'index': 0, 'message': message, 'finish_reason': reason}],
                            'usage': {'prompt_tokens': 20, 'completion_tokens': 5, 'cost': .000001}}

            gateway = GatewayThread(make_server(ModelGateway(Provider(), store, 'fixture-token'), port=0))
            gateway.start()
            self.addCleanup(gateway.close)
            with (root / 'server.log').open('w') as log:
                server = subprocess.Popen(['node', os.environ['ANDREW_LETTA_TEST_BINARY'], 'server',
                    '--backend', 'local', '--listen', f'ws://127.0.0.1:{port}', '--openai-api'],
                    cwd=root, env=environment, stdout=log, stderr=log)
                agent = worker = None
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
                        agent_id = client.create_agent({'name': 'Saved work fixture', 'system': 'Use the fixture tools.',
                            'model': 'openai-compatible/qwen/qwen3.8-flash',
                            'model_settings': {'provider_type': 'openai-compatible', 'max_tokens': 512,
                                               'parallel_tool_calls': False},
                            'include_base_tools': False, 'memory_blocks': []})
                    finally:
                        client.close()
                    agent = LettaAgent(store, 'fixture-token', url=f'ws://127.0.0.1:{port}/ws')
                    agent.agent_id = agent_id
                    controller = Controller(store, agent, 17, autonomous_enabled=True)
                    tool_names = []
                    def tool_handler(name, arguments):
                        tool_names.append(name)
                        return controller.execute_tool(name, arguments)
                    agent.tool_handler = tool_handler
                    store.add_message(17, 'user', 'Complete the saved fixture work.', queued=True)
                    due = datetime.now(timezone.utc) - timedelta(seconds=1)
                    store.transition_state({'sleeping': '1', 'sleep_until': due.isoformat()}, 'fixture sleep')
                    self.assertTrue(controller.check_scheduled_wake())
                    replies = []
                    started = time.monotonic()
                    worker = threading.Thread(target=lambda: replies.append(controller.flush_queued()), daemon=True)
                    worker.start()
                    worker.join(170)
                    self.assertFalse(worker.is_alive(), 'saved work did not finish within fixture watchdog')
                    self.assertEqual(replies, ['Saved work completed.'])
                    self.assertEqual(tool_names, ['check_budget', 'recent_activity'])
                    self.assertEqual(len(calls), 3)
                    self.assertGreaterEqual(time.monotonic() - started, 3 * delay)
                    self.assertEqual(store.get_state('autonomy_error', ''), '')
                    self.assertEqual(store.pending_message_count(17), 0)
                    self.assertEqual(store.connection.execute('SELECT count(*) FROM usage').fetchone()[0], 3)
                finally:
                    if worker is not None and worker.is_alive():
                        agent.cancel_active_turn()
                        worker.join(5)
                    server.terminate()
                    server.wait(timeout=10)
