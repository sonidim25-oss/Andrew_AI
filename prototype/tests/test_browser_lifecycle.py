import asyncio
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from andrewbot.browser_service import BrowserProcess, BrowserServer
from andrewbot.runner_service import RunnerService, WorkerConfig


class BrowserLifecycleTests(unittest.TestCase):
    def test_new_session_cannot_start_while_old_container_cleanup_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proxy = root / 'proxy.sock'
            proxy.touch()
            projects = root / 'projects'
            projects.mkdir()
            browser = BrowserProcess(WorkerConfig('localhost/fixture@sha256:' + 'a' * 64, str(projects), str(proxy)))
            original_popen = subprocess.Popen
            fixture = ("import sys,json\nfor line in sys.stdin:\n"
                       " json.loads(line)\n print(json.dumps({'status':'ok','result':{'tabs':[]}}),flush=True)\n")
            def spawn(*_args, **_kwargs):
                return original_popen([sys.executable, '-u', '-c', fixture], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      close_fds=True, env={'PATH': os.defpath})
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            errors, removed = [], []
            old_name = None
            def remove(name):
                removed.append(name)
                if name == old_name:
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('fixture cleanup was not released')
                return True
            def close():
                try:
                    browser.close()
                except BaseException as error:
                    errors.append(error)
            def run():
                try:
                    self.assertEqual(browser.run({'action': 'tabs'})['status'], 'ok')
                except BaseException as error:
                    errors.append(error)
                finally:
                    finished.set()
            with patch('andrewbot.browser_service.subprocess.Popen', side_effect=spawn), \
                 patch.object(RunnerService, '_remove_container', side_effect=remove):
                closing = running = None
                try:
                    browser.run({'action': 'tabs'})
                    old_name = browser.name
                    closing = threading.Thread(target=close)
                    closing.start()
                    self.assertTrue(entered.wait(1))
                    running = threading.Thread(target=run)
                    running.start()
                    self.assertFalse(finished.wait(.2), 'new request entered before old cleanup finished')
                    release.set()
                    closing.join(2)
                    running.join(2)
                    self.assertFalse(closing.is_alive())
                    self.assertFalse(running.is_alive())
                    self.assertEqual(errors, [])
                    self.assertIsNotNone(browser.process)
                    self.assertIsNotNone(browser.name)
                    self.assertNotEqual(browser.name, old_name)
                    self.assertEqual(removed, [old_name])
                    current = browser.process
                    browser.reap_idle()
                    self.assertIs(browser.process, current)
                    self.assertEqual(removed, [old_name])
                finally:
                    release.set()
                    for thread in (closing, running):
                        if thread is not None:
                            thread.join(2)
                    browser.close()


class BrowserReaperAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_reaping_reserves_admission_until_cleanup_finishes(self):
        process = Mock()
        process.process = object()
        process.deadline = 0
        process.run.return_value = {'status': 'ok', 'result': {'tabs': []}}
        entered, release = threading.Event(), threading.Event()
        def cleanup():
            entered.set()
            if not release.wait(3):
                raise AssertionError('fixture reaper was not released')
        process.reap_idle.side_effect = cleanup
        server = BrowserServer(process)
        self.assertTrue(callable(getattr(server, 'reap_idle', None)), 'reaper must reserve browser admission')
        reap = asyncio.create_task(server.reap_idle())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertTrue(server.busy)
            payload = json.dumps({'action': 'tabs'}).encode()
            reader = Mock(readexactly=AsyncMock(side_effect=[struct.pack('!I', len(payload)), payload]))
            writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
            await server.handle(reader, writer)
            process.run.assert_not_called()
            writer.close.assert_called_once()
        finally:
            release.set()
            await reap
        self.assertFalse(server.busy)
        payload = json.dumps({'action': 'tabs'}).encode()
        reader = Mock(readexactly=AsyncMock(side_effect=[struct.pack('!I', len(payload)), payload]))
        writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
        await server.handle(reader, writer)
        process.run.assert_called_once_with({'action': 'tabs'})
        writer.write.assert_called_once()
