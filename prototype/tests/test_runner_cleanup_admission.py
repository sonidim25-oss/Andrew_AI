import asyncio
import os
import unittest
from unittest.mock import Mock, patch

from andrewbot.runner_protocol import CONTRACT, VERSION, RunnerProtocolError, encode_frame
from andrewbot.runner_service import RunnerService, RunnerSocketServer, WorkerConfig
from prototype.tests import test_runner_receipt_protocol as fixture


class RunnerCleanupAdmissionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.RunnerReceiptProtocolTests.asyncSetUp
    asyncTearDown = fixture.RunnerReceiptProtocolTests.asyncTearDown
    identity = fixture.RunnerReceiptProtocolTests.identity
    execute = fixture.RunnerReceiptProtocolTests.execute
    lookup = fixture.RunnerReceiptProtocolTests.lookup
    connection = fixture.RunnerReceiptProtocolTests.connection
    wait_idle = fixture.RunnerReceiptProtocolTests.wait_idle

    async def capabilities(self):
        return await asyncio.to_thread(self.client.receipt_capabilities, expected_runner_uid=os.getuid())

    async def test_failed_cleanup_disables_new_admissions_but_preserves_saved_evidence(self):
        self.runner.cleanup_verified = False
        saved = await self.execute()
        self.assertEqual(saved.state, 'uncertain')
        self.assertFalse((await self.capabilities())['execution_enabled'])
        self.assertEqual(await self.lookup(), saved)
        forbidden = Mock(side_effect=AssertionError('saved evidence must not acquire a new grant'))
        self.assertEqual(await self.execute(factory=forbidden), saved)
        with self.assertRaises(RunnerProtocolError):
            await self.execute(self.identity('b'), factory=forbidden)
        forbidden.assert_not_called()
        self.assertEqual((await self.lookup(self.identity('b'))).state, 'missing')
        self.assertEqual(self.receipts.capabilities()['records'], 1)
        self.assertEqual(len(self.runner.calls), 1)

    async def test_real_runner_failed_container_cleanup_closes_socket_admission(self):
        from andrewbot.process_limits import ProcessResult
        workspace = self.root / 'projects'
        workspace.mkdir()
        runner = RunnerService(WorkerConfig('localhost/fixture@sha256:' + 'a' * 64,
                                            str(workspace), str(self.root / 'egress.sock')))
        self.server.runner = runner
        result = ProcessResult(0, b'{"status":"ok","stdout":"42","stderr":"","artifacts":[],"safe_error":null}', b'')
        with patch('andrewbot.runner_service.run_bounded', return_value=result) as worker, \
                patch.object(runner, '_remove_container', return_value=False) as cleanup:
            saved = await self.execute()
            self.assertFalse(runner.healthy)
            self.assertFalse(saved.cleanup_verified)
            self.assertEqual(saved.state, 'uncertain')
            self.assertFalse((await self.capabilities())['execution_enabled'])
            with self.assertRaises(RunnerProtocolError):
                await self.execute(self.identity('b'))
            self.assertEqual((await self.lookup(self.identity('b'))).state, 'missing')
            self.assertEqual(await self.lookup(), saved)
            worker.assert_called_once()
            cleanup.assert_called_once()

    async def test_unhealthy_runner_refused_before_preparation_or_grant(self):
        self.runner.healthy = False
        factory = Mock(side_effect=fixture.grant_for)
        with self.assertRaises(RunnerProtocolError):
            await self.execute(factory=factory)
        factory.assert_not_called()
        self.assertEqual(self.receipts.capabilities()['records'], 0)
        self.assertEqual(self.runner.calls, [])

    async def test_health_change_after_ready_is_refused_before_receiver_admission(self):
        reader, writer, ready = await self.connection(self.identity())
        try:
            self.runner.healthy = False
            writer.write(encode_frame(dict(contract=CONTRACT, version=VERSION, action='dispatch',
                                           grant=fixture.grant_for(ready).to_dict())))
            await writer.drain()
            await asyncio.wait_for(reader.read(), 2)
        finally:
            writer.close()
            await writer.wait_closed()
        await self.wait_idle()
        self.assertEqual((await self.lookup()).state, 'prepared')
        self.assertEqual(self.runner.calls, [])

    async def test_health_change_during_preparation_never_requests_a_grant(self):
        original = self.receipts.prepare
        def prepare(*args, **kwargs):
            result = original(*args, **kwargs)
            self.runner.healthy = False
            return result
        self.receipts.prepare = prepare
        factory = Mock(side_effect=fixture.grant_for)
        with self.assertRaises(RunnerProtocolError):
            await self.execute(factory=factory)
        factory.assert_not_called()
        self.assertEqual((await self.lookup()).state, 'prepared')
        self.assertEqual(self.runner.calls, [])

    async def test_health_change_after_receiver_admission_stays_uncertain_without_execution(self):
        original = self.receipts.admit
        def admit(*args, **kwargs):
            result = original(*args, **kwargs)
            self.runner.healthy = False
            return result
        self.receipts.admit = admit
        with self.assertRaises(RunnerProtocolError):
            await self.execute()
        await self.wait_idle()
        self.assertEqual((await self.lookup()).state, 'uncertain')
        self.assertEqual(self.runner.calls, [])

    async def test_unhealthy_runner_keeps_success_lookup_and_duplicate_result_available(self):
        saved = await self.execute()
        self.runner.healthy = False
        self.assertFalse((await self.capabilities())['execution_enabled'])
        self.assertEqual(await self.lookup(), saved)
        forbidden = Mock(side_effect=AssertionError('must reuse historical result'))
        self.assertEqual(await self.execute(factory=forbidden), saved)
        forbidden.assert_not_called()
        self.assertEqual(len(self.runner.calls), 1)

    async def test_failed_cleanup_latch_also_blocks_legacy_worker_calls(self):
        self.runner.cleanup_verified = False
        await self.execute()
        self.runner.cleanup_verified = True
        result = await asyncio.to_thread(self.client.run, {'type': 'python', 'code': 'print(42)'})
        self.assertEqual(result['safe_error'], 'worker_unavailable')
        self.assertEqual(len(self.runner.calls), 1)

    async def test_unhealthy_legacy_request_does_not_enter_runner(self):
        self.runner.healthy = False
        result = await asyncio.to_thread(self.client.run, {'type': 'python', 'code': 'print(42)'})
        self.assertEqual(result['safe_error'], 'worker_unavailable')
        self.assertEqual(self.runner.calls, [])

    async def test_program_error_with_verified_cleanup_does_not_disable_new_work(self):
        def run(request, cancel_event=None):
            self.runner.calls.append(request)
            return dict(status='error', stdout='', stderr='ValueError: fixture',
                        artifacts=[], safe_error='execution_failed')
        self.runner.run = run
        first = await self.execute()
        self.assertTrue(first.importable)
        self.assertTrue((await self.capabilities())['execution_enabled'])
        second = await self.execute(self.identity('b'))
        self.assertTrue(second.importable)
        self.assertEqual(len(self.runner.calls), 2)

    async def test_cleanup_latch_cannot_be_cleared_by_flipping_worker_flags(self):
        self.runner.cleanup_verified = False
        old = await self.execute()
        await self.wait_idle()
        self.runner.cleanup_verified = True
        self.runner.healthy = True
        self.assertFalse((await self.capabilities())['execution_enabled'])
        # A new service owner with a recovered worker may admit new identities.
        self.server = RunnerSocketServer(self.runner, receipts=self.receipts,
                                         controller_uid=os.getuid(), receipts_enabled=True)
        self.assertTrue((await self.capabilities())['execution_enabled'])
        self.assertTrue((await self.execute(self.identity('b'))).importable)
        self.assertEqual(await self.lookup(), old)
