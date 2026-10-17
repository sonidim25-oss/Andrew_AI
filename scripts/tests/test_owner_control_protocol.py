"""Pinned Letta proof of letter receipts and cancellation; no live credentials or model calls."""
import os
from pathlib import Path
import queue
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
from andrewbot.dispatch import ControllerDispatch
from andrewbot.gateway import ModelGateway, GatewayThread, make_server
from andrewbot.memory import MemoryService
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store
from andrewbot.telegram import OwnerRateLimit


@unittest.skipUnless(os.environ.get('ANDREW_LETTA_TEST_BINARY'), 'set pinned Letta JS path')
class OwnerControlProtocolTests(unittest.TestCase):
    @patch('andrewbot.appserver.gmail_permissions', return_value={'enabled': False})
    def test_letter_is_verified_and_owner_pause_interrupts_real_blocked_turn(self, permissions):
        with tempfile.TemporaryDirectory(prefix='andrew-owner-proof-') as directory:
            root = Path(directory)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            environment = dict(os.environ, HOME=str(root), LETTA_HOME=str(root / 'settings'),
                               LETTA_LOCAL_BACKEND_DIR=str(root / 'backend'), DO_NOT_TRACK='1')
            store = Store(root / 'controller.sqlite3')
            entered, release = threading.Event(), threading.Event()
            calls = []
            class Provider:
                def complete(self, payload):
                    calls.append(payload)
                    if len(calls) == 2:
                        entered.set()
                        release.wait(15)
                    return {'id': 'fixture-completion', 'model': 'qwen/qwen3.8-flash',
                            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'Fixture response.'},
                                         'finish_reason': 'stop'}],
                            'usage': {'prompt_tokens': 20, 'completion_tokens': 5, 'cost': 0.000001}}
            http = make_server(ModelGateway(Provider(), store, 'fixture-token'), port=0)
            # Cancellation deliberately disconnects a response in this fixture.
            http.handle_error = lambda *args: None
            gateway = GatewayThread(http)
            gateway.start()
            dispatch = worker = None
            with (root / 'server.log').open('w') as log:
                server = subprocess.Popen(['node', os.environ['ANDREW_LETTA_TEST_BINARY'], 'server', '--backend', 'local',
                                           '--listen', f'ws://127.0.0.1:{port}', '--openai-api'],
                                          cwd=root, env=environment, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 30
                    while True:
                        self.assertIsNone(server.poll(), 'isolated Letta server exited')
                        try:
                            client = AppServerClient(f'ws://127.0.0.1:{port}/ws', timeout=5)
                            client.close()
                            break
                        except ConnectionRefusedError:
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(0.1)
                    class FixtureClient(AppServerClient):
                        def ensure_provider(self, token):
                            self.request({'type': 'connect_provider', 'target': 'local', 'provider_id': 'openai-compatible',
                                          'fields': {'apiKey': token, 'baseUrl': f'http://127.0.0.1:{http.server_port}/v1'}},
                                         'connect_provider_response')
                    with patch('andrewbot.appserver.AppServerClient', FixtureClient):
                        agent = LettaAgent(store, 'fixture-token', url=f'ws://127.0.0.1:{port}/ws', owner_id=17)
                        agent.founding_letter = 'An immutable owner-authored fixture introduction.'
                        agent.initialize()
                        self.assertEqual(calls, [], 'startup must not make model calls')
                        self.assertEqual(store.get_state('founding_letter_delivered'), '0')
                        self.assertEqual(len(store.queued_messages(17)), 1)
                        controller = Controller(store, agent, 17, autonomous_enabled=True, memory_service=MemoryService(store))
                        agent.tool_handler = controller.execute_tool
                        self.assertEqual(controller.flush_queued(), 'Fixture response.')
                        self.assertEqual(store.get_state('founding_letter_delivered'), '1')
                        self.assertEqual(store.queued_messages(17), [])
                        self.assertEqual(len(calls), 1)
                        agent.initialize()
                        self.assertEqual(store.queued_messages(17), [], 'restart must not redeliver the letter')
                        self.assertEqual(len(calls), 1)
                        inbox = queue.Queue()
                        notification = threading.Event()
                        class Telegram:
                            def get_updates(self, **kwargs):
                                try:
                                    return inbox.get(timeout=0.05)
                                except queue.Empty:
                                    return []
                            def send_message(self, owner, text):
                                if 'Paused' in text:
                                    notification.set()
                                return {'message_id': 1, 'chat': {'id': owner}, 'text': text}
                        runner = SessionRunner(store, controller, agent, enabled=True)
                        # Explicit owner wake starts a fresh session immediately,
                        # including after the letter reply's quiet window.
                        controller._command('/wake')
                        dispatch = ControllerDispatch(store, controller, Telegram(), runner, OwnerRateLimit(), 17, True, False)
                        dispatch.start()
                        dispatch._updates_ready.set()
                        worker = threading.Thread(target=dispatch.step)
                        worker.start()
                        self.assertTrue(entered.wait(10), 'the real Letta turn did not reach the fixture provider')
                        inbox.put([{'update_id': 1, 'message': {'from': {'id': 17}, 'chat': {'id': 17, 'type': 'private'},
                                                             'text': '/pause'}}])
                        self.assertTrue(notification.wait(2), 'owner pause waited for model completion')
                        worker.join(2)
                        self.assertFalse(worker.is_alive(), 'cancel did not interrupt the WebSocket wait')
                        self.assertFalse(release.is_set(), 'provider completed before pause was proved')
                        self.assertEqual(store.get_state('paused'), '1')
                        self.assertEqual(store.get_state('active_session_status'), 'paused')
                        # dispatch.step only signals the separate model worker.
                        # Wait for that worker's durable cancellation cleanup.
                        deadline = time.monotonic() + 2
                        while (store.connection.execute('SELECT status FROM cycles').fetchone()[0] == 'running'
                               and time.monotonic() < deadline):
                            time.sleep(.01)
                        self.assertEqual(store.connection.execute('SELECT status FROM cycles').fetchone()[0], 'stopped')
                        self.assertEqual(store.pending_diary_deliveries(), [])
                        self.assertFalse(runner.tick())
                        self.assertEqual(len(calls), 2)
                finally:
                    release.set()
                    if worker is not None:
                        worker.join(3)
                    if dispatch is not None:
                        dispatch.stop()
                    server.terminate()
                    try:
                        server.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
                    gateway.close()
                    store.close()
