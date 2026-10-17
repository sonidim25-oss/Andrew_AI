import importlib
import importlib.util
import unittest
import tempfile
import json
from pathlib import Path


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
        self.assertTrue(result['output_truncated'])

    def test_escaped_output_and_long_artifacts_fit_wire_without_losing_execution_status(self):
        from andrewbot.runner_service import RunnerService
        from andrewbot.runner_client import RunnerClient, MAX_RESPONSE_BYTES
        worker = worker_module(self)
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code):
                workspace = Path(self.workspace.name) / str(exit_code)
                workspace.mkdir()
                code = ("import os,sys\n"
                        "open('executed.once','a').write('x')\n"
                        "for i in range(32): open(('\\n'*240)+str(i),'w').close()\n"
                        "os.write(1,b'\\0'*16384)\nos.write(2,b'\\0'*16384)\n"
                        f"sys.exit({exit_code})")
                result = worker.execute_request({'type': 'python', 'code': code}, workspace=workspace)
                wire = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()
                self.assertLessEqual(len(wire), MAX_RESPONSE_BYTES)
                self.assertTrue(result['output_truncated'])
                self.assertEqual(result['status'], 'ok' if exit_code == 0 else 'error')
                self.assertEqual(result['safe_error'], None if exit_code == 0 else 'execution_failed')
                forwarded = RunnerClient._validate_result(RunnerService._worker_result(wire))
                self.assertEqual(forwarded['status'], result['status'])
                self.assertTrue(forwarded['output_truncated'])
                self.assertEqual((workspace / 'executed.once').read_text(), 'x')

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
