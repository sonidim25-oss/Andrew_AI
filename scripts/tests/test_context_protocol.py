"""Long-conversation recovery against the pinned SDK and an unbilled provider."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'prototype'))
from andrewbot.appserver import AppServerClient, LettaAgent
from andrewbot.gateway import GatewayThread, ModelGateway, make_server
from andrewbot.store import Store


@unittest.skipUnless(os.environ.get('ANDREW_LETTA_TEST_BINARY'), 'requires isolated pinned Letta harness')
class ContextProtocolTests(unittest.TestCase):
    @patch('andrewbot.appserver.gmail_permissions', return_value={'enabled': False})
    def test_runtime_compacts_history_without_controller_byte_rejections(self, _permissions):
        with tempfile.TemporaryDirectory(prefix='andrew-context-protocol-') as directory:
            root = Path(directory)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            environment = dict(os.environ, LETTA_HOME=str(root / 'settings'),
                               LETTA_LOCAL_BACKEND_DIR=str(root / 'backend'), DO_NOT_TRACK='1')
            with (root / 'server.log').open('w') as log:
                process = subprocess.Popen(['node', os.environ['ANDREW_LETTA_TEST_BINARY'],
                    'server', '--backend', 'local', '--listen', f'ws://127.0.0.1:{port}', '--openai-api'],
                    cwd=root, env=environment, stdout=log, stderr=log)
                gateway_thread = None
                store = Store(root / 'state.db')
                try:
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            client = AppServerClient(f'ws://127.0.0.1:{port}/ws')
                            break
                        except ConnectionRefusedError:
                            self.assertIsNone(process.poll())
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(.1)
                    calls, overflow, summaries = [], [], []

                    class Provider:
                        def complete(self, payload):
                            calls.append(payload)
                            text = json.dumps(payload['messages']).lower()
                            summarizing = ('messages are being evicted' in text
                                           or 'task is to create a detailed summary' in text)
                            if summarizing:
                                summaries.append(payload)
                            content = 'The fixture observations are retained.' if summarizing else 'A retained fixture observation. ' * 250
                            return {'id': f'fixture-{len(calls)}', 'model': 'qwen/qwen3.8-flash',
                                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
                                'usage': {'prompt_tokens': max(20, len(json.dumps(payload['messages'])) // 4),
                                          'completion_tokens': max(20, len(content) // 4), 'cost': .000002}}

                    class Gateway(ModelGateway):
                        def complete(self, auth, payload):
                            summarizing = self.is_compaction_request(payload)
                            before = self.store.get_state('owner_reply_rounds', '0')
                            result = super().complete(auth, payload)
                            if summarizing:
                                self_test.assertEqual(self.store.get_state('owner_reply_rounds', '0'), before)
                            if result[0] == 413:
                                overflow.append(True)
                            return result

                    self_test = self
                    gateway_thread = GatewayThread(make_server(Gateway(Provider(), store, 'fixture-token'), port=0))
                    gateway_thread.start()
                    client.request({'type': 'connect_provider', 'target': 'local', 'provider_id': 'openai-compatible',
                        'fields': {'apiKey': 'fixture-token', 'baseUrl': f'http://127.0.0.1:{gateway_thread.server.server_address[1]}/v1'}},
                        'connect_provider_response')
                    agent_id = client.create_agent({'name': 'Context fixture', 'system': 'Retain the fixture observations.',
                        'model': 'openai-compatible/qwen/qwen3.8-flash', 'context_window_limit': 8192,
                        'model_settings': {'provider_type': 'openai-compatible', 'max_tokens': 512},
                        'include_base_tools': False, 'memory_blocks': []})
                    client.close()
                    agent = LettaAgent(store, 'fixture-token', url=f'ws://127.0.0.1:{port}/ws')
                    agent.agent_id = agent_id
                    for number in range(20):
                        reply, _ = agent.reply([{'role': 'user', 'content': f'Continue fixture observation {number}.'}])
                        self.assertTrue(reply)
                    self.assertFalse(overflow, 'controller must not impose a byte-based context window')
                    self.assertTrue(summaries, 'fixture must actually compact the saved conversation')
                    for payload in summaries:
                        self.assertNotIn('tool_choice', payload)
                        self.assertFalse(any('This is an owner reply' in str(row.get('content', '')) for row in payload['messages']))
                finally:
                    if gateway_thread:
                        gateway_thread.close()
                    process.terminate()
                    process.wait(timeout=10)
                    store.close()
