"""Owner-requested removal of work deadlines, with explicit cancellation retained."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from andrewbot.appserver import AppServerClient, AppServerConnection
from andrewbot.controller import Controller
from andrewbot.model import OpenRouterModel
from andrewbot.process_limits import run_bounded
from andrewbot.runner_client import RunnerClient
from andrewbot.runner_service import RunnerService, WorkerConfig
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store


class UnlimitedWorkTests(unittest.TestCase):
    def test_provider_socket_wait_is_120_seconds(self):
        model = OpenRouterModel('fixture-key')
        self.assertEqual(model.timeout, 120)

    def test_autonomous_turn_can_wait_without_an_absolute_or_socket_deadline(self):
        connection = Mock()
        connection.recv_json.side_effect = [
            {'type': 'stream_delta', 'delta': {'message_type': 'assistant_message', 'id': 'reply'}},
            {'type': 'turn_finished', 'stop_reason': 'end_turn'},
        ]
        client = AppServerClient.__new__(AppServerClient)
        client.connection = connection
        client.request = Mock(return_value={'messages': [
            {'message_type': 'assistant_message', 'id': 'reply', 'content': 'finished'}]})
        reply = client.send_user_turn({'agent_id': 'a', 'conversation_id': 'c'}, 'fixture', timeout=None)
        self.assertEqual(reply, 'finished')
        self.assertIsNone(connection.deadline)

    def test_no_hidden_websocket_read_timeout_without_turn_deadline(self):
        connection = AppServerConnection.__new__(AppServerConnection)
        connection.deadline = None
        connection.read_timeout = 120
        connection.socket = Mock()
        connection.socket.recv.return_value = b'x'
        self.assertEqual(connection._read_exactly(1), b'x')
        connection.socket.settimeout.assert_called_once_with(None)

    def test_process_capture_supports_no_deadline_and_still_caps_output(self):
        result = run_bounded([sys.executable, '-c', 'print(42)'], timeout=None)
        self.assertEqual(result.stdout.strip(), b'42')
        self.assertFalse(result.timed_out)
        flood = run_bounded([sys.executable, '-c', "import os\nwhile True: os.write(1,b'x'*65536)"],
                            timeout=None, output_limit=1024)
        self.assertTrue(flood.output_exceeded)
        self.assertLessEqual(len(flood.stdout), 1024)

    def test_no_deadline_process_remains_explicitly_cancellable(self):
        cancelled = threading.Event()
        result = []
        entered = threading.Event()
        def execute():
            entered.set()
            result.append(run_bounded([sys.executable, '-c', 'import time;time.sleep(60)'],
                                      timeout=None, cancel_event=cancelled))
        thread = threading.Thread(target=execute)
        thread.start()
        self.assertTrue(entered.wait(1))
        cancelled.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].cancelled)
        self.assertFalse(result[0].timed_out)

    def test_code_worker_has_no_container_or_host_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'projects'
            workspace.mkdir()
            config = WorkerConfig('localhost/fixture@sha256:' + 'a' * 64, str(workspace), '/tmp/proxy.sock')
            service = RunnerService(config)
            self.assertIsNone(config.timeout_seconds)
            self.assertFalse(any(arg.startswith('--timeout=') for arg in service._podman_args('fixture')))
            from andrewbot.process_limits import ProcessResult
            response = {'status': 'ok', 'stdout': 'done', 'stderr': '', 'artifacts': [], 'safe_error': None}
            with patch('andrewbot.runner_service.run_bounded', return_value=ProcessResult(0, json.dumps(response).encode(), b'')) as execute, \
                 patch.object(service, '_remove_container', return_value=True):
                self.assertEqual(service.run({'type': 'shell', 'command': 'echo done'})['status'], 'ok')
            self.assertIsNone(execute.call_args.kwargs['timeout'])

    def test_runner_client_waits_without_execution_timeout(self):
        self.assertIsNone(RunnerClient('/tmp/fixture.sock').timeout_seconds)

    def test_worker_image_executes_code_without_inner_deadline(self):
        from runtime import worker_entry
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(worker_entry, '_run_child', wraps=worker_entry._run_child) as child:
                response = worker_entry.execute_request({'type': 'python', 'code': 'print(42)'},
                                                        workspace=directory)
            self.assertEqual(response['stdout'].strip(), '42')
            self.assertIsNone(child.call_args.kwargs['timeout'])
            response = worker_entry.execute_request({'type': 'shell', 'command': 'echo 42'},
                                                    workspace=directory, timeout=None)
            self.assertEqual(response['status'], 'ok')

    def test_letta_tool_callback_propagates_the_actual_turn_cancellation_event(self):
        from andrewbot.appserver import LettaAgent
        from andrewbot.runner_client import TOOL_CANCELLATION
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.db')
            client = Mock()
            runtime = {'agent_id': 'a', 'conversation_id': 'c'}
            client.start_runtime.return_value = runtime
            captured = []
            agent = LettaAgent(store, 'fixture-token')
            agent.agent_id = 'a'
            def tool(name, arguments):
                captured.append(TOOL_CANCELLATION.get())
                self.assertIs(TOOL_CANCELLATION.get(), agent._turn_cancel)
                return 'tool complete'
            agent.tool_handler = tool
            client.send_user_turn.side_effect = lambda runtime, text, tool_handler, timeout: tool_handler('python.run', {'code': 'pass'})
            try:
                with patch('andrewbot.appserver.AppServerClient', return_value=client), \
                     patch('andrewbot.appserver.gmail_permissions', return_value={'enabled': False}), \
                     patch.object(agent, '_verify_founding_letter'):
                    result, usage = agent.autonomous_turn('fixture')
                self.assertEqual(result, 'tool complete')
                self.assertEqual(len(captured), 1)
                self.assertIsNotNone(captured[0])
                self.assertIsNone(TOOL_CANCELLATION.get())
            finally:
                store.close()

    def test_cancelled_job_is_not_spawned(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch('andrewbot.process_limits.subprocess.Popen', side_effect=AssertionError('cancelled job was spawned')):
            result = run_bounded([sys.executable, '-c', 'print(42)'], timeout=None, cancel_event=cancelled)
        self.assertTrue(result.cancelled)
