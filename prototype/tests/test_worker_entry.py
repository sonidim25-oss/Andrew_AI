import importlib
import importlib.util
import unittest
import tempfile


def worker_module(case):
    case.assertIsNotNone(importlib.util.find_spec("runtime.worker_entry"), "worker entry is implemented")
    return importlib.import_module("runtime.worker_entry")


class WorkerEntryTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.workspace.cleanup()

    def test_python_request_runs_in_workspace_and_returns_structured_output(self):
        worker = worker_module(self)
        result = worker.execute_request({"type": "python", "code": "print(6 * 7)"}, proxy_url=None, workspace=self.workspace.name)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["stdout"].strip(), "42")
        self.assertEqual(result["artifacts"], [])

    def test_rejects_unknown_request_fields(self):
        worker = worker_module(self)
        result = worker.execute_request({"type": "python", "code": "pass", "env": {"SECRET": "x"}}, proxy_url=None)
        self.assertEqual(result["safe_error"], "invalid_request")
        self.assertNotIn("SECRET", str(result))

    def test_caps_stdout_and_stderr_independently(self):
        worker = worker_module(self)
        result = worker.execute_request({"type": "python", "code": "import sys;print('x'*40000);print('y'*40000,file=sys.stderr)"}, proxy_url=None, workspace=self.workspace.name)
        self.assertLessEqual(len(result["stdout"].encode()), 16 * 1024)
        self.assertLessEqual(len(result["stderr"].encode()), 16 * 1024)

    def test_proxy_bridge_binds_loopback_only(self):
        worker = worker_module(self)
        bridge = worker.UnixProxyBridge("/tmp/egress.sock")
        server = bridge.create_server()
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
