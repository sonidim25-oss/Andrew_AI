import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def runner_module(case):
    case.assertIsNotNone(importlib.util.find_spec("andrewbot.runner_service"), "runner service is implemented")
    return importlib.import_module("andrewbot.runner_service")


class RunnerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "projects"
        self.workspace.mkdir()
        self.proxy = root / "egress.sock"
        self.proxy.touch()

    def tearDown(self):
        self.temp.cleanup()

    def test_fixed_command_uses_only_workspace_and_proxy_mounts(self):
        module = runner_module(self)
        config = module.WorkerConfig("localhost/andrew@sha256:" + "a" * 64, str(self.workspace), str(self.proxy))
        service = module.RunnerService(config)
        args = service._podman_args("worker-test")
        self.assertIn("--network=none", args)
        self.assertIn("--userns=keep-id:uid=1000,gid=1000", args)
        self.assertIn("--user=1000:1000", args)
        self.assertIn("--read-only", args)
        mounts = [part for part in args if part.startswith("type=bind,")]
        self.assertEqual(len(mounts), 2)
        self.assertTrue(any(str(self.workspace) in mount for mount in mounts))
        self.assertTrue(any(str(self.proxy) in mount for mount in mounts))
        self.assertFalse(any("vault" in mount or "/home/" in mount for mount in mounts))

    def test_rejects_unknown_request_types(self):
        module = runner_module(self)
        service = module.RunnerService(module.WorkerConfig("localhost/a@sha256:" + "b" * 64, str(self.workspace), str(self.proxy)))
        with self.assertRaises(ValueError):
            service.run({"type": "host_shell", "command": "id"})

    def test_bounds_output_and_returns_only_safe_worker_fields(self):
        module = runner_module(self)
        service = module.RunnerService(module.WorkerConfig("localhost/a@sha256:" + "b" * 64, str(self.workspace), str(self.proxy)))
        completed = type("Completed", (), {"returncode": 0, "stdout": json.dumps({"status": "ok", "stdout": "x" * 20000, "stderr": "", "artifacts": [], "safe_error": None}).encode(), "stderr": b""})()
        with patch("andrewbot.runner_service.subprocess.run", return_value=completed):
            result = service.run({"type": "python", "code": "print(1)"})
        self.assertLessEqual(len(result["stdout"].encode()), 16 * 1024)
        self.assertEqual(set(result), {"status", "stdout", "stderr", "artifacts", "safe_error", "visit"})

    def test_timeout_removes_named_container(self):
        module = runner_module(self)
        service = module.RunnerService(module.WorkerConfig("localhost/a@sha256:" + "b" * 64, str(self.workspace), str(self.proxy)))
        with patch("andrewbot.runner_service.subprocess.run", side_effect=[module.subprocess.TimeoutExpired("podman", 1), type("R", (), {"returncode": 0})()]) as mocked:
            result = service.run({"type": "python", "code": "pass"})
        self.assertEqual(result["safe_error"], "timeout")
        self.assertTrue(any("rm" in call.args[0] for call in mocked.call_args_list))


if __name__ == "__main__":
    unittest.main()
