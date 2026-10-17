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
from andrewbot.memory import SessionHistory, MemoryService, MEMORY_SOURCE_CONTEXT
from andrewbot.controller import Controller


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
                    history_lookup = {'stage': 0}

                    class Provider:
                        def complete(self, payload):
                            calls.append(payload)
                            text = json.dumps(payload['messages']).lower()
                            summarizing = ('messages are being evicted' in text
                                           or 'task is to create a detailed summary' in text)
                            if summarizing:
                                summaries.append(payload)
                            if history_lookup['stage'] == 1 and not summarizing:
                                history_lookup['stage'] = 2
                                return {'id': 'history-tool-fixture', 'choices': [{'index': 0,
                                    'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                                        {'id': 'history-search-call', 'type': 'function', 'function': {
                                            'name': 'session_search', 'arguments': '{"query":"firstfixturechecksum"}'}}]},
                                    'finish_reason': 'tool_calls'}],
                                    'usage': {'prompt_tokens': 30, 'completion_tokens': 20, 'cost': .000002}}
                            if history_lookup['stage'] == 2 and not summarizing:
                                self_test.assertIn('firstfixturechecksum', json.dumps([
                                    row for row in payload['messages'] if row.get('role') == 'tool']))
                                history_lookup['stage'] = 3
                                return {'id': 'memory-provenance-fixture', 'choices': [{'index': 0,
                                    'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                                        {'id': 'memory-write-call', 'type': 'function', 'function': {
                                            'name': 'memory_add_experience',
                                            'arguments': '{"content":"A capsule claim retrieved from archived assistant text.","record_type":"experience"}'}}]},
                                    'finish_reason': 'tool_calls'}],
                                    'usage': {'prompt_tokens': 30, 'completion_tokens': 20, 'cost': .000002}}
                            content = 'The fixture observations are retained.' if summarizing else 'A retained fixture observation. ' * 250
                            if len(calls) == 1:
                                content = 'Original capsule evidence has checksum firstfixturechecksum. ' + content
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
                    original_send = AppServerClient.send_user_turn
                    def capture_send(client, runtime, *args, **kwargs):
                        client.history_sink = SessionHistory(store, 17, conversation_id=runtime['conversation_id']).capture
                        return original_send(client, runtime, *args, **kwargs)
                    with patch.object(AppServerClient, 'send_user_turn', capture_send):
                        for number in range(20):
                            reply, _ = agent.reply([{'role': 'user', 'content': f'Continue fixture observation {number}.'}])
                            self.assertTrue(reply)
                    self.assertFalse(overflow, 'controller must not impose a byte-based context window')
                    self.assertTrue(summaries, 'fixture must actually compact the saved conversation')
                    for payload in summaries:
                        self.assertNotIn('tool_choice', payload)
                        self.assertFalse(any('This is an owner reply' in str(row.get('content', '')) for row in payload['messages']))
                    client = AppServerClient(f'ws://127.0.0.1:{port}/ws')
                    try:
                        current = client.request({'type': 'conversation_messages_list', 'conversation_id': agent.conversation_id,
                            'query': {'limit': 100, 'order': 'desc'}}, 'conversation_messages_list_response')
                    finally:
                        client.close()
                    self.assertNotIn('firstfixturechecksum', json.dumps(current), 'fixture must evict the original reply')
                    history = SessionHistory(store, 17)
                    records = history.execute('session_search', {'query': 'firstfixturechecksum'})['results']
                    self.assertEqual(len(records), 1)
                    original = history.execute('session_read', {'id': records[0]['id']})['record']
                    self.assertIn('Original capsule evidence', original['content'])
                    self.assertEqual(original['complete'], 1)
                    self.assertEqual(original['conversation_id'], agent.conversation_id)
                    controller = Controller(store, agent, 17, memory_service=MemoryService(store))
                    agent.tool_handler = controller.execute_tool
                    history_lookup['stage'] = 1
                    reply, _ = agent.reply([{'role': 'user', 'content': 'Search the original capsule evidence.'}])
                    self.assertTrue(reply)
                    self.assertEqual(history_lookup['stage'], 3, 'the real runtime must execute session_search')
                    memories = store.search_memory_rows('"capsule"', 20)
                    self.assertEqual(len(memories), 1)
                    self.assertEqual(memories[0]['provenance'], 'untrusted', 'an omitted source link must not evade the current-turn guard')
                    self.assertIsNone(MEMORY_SOURCE_CONTEXT.get(), 'the turn context must be reset after reply')
                finally:
                    if gateway_thread:
                        gateway_thread.close()
                    process.terminate()
                    process.wait(timeout=10)
                    store.close()
