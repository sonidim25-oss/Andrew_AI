import base64
import hashlib
import json
import os
import secrets
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from andrewbot.appserver import AppServerConnection
from andrewbot.appserver import AppServerClient, LettaAgent, LettaCommandError, StartupValidationError, _safe_error_category
from andrewbot.configuration import load_model_config
from andrewbot.store import Store


class AppServerWebSocketTests(unittest.TestCase):
    def test_startup_waits_for_a_temporarily_refused_letta_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "controller.sqlite3")
            self.addCleanup(store.close)
            agent = LettaAgent(store, "gateway-token")
            self.assertTrue(callable(getattr(agent, "_connect_for_startup", None)), "startup needs bounded connection readiness")
            ready_client = object()
            with patch("andrewbot.appserver.AppServerClient", side_effect=[ConnectionRefusedError(), ready_client]), patch("andrewbot.appserver.time.sleep"):
                self.assertIs(agent._connect_for_startup(timeout=1), ready_client)

    def test_startup_connection_wait_has_a_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "controller.sqlite3")
            self.addCleanup(store.close)
            agent = LettaAgent(store, "gateway-token")
            self.assertTrue(callable(getattr(agent, "_connect_for_startup", None)), "startup needs bounded connection readiness")
            with patch("andrewbot.appserver.AppServerClient", side_effect=ConnectionRefusedError()), patch("andrewbot.appserver.time.monotonic", side_effect=[0, 2]):
                with self.assertRaises(TimeoutError):
                    agent._connect_for_startup(timeout=1)

    def test_agent_model_update_uses_app_server_websocket_command(self):
        client = AppServerClient.__new__(AppServerClient)
        client.url = "ws://127.0.0.1:8283/ws"
        client.timeout = 5

        class Connection:
            def __init__(self):
                self.sent = []

            def send_json(self, value):
                self.sent.append(value)

            def recv_json(self):
                return {
                    "type": "agent_update_response", "success": True,
                    "request_id": self.sent[-1]["request_id"],
                    "agent": {"id": "agent-test", "model": "openai-compatible/google/gemini-2.5-flash"},
                }

        client.connection = Connection()

        with patch("urllib.request.urlopen", side_effect=AssertionError("Agent management must use WebSocket")):
            client.update_agent_model(
                "agent-test", load_model_config({"ANDREW_MODEL_ID": "google/gemini-2.5-flash", "ANDREW_MODEL_INPUT_USD_PER_MILLION": "0.15", "ANDREW_MODEL_OUTPUT_USD_PER_MILLION": "0.47"})
            )
        self.assertEqual(len(client.connection.sent), 1, "agent updates must use the existing WebSocket")
        command = client.connection.sent[0]
        self.assertEqual(command["type"], "agent_update")
        self.assertEqual(command["agent_id"], "agent-test")
        self.assertEqual(command["body"]["model"], "openai-compatible/google/gemini-2.5-flash")
        self.assertEqual(command["body"]["model_settings"]["provider_type"], "openai-compatible")
        self.assertEqual(command["body"]["model_settings"]["max_tokens"], 512)

    def test_owner_model_change_updates_existing_agent_without_changing_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "controller.sqlite3")
            revision_id = store.create_personality_revision("I enjoy astronomy.", "A lasting interest.", "andrew")
            store.set_state("letta_agent_id", "agent-existing")
            store.set_state("letta_conversation_id", "conversation-existing")
            store.set_state("letta_model_id", "qwen/qwen3.8-flash")
            config = load_model_config({"ANDREW_MODEL_ID": "google/gemini-2.5-flash", "ANDREW_MODEL_INPUT_USD_PER_MILLION": "0.15", "ANDREW_MODEL_OUTPUT_USD_PER_MILLION": "0.47"})

            class FakeClient:
                instances = []

                def __init__(self, *_args, **_kwargs):
                    self.updated_model = None
                    self.instances.append(self)

                def ensure_provider(self, _token):
                    pass

                def update_agent_model(self, agent_id, model_config):
                    self.updated_model = (agent_id, model_config.model_id)

                def start_runtime(self, agent_id, conversation_id=None):
                    return {"agent_id": agent_id, "conversation_id": conversation_id or "conversation-existing"}

                def close(self):
                    pass

            try:
                with patch("andrewbot.appserver.AppServerClient", FakeClient):
                    agent = LettaAgent(store, "gateway-token", model_config=config)
                    agent.initialize()
                self.assertEqual(FakeClient.instances[0].updated_model, ("agent-existing", "google/gemini-2.5-flash"))
                self.assertEqual(store.get_state("letta_model_id"), "google/gemini-2.5-flash")
                self.assertEqual(store.current_personality_revision()["revision_id"], revision_id)
            finally:
                store.close()

    def test_safe_error_categories_distinguish_local_provider_setup_from_auth_rejection(self):
        self.assertEqual(_safe_error_category({"error": "OpenAI-compatible API requires a base URL."}), "base_url_required")
        self.assertEqual(_safe_error_category({"error": 'Provider type "openai-compatible" is not supported in local mode yet.'}), "provider_type_unsupported")
        self.assertEqual(_safe_error_category({"error": "Invalid API key; authentication failed."}), "credentials_rejected")
        self.assertEqual(_safe_error_category({"error": "providerName requires OAuth credentials."}), "provider_name_requires_oauth")

    def test_existing_agent_migrates_provider_handle_even_when_model_id_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "controller.sqlite3")
            self.addCleanup(store.close)
            store.set_state("letta_agent_id", "agent-existing")
            store.set_state("letta_conversation_id", "conversation-existing")
            store.set_state("letta_model_id", "qwen/qwen3.8-flash")
            store.add_message(123, "user", "A memory that must survive migration")

            class FakeClient:
                updated = []

                def __init__(self, *_args, **_kwargs):
                    pass

                def ensure_provider(self, _token):
                    pass

                def update_agent_model(self, agent_id, config):
                    self.updated.append((agent_id, config.model_id))

                def start_runtime(self, agent_id, conversation_id=None):
                    return {"agent_id": agent_id, "conversation_id": conversation_id}

                def close(self):
                    pass

            with patch("andrewbot.appserver.AppServerClient", FakeClient):
                agent = LettaAgent(store, "gateway-token")
                agent.initialize()
                agent.initialize()
            self.assertEqual(FakeClient.updated, [("agent-existing", "qwen/qwen3.8-flash")])
            self.assertEqual(store.get_state("letta_model_handle"), "openai-compatible/qwen/qwen3.8-flash")
            self.assertEqual(store.get_state("letta_agent_id"), "agent-existing")
            self.assertEqual(store.get_state("letta_conversation_id"), "conversation-existing")
            self.assertEqual(store.connection.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    def test_provider_registration_uses_the_local_openai_compatible_gateway(self):
        class FakeClient(AppServerClient):
            def __init__(self):
                self.command = None

            def request(self, command, response_type, timeout=None):
                self.command = command
                self.response_type = response_type

        client = FakeClient()
        client.ensure_provider("gateway-test-token")
        self.assertEqual(client.command["type"], "connect_provider")
        self.assertEqual(client.command["target"], "local")
        self.assertEqual(client.command["provider_id"], "openai-compatible")
        self.assertNotIn("provider_name", client.command)
        self.assertEqual(client.command["fields"], {
            "apiKey": "gateway-test-token",
            "baseUrl": "http://127.0.0.1:8284/v1",
        })

    def test_failed_letta_command_reports_only_a_safe_error_category(self):
        class FakeConnection:
            def send_json(self, value):
                self.request_id = value["request_id"]

            def recv_json(self):
                return {
                    "type": "connect_provider_response",
                    "request_id": self.request_id,
                    "success": False,
                    "error": "Invalid API key token=do-not-log",
                }

        client = AppServerClient.__new__(AppServerClient)
        client.connection = FakeConnection()
        with self.assertRaises(LettaCommandError) as raised:
            client.request({"type": "connect_provider"}, "connect_provider_response")
        self.assertEqual(raised.exception.safe_category, "credentials_rejected")
        self.assertNotIn("do-not-log", str(raised.exception))

    def test_startup_error_names_check_without_retaining_sensitive_exception_text(self):
        class SensitiveError(Exception):
            pass

        def fail():
            raise SensitiveError("provider rejected token=do-not-log")

        with self.assertRaises(StartupValidationError) as raised:
            LettaAgent._startup_check("Letta provider registration", fail)
        self.assertEqual(raised.exception.check, "Letta provider registration")
        self.assertEqual(raised.exception.cause_type, "SensitiveError")
        self.assertNotIn("do-not-log", str(raised.exception))

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
        self.assertEqual(set(client.connection.sent[0]["payload"]["client_tool_allowlist"]), {
            "sleep", "wake_at", "continue_activity", "check_budget", "recent_activity", "save_diary_entry",
            "memory_search", "memory_add_experience", "memory_propose_personality_edit",
        })

    def test_model_step_stop_does_not_close_before_diary_and_sleep_callbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            from andrewbot.controller import Controller
            store = Store(Path(directory) / "controller.sqlite3")
            self.addCleanup(store.close)
            store.set_state("active_session_id", "protocol-check")
            controller = Controller(store, object(), 17, autonomous_enabled=True)
            runtime = {"agent_id": "a", "conversation_id": "c"}

            class Connection:
                def __init__(self):
                    self.sent = []
                    self.events = [
                        {"type": "stream_delta", "delta": {"message_type": "stop_reason", "stop_reason": "requires_approval"}},
                        {"type": "external_tool_call_request", "request_id": "diary", "runtime": runtime,
                         "tool_name": "save_diary_entry", "input": {"content": "I tried a small idea and will return after resting."}},
                        {"type": "stream_delta", "delta": {"message_type": "stop_reason", "stop_reason": "requires_approval"}},
                        {"type": "external_tool_call_request", "request_id": "sleep", "runtime": runtime,
                         "tool_name": "sleep", "input": {"minutes": 1}},
                    ]

                def send_json(self, value):
                    self.sent.append(value)
                    if value["type"] == "abort_message":
                        self.events.append({"type": "abort_message_response", "request_id": value["request_id"], "success": True})
                    if value["type"] == "conversation_messages_list":
                        self.events.append({"type": "conversation_messages_list_response", "request_id": value["request_id"],
                                            "success": True, "messages": [{"message_type": "assistant_message", "id": "old", "content": "An old reply"}]})

                def recv_json(self):
                    return self.events.pop(0)

            client = AppServerClient.__new__(AppServerClient)
            client.connection = Connection()
            result = client.send_user_turn(runtime, "save a diary and sleep", tool_handler=controller.execute_tool)
            self.assertIn("sleep", result)
            self.assertIsNotNone(store.diary_entry())
            self.assertEqual(store.get_state("sleeping"), "1")
            self.assertEqual(sum(item["type"] == "external_tool_call_response" for item in client.connection.sent), 2)

    def test_finished_turn_cannot_return_a_previous_assistant_reply(self):
        class Connection:
            def __init__(self):
                self.events = [{"type": "turn_finished", "stop_reason": "end_turn"}]
            def send_json(self, value):
                if value["type"] == "conversation_messages_list":
                    self.events.append({"type": "conversation_messages_list_response", "request_id": value["request_id"],
                                        "success": True, "messages": [{"message_type": "assistant_message", "id": "old", "content": "An old reply"}]})
            def recv_json(self):
                return self.events.pop(0)
        client = AppServerClient.__new__(AppServerClient)
        client.connection = Connection()
        with self.assertRaisesRegex(RuntimeError, "without an assistant text response"):
            client.send_user_turn({"agent_id": "a", "conversation_id": "c"}, "new question")

    def test_runtime_advertises_memory_tools_without_local_tools(self):
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
        self.assertEqual(set(names), {
            "sleep", "wake_at", "continue_activity", "check_budget", "recent_activity", "save_diary_entry",
            "memory_search", "memory_add_experience", "memory_propose_personality_edit",
        })
        schemas = {tool["name"]: tool["parameters"] for group in start["external_tools"] for tool in group["tools"]}
        self.assertEqual(schemas["memory_search"]["properties"]["limit"]["maximum"], 20)
        self.assertFalse(schemas["memory_search"]["additionalProperties"])
        self.assertNotIn("edit_founding_letter", names)
        self.assertNotIn("edit_safety_policy", names)
        self.assertEqual(start["mode"], "strict")
        self.assertEqual(set(start["execution_settings"]["tools"]), set(names))
        self.assertEqual(set(start["execution_settings"]["allowed_tools"]), set(names))
        self.assertIn("Bash", start["execution_settings"]["disallowed_tools"])
        self.assertEqual(client.connection.sent[1]["toolset_preference"], "none")
        self.assertEqual(runtime["agent_id"], "a")
        client.start_runtime("a", controller_tools=False)
        disabled_start = client.connection.sent[2]
        self.assertEqual(disabled_start["execution_settings"]["tools"], [])
        self.assertEqual(disabled_start["execution_settings"]["allowed_tools"], [])
        self.assertNotIn("external_tools", disabled_start)

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
