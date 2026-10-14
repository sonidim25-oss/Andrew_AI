import json
import importlib
import importlib.util
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path


def load_client(test_case):
    test_case.assertIsNotNone(importlib.util.find_spec("andrewbot.runner_client"), "runner client is implemented")
    return importlib.import_module("andrewbot.runner_client")


def send_frame(connection, value):
    payload = json.dumps(value).encode("utf-8")
    connection.sendall(struct.pack("!I", len(payload)) + payload)


class RunnerClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.socket_path = str(Path(self.temp.name) / "runner.sock")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.socket_path)
        self.server.listen(1)

    def tearDown(self):
        self.server.close()
        self.temp.cleanup()

    def run_server(self, handler):
        thread = threading.Thread(target=handler, daemon=True)
        thread.start()
        return thread

    def test_run_sends_json_frame_and_receives_result(self):
        received = []

        def serve():
            connection, _ = self.server.accept()
            with connection:
                size = struct.unpack("!I", connection.recv(4))[0]
                received.append(json.loads(connection.recv(size)))
                send_frame(connection, {"status": "ok", "stdout": "42", "stderr": "", "artifacts": [], "safe_error": None})

        self.run_server(serve)
        module = load_client(self)
        result = module.RunnerClient(self.socket_path).run({"type": "python", "code": "print(42)"})
        self.assertEqual(received, [{"type": "python", "code": "print(42)"}])
        self.assertEqual(result["stdout"], "42")

    def test_rejects_oversized_request_before_connect(self):
        module = load_client(self)
        with self.assertRaises(module.RunnerProtocolError):
            module.RunnerClient(self.socket_path).run({"type": "python", "code": "x" * 65536})

    def test_rejects_unknown_request_type(self):
        module = load_client(self)
        with self.assertRaises(module.RunnerProtocolError):
            module.RunnerClient(self.socket_path).run({"type": "host_shell", "command": "id"})

    def test_rejects_malformed_json_response(self):
        def serve():
            connection, _ = self.server.accept()
            with connection:
                connection.recv(4)
                connection.recv(1024)
                connection.sendall(struct.pack("!I", 1) + b"{")

        self.run_server(serve)
        module = load_client(self)
        with self.assertRaises(module.RunnerProtocolError):
            module.RunnerClient(self.socket_path).run({"type": "python", "code": "pass"})

    def test_timeout_is_bounded(self):
        def serve():
            connection, _ = self.server.accept()
            with connection:
                connection.recv(1024)
                connection.recv(1024)
                threading.Event().wait(0.2)

        self.run_server(serve)
        module = load_client(self)
        with self.assertRaises(module.RunnerProtocolError):
            module.RunnerClient(self.socket_path, timeout_seconds=0.05).run({"type": "python", "code": "pass"})

    def test_socket_error_has_safe_message(self):
        self.server.close()
        module = load_client(self)
        with self.assertRaisesRegex(module.RunnerProtocolError, "runner unavailable"):
            module.RunnerClient(self.socket_path).run({"type": "python", "code": "pass"})


if __name__ == "__main__":
    unittest.main()
