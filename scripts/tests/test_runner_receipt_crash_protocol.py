"""Independent controller/receiver/backend deaths; every worker is a counted fake."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

import test_runner_backend_protocol as base
from andrewbot.runner_client import RunnerClient
from andrewbot.runner_recovery import RunnerRecoveryAdapter,reconcile_pending_runner_result
from andrewbot.recovery import LocalRecoveryClient
from andrewbot.recovery_records import RecoveryRecords,digest
from andrewbot.runner_protocol import validate_identity

RECEIVER='''import asyncio,json,os,sys
from pathlib import Path
from andrewbot.runner_receipts import RunnerReceiptStore
from andrewbot.runner_service import RunnerSocketServer
root=Path(sys.argv[1]);s=RunnerReceiptStore(root/'receipts',b'k'*32,expected_uid=os.getuid())
if s._identity is None:s.provision(controller_store_id='a'*32)
s._fault=lambda p:os._exit(72) if p==sys.argv[2] else None
class Worker:
 cleanup_verified=True
 def run(self,*args):
  fd=os.open(root/'executions',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
  try:os.write(fd,b'1\\n');os.fsync(fd)
  finally:os.close(fd)
  return dict(status='ok',stdout='crash fixture result',stderr='',artifacts=[],safe_error=None)
async def main():
 path=root/'runner.sock'
 if path.exists():path.unlink()
 server=await asyncio.start_unix_server(RunnerSocketServer(Worker(),receipts=s,controller_uid=os.getuid(),receipts_enabled=True).handle,path=str(path))
 (root/'ready').write_text('ready')
 async with server:await server.serve_forever()
try:asyncio.run(main())
finally:s.close()
'''
CONTROLLER='''import json,os,sys
from andrewbot.store import Store
from andrewbot.runner_client import RunnerClient
from andrewbot.runner_recovery import RunnerRecoveryAdapter
v=json.load(sys.stdin);s=Store(v['database']);a=RunnerRecoveryAdapter(s,RunnerClient(v['socket'],timeout_seconds=5),expected_runner_uid=os.getuid(),runner_store_id=v['runner'])
a._fault=lambda p:os._exit(71) if p==v['fault'] else None
try:a.execute(v['metadata'],v['name'],v['arguments'])
finally:s.close()
'''


@unittest.skipUnless(os.environ.get('ANDREW_LOCAL_RECOVERY_TEST_RELEASE'),'requires isolated recovery release')
class RunnerReceiptCrashProtocolTests(unittest.TestCase):
    setUp=base.RunnerBackendProtocolTests.setUp
    prepare=base.RunnerBackendProtocolTests.prepare
    command=base.RunnerBackendProtocolTests.command

    def start_receiver(self,fault=''):
        if getattr(self,'receiver',None) is not None:
            if self.receiver.poll() is None:self.receiver.kill()
            self.receiver.wait(5);self.receiver.stderr.close()
        ready=self.root/'ready';ready.unlink(missing_ok=True)
        self.receiver=subprocess.Popen([sys.executable,'-c',RECEIVER,str(self.root),fault],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        deadline=time.monotonic()+10
        while not ready.exists():
            self.assertIsNone(self.receiver.poll(),self.receiver.stderr.read().decode() if self.receiver.poll() is not None else '')
            self.assertLess(time.monotonic(),deadline);time.sleep(.02)
        self.runner_id=json.loads((self.root/'receipts/identity.json').read_text())['runner_store_id']
        self.runner=RunnerClient(str(self.root/'runner.sock'),timeout_seconds=5)

    def stop_receiver(self):
        if self.receiver.poll() is None:self.receiver.kill()
        self.receiver.wait(5);self.receiver.stderr.close()

    def count(self):
        return len((self.root/'executions').read_text().splitlines()) if (self.root/'executions').exists() else 0

    def begin(self,controller_fault='',runner_fault=''):
        self.root=Path(self.tmp.name)/'runner-process';self.root.mkdir()
        self.start_receiver(runner_fault);self.addCleanup(self.stop_receiver)
        binding,input,unused=self.prepare();self.binding=binding;store=self.fixture.store;RecoveryRecords(store).revoke(unused['admission_id'])
        input['tools']=[dict(type='function',function=dict(name='python.run',parameters=dict(type='object')))];binding['input_sha']=digest(input)
        for k,v in dict(local_recovery_controller_store_id='a'*32,runner_receipt_store_id=self.runner_id,runner_receipts_enabled='1',runner_receipt_backend_capability='1').items():store.set_state(k,v)
        def tool(name,args,*,recovery):
            value=dict(database=str(self.fixture.root/'controller.sqlite3'),socket=str(self.root/'runner.sock'),runner=self.runner_id,
                fault=controller_fault,metadata=recovery,name=name,arguments=args)
            child=subprocess.run([sys.executable,'-c',CONTROLLER],input=json.dumps(value),capture_output=True,text=True,timeout=20)
            if child.returncode:raise TimeoutError('counted controller process interrupted')
            raise TimeoutError('reply deliberately lost after saved result')
        self.fixture.model_gateway.recovery_tool_handler=tool
        self.responses.insert(0,dict(id='crash-runner-provider',choices=[dict(index=0,message=dict(role='assistant',content=None,
            tool_calls=[dict(id='crash-runner-call',type='function',function=dict(name='python.run',arguments='{"code":"print(42)"}'))]),finish_reason='tool_calls')],
            usage=dict(prompt_tokens=20,completion_tokens=5,cost=.000001)))
        self.scope=RecoveryRecords(store).admit_scope(binding)
        admitted=self.command('recovery_admit',binding=binding,input=input,**self.scope)
        self.claim=self.command('recovery_claim',binding=binding,expected_revision=admitted['revision'],**self.scope)
        self.command('recovery_resume',binding=binding,attempt_id='crash-runner-attempt',expected_revision=self.claim['revision'],claim_id=self.claim['claim_id'],fence=self.claim['fence'],**self.scope)
        return self.wait('crash-runner-attempt')

    def wait(self,attempt):
        deadline=time.monotonic()+15
        while True:
            state=self.command('recovery_status',binding=self.binding,attempt_id=attempt)
            if state['phase'] in ('held','terminal_committed'):return state
            self.assertLess(time.monotonic(),deadline,state);time.sleep(.03)

    def import_and_resume(self,state):
        backend=LocalRecoveryClient(self.fixture.client)
        backend.release(self.binding,self.scope,self.claim)
        scope=RecoveryRecords(self.fixture.store).admit_scope(self.binding)
        state=backend.inspect(self.binding);claim=backend.claim(self.binding,state['revision'],scope)
        operation=self.fixture.store.connection.execute('SELECT admission_id FROM runner_operations').fetchone()[0]
        confirmed=backend.reconcile_tool(self.binding,scope,claim,operation,claim['revision'])
        backend.resume(dict(binding=self.binding,attempt_id='crash-continuation',claim_id=claim['claim_id'],fence=claim['fence'],expected_revision=confirmed['revision']),scope)
        return self.wait('crash-continuation')

    def test_controller_loss_after_grant_commit_stays_held_without_execution(self):
        state=self.begin(controller_fault='grant_after_commit');self.assertEqual(state['phase'],'held',state)
        self.assertEqual(self.count(),0)
        self.assertFalse(reconcile_pending_runner_result(self.fixture.store,self.runner,self.binding,expected_runner_uid=os.getuid()))
        self.assertEqual(self.count(),0);self.assertEqual(len(self.fixture.provider_calls),1)

    def test_controller_loss_before_import_recovers_saved_result_once(self):
        state=self.begin(controller_fault='import_before_commit');self.assertEqual(state['phase'],'held',state)
        self.assertTrue(reconcile_pending_runner_result(self.fixture.store,self.runner,self.binding,expected_runner_uid=os.getuid()))
        terminal=self.import_and_resume(state);self.assertEqual(terminal['phase'],'terminal_committed',terminal)
        self.assertEqual(self.count(),1);self.assertEqual(len(self.fixture.provider_calls),2)

    def test_controller_loss_after_import_reuses_exact_saved_proof(self):
        state=self.begin(controller_fault='import_after_commit');self.assertEqual(state['phase'],'terminal_committed',state)
        self.assertEqual(self.count(),1);self.assertEqual(len(self.fixture.provider_calls),2)
        self.assertEqual(self.fixture.store.connection.execute('SELECT count(*) FROM runner_imports').fetchone()[0],1)

    def test_runner_loss_before_receipt_commit_stays_unknown_without_reexecution(self):
        state=self.begin(runner_fault='result_before_commit');self.assertEqual(state['phase'],'held',state)
        self.start_receiver()
        self.assertFalse(reconcile_pending_runner_result(self.fixture.store,self.runner,self.binding,expected_runner_uid=os.getuid()))
        self.assertEqual(self.count(),1);self.assertEqual(len(self.fixture.provider_calls),1)

    def test_runner_loss_after_receipt_commit_recovers_once(self):
        state=self.begin(runner_fault='result_after_commit');self.assertEqual(state['phase'],'held',state)
        self.start_receiver()
        self.assertTrue(reconcile_pending_runner_result(self.fixture.store,self.runner,self.binding,expected_runner_uid=os.getuid()))
        terminal=self.import_and_resume(state);self.assertEqual(terminal['phase'],'terminal_committed',terminal)
        self.assertEqual(self.count(),1);self.assertEqual(len(self.fixture.provider_calls),2)

    def backend_loss(self,moment):
        state=self.begin(controller_fault='import_before_commit')
        self.assertTrue(reconcile_pending_runner_result(self.fixture.store,self.runner,self.binding,expected_runner_uid=os.getuid()))
        preload=self.fixture.root/'fault.mjs';config=self.fixture.root/'backend-fault-config.json'
        import test_local_recovery_crash_protocol as crash
        preload.write_text(crash.PRELOAD);config.write_text(json.dumps(dict(phase='tool_confirmed',moment=moment)))
        self.fixture.environment.update(NODE_OPTIONS='--import='+str(preload),ANDREW_TEST_FAULT_ROOT=str(self.fixture.root))
        self.fixture.restart();backend=LocalRecoveryClient(self.fixture.client)
        scope=RecoveryRecords(self.fixture.store).admit_scope(self.binding);current=backend.inspect(self.binding);claim=backend.claim(self.binding,current['revision'],scope)
        operation=self.fixture.store.connection.execute('SELECT admission_id FROM runner_operations').fetchone()[0]
        with self.assertRaises(Exception):backend.reconcile_tool(self.binding,scope,claim,operation,claim['revision'])
        self.fixture.environment.pop('NODE_OPTIONS');self.fixture.environment.pop('ANDREW_TEST_FAULT_ROOT');self.fixture.restart()
        backend=LocalRecoveryClient(self.fixture.client);current=backend.inspect(self.binding)
        self.assertEqual(current['phase'],'held' if moment=='before' else 'tool_confirmed');self.assertEqual(current['run_id'],state['run_id'])
        scope=RecoveryRecords(self.fixture.store).admit_scope(self.binding);claim=backend.claim(self.binding,current['revision'],scope)
        repeated=backend.reconcile_tool(self.binding,scope,claim,operation,claim['revision'])
        backend.resume(dict(binding=self.binding,attempt_id='backend-crash-continuation',claim_id=claim['claim_id'],fence=claim['fence'],expected_revision=repeated['revision']),scope)
        terminal=self.wait('backend-crash-continuation');self.assertEqual(terminal['phase'],'terminal_committed',terminal)
        self.assertEqual(self.count(),1);self.assertEqual(len(self.fixture.provider_calls),2)

    def test_backend_loss_before_held_confirmation_reuses_same_input_and_worker(self):
        self.backend_loss('before')

    def test_backend_loss_after_held_confirmation_reuses_same_input_and_worker(self):
        self.backend_loss('after')
