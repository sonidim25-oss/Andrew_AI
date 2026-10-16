"""Real pinned Letta harness with an isolated deterministic model provider."""
import os
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "prototype"))
from andrewbot.appserver import AppServerClient, LettaAgent
from andrewbot.controller import Controller
from andrewbot.gateway import GatewayThread, ModelGateway, make_server
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store


@unittest.skipUnless(os.environ.get("ANDREW_LETTA_TEST_BINARY"), "set the pinned Letta JS path for isolated protocol integration")
class LettaProtocolTests(unittest.TestCase):
    @patch('andrewbot.appserver.gmail_permissions', return_value={'enabled': False})
    def test_real_harness_completes_diary_and_sleep_callbacks_before_returning(self, _permissions):
        with tempfile.TemporaryDirectory(prefix="andrew-protocol-") as directory:
            root = Path(directory)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            environment = dict(os.environ, LETTA_HOME=str(root / "settings"),
                               LETTA_LOCAL_BACKEND_DIR=str(root / "backend"), DO_NOT_TRACK="1")
            with (root / "server.log").open("w") as log:
                def launch():
                    return subprocess.Popen(
                        ["node", os.environ["ANDREW_LETTA_TEST_BINARY"], "server", "--backend", "local",
                         "--listen", f"ws://127.0.0.1:{port}", "--openai-api"],
                        cwd=root, env=environment, stdout=log, stderr=log,
                    )
                def connect(process):
                    deadline = time.monotonic() + 30
                    while True:
                        self.assertIsNone(process.poll(), "isolated Letta server exited before readiness")
                        try:
                            return AppServerClient(f"ws://127.0.0.1:{port}/ws", timeout=5)
                        except ConnectionRefusedError:
                            self.assertLess(time.monotonic(), deadline, "isolated Letta readiness timed out")
                            time.sleep(0.1)
                server = launch()
                gateway_thread = None
                store = None
                try:
                    client = connect(server)
                    store = Store(root / "controller.sqlite3")
                    calls = []

                    class Provider:
                        def complete(self, payload):
                            calls.append(payload)
                            names = {tool["function"]["name"] for tool in payload.get("tools", [])}
                            if names != AppServerClient.SAFE_CONTROLLER_TOOL_NAMES:
                                raise AssertionError("provider must see exactly the approved controller tools")
                            if len(calls) == 1:
                                return {"id": "local-text-check", "model": "qwen/qwen3.8-flash",
                                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "A fresh protocol reply."},
                                                     "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 20, "completion_tokens": 20, "cost": 0.000002}}
                            if len(calls) == 2:
                                name, arguments = 'research.save', json.dumps({'question': 'How does an isolated fixture preserve its state?', 'next_step': 'Read the saved entry at another wake.'})
                            elif len(calls) == 3:
                                entry = store.research_page()['entries'][0]
                                name, arguments = 'research.focus', json.dumps({'id': entry['record_id']})
                            elif len(calls) == 4:
                                name, arguments = 'save_diary_entry', json.dumps({'content': 'I tested a small idea and will rest before returning.'})
                            elif len(calls) == 5:
                                name, arguments = 'sleep', json.dumps({'minutes': 1, 'reason': 'Return to this fixture later.'})
                            elif len(calls) in (6, 7):
                                entry = store.research_page()['entries'][0]
                                name = 'research.get' if len(calls) == 6 else 'research.focus'
                                arguments = json.dumps({'id': entry['record_id']})
                            elif len(calls) == 8:
                                entry = store.research_page()['entries'][0]
                                name, arguments = 'research.save', json.dumps({'id': entry['record_id'], 'revision': entry['revision'], 'findings': 'The fixture entry survived the backend and SQLite restart.'})
                            else:
                                name, arguments = 'wake_at', json.dumps({'time': '09:00'})
                            return {"id": "local-protocol-check", "model": "qwen/qwen3.8-flash",
                                    "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                                        "tool_calls": [{"id": f"tool-{len(calls)}", "type": "function",
                                                        "function": {"name": name, "arguments": arguments}}]},
                                                 "finish_reason": "tool_calls"}],
                                    "usage": {"prompt_tokens": 20, "completion_tokens": 20, "cost": 0.000002}}

                    gateway = ModelGateway(Provider(), store, "isolated-test-token")
                    gateway_thread = GatewayThread(make_server(gateway, port=0))
                    gateway_thread.start()
                    gateway_port = gateway_thread.server.server_address[1]
                    try:
                        client.request({"type": "connect_provider", "target": "local", "provider_id": "openai-compatible",
                                        "fields": {"apiKey": "isolated-test-token", "baseUrl": f"http://127.0.0.1:{gateway_port}/v1"}},
                                       "connect_provider_response")
                        agent_id = client.create_agent({"name": "Isolated protocol check", "system": "Use the supplied controller tools.",
                            "model": "openai-compatible/qwen/qwen3.8-flash",
                            "model_settings": {"provider_type": "openai-compatible", "max_tokens": 512, "parallel_tool_calls": False},
                            "include_base_tools": False, "memory_blocks": []})
                        # Upgrade an existing agent's old output cap through the real protocol.
                        from andrewbot.configuration import load_model_config, MAX_LETTA_OUTPUT_TOKENS
                        client.update_agent_model(agent_id, load_model_config({}))
                        runtime = client.start_runtime(agent_id)
                    finally:
                        client.close()
                    agent = LettaAgent(store, "isolated-test-token", url=f"ws://127.0.0.1:{port}/ws")
                    agent.agent_id = agent_id
                    agent.conversation_id = runtime["conversation_id"]
                    store.set_state("letta_agent_id", agent_id)
                    controller = Controller(store, agent, 17, autonomous_enabled=True)
                    agent.tool_handler = controller.execute_tool
                    reply = controller.handle_update({'message': {'from': {'id': 17},
                        'chat': {'id': 17, 'type': 'private'}, 'text': 'Reply with the fresh protocol greeting.'}})
                    self.assertEqual(reply, "A fresh protocol reply.")
                    runner = SessionRunner(store, controller, agent, enabled=True)
                    self.assertFalse(runner.tick(), 'the conversation must remain available without another model call')
                    self.assertEqual(len(calls), 1)
                    clock_now = datetime.fromisoformat(store.get_state('conversation_until'))
                    class Clock(datetime):
                        @classmethod
                        def now(cls, tz=None):
                            return clock_now.astimezone(tz) if tz else clock_now.replace(tzinfo=None)
                    with patch('andrewbot.controller.datetime', Clock):
                        completed = runner.tick(clock_now)
                    self.assertTrue(calls, "the isolated provider must receive the real Letta request")
                    self.assertEqual(store.connection.execute("SELECT count(*) FROM usage").fetchone()[0], len(calls),
                                     "the deterministic provider must settle before checking callback behavior")
                    self.assertTrue(completed, "the real SDK callback chain must complete in one cycle")
                    self.assertIsNotNone(store.diary_entry(), "the diary callback must commit")
                    self.assertEqual(store.get_state("sleeping"), "1")
                    self.assertTrue(store.get_state("sleep_until"), "the sleep callback must save a deadline")
                    self.assertEqual(store.get_state("active_session_status"), "agent_sleep")
                    self.assertIn('expect to wake', store.next_reply()['text'])
                    self.assertEqual(len(calls), 5)
                    self.assertTrue(all(call['max_tokens'] == MAX_LETTA_OUTPUT_TOKENS for call in calls))
                    self.assertEqual(store.connection.execute("SELECT count(*) FROM budget_reservations").fetchone()[0], 0)
                    diary_id = store.diary_entry()["entry_id"]
                    due = store.get_state("sleep_until")
                    before_research_context = controller.research.wake_context()
                    before = store.connection.execute("SELECT * FROM usage ORDER BY id").fetchall()
                    server.terminate()
                    server.wait(timeout=5)
                    store.close()
                    store = Store(root / "controller.sqlite3")
                    gateway.store = store
                    server = launch()
                    client = connect(server)
                    try:
                        saved = client.request({"type": "agent_retrieve", "agent_id": agent_id}, "agent_retrieve_response")
                        self.assertEqual(saved["agent"]["id"], agent_id)
                    finally:
                        client.close()
                    agent = LettaAgent(store, "isolated-test-token", url=f"ws://127.0.0.1:{port}/ws")
                    self.assertEqual(agent.agent_id, agent_id)
                    self.assertEqual(agent.conversation_id, runtime["conversation_id"])
                    self.assertEqual(store.diary_entry()["entry_id"], diary_id)
                    self.assertEqual(store.get_state("sleep_until"), due)
                    self.assertEqual(store.connection.execute("SELECT * FROM usage ORDER BY id").fetchall(), before)
                    controller.store = store
                    controller.model = agent
                    agent.tool_handler = controller.execute_tool
                    runner = SessionRunner(store, controller, agent, enabled=True)
                    runner.recover()
                    self.assertEqual(len(calls), 5, "restart must not replay a model request")
                    self.assertTrue(controller.check_scheduled_wake(datetime.fromisoformat(due)))
                    self.assertFalse(controller.check_scheduled_wake(datetime.fromisoformat(due)))
                    clock_now = datetime.fromisoformat(due)
                    with patch('andrewbot.controller.datetime', Clock):
                        self.assertTrue(runner.tick(clock_now))
                    self.assertEqual(len(calls), 9)
                    research_entry = store.research_page()['entries'][0]
                    self.assertEqual(research_entry['revision'], 2)
                    self.assertIn('survived', research_entry['findings'])
                    self.assertTrue(any(row['kind'] == 'topic_resumed' for row in store.research_observations(limit=100)))
                    self.assertIn('Return to this fixture later.', before_research_context)
                    self.assertEqual(store.get_state("active_session_status"), "agent_sleep")
                    self.assertTrue(store.get_state("sleep_until"))
                    self.assertEqual(store.connection.execute("SELECT * FROM usage ORDER BY id").fetchall()[:len(before)], before)
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=5)
                    if gateway_thread:
                        gateway_thread.close()
                    if store:
                        store.close()
