import asyncio
import os
import unittest

from andrewbot.runner_client import RunnerClient
from andrewbot.runner_receipts import RunnerReceiptStore
from andrewbot.runner_recovery import RunnerRecoveryAdapter, reconcile_pending_runner_result
from andrewbot.runner_service import RunnerSocketServer
from runner_recovery_fixture import CONTROLLER_ID, RunnerRecoveryFixture


class RunnerRecoverySocketTests(RunnerRecoveryFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.receiver = RunnerReceiptStore(self.path.parent / 'receiver', b'K'*32, expected_uid=os.getuid())
        self.receiver_id = self.receiver.provision(controller_store_id=CONTROLLER_ID)
        self.store.set_state('runner_receipt_store_id', self.receiver_id)
        class Worker:
            calls = 0
            cleanup_verified = True
            def run(self, request, cancel_event):
                self.calls += 1
                return dict(status='ok',stdout='42',stderr='',artifacts=[],safe_error=None)
        self.worker = Worker()
        server = RunnerSocketServer(self.worker, receipts=self.receiver, controller_uid=os.getuid(), receipts_enabled=True)
        self.handlers = set()
        async def handle(reader, writer):
            task = asyncio.current_task(); self.handlers.add(task)
            try:
                await server.handle(reader, writer)
            finally:
                self.handlers.discard(task)
        self.socket_path = str(self.path.parent / 'runner.sock')
        self.listener = await asyncio.start_unix_server(handle, path=self.socket_path)
        self.client = RunnerClient(self.socket_path, timeout_seconds=2)
        self.adapter = RunnerRecoveryAdapter(self.store, self.client, expected_runner_uid=os.getuid(), runner_store_id=self.receiver_id)
        self.controller.runner_recovery = self.adapter

    async def asyncTearDown(self):
        self.listener.close(); await self.listener.wait_closed()
        if self.handlers:
            await asyncio.gather(*self.handlers, return_exceptions=True)
        self.receiver.close()

    async def test_actual_socket_dispatch_and_atomic_import_count_one_worker(self):
        first = await asyncio.to_thread(self.controller.execute_tool, self.name, self.arguments, recovery=self.metadata)
        second = await asyncio.to_thread(self.controller.execute_tool, self.name, self.arguments, recovery=self.metadata)
        self.assertEqual(first, second)
        self.assertIn('stdout:\n42', first)
        self.assertEqual(self.worker.calls, 1)
        self.assertEqual(self.operation()['state'], 'imported')
        self.assertEqual(self.records.effect_result(self.metadata['admission_id'])['runner_receipt']['runner_store_id'], self.receiver_id)

    async def test_lost_controller_reply_imports_receiver_result_without_second_execution(self):
        original = self.client.execute_receipted
        def lose(*args, **kwargs):
            original(*args, **kwargs)
            raise TimeoutError('lost response before controller import')
        self.client.execute_receipted = lose
        with self.assertRaises(Exception):
            await asyncio.to_thread(self.controller.execute_tool, self.name, self.arguments, recovery=self.metadata)
        self.assertEqual(self.operation()['state'], 'admitted')
        self.assertTrue(await asyncio.to_thread(reconcile_pending_runner_result,
            self.store, self.client, self.binding, expected_runner_uid=os.getuid()))
        self.assertEqual(self.operation()['state'], 'imported')
        self.assertEqual(self.worker.calls, 1)

    async def test_pause_between_real_ready_and_grant_keeps_receiver_prepared(self):
        original = self.client.execute_receipted
        def stop(*args, **kwargs):
            factory = kwargs['grant_factory']
            def paused(ready):
                self.store.set_state('paused', '1')
                return factory(ready)
            kwargs['grant_factory'] = paused
            return original(*args, **kwargs)
        self.client.execute_receipted = stop
        with self.assertRaises(Exception):
            await asyncio.to_thread(self.controller.execute_tool, self.name, self.arguments, recovery=self.metadata)
        self.assertEqual(self.worker.calls, 0)
        self.assertEqual(self.operation()['state'], 'cancelled')
        self.assertEqual(self.store.connection.execute('SELECT outcome FROM cycle_tool_attempts').fetchone()[0], 'not_executed')
        self.assertEqual(self.store.get_state('paused'), '1')
