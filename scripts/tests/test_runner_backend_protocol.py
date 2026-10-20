"""Receipt recovery through a real isolated SDK, with a counted fake worker."""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

import test_local_recovery_protocol as protocol_tests

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'prototype/tests'))
from runner_recovery_fixture import EvidenceRunner, CONTROLLER_ID, RUNNER_ID
from andrewbot.recovery import LocalRecoveryClient, RecoveryCoordinator
from andrewbot.controller import Controller
from andrewbot.recovery_dispatch import RecoveryDispatch
from andrewbot.recovery_records import RecoveryRecords, digest
from andrewbot.runner_protocol import validate_identity
from andrewbot.runner_recovery import RunnerRecoveryAdapter, reconcile_pending_runner_result
from andrewbot.appserver import LettaCommandError


@unittest.skipUnless(os.environ.get('ANDREW_LOCAL_RECOVERY_TEST_RELEASE'),'requires isolated recovery release')
class RunnerBackendProtocolTests(unittest.TestCase):
    setUp=protocol_tests.LocalRecoveryProtocolTests.setUp
    prepare=protocol_tests.LocalRecoveryProtocolTests.prepare
    command=protocol_tests.LocalRecoveryProtocolTests.command

    def wait(self,attempt):
        deadline=time.monotonic()+10
        while True:
            state=self.command('recovery_status',binding=self.binding,attempt_id=attempt)
            if state.get('phase') in ('held','terminal_committed'):return state
            self.assertLess(time.monotonic(),deadline,state);time.sleep(.03)

    def runner_turn(self,lose_reply=True):
        binding,input,unused_scope=self.prepare();self.binding=binding
        RecoveryRecords(self.fixture.store).revoke(unused_scope['admission_id'])
        capability=self.fixture.client.request(
            dict(type='recovery_capabilities',contract='local_durable_recovery_v1',version=1),'recovery_capabilities_response')
        self.assertEqual(capability['runner_result_recovery_v1'],1);self.assertEqual(capability['held_tool_result_v1'],1)
        input['tools']=[dict(type='function',function=dict(name='python.run',parameters=dict(type='object')))]
        binding['input_sha']=digest(input);store=self.fixture.store
        for key,value in dict(local_recovery_controller_store_id=CONTROLLER_ID,runner_receipt_store_id=RUNNER_ID,
                              runner_receipts_enabled='1',runner_receipt_backend_capability='1').items():store.set_state(key,value)
        self.worker=EvidenceRunner(store);self.worker.lose_reply=lose_reply
        self.adapter=RunnerRecoveryAdapter(store,self.worker,expected_runner_uid=os.getuid(),runner_store_id=RUNNER_ID)
        self.fixture.model_gateway.recovery_tool_handler=lambda name,args,*,recovery:RecoveryDispatch(store,runner_adapter=self.adapter).run_tool(
            recovery,name,args,lambda:self.fail('generic callback executed'))
        self.responses.insert(0,dict(id='runner-response',choices=[dict(index=0,message=dict(role='assistant',content=None,
            tool_calls=[dict(id='runner-call',type='function',function=dict(name='python.run',arguments='{"code":"print(42)"}'))]),finish_reason='tool_calls')],
            usage=dict(prompt_tokens=20,completion_tokens=5,cost=.000001)))
        scope=RecoveryRecords(store).admit_scope(binding)
        admitted=self.command('recovery_admit',binding=binding,input=input,**scope)
        claim=self.command('recovery_claim',binding=binding,expected_revision=admitted['revision'],**scope)
        self.command('recovery_resume',binding=binding,attempt_id='runner-attempt',expected_revision=claim['revision'],claim_id=claim['claim_id'],fence=claim['fence'],**scope)
        return scope,claim,self.wait('runner-attempt')

    def test_lost_runner_result_recovers_exact_tool_and_one_continuation_after_restart(self):
        scope,claim,state=self.runner_turn()
        self.assertEqual(state['phase'],'held',state);self.assertEqual(state['hold_reason'],'tool_uncertain')
        self.assertEqual(self.worker.calls,1);self.assertEqual(len(self.fixture.provider_calls),1)
        self.fixture.restart()
        self.assertTrue(reconcile_pending_runner_result(self.fixture.store,self.worker,self.binding,expected_runner_uid=os.getuid()))
        operation=self.fixture.store.connection.execute('SELECT * FROM runner_operations').fetchone()
        identity=validate_identity(json.loads(operation['identity_json']))
        original_run=state['run_id'];backend=LocalRecoveryClient(self.fixture.client)
        scope=RecoveryRecords(self.fixture.store).admit_scope(self.binding)
        state=backend.inspect(self.binding);claim=backend.claim(self.binding,state['revision'],scope)
        fields=dict(binding=self.binding,**scope,claim_id=claim['claim_id'],fence=claim['fence'],expected_revision=claim['revision'],effect_admission_id=identity.admission_id)
        for change in [dict(fence=claim['fence']+1),dict(expected_revision=claim['revision']-1),dict(result='caller supplied result')]:
            with self.assertRaises(LettaCommandError):self.command('recovery_reconcile_tool',**(fields|change))
        confirmed=backend.reconcile_tool(self.binding,scope,claim,identity.admission_id,claim['revision'])
        self.assertEqual(confirmed['phase'],'tool_confirmed');self.assertEqual(confirmed['run_id'],original_run)
        self.assertEqual(len(self.fixture.provider_calls),1);self.assertEqual(self.worker.calls,1)
        repeated=backend.reconcile_tool(self.binding,scope,claim,identity.admission_id,confirmed['revision'])
        self.assertEqual(repeated['revision'],confirmed['revision'])
        backend.resume(dict(binding=self.binding,attempt_id='runner-continuation',claim_id=claim['claim_id'],fence=claim['fence'],expected_revision=confirmed['revision']),scope)
        terminal=self.wait('runner-continuation');self.assertEqual(terminal['phase'],'terminal_committed',terminal)
        self.assertEqual(terminal['run_id'],original_run);self.assertEqual(self.worker.calls,1);self.assertEqual(len(self.fixture.provider_calls),2)
        self.assertIn('saved exact result',self.fixture.provider_calls[-1]['messages'][-1]['content'])
        self.assertEqual(self.fixture.store.connection.execute('SELECT count(*) FROM cycle_tool_attempts').fetchone()[0],1)
        self.assertEqual(self.fixture.store.connection.execute('SELECT count(*) FROM runner_imports').fetchone()[0],1)

    def test_pause_after_confirmation_blocks_resume_without_losing_saved_result(self):
        scope,claim,state=self.runner_turn()
        self.assertEqual(state['phase'],'held',state)
        self.assertTrue(reconcile_pending_runner_result(self.fixture.store,self.worker,self.binding,expected_runner_uid=os.getuid()))
        backend=LocalRecoveryClient(self.fixture.client);backend.release(self.binding,scope,claim)
        scope=RecoveryRecords(self.fixture.store).admit_scope(self.binding)
        state=backend.inspect(self.binding);claim=backend.claim(self.binding,state['revision'],scope)
        admission=self.fixture.store.connection.execute('SELECT admission_id FROM runner_operations').fetchone()[0]
        confirmed=backend.reconcile_tool(self.binding,scope,claim,admission,claim['revision'])
        self.fixture.store.set_state('paused','1')
        with self.assertRaises(Exception):backend.resume(dict(binding=self.binding,attempt_id='paused-continuation',claim_id=claim['claim_id'],fence=claim['fence'],expected_revision=confirmed['revision']),scope)
        self.assertEqual(backend.inspect(self.binding)['phase'],'tool_confirmed')
        self.assertEqual(self.fixture.store.get_state('paused'),'1');self.assertEqual(self.worker.calls,1);self.assertEqual(len(self.fixture.provider_calls),1)

    def test_normal_receipted_tool_confirms_without_recovery_or_duplicate_callback(self):
        _,_,state=self.runner_turn(lose_reply=False)
        self.assertEqual(state['phase'],'terminal_committed',state)
        operation=self.fixture.store.connection.execute('SELECT admission_id FROM runner_operations').fetchone()
        self.assertIn(operation[0],state['runner_receipt_admissions'])
        self.assertEqual(self.worker.calls,1);self.assertEqual(len(self.fixture.provider_calls),2)

    def test_coordinator_imports_confirms_then_continues_the_same_turn_on_separate_passes(self):
        scope,_,state=self.runner_turn();self.assertEqual(state['phase'],'held',state)
        store=self.fixture.store
        with store.connection:store.connection.execute("UPDATE recovery_attempts SET state='held',hold_reason='tool_uncertain',backend_attempt_id='runner-attempt' WHERE admission_id=?",(scope['admission_id'],))
        self.fixture.restart()
        controller=Controller(store,Mock(),17,autonomous_enabled=True);controller.runner_recovery=self.adapter
        coordinator=RecoveryCoordinator(store,controller,lambda:LocalRecoveryClient(self.fixture.client),enabled=True,config_sha='b'*64)
        # Keep the shared fixture transport open between worker passes.
        def client():
            result=LocalRecoveryClient(self.fixture.client);result.close=lambda:None;return result
        coordinator.client_factory=client
        self.assertTrue(coordinator.reconcile_one()) # historical import only
        self.assertEqual(self.command('recovery_inspect',binding=self.binding)['phase'],'held')
        self.assertTrue(coordinator.reconcile_one()) # exact backend confirmation only
        self.assertEqual(self.command('recovery_inspect',binding=self.binding)['phase'],'tool_confirmed')
        self.assertEqual(len(self.fixture.provider_calls),1)
        self.assertTrue(coordinator.reconcile_one()) # normal continuation guards and completion
        terminal=self.command('recovery_inspect',binding=self.binding)
        self.assertEqual(terminal['phase'],'terminal_committed');self.assertEqual(terminal['run_id'],state['run_id'])
        self.assertFalse(coordinator.has_pending());self.assertEqual(self.worker.calls,1);self.assertEqual(len(self.fixture.provider_calls),2)
