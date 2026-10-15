import importlib
import importlib.util
import json
import tempfile
import unittest
import asyncio
import threading
import struct
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
        self.assertIn("--timeout=120", args)
        self.assertIn("--log-driver=none", args)
        self.assertIn("--pull=never", args)
        self.assertIn("--group-add=keep-groups", args)
        self.assertIn("--runtime=/usr/bin/crun", args)
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

    def test_service_worker_stays_in_its_delegated_cgroup(self):
        module=runner_module(self)
        config=module.WorkerConfig('localhost/a@sha256:'+'b'*64,str(self.workspace),str(self.proxy),
            cgroup_parent='/system.slice/andrew-runner.service')
        with patch.object(Path,'read_text',return_value='0::/system.slice/andrew-runner.service/supervisor\n'):
            args=module.RunnerService(config)._podman_args('worker-test')
        self.assertIn('--cgroup-manager=cgroupfs',args)
        self.assertIn('--cgroup-parent=/system.slice/andrew-runner.service',args)
        for limit in ('--pids-limit=256','--memory=2g','--cpus=2'):
            self.assertIn(limit,args)
        with patch.object(Path,'read_text',return_value='0::/system.slice/unrelated.service/supervisor\n'):
            with self.assertRaises(ValueError): module.RunnerService(config)._podman_args('worker-test')

    def test_serialized_frame_limit_includes_json_escaping(self):
        module = runner_module(self)
        with self.assertRaises(ValueError):
            module.RunnerService._validate_request({"type": "python", "code": '"' * 40000})

    def test_bounds_output_and_returns_only_safe_worker_fields(self):
        module = runner_module(self)
        service = module.RunnerService(module.WorkerConfig("localhost/a@sha256:" + "b" * 64, str(self.workspace), str(self.proxy)))
        from andrewbot.process_limits import ProcessResult
        completed = ProcessResult(0, json.dumps({"status": "ok", "stdout": "x" * 20000, "stderr": "", "artifacts": [], "safe_error": None}).encode(), b"")
        with patch("andrewbot.runner_service.run_bounded", return_value=completed), patch("andrewbot.runner_service.subprocess.run"):
            result = service.run({"type": "python", "code": "print(1)"})
        self.assertLessEqual(len(result["stdout"].encode()), 16 * 1024)
        self.assertEqual(set(result), {"status", "stdout", "stderr", "artifacts", "safe_error", "visit"})

    def test_timeout_removes_named_container(self):
        module = runner_module(self)
        service = module.RunnerService(module.WorkerConfig("localhost/a@sha256:" + "b" * 64, str(self.workspace), str(self.proxy)))
        from andrewbot.process_limits import ProcessResult
        with patch("andrewbot.runner_service.run_bounded", return_value=ProcessResult(-9, b"", b"", timed_out=True)), patch("andrewbot.runner_service.subprocess.run") as mocked:
            result = service.run({"type": "python", "code": "pass"})
        self.assertEqual(result["safe_error"], "timeout")
        self.assertTrue(any("rm" in call.args[0] for call in mocked.call_args_list))


    def test_failed_cleanup_blocks_further_workers(self):
        module=runner_module(self)
        service=module.RunnerService(module.WorkerConfig("localhost/a@sha256:"+"b"*64,str(self.workspace),str(self.proxy)))
        from andrewbot.process_limits import ProcessResult
        result=ProcessResult(0,b'{"status":"ok","stdout":"","stderr":"","artifacts":[],"safe_error":null}',b'')
        with patch("andrewbot.runner_service.run_bounded",return_value=result) as run, patch.object(service,"_remove_container",return_value=False):
            service.run({"type":"python","code":"pass"})
            second=service.run({"type":"python","code":"pass"})
            self.assertEqual(run.call_count,1)
            self.assertEqual(second['safe_error'],'worker_unavailable')


class RunnerConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_one_worker_can_run_at_a_time(self):
        module = runner_module(self)
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        count = []
        class Runner:
            def run(self, request):
                count.append(request)
                loop.call_soon_threadsafe(started.set)
                release.wait(timeout=2)
                return {"status": "ok", "stdout": "", "stderr": "", "artifacts": [], "safe_error": None}
        class Writer:
            def __init__(self):self.data = bytearray()
            def write(self, data):self.data.extend(data)
            async def drain(self):pass
            def close(self):pass
            async def wait_closed(self):pass
        def reader():
            stream = asyncio.StreamReader()
            raw = json.dumps({"type": "python", "code": "print(1)"}).encode()
            stream.feed_data(struct.pack("!I", len(raw)) + raw)
            stream.feed_eof()
            return stream
        server = module.RunnerSocketServer(Runner())
        first_writer, second_writer = Writer(), Writer()
        first = asyncio.create_task(server.handle(reader(), first_writer))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.wait_for(server.handle(reader(), second_writer), timeout=0.5)
            response = json.loads(second_writer.data[4:])
            self.assertEqual(response["status"], "error")
            self.assertEqual(len(count), 1)
        finally:
            release.set()
            await first


if __name__ == "__main__":
    unittest.main()
