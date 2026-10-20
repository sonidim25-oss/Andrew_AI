import base64
import hashlib
import os
import secrets
import socket
import threading
import unittest
from pathlib import Path

from andrewbot.appserver import AppServerConnection
from andrewbot.appserver import AppServerClient
from andrewbot.store import Store


class AppServerWebSocketTests(unittest.TestCase):
    def test_client_handshakes_and_round_trips_a_json_frame(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        received = []

        def serve():
            peer, _ = listener.accept()
            stream = peer.makefile("rwb")
            request = b""
            while not request.endswith(b"\r\n\r\n"):
                request += stream.readline()
            headers = dict(line.decode().strip().split(": ", 1) for line in request.split(b"\r\n")[1:] if b": " in line)
            accept = base64.b64encode(hashlib.sha1((headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            stream.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            stream.flush()
            head = stream.read(2)
            length = head[1] & 0x7f
            if length == 126:
                length = int.from_bytes(stream.read(2), "big")
            mask = stream.read(4)
            payload = stream.read(length)
            received.append(bytes(value ^ mask[i % 4] for i, value in enumerate(payload)))
            response = b'{"type":"test_response","success":true}'
            stream.write(bytes((0x81, len(response))) + response)
            stream.flush()
            peer.close()
            listener.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        connection = AppServerConnection(f"ws://127.0.0.1:{port}/ws")
        try:
            connection.send_json({"type": "test", "request_id": "probe"})
            self.assertEqual(connection.recv_json(), {"type": "test_response", "success": True})
            thread.join(timeout=2)
            self.assertEqual(received, [b'{"type":"test","request_id":"probe"}'])
        finally:
            connection.close()

    def test_sleep_callback_aborts_the_agent_turn_without_another_model_call(self):
        class FakeConnection:
            def __init__(self):
                self.sent = []
                self.events = [{"type": "external_tool_call_request", "request_id": "tool-1", "runtime": {"agent_id": "a", "conversation_id": "c"}, "tool_name": "sleep", "input": {"minutes": 5}}]

            def send_json(self, value):
                self.sent.append(value)
                if value.get("type") == "abort_message":
                    self.events.append({"type": "abort_message_response", "request_id": value["request_id"], "success": True})

            def recv_json(self):
                return self.events.pop(0)

        client = AppServerClient.__new__(AppServerClient)
        client.connection = FakeConnection()
        result = client.send_user_turn({"agent_id": "a", "conversation_id": "c"}, "sleep", tool_handler=lambda name, args: f"slept {args['minutes']}")
        self.assertEqual(result, "slept 5")
        self.assertEqual(client.connection.sent[1]["type"], "external_tool_call_response")
        self.assertEqual(client.connection.sent[2]["type"], "abort_message")

    def test_runtime_disables_local_tools_and_registers_only_lifecycle_callbacks(self):
        class FakeConnection:
            def __init__(self):
                self.sent = []
                self.events = []

            def send_json(self, value):
                self.sent.append(value)
                if value["type"] == "runtime_start":
                    self.events.append({"type": "runtime_start_response", "request_id": value["request_id"], "success": True, "runtime": {"agent_id": "a", "conversation_id": "c"}})
                elif value["type"] == "update_toolset":
                    self.events.append({"type": "update_toolset_response", "request_id": value["request_id"], "success": True})

            def recv_json(self):
                return self.events.pop(0)

        client = AppServerClient.__new__(AppServerClient)
        client.connection = FakeConnection()
        runtime = client.start_runtime("a")
        start = client.connection.sent[0]
        names = [tool["name"] for group in start["external_tools"] for tool in group["tools"]]
        self.assertEqual(set(names), {"sleep", "wake_at", "continue_activity", "check_budget"})
        self.assertEqual(start["mode"], "strict")
        self.assertEqual(start["execution_settings"]["tools"], [])
        self.assertIn("Bash", start["execution_settings"]["disallowed_tools"])
        self.assertEqual(client.connection.sent[1]["toolset_preference"], "none")
        self.assertEqual(runtime["agent_id"], "a")

    @unittest.skipUnless(os.environ.get("ANDREW_LETTA_SERVER_TEST") == "1", "enable when the local Letta service is available")
    def test_running_server_advertises_persistent_agent_operations(self):
        client = AppServerClient(os.environ.get("ANDREW_LETTA_URL", "ws://127.0.0.1:8283/ws"))
        try:
            result = client.request({"type": "app_server_info"}, "app_server_info_response")
            self.assertEqual(result["backend"], "local")
            self.assertTrue(result["capabilities"]["runtime_start"])
            self.assertTrue(result["capabilities"]["agent_management"])
        finally:
            client.close()


@unittest.skipUnless(os.environ.get("ANDREW_LETTA_INTEGRATION") == "1", "requires the live Letta App Server and configured Qwen gateway")
class LiveLettaIntegrationTests(unittest.TestCase):
    def test_live_agent_turn_and_memory_survive_client_or_service_restart(self):
        data_dir = Path(os.environ.get("ANDREW_DATA_DIR", "/var/lib/andrew/control"))
        store = Store(data_dir / "controller.sqlite3")
        client = AppServerClient(os.environ.get("ANDREW_LETTA_URL", "ws://127.0.0.1:8283/ws"))
        stage = os.environ.get("ANDREW_LETTA_INTEGRATION_STAGE", "prepare")
        try:
            agent_id = store.get_state("letta_agent_id", "")
            self.assertTrue(agent_id, "controller must create Andrew's Letta agent first")
            if stage == "prepare":
                runtime = client.start_runtime(agent_id, controller_tools=False)
                marker = "ANDREW-MEMORY-" + secrets.token_hex(8)
                answer = client.send_user_turn(runtime, f"Please remember this exact private test phrase for our next message: {marker}")
                self.assertTrue(answer.strip())
                store.set_state("letta_restart_test_marker", marker)
                store.set_state("letta_restart_test_conversation", runtime["conversation_id"])
            elif stage == "verify":
                marker = store.get_state("letta_restart_test_marker", "")
                conversation_id = store.get_state("letta_restart_test_conversation", "")
                self.assertTrue(marker and conversation_id, "run the integration test in prepare stage before restart")
                runtime = client.start_runtime(agent_id, conversation_id, controller_tools=False)
                answer = client.send_user_turn(runtime, "What exact private test phrase did I ask you to remember earlier?")
                self.assertIn(marker.casefold(), answer.casefold())
            else:
                self.fail("ANDREW_LETTA_INTEGRATION_STAGE must be prepare or verify")
        finally:
            client.close()
            store.close()
