"""Real subprocess cleanup with injected signal/wait failures."""
import signal
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from andrewbot import process_limits


class ProcessCleanupFailureTests(unittest.TestCase):
    def setUp(self):
        self.processes = []
        self.popen = subprocess.Popen
        self.waits = []
        self.addCleanup(self.cleanup_children)

    def spawn(self, *args, **kwargs):
        process = self.popen(*args, **kwargs)
        self.processes.append(process)
        wait = process.wait
        self.waits.append(wait)
        process.wait = Mock(wraps=wait)
        return process

    def cleanup_children(self):
        # Test isolation also reaps children when assertions fail on old code.
        for process, wait in zip(self.processes, self.waits):
            if process.poll() is None:
                process.kill()
            wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def run_child(self):
        return process_limits.run_bounded(
            [sys.executable, '-c', 'import time; time.sleep(0.05)'], timeout=0)

    def assert_pipes_closed(self):
        process = self.processes[0]
        self.assertTrue(all(stream.closed for stream in
                            (process.stdin, process.stdout, process.stderr)))

    def test_signal_failure_still_waits_for_child_exit_and_closes_pipes(self):
        with patch.object(process_limits.subprocess, 'Popen', side_effect=self.spawn), \
             patch.object(process_limits.os, 'killpg', side_effect=PermissionError('fixture denied')):
            with self.assertRaises(PermissionError):
                self.run_child()
        self.processes[0].wait.assert_called_once_with(timeout=5)
        self.assertIsNotNone(self.processes[0].returncode)
        self.assert_pipes_closed()

    def test_wait_timeout_still_closes_pipes_and_propagates_uncertainty(self):
        def spawn(*args, **kwargs):
            process = self.spawn(*args, **kwargs)
            process.wait.side_effect = subprocess.TimeoutExpired(args[0], 5)
            return process
        with patch.object(process_limits.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.run_child()
        self.assert_pipes_closed()

    def test_wait_oserror_still_closes_pipes_and_preserves_error(self):
        def spawn(*args, **kwargs):
            process = self.spawn(*args, **kwargs)
            process.wait.side_effect = OSError('fixture wait failed')
            return process
        with patch.object(process_limits.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaisesRegex(OSError, 'fixture wait failed'):
                self.run_child()
        self.assert_pipes_closed()
