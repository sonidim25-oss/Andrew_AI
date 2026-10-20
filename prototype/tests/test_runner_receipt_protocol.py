import array
import asyncio
import json
import os
import subprocess
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from andrewbot.recovery_identity import digest
from andrewbot.runner_client import RunnerClient
from andrewbot.runner_protocol import (
    CONTRACT, VERSION, DispatchGrant, RunnerProtocolError, encode_frame,
    read_frame_async, validate_identity,
)
from andrewbot.runner_receipts import RunnerReceiptStore
from andrewbot.runner_service import RunnerSocketServer
from andrewbot.runner_socket import read_authenticated_frame
from test_runner_protocol import identity_document


def grant_for(ready, **changes):
    value = dict(operation_sha=ready['operation_sha'], challenge=ready['challenge'],
                 scope_id='scope', fence=1,
                 boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                 expires_monotonic_ms=time.monotonic() * 1000 + 12000)
    value.update(changes)
    return DispatchGrant(**value)


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.cleanup_verified = True
        self.entered = threading.Event()
        self.cancel_seen = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def run(self, request, cancel_event=None):
        self.calls.append(request)
        self.entered.set()
        deadline = time.monotonic() + 3
        while not self.release.wait(.005) and time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                self.cancel_seen.set()
        if cancel_event is not None and cancel_event.is_set():
            return dict(status='error', stdout='', stderr='', artifacts=[], safe_error='cancelled')
        return dict(status='ok', stdout='42', stderr='', artifacts=[], safe_error=None)


class RunnerReceiptProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.receipts = RunnerReceiptStore(self.root / 'receipts', b'K' * 32, expected_uid=os.getuid())
        self.store_id = self.receipts.provision(controller_store_id=identity_document()['controller_store_id'])
        self.runner = FakeRunner()
        self.server = RunnerSocketServer(self.runner, receipts=self.receipts,
                                         controller_uid=os.getuid(), receipts_enabled=True)
        self.handlers = set()
        async def handle(reader, writer):
            task = asyncio.current_task(); self.handlers.add(task)
            try:
                await self.server.handle(reader, writer)
            finally:
                self.handlers.discard(task)
        self.path = str(self.root / 'runner.sock')
        self.listener = await asyncio.start_unix_server(handle, path=self.path)
        self.client = RunnerClient(self.path, timeout_seconds=2)

    async def asyncTearDown(self):
        self.runner.release.set()
        self.listener.close(); await self.listener.wait_closed()
        if self.handlers:
            await asyncio.wait_for(asyncio.gather(*self.handlers, return_exceptions=True), 5)
        self.receipts.close(); self.temp.cleanup()

    def identity(self, operation='a', **changes):
        doc = identity_document()
        doc.update(operation_id=operation * 64, admission_id=operation * 64,
                   legacy_attempt_id='attempt-' + operation, runner_store_id=self.store_id,
                   worker_request_sha=digest({'type': 'python', 'code': 'print(42)'}),
                   request_sha=digest({'name': 'python.run', 'arguments': {'code': 'print(42)'}}))
        doc.update(changes)
        return validate_identity(doc)

    async def execute(self, identity=None, factory=grant_for):
        return await asyncio.to_thread(self.client.execute_receipted,
                                      {'type': 'python', 'code': 'print(42)'}, identity or self.identity(),
                                      expected_runner_uid=os.getuid(), grant_factory=factory)

    async def lookup(self, identity=None):
        return await asyncio.to_thread(self.client.lookup_receipt, identity or self.identity(),
                                      expected_runner_uid=os.getuid())

    async def connection(self, identity):
        reader, writer = await asyncio.open_unix_connection(self.path)
        writer.write(encode_frame(dict(contract=CONTRACT, version=VERSION, action='execute',
                                       identity=identity.to_dict(), request={'type': 'python', 'code': 'print(42)'})))
        await writer.drain()
        ready = await read_frame_async(reader)
        self.assertEqual(ready['action'], 'ready')
        return reader, writer, ready

    async def wait_idle(self):
        for _ in range(100):
            if not self.server._busy:
                return
            await asyncio.sleep(.01)
        self.fail('runner did not release ownership')

    async def test_authenticated_result_lookup_and_duplicate_execute_count_once(self):
        capabilities = await asyncio.to_thread(self.client.receipt_capabilities, expected_runner_uid=os.getuid())
        self.assertTrue(capabilities['execution_enabled'])
        saved = await self.execute()
        self.assertTrue(saved.importable)
        self.assertEqual(await self.lookup(), saved)
        def forbidden(_):
            self.fail('duplicate operation requested another grant')
        self.assertEqual(await self.execute(factory=forbidden), saved)
        self.assertEqual(self.runner.calls, [{'type': 'python', 'code': 'print(42)'}])

    async def test_wrong_controller_uid_or_sender_uid_never_executes(self):
        self.server.controller_uid = os.getuid() + 1
        with self.assertRaises(RunnerProtocolError):
            await self.execute()
        self.server.controller_uid = os.getuid()
        with self.assertRaises(RunnerProtocolError):
            await asyncio.to_thread(self.client.execute_receipted,
                                   {'type': 'python', 'code': 'print(42)'}, self.identity(),
                                   expected_runner_uid=os.getuid() + 1, grant_factory=grant_for)
        await self.wait_idle()
        self.assertEqual(self.runner.calls, [])

    async def test_owner_cancellation_before_grant_keeps_prepared_and_zero_calls(self):
        def denied(_):
            raise RunnerProtocolError('owner blocked')
        with self.assertRaises(RunnerProtocolError):
            await self.execute(factory=denied)
        await self.wait_idle()
        self.assertEqual((await self.lookup()).state, 'prepared')
        self.assertEqual((await self.execute()).state, 'prepared')
        self.assertEqual(self.runner.calls, [])

    async def test_foreign_boot_expiry_challenge_and_digest_grants_refused(self):
        for operation, changes in [
            ('a', {'boot_id': '01234567-1234-4321-8123-123456789abc'}),
            ('b', {'expires_monotonic_ms': 1}), ('c', {'challenge': '0' * 64}),
            ('d', {'operation_sha': '0' * 64}),
            ('e', {'expires_monotonic_ms': time.monotonic() * 1000 + 60000}),
        ]:
            with self.subTest(changes=changes), self.assertRaises(RunnerProtocolError):
                await self.execute(self.identity(operation), lambda ready: grant_for(ready, **changes))
            await self.wait_idle()
            self.assertEqual((await self.lookup(self.identity(operation))).state, 'prepared')
        self.assertEqual(self.runner.calls, [])

    async def test_mismatched_worker_request_rejected_before_preparation(self):
        with self.assertRaises(RunnerProtocolError):
            await self.execute(self.identity(worker_request_sha='0' * 64))
        self.assertEqual(self.receipts.capabilities()['records'], 0)
        self.assertEqual(self.runner.calls, [])

    async def test_mismatched_tool_request_digest_rejected_before_preparation(self):
        with self.assertRaises(RunnerProtocolError):
            await self.execute(self.identity(request_sha='0' * 64))
        self.assertEqual(self.receipts.capabilities()['records'], 0)
        self.assertEqual(self.runner.calls, [])

    async def test_disconnect_before_grant_never_executes(self):
        _, writer, _ = await self.connection(self.identity())
        writer.close(); await writer.wait_closed()
        await self.wait_idle()
        self.assertEqual((await self.lookup()).state, 'prepared')
        self.assertEqual(self.runner.calls, [])

    async def test_trailing_or_repeated_dispatch_frame_never_starts_worker(self):
        reader, writer, ready = await self.connection(self.identity())
        frame = encode_frame(dict(contract=CONTRACT, version=VERSION, action='dispatch',
                                  grant=grant_for(ready).to_dict()))
        writer.write(frame + frame); await writer.drain()
        await reader.read(); writer.close(); await writer.wait_closed()
        await self.wait_idle()
        self.assertEqual(self.runner.calls, [])

    async def test_busy_through_cleanup_and_receipt_commit(self):
        entered, release = threading.Event(), threading.Event()
        original = self.receipts.finish
        def finish(*args, **kwargs):
            entered.set(); release.wait(3)
            return original(*args, **kwargs)
        self.receipts.finish = finish
        first = asyncio.create_task(self.execute())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertTrue(self.server._busy)
            self.assertEqual((await asyncio.wait_for(self.lookup(), .5)).state, 'admitted')
            caps = await asyncio.wait_for(asyncio.to_thread(
                self.client.receipt_capabilities, expected_runner_uid=os.getuid()), .5)
            self.assertTrue(caps['execution_enabled'])
            with self.assertRaises(RunnerProtocolError):
                await self.execute(self.identity('b'))
            self.assertEqual((await self.execute()).state, 'admitted')
            self.assertEqual(len(self.runner.calls), 1)
        finally:
            release.set()
            saved = await first
        self.assertTrue(saved.importable)
        await self.wait_idle()

    async def test_disconnect_after_completion_still_saves_exact_result(self):
        entered, release = threading.Event(), threading.Event()
        original = self.receipts.finish
        def finish(*args, **kwargs):
            entered.set(); release.wait(3)
            return original(*args, **kwargs)
        self.receipts.finish = finish
        reader, writer, ready = await self.connection(self.identity())
        writer.write(encode_frame(dict(contract=CONTRACT, version=VERSION, action='dispatch',
                                       grant=grant_for(ready).to_dict())))
        await writer.drain()
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        writer.close(); await writer.wait_closed()
        release.set(); await self.wait_idle()
        saved = await self.lookup()
        self.assertTrue(saved.importable)
        self.assertEqual(saved.response['stdout'], '42')
        self.assertEqual(len(self.runner.calls), 1)

    async def test_cancel_during_execution_does_not_repeat_or_import_success(self):
        self.runner.release.clear()
        reader, writer, ready = await self.connection(self.identity())
        writer.write(encode_frame(dict(contract=CONTRACT, version=VERSION, action='dispatch',
                                       grant=grant_for(ready).to_dict())))
        await writer.drain()
        self.assertTrue(await asyncio.to_thread(self.runner.entered.wait, 1))
        writer.close(); await writer.wait_closed()
        await asyncio.sleep(.02)
        self.assertTrue(self.server._busy)
        self.runner.release.set(); await self.wait_idle()
        saved = await self.lookup()
        self.assertFalse(saved.importable)
        self.assertEqual(await self.execute(), saved)
        self.assertEqual(len(self.runner.calls), 1)

    async def test_failed_receipt_save_disables_future_execution_but_keeps_lookup(self):
        original=self.receipts.finish
        self.receipts.finish=lambda *a,**k:(_ for _ in ()).throw(OSError('fixture storage failure'))
        with self.assertRaises(RunnerProtocolError):await self.execute()
        await self.wait_idle();self.receipts.finish=original
        self.assertEqual((await self.lookup()).state,'uncertain')
        with self.assertRaises(RunnerProtocolError):await self.execute(self.identity('b'))
        capabilities=await asyncio.to_thread(self.client.receipt_capabilities,expected_runner_uid=os.getuid())
        self.assertFalse(capabilities['execution_enabled'])
        self.assertEqual(len(self.runner.calls),1)

    async def test_success_response_with_failed_cleanup_stays_uncertain(self):
        self.runner.cleanup_verified = False
        saved = await self.execute()
        self.assertEqual(saved.state, 'uncertain')
        self.assertFalse(saved.importable)
        self.assertEqual(await self.execute(), saved)
        self.assertEqual(len(self.runner.calls), 1)

    async def test_handler_cancellation_keeps_busy_until_cleanup_and_result_saved(self):
        self.runner.release.clear()
        first = asyncio.create_task(self.execute())
        self.assertTrue(await asyncio.to_thread(self.runner.entered.wait, 1))
        task = next(task for task in self.handlers if not task.done())
        task.cancel()
        await asyncio.sleep(.02)
        self.assertTrue(self.server._busy)
        self.runner.release.set()
        await asyncio.gather(first, return_exceptions=True)
        await self.wait_idle()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertFalse((await self.lookup()).importable)

    async def test_disabled_execution_preserves_lookup_and_legacy_worker_frames(self):
        self.server.receipts_enabled = False
        with self.assertRaises(RunnerProtocolError):
            await self.execute()
        self.assertEqual((await self.lookup()).state, 'missing')
        result = await asyncio.to_thread(self.client.run, {'type': 'python', 'code': 'print(42)'})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.receipts.capabilities()['records'], 0)
        browser = await asyncio.to_thread(self.client.run, {'type': 'browser_open', 'url': 'https://example.com'})
        self.assertEqual(browser['status'], 'ok')

    async def test_legacy_frame_limit_includes_original_json_whitespace(self):
        reader, writer = await asyncio.open_unix_connection(self.path)
        payload = json.dumps({'type': 'python', 'code': 'print(42)'}).encode() + b' ' * 65536
        writer.write(struct.pack('!I', len(payload)) + payload); await writer.drain()
        response = await reader.read()
        writer.close(); await writer.wait_closed()
        self.assertEqual(response, b'')
        self.assertEqual(self.runner.calls, [])

    async def test_node_and_shell_use_the_same_receipt_contract(self):
        for operation, name, arguments, request in [
            ('a', 'node.run', {'code': 'console.log(42)'}, {'type': 'node', 'code': 'console.log(42)'}),
            ('b', 'shell.run', {'command': 'echo 42'}, {'type': 'shell', 'command': 'echo 42'}),
        ]:
            identity = self.identity(operation, tool_name=name, request_sha=digest({'name': name, 'arguments': arguments}),
                                     worker_request_sha=digest(request))
            saved = await asyncio.to_thread(self.client.execute_receipted, request, identity,
                                            expected_runner_uid=os.getuid(), grant_factory=grant_for)
            self.assertTrue(saved.importable)
            self.assertEqual(await self.lookup(identity), saved)
        self.assertEqual(len(self.runner.calls), 2)

    async def test_capacity_exhaustion_cannot_dispatch_or_fall_back_to_legacy(self):
        self.receipts.capacity_bytes = self.receipts.allocated_bytes
        with self.assertRaises(RunnerProtocolError):
            await self.execute()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual((await self.lookup()).state, 'missing')

    async def test_lookup_unknown_fields_and_executable_payload_refused(self):
        reader, writer = await asyncio.open_unix_connection(self.path)
        writer.write(encode_frame(dict(contract=CONTRACT, version=VERSION, action='lookup',
                                       identity=self.identity().to_dict(), request={'type': 'python', 'code': 'print(42)'})))
        await writer.drain()
        await reader.read(); writer.close(); await writer.wait_closed()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.receipts.capabilities()['records'], 0)

    async def test_lease_expired_during_admission_commit_cannot_start_worker(self):
        original = self.receipts.admit
        def admit(identity):
            time.sleep(.05)
            return original(identity)
        self.receipts.admit = admit
        with self.assertRaises(RunnerProtocolError):
            await self.execute(factory=lambda ready: grant_for(
                ready, expires_monotonic_ms=time.monotonic() * 1000 + 20))
        await self.wait_idle()
        self.assertEqual((await self.lookup()).state, 'uncertain')
        self.assertEqual(self.runner.calls, [])

    async def test_cancel_event_interrupts_unlimited_receipt_wait(self):
        self.client.timeout_seconds = None
        self.runner.release.clear()
        cancel_event = threading.Event()
        first = asyncio.create_task(asyncio.to_thread(self.client.execute_receipted,
            {'type': 'python', 'code': 'print(42)'}, self.identity(), expected_runner_uid=os.getuid(),
            grant_factory=grant_for, cancel_event=cancel_event))
        try:
            self.assertTrue(await asyncio.to_thread(self.runner.entered.wait, 1))
            cancel_event.set()
            with self.assertRaises(RunnerProtocolError):
                await asyncio.wait_for(first, .5)
            self.assertTrue(await asyncio.to_thread(self.runner.cancel_seen.wait, .5))
            self.assertTrue(self.server._busy)
        finally:
            self.runner.release.set()
            await asyncio.gather(first, return_exceptions=True)
        await self.wait_idle()
        self.assertFalse((await self.lookup()).importable)

    async def test_client_rejects_extra_response_frames(self):
        await self.execute()
        async def malicious(reader, writer):
            await read_frame_async(reader)
            saved = self.receipts.lookup(self.identity()).to_dict()
            writer.write(encode_frame(saved) + encode_frame(saved))
            await writer.drain(); writer.close(); await writer.wait_closed()
        other_path = str(self.root / 'other.sock')
        other = await asyncio.start_unix_server(malicious, path=other_path)
        try:
            client = RunnerClient(other_path)
            with self.assertRaises(RunnerProtocolError):
                await asyncio.to_thread(client.lookup_receipt, self.identity(), expected_runner_uid=os.getuid())
        finally:
            other.close(); await other.wait_closed()

    async def test_lookup_deadline_is_total_not_reset_by_partial_frames(self):
        async def slow(reader, writer):
            try:
                await read_frame_async(reader)
                for byte in encode_frame(self.receipts.lookup(self.identity()).to_dict()):
                    writer.write(bytes([byte])); await writer.drain(); await asyncio.sleep(.02)
            except (OSError, ConnectionError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
        path = str(self.root / 'slow.sock')
        listener = await asyncio.start_unix_server(slow, path=path)
        try:
            client = RunnerClient(path)
            started = time.monotonic()
            with self.assertRaises(RunnerProtocolError):
                await asyncio.to_thread(client.lookup_receipt, self.identity(),
                    expected_runner_uid=os.getuid(), timeout_seconds=.06)
            self.assertLess(time.monotonic() - started, .3)
        finally:
            listener.close(); await listener.wait_closed()

    async def test_unlimited_worker_wait_does_not_inherit_connection_timeout(self):
        self.client.timeout_seconds = None
        release = threading.Event()
        original = self.runner.run
        def run(request, cancel_event):
            release.wait(7)
            return original(request, cancel_event)
        self.runner.run = run
        first = asyncio.create_task(self.execute())
        try:
            await asyncio.sleep(5.1)
            release.set()
            self.assertTrue((await first).importable)
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)


class ActualSenderCredentialTests(unittest.TestCase):
    def test_inherited_listener_reports_actual_child_sender(self):
        from andrewbot import runner_socket
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'inherited.sock')
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path); listener.listen(1)
            script = '''import socket,sys
from andrewbot.runner_protocol import encode_frame,read_frame
listener=socket.socket(fileno=int(sys.argv[1]));conn,_=listener.accept()
read_frame(conn)
conn.sendall(encode_frame(dict(contract='runner_result_recovery_v1',version=1,
 runner_store_id='11234567-1234-4321-8123-123456789abc',
 controller_store_id='01234567-1234-4321-8123-123456789abc',
 formatter='runner_text_v1',records=0,capacity_available=True,execution_enabled=False)))
conn.close();listener.close()
'''
            child = subprocess.Popen([sys.executable, '-c', script, str(listener.fileno())],
                                     pass_fds=(listener.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            observed = []
            original = runner_socket._credentials
            def credentials(ancillary, flags, expected_uid):
                for level, kind, data in ancillary:
                    if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS:
                        observed.append(struct.unpack('3i', data)[0])
                return original(ancillary, flags, expected_uid)
            try:
                with patch.object(runner_socket, '_credentials', credentials):
                    RunnerClient(path).receipt_capabilities(expected_runner_uid=os.getuid())
                _, error = child.communicate(timeout=2)
                self.assertEqual(child.returncode, 0, error)
                self.assertIn(child.pid, observed)
                self.assertNotIn(os.getpid(), observed)
            finally:
                if child.poll() is None:
                    child.kill(); child.communicate()
                listener.close()

    def test_real_socket_sender_credentials_and_missing_credentials(self):
        for enabled in [True, False]:
            with self.subTest(enabled=enabled):
                receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    if enabled:
                        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
                    sender.sendall(encode_frame({'value': 42}))
                    if enabled:
                        self.assertEqual(read_authenticated_frame(receiver, expected_uid=os.getuid()), {'value': 42})
                    else:
                        with self.assertRaises(RunnerProtocolError):
                            read_authenticated_frame(receiver, expected_uid=os.getuid())
                finally:
                    receiver.close(); sender.close()

    def test_passed_file_descriptors_are_closed_and_rejected(self):
        receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        fd = os.open('/dev/null', os.O_RDONLY)
        try:
            receiver.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            before = len(os.listdir('/proc/self/fd'))
            sender.sendmsg([encode_frame({'value': 42})], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [fd]))])
            with self.assertRaises(RunnerProtocolError):
                read_authenticated_frame(receiver, expected_uid=os.getuid())
            self.assertEqual(len(os.listdir('/proc/self/fd')), before)
        finally:
            os.close(fd); receiver.close(); sender.close()

    def test_truncated_control_messages_are_refused(self):
        class Connection:
            def recvmsg(self, *args):
                return b'xxxx', [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS,
                                  struct.pack('3i', os.getpid(), os.getuid(), os.getgid()))], socket.MSG_CTRUNC, None
            def fileno(self):
                return -1
        with self.assertRaises(RunnerProtocolError):
            read_authenticated_frame(Connection(), expected_uid=os.getuid())
