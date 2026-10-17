"""Pinned Letta media tool callbacks with isolated state and a fake provider."""
import json
import os
import re
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from andrewbot.appserver import AppServerClient, LettaAgent
from andrewbot.controller import Controller
from andrewbot.gateway import ModelGateway, GatewayThread, make_server
from andrewbot.media import MediaService, MediaPolicy
from andrewbot.store import Store


@unittest.skipUnless(os.environ.get('ANDREW_LETTA_TEST_BINARY'), 'set pinned Letta JS path')
class MediaProtocolTests(unittest.TestCase):
    @patch('andrewbot.appserver.gmail_permissions', return_value={'enabled': False})
    def test_real_harness_preserves_turn_scoped_media_authorization(self, permissions):
        with tempfile.TemporaryDirectory(prefix='andrew-media-protocol-') as directory:
            root = Path(directory)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            store = Store(root / 'controller.sqlite3')
            media = MediaService(store, root / 'artifacts', 17, MediaPolicy(enabled=True))
            calls = []
            class Provider:
                def complete(inner, payload):
                    calls.append(payload)
                    tools = {tool['function']['name'] for tool in payload.get('tools', [])}
                    self.assertIn('voice.generate', tools)
                    message = {'role': 'assistant', 'content': 'Fixture response.'}
                    finish = 'stop'
                    if len(calls) == 2:
                        message = {'role': 'assistant', 'content': None, 'tool_calls': [
                            {'id': 'skill-fixture', 'type': 'function', 'function': {
                                'name': 'skills.load', 'arguments': json.dumps({'name': 'voice-communication'})}}]}
                        finish = 'tool_calls'
                    elif len(calls) == 3:
                        request_id = re.findall(r'owner-reply:\d+', json.dumps(payload['messages']))[-1]
                        message = {'role': 'assistant', 'content': None, 'tool_calls': [
                            {'id': 'voice-fixture', 'type': 'function', 'function': {
                                'name': 'voice.generate', 'arguments': json.dumps({'text': 'A spoken fixture answer.', 'owner_request_id': request_id})}}]}
                        finish = 'tool_calls'
                    return {'id': 'fixture', 'model': 'qwen/qwen3.8-flash',
                            'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
                            'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'cost': .000001}}
            http = make_server(ModelGateway(Provider(), store, 'fixture-token'), port=0)
            gateway = GatewayThread(http)
            gateway.start()
            environment = dict(os.environ, LETTA_HOME=str(root / 'settings'),
                               LETTA_LOCAL_BACKEND_DIR=str(root / 'backend'), DO_NOT_TRACK='1')
            with (root / 'server.log').open('w') as log:
                process = subprocess.Popen(['node', os.environ['ANDREW_LETTA_TEST_BINARY'], 'server', '--backend', 'local',
                                            '--listen', f'ws://127.0.0.1:{port}', '--openai-api'],
                                           cwd=root, env=environment, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 30
                    while True:
                        self.assertIsNone(process.poll())
                        try:
                            client = AppServerClient(f'ws://127.0.0.1:{port}/ws', timeout=5)
                            client.close()
                            break
                        except ConnectionRefusedError:
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(.1)
                    class FixtureClient(AppServerClient):
                        def ensure_provider(inner, token):
                            inner.request({'type': 'connect_provider', 'target': 'local', 'provider_id': 'openai-compatible',
                                           'fields': {'apiKey': token, 'baseUrl': f'http://127.0.0.1:{http.server_port}/v1'}},
                                          'connect_provider_response')
                    with patch('andrewbot.appserver.AppServerClient', FixtureClient):
                        agent = LettaAgent(store, 'fixture-token', url=f'ws://127.0.0.1:{port}/ws', owner_id=17)
                        agent.founding_letter = 'Isolated fixture introduction.'
                        agent.media_tools_enabled = True
                        agent.initialize()
                        controller = Controller(store, agent, 17, media=media)
                        agent.tool_handler = controller.execute_tool
                        controller.flush_queued()
                        controller.handle_update({'update_id': 101, 'message': {'from': {'id': 17},
                                                 'chat': {'id': 17, 'type': 'private'}, 'text': '/voice_reply Hello'}})
                        self.assertEqual(4, len(calls))
                        self.assertTrue(any('# Voice communication' in json.dumps(call['messages']) for call in calls))
                        self.assertEqual(1, len(media.status()['jobs']))
                        self.assertEqual('queued', media.status()['jobs'][0]['state'])
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    gateway.close()
                    store.close()
