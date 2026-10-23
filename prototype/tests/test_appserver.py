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
from andrewbot.appserver import AppServerClient, LettaAgent, LettaCommandError, LettaTurnError, StartupValidationError, _safe_error_category
from andrewbot.configuration import load_model_config
from andrewbot.store import Store


class AppServerWebSocketTests(unittest.TestCase):
    def test_startup_conversation_lookup_never_attaches_or_substitutes_a_conversation(self):
        from unittest.mock import Mock
        client = AppServerClient.__new__(AppServerClient)
        client.request = Mock(return_value={'conversation': {'id': 'saved', 'agent_id': 'andrew'}})
        self.assertEqual(client.prepare_conversation('andrew', 'saved'),
                         {'agent_id': 'andrew', 'conversation_id': 'saved'})
        client.request.assert_called_once_with(
            {'type': 'conversation_retrieve', 'conversation_id': 'saved'}, 'conversation_retrieve_response')
        for conversation in (None, {}, {'id': 'saved', 'agent_id': 'other'},
                             {'id': 'different', 'agent_id': 'andrew'}, {'id': 123, 'agent_id': 'andrew'}):
            with self.subTest(conversation=conversation):
                client.request.reset_mock()
                client.request.return_value = {'conversation': conversation}
                with self.assertRaisesRegex(RuntimeError, 'invalid conversation scope'):
                    client.prepare_conversation('andrew', 'saved')
                self.assertEqual(client.request.call_count, 1, 'invalid history must not create a replacement')
        client.request.reset_mock()
        client.request.side_effect = ConnectionError('unavailable')
        with self.assertRaises(ConnectionError):
            client.prepare_conversation('andrew', 'saved')
        self.assertEqual(client.request.call_count, 1)

    def test_first_start_creates_a_conversation_without_starting_a_runtime(self):
        from unittest.mock import Mock
        client = AppServerClient.__new__(AppServerClient)
        client.request = Mock(return_value={'conversation': {'id': 'new', 'agent_id': 'andrew'}})
        self.assertEqual(client.prepare_conversation('andrew'),
                         {'agent_id': 'andrew', 'conversation_id': 'new'})
        client.request.assert_called_once_with(
            {'type': 'conversation_create', 'body': {'agent_id': 'andrew'}}, 'conversation_create_response')

    def test_terminal_error_keeps_safe_category_source_and_stop_reason(self):
        for error, category in (
                ('context window exceeded: private fixture text', 'context_compaction_failed'),
                ('owner_reply_tools_disallowed: private fixture text', 'owner_reply_tools_disallowed'),
                ('unrecognized private fixture text', 'turn_failed')):
            with self.subTest(category=category):
                client = AppServerClient.__new__(AppServerClient)
                class Connection:
                    def send_json(self, _value):
                        pass
                    def recv_json(self):
                        return {'type': 'turn_finished', 'stop_reason': 'error', 'error': error}
                client.connection = Connection()
                with self.assertRaises(LettaTurnError) as caught:
                    client.send_user_turn({'agent_id': 'a', 'conversation_id': 'c'}, 'hello')
                self.assertEqual(caught.exception.safe_category, category)
                self.assertEqual(caught.exception.safe_source, 'turn_finished')
                self.assertEqual(caught.exception.safe_stop_reason, 'error')
                self.assertNotIn('private fixture text', str(caught.exception))

    def test_context_failure_records_a_safe_specific_reason(self):
        for message, reason in (
            ('The system prompt and tool definitions need about 50000 tokens, context window too small', 'fixed_prompt_too_large'),
            ('request too large: controller context window limit exceeded', 'controller_byte_limit'),
            ('Context window exceeded', 'context_overflow'),
            ('Local compaction failed', 'summary_failed'),
        ):
            client = AppServerClient.__new__(AppServerClient)
            class Connection:
                def send_json(self, _value):
                    pass
                def recv_json(self):
                    return {'type': 'turn_finished', 'stop_reason': 'error', 'error': message}
            client.connection = Connection()
            with self.assertRaises(LettaTurnError) as caught:
                client.send_user_turn({'agent_id': 'a', 'conversation_id': 'c'}, 'hello')
            self.assertEqual(caught.exception.safe_context_reason, reason)

    def test_recovered_context_notice_does_not_fail_a_completed_reply(self):
        client = AppServerClient.__new__(AppServerClient)
        class Connection:
            def __init__(self):
                self.events = [
                    {'type': 'stream_delta', 'delta': {'message_type': 'loop_error',
                        'message': 'context window exceeded; recovering', 'is_terminal': False}},
                    {'type': 'stream_delta', 'delta': {'message_type': 'assistant_message', 'id': 'fresh'}},
                    {'type': 'turn_finished', 'stop_reason': 'end_turn'}]
            def send_json(self, _value):
                pass
            def recv_json(self):
                return self.events.pop(0)
        client.connection = Connection()
        client.request = lambda *_args: {'messages': [{'message_type': 'assistant_message', 'id': 'fresh', 'content': 'Recovered.'}]}
        self.assertEqual(client.send_user_turn({'agent_id': 'a', 'conversation_id': 'c'}, 'hello'), 'Recovered.')

    def test_owner_reply_has_no_total_deadline_and_explicit_deadline_is_preserved(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.db')
            self.addCleanup(store.close)
            for kwargs, expected_timeout in (({}, None), ({'timeout': 3}, 3)):
                with self.subTest(kwargs=kwargs):
                    client = Mock()
                    runtime = {'agent_id': 'a', 'conversation_id': 'c'}
                    client.start_runtime.return_value = runtime
                    client.send_user_turn.return_value = 'Completed after tool rounds.'
                    agent = LettaAgent(store, 'fixture-token')
                    agent.agent_id = 'a'
                    with patch('andrewbot.appserver.AppServerClient', return_value=client), \
                         patch('andrewbot.appserver.gmail_permissions', return_value={'enabled': False}):
                        reply, _ = agent.reply([{'role': 'user', 'content': 'fixture input'}], **kwargs)
                    self.assertEqual(reply, 'Completed after tool rounds.')
                    client.send_user_turn.assert_called_once_with(
                        runtime, 'fixture input', tool_handler=None, timeout=expected_timeout)
                    self.assertIsNone(agent._turn_cancel)

    def test_active_backend_run_has_a_safe_explicit_category(self):
        from unittest.mock import Mock
        client = AppServerClient.__new__(AppServerClient)
        client.connection = Mock()
        client.connection.recv_json.side_effect = [
            {'type': 'stream_delta', 'delta': {'message_type': 'loop_error',
                                             'message': 'Conversation private-id already has an active run (private-run)'}},
            {'type': 'turn_finished', 'stop_reason': 'error'},
        ]
        with self.assertRaises(LettaTurnError) as caught:
            client.send_user_turn({'agent_id': 'a', 'conversation_id': 'c'}, 'fixture')
        self.assertEqual(caught.exception.safe_category, 'active_run_conflict')
        self.assertNotIn('private', str(caught.exception))

    def test_failed_turn_is_cancelled_before_disconnect_without_masking_timeout(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.db')
            self.addCleanup(store.close)
            for abort_fails in (False, True):
                with self.subTest(abort_fails=abort_fails):
                    client = Mock()
                    runtime = {'agent_id': 'a', 'conversation_id': 'c'}
                    client.start_runtime.return_value = runtime
                    original = TimeoutError('fixture deadline')
                    client.send_user_turn.side_effect = original
                    if abort_fails:
                        client.request.side_effect = ConnectionError('fixture cancellation disconnected')
                    agent = LettaAgent(store, 'fixture-token')
                    agent.agent_id = 'a'
                    with patch('andrewbot.appserver.AppServerClient', return_value=client), \
                         patch('andrewbot.appserver.gmail_permissions', return_value={'enabled': False}):
                        with self.assertRaises(TimeoutError) as caught:
                            agent.reply([{'role': 'user', 'content': 'fixture input'}])
                    self.assertIs(caught.exception, original)
                    client.request.assert_called_once_with({'type': 'abort_message', 'runtime': runtime},
                                                           'abort_message_response', timeout=5)
                    calls = [call[0] for call in client.mock_calls]
                    self.assertLess(calls.index('request'), calls.index('close'))
                    self.assertIsNone(agent._turn_cancel)

    def test_token_limit_stop_has_explicit_safe_category(self):
        class Connection:
            def __init__(self):
                self.events = [
                    {'type': 'stream_delta', 'delta': {'message_type': 'stop_reason', 'stop_reason': 'max_tokens_exceeded'}},
                    {'type': 'turn_finished', 'stop_reason': 'max_tokens_exceeded', 'error': 'secret payload'},
                ]
            def send_json(self, value):
                pass
            def recv_json(self):
                return self.events.pop(0)
        client = AppServerClient.__new__(AppServerClient)
        client.connection = Connection()
        with self.assertRaises(LettaTurnError) as caught:
            client.send_user_turn({'agent_id': 'a', 'conversation_id': 'c'}, 'hello')
        self.assertEqual(caught.exception.safe_category, 'output_token_limit')
        self.assertNotIn('secret payload', str(caught.exception))

    def test_fragmented_websocket_message_has_an_aggregate_limit(self):
        connection = AppServerConnection.__new__(AppServerConnection)
        with patch.object(connection, '_read_frame', side_effect=[
                (False, 1, b'x' * 1_100_000), (True, 0, b'x' * 1_100_000)]):
            with self.assertRaisesRegex(RuntimeError, 'message exceeds'):
                connection.recv_json()

    def test_websocket_reads_obey_total_deadline_despite_partial_frames(self):
        from unittest.mock import Mock
        connection = AppServerConnection.__new__(AppServerConnection)
        connection.socket = Mock()
        connection.socket.recv.return_value = b'x'
        connection.read_timeout = 120
        connection.deadline = 10
        with patch('andrewbot.appserver.time.monotonic', side_effect=[9, 11]):
            with self.assertRaises(TimeoutError):
                connection._read_exactly(2)
        connection.socket.recv.assert_called_once()
        connection.socket.settimeout.assert_called_once_with(1)

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
        self.assertEqual(command["body"]["model_settings"]["max_tokens"], None)

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

                def request(self, command, response_type):
                    assert command['type'] == 'agent_update'
                    assert response_type == 'agent_update_response'
                    return {'success': True}

                def update_agent_model(self, agent_id, model_config):
                    self.updated_model = (agent_id, model_config.model_id)

                def prepare_conversation(self, agent_id, conversation_id=None):
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

                def request(self, command, response_type):
                    assert command['type'] == 'agent_update'
                    assert response_type == 'agent_update_response'
                    return {'success': True}

                def update_agent_model(self, agent_id, config):
                    self.updated.append((agent_id, config.model_id))

                def prepare_conversation(self, agent_id, conversation_id=None):
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
            "memory_search", "memory_add_experience", "memory_propose_personality_edit", "memory_revise",
            "session_search", "session_read",
            "research.list", "research.get", "research.save", "research.focus", "research.delete", "research.observations",
            "skills.list", "skills.load",
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
            self.assertIsNone(store.diary_entry())
            self.assertEqual(len(store.open_diary_drafts()), 1)
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
            "memory_search", "memory_add_experience", "memory_propose_personality_edit", "memory_revise",
            "session_search", "session_read",
            "research.list", "research.get", "research.save", "research.focus", "research.delete", "research.observations",
            "skills.list", "skills.load",
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
