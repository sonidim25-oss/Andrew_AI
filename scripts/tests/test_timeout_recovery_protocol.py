"""Pinned, unbilled proof of orphan-run prevention and restart recovery."""
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
from andrewbot.appserver import AppServerClient, LettaAgent, LettaTurnError
from andrewbot.gateway import ModelGateway, GatewayThread, make_server
from andrewbot.store import Store


@unittest.skipUnless(os.environ.get('ANDREW_LETTA_TEST_BINARY'), 'set pinned Letta JS path')
class TimeoutRecoveryProtocolTests(unittest.TestCase):
    def exercise(self, legacy_disconnect):
        with tempfile.TemporaryDirectory(prefix='andrew-timeout-proof-') as directory:
            root = Path(directory)
            binary = Path(os.environ['ANDREW_LETTA_TEST_BINARY'])
            injected_binary = root / 'letta-orphan-fixture.mjs'
            if legacy_disconnect:
                dependencies = binary.parent / 'node_modules'
                if not dependencies.is_dir():
                    dependencies = next(parent for parent in binary.parents if parent.name == 'node_modules')
                (root / 'node_modules').symlink_to(dependencies, target_is_directory=True)
                # Reproduce the pinned backend's missing finalization when a
                # consumer stops its stream before the stop-reason chunk.
                source = binary.read_text()
                start = source.index('class HeadlessBackend')
                end = source.index('var FAKE_HEADLESS_MODEL', start)
                fragment = 'yield backend.recordRunChunk(runId, attachRunId(persisted, runId));'
                section = source[start:end]
                self.assertIn(fragment, section)
                section = section.replace(fragment, fragment + '\nif (chunk.message_type === "assistant_message") return;', 1)
                injected_binary.write_text(source[:start] + section + source[end:])
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            env = dict(os.environ, HOME=str(root), LETTA_HOME=str(root / 'settings'),
                       LETTA_LOCAL_BACKEND_DIR=str(root / 'backend'), DO_NOT_TRACK='1')
            store = Store(root / 'state.db')
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            calls, outcome, trace = [], [], []
            class Provider:
                def complete(self, payload):
                    calls.append(payload)
                    if len(calls) == 1 and not legacy_disconnect:
                        entered.set()
                        release.wait(10)
                    return {'id': 'fixture', 'model': 'qwen/qwen3.8-flash',
                            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'Recovered reply.'},
                                         'finish_reason': 'stop'}],
                            'usage': {'prompt_tokens': 20, 'completion_tokens': 5, 'cost': 0.000001}}
            http = make_server(ModelGateway(Provider(), store, 'fixture-token'), port=0)
            http.handle_error = lambda *args: None
            gateway = GatewayThread(http)
            gateway.start()
            url = f'ws://127.0.0.1:{port}/ws'
            server = worker = None
            launches = 0
            with (root / 'server.log').open('w') as log:
                def start_server():
                    nonlocal launches
                    launches += 1
                    executable = injected_binary if legacy_disconnect and launches == 1 else binary
                    process = subprocess.Popen(['node', str(executable), 'server', '--backend', 'local',
                                                '--listen', f'ws://127.0.0.1:{port}', '--openai-api'],
                                               cwd=root, env=env, stdout=log, stderr=log)
                    deadline = time.monotonic() + 20
                    while True:
                        self.assertIsNone(process.poll(), (root / 'server.log').read_text()[-1400:])
                        try:
                            client = AppServerClient(url, timeout=3)
                            client.close()
                            return process
                        except ConnectionRefusedError:
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(0.05)
                class FixtureClient(AppServerClient):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        original_read = self.connection.recv_json
                        def read():
                            event = original_read()
                            delta = event.get('delta') or {}
                            trace.append({'type': event.get('type'), 'delta_type': delta.get('message_type'),
                                          'stop': event.get('stop_reason') or delta.get('stop_reason'),
                                          'accepted': event.get('accepted'),
                                          'messages': [(m.get('message_type'), m.get('id')) for m in event.get('messages', [])]})
                            return event
                        self.connection.recv_json = read
                    def ensure_provider(self, token):
                        self.request({'type': 'connect_provider', 'target': 'local', 'provider_id': 'openai-compatible',
                                      'fields': {'apiKey': token, 'baseUrl': f'http://127.0.0.1:{http.server_port}/v1'}},
                                     'connect_provider_response')
                    def request(self, command, response_type, timeout=None):
                        if legacy_disconnect and command.get('type') == 'abort_message':
                            return {'success': True, 'aborted': False}
                        return super().request(command, response_type, timeout)
                try:
                    server = start_server()
                    with patch('andrewbot.appserver.AppServerClient', FixtureClient), \
                         patch('andrewbot.appserver.gmail_permissions', return_value={'enabled': False}):
                        agent = LettaAgent(store, 'fixture-token', url=url)
                        agent.initialize()
                        with self.assertRaises(TimeoutError):
                            agent.reply([{'role': 'user', 'content': 'An input whose response is blocked.'}], timeout=0.3)
                        if not legacy_disconnect:
                            self.assertTrue(entered.is_set())
                        def next_turn():
                            try:
                                outcome.append(agent.reply([{'role': 'user', 'content': 'A distinct subsequent input.'}], timeout=3))
                            except Exception as error:
                                outcome.append(error)
                            finally:
                                finished.set()
                        worker = threading.Thread(target=next_turn)
                        worker.start()
                        if legacy_disconnect:
                            self.assertTrue(finished.wait(2))
                            self.assertIsInstance(outcome[0], LettaTurnError)
                            self.assertEqual(outcome[0].safe_category, 'active_run_conflict')
                        else:
                            self.assertFalse(finished.wait(0.3), 'Successor was rejected before the first provider settled')
                        release.set()
                        worker.join(4)
                        self.assertFalse(worker.is_alive())
                        if legacy_disconnect:
                            ids = agent.agent_id, agent.conversation_id
                            server.terminate()
                            server.wait(timeout=5)
                            server = start_server()
                            reply, _ = agent.reply([{'role': 'user', 'content': 'A new input after backend recovery.'}], timeout=3)
                            self.assertEqual((agent.agent_id, agent.conversation_id), ids)
                            self.assertEqual(reply, 'Recovered reply.')
                        else:
                            self.assertIsInstance(outcome[0], tuple, str(outcome[0]) + str(trace[-25:]))
                            self.assertEqual(outcome[0][0], 'Recovered reply.')
                        self.assertEqual(len(calls), 2, 'Failed input must not be automatically replayed')
                finally:
                    release.set()
                    if worker is not None:
                        worker.join(5)
                    if server is not None:
                        server.terminate()
                        server.wait(timeout=5)
                    gateway.close()
                    store.close()

    def test_timeout_abort_allows_a_subsequent_turn(self):
        self.exercise(legacy_disconnect=False)

    def test_early_stream_exit_orphans_run_and_backend_restart_recovers(self):
        self.exercise(legacy_disconnect=True)
