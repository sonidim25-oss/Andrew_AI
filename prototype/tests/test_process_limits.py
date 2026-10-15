import importlib
import importlib.util
import sys
import time
import unittest
import json
import os
from unittest.mock import patch


class BoundedProcessTests(unittest.TestCase):
    def test_activation_metadata_is_not_forwarded_to_worker_processes(self):
        activation={'LISTEN_PID':str(os.getpid()),'LISTEN_FDS':'1','LISTEN_FDNAMES':'runner'}
        with patch.dict(os.environ,{**activation,'ANDREW_PROBE_VALUE':'preserved'}):
            result=self.run_child("import json,os; print(json.dumps({k:os.environ.get(k) for k in ['LISTEN_PID','LISTEN_FDS','LISTEN_FDNAMES','ANDREW_PROBE_VALUE']}))")
            self.assertEqual(json.loads(result.stdout),{'LISTEN_PID':None,'LISTEN_FDS':None,
                'LISTEN_FDNAMES':None,'ANDREW_PROBE_VALUE':'preserved'})
            self.assertEqual({k:os.environ[k] for k in activation},activation)

    def run_child(self, code, **kwargs):
        self.assertIsNotNone(importlib.util.find_spec("andrewbot.process_limits"), "host capture must be bounded")
        module = importlib.import_module("andrewbot.process_limits")
        return module.run_bounded([sys.executable, "-c", code], **kwargs)

    def test_reads_and_writes_complete_bounded_frames(self):
        payload = b"request" * 8000
        result = self.run_child("import sys; data=sys.stdin.buffer.read();sys.stdout.buffer.write(data)",
                                payload=payload, timeout=2, output_limit=64 * 1024)
        self.assertEqual(result.stdout, payload)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out or result.output_exceeded)

    def test_output_flood_is_killed_without_unbounded_capture(self):
        result = self.run_child("import os\nwhile True: os.write(1,b'x'*65536)",
                                payload=b"", timeout=2, output_limit=4096)
        self.assertTrue(result.output_exceeded)
        self.assertLessEqual(len(result.stdout), 4096)

    def test_wall_timeout_includes_a_child_that_never_reads_stdin(self):
        started = time.monotonic()
        result = self.run_child("import time;time.sleep(30)", payload=b"x" * 1000000, timeout=0.1)
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 2)

    def test_timeout_kills_group_even_when_parent_exited(self):
        import os,signal
        from unittest.mock import patch
        code="import os,time\nprint(os.getpid(),flush=True)\npid=os.fork()\nif pid: os._exit(0)\ntime.sleep(30)"
        with patch('andrewbot.process_limits.os.killpg', wraps=os.killpg) as killed:
            result=self.run_child(code,timeout=.15)
        try:
            self.assertTrue(result.timed_out)
            self.assertTrue(killed.called,'orphaned descendants must be killed too')
        finally:
            try: os.killpg(int(result.stdout),signal.SIGKILL)
            except ProcessLookupError: pass
