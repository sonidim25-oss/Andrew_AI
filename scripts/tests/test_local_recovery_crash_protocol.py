"""Kill actual backend/controller processes at durable boundaries; no paid destinations."""
import json
import fcntl
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'prototype'))
from andrewbot.appserver import AppServerClient

PRELOAD=r'''
import fs from 'node:fs';
import {syncBuiltinESMExports} from 'node:module';
const root=process.env.ANDREW_TEST_FAULT_ROOT;
const fault=JSON.parse(fs.readFileSync(root+'/backend-fault-config.json','utf8'));
const rename=fs.renameSync;let armed=true;
fs.renameSync=function(from,to){
 let snapshot=null,hit=false;
 if(armed&&String(to).includes('local-recovery-v1/conversations/')&&/\/[a-f0-9]{64}\.json$/.test(String(to))){
  snapshot=JSON.parse(fs.readFileSync(from,'utf8'));hit=snapshot.phase===fault.phase;
  if(fault.phase==='input_committed')hit=hit&&snapshot.fence===0;
 }
 if(armed&&fault.phase==='projection'&&String(to).endsWith('.jsonl'))hit=fs.readFileSync(from,'utf8').includes('"operation_id":"tool:');
 function kill(){armed=false;fs.writeFileSync(root+'/backend-fault.json',JSON.stringify({phase:fault.phase,moment:fault.moment,client_message_id:snapshot?.binding.client_message_id??null}));process.kill(process.pid,'SIGKILL');}
 if(hit&&fault.moment==='before')kill();
 const result=rename.apply(this,arguments);
 if(hit&&fault.moment==='after')kill();
 return result;
};syncBuiltinESMExports();
'''


def available_port():
    with socket.socket() as listener:listener.bind(('127.0.0.1',0));return listener.getsockname()[1]


class Harness:
    def __init__(self,test,*,phase=None,moment=None,controller_fault=None,compaction=False,rate_limit=False,owner_arrives=False,unreached_first=False):
        self.test=test;self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.provider_calls=[];self.effects=[];fixture=self;self.compaction=compaction;self.unreached_first=unreached_first;self.model_id=None;self.system_suffix=None
        class Provider(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path=='/provider':
                    fixture.provider_calls.append(payload)
                    if owner_arrives and len(fixture.provider_calls)==1:
                        import sqlite3
                        with sqlite3.connect(fixture.root/'controller.sqlite3') as db:
                            db.execute("INSERT INTO messages(owner_id,role,content,queued,created_at) VALUES(17,'user','New waiting owner input',1,datetime('now'))")
                    if rate_limit and len(fixture.provider_calls)==1:
                        encoded=b'{"error":{"code":429,"message":"fixture rate limit"}}'
                        self.send_response(429);self.send_header('Retry-After','5');self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded);return
                    if compaction and len(fixture.provider_calls)==1:
                        message={'role':'assistant','content':'Historical fixture text. '*150000};reason='stop'
                    elif compaction and payload['messages'][0]['content'].startswith('Your task is to create a detailed summary'):
                        message={'role':'assistant','content':'Exact confirmed compaction summary.'};reason='stop'
                    elif len(fixture.provider_calls)==(3 if compaction else 2 if rate_limit else 1):
                        message={'role':'assistant','content':None,'tool_calls':[{'id':'crash-call','type':'function','function':{'name':'recent_activity','arguments':'{}'}}]};reason='tool_calls'
                    else:message={'role':'assistant','content':'Exact final fixture answer.'};reason='stop'
                    result={'id':'fixture-'+str(len(fixture.provider_calls)),'choices':[{'index':0,'message':message,'finish_reason':reason}],
                        'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}}
                elif self.path=='/effect':fixture.effects.append(payload);result={'entries':['exact durable fixture event']}
                else:raise AssertionError('Unknown fixture destination')
                encoded=json.dumps(result).encode();self.send_response(200);self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
        self.provider=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        self.thread=threading.Thread(target=self.provider.serve_forever,daemon=True);self.thread.start()
        self.port=available_port();self.url=f'ws://127.0.0.1:{self.port}/ws';self.backend=None;self.controller=None
        self.environment={**os.environ,'HOME':str(self.root/'backend-home'),'LETTA_HOME':str(self.root/'settings'),
            'LETTA_LOCAL_BACKEND_DIR':str(self.root/'backend-home/backend'),'DO_NOT_TRACK':'1','ANDREW_LOCAL_RECOVERY_ARTIFACT':'1'}
        (self.root/'backend-home').mkdir()
        if phase:
            (self.root/'fault.mjs').write_text(PRELOAD)
            (self.root/'backend-fault-config.json').write_text(json.dumps({'phase':phase,'moment':moment}))
            self.environment['NODE_OPTIONS']='--import='+str(self.root/'fault.mjs');self.environment['ANDREW_TEST_FAULT_ROOT']=str(self.root)
        test.addCleanup(self.close)
        self.start_backend();self.start_controller('start',controller_fault)

    def start_backend(self):
        log=(self.root/'backend.log').open('a')
        self.backend=subprocess.Popen(['node',str(Path(os.environ['ANDREW_LOCAL_RECOVERY_TEST_RELEASE'])/'letta.js'),
            'server','--backend','local','--listen',f'ws://127.0.0.1:{self.port}','--openai-api'],env=self.environment,cwd=self.root,stdout=log,stderr=log)
        log.close();deadline=time.monotonic()+20
        while True:
            self.test.assertIsNone(self.backend.poll(),(self.root/'backend.log').read_text()[-1500:])
            try:client=AppServerClient(self.url,timeout=2);client.close();return
            except ConnectionRefusedError:
                self.test.assertLess(time.monotonic(),deadline);time.sleep(.02)

    def start_controller(self,mode,fault=None,*,recovery_once=False):
        for name in ('controller-ready.json','controller-done.json'):(self.root/name).unlink(missing_ok=True)
        command=[sys.executable,str(ROOT/'scripts/tests/recovery_controller_fixture.py'),'--root',str(self.root),
            '--backend-url',self.url,'--provider-url',f'http://127.0.0.1:{self.provider.server_port}','--mode',mode]
        if fault:command+=['--fault',fault]
        if self.compaction:command+=['--compaction']
        if recovery_once:command+=['--recovery-once']
        if self.unreached_first and mode=='start':command+=['--unreached-first']
        if self.model_id:command+=['--model-id',self.model_id]
        if self.system_suffix:command+=['--system-suffix',self.system_suffix]
        log=(self.root/'controller.log').open('a')
        environment={**os.environ,'PYTHONPATH':str(ROOT/'prototype'),'NO_PROXY':'*','no_proxy':'*'}
        self.controller=subprocess.Popen(command,cwd=self.root,env=environment,stdout=log,stderr=log);log.close()

    def wait_file(self,name,timeout=20):
        deadline=time.monotonic()+timeout;path=self.root/name
        while not path.exists():
            self.test.assertLess(time.monotonic(),deadline,(self.root/'controller.log').read_text()[-2200:]+'\n'+(self.root/'backend.log').read_text()[-800:])
            time.sleep(.02)
        return json.loads(path.read_text())

    def original_input(self):
        import sqlite3
        with sqlite3.connect(self.root/'controller.sqlite3') as db:
            return db.execute('SELECT client_message_id FROM model_turn_receipts').fetchone()[0]

    def recover(self,*,restart_backend=True,recovery_once=False):
        if self.controller and self.controller.poll() is None:self.controller.kill();self.controller.wait(5)
        if restart_backend:
            if self.backend and self.backend.poll() is None:self.backend.kill();self.backend.wait(5)
            self.environment.pop('NODE_OPTIONS',None);self.environment.pop('ANDREW_TEST_FAULT_ROOT',None);self.start_backend()
        self.start_controller('recover',recovery_once=recovery_once)
        return self.wait_file('controller-done.json')

    def close(self):
        if getattr(self,'closed',False):return
        self.closed=True
        for process in (self.controller,self.backend):
            if process and process.poll() is None:process.kill();process.wait(5)
        self.provider.shutdown();self.provider.server_close();self.thread.join(2);self.temp.cleanup()


@unittest.skipUnless(os.environ.get('ANDREW_LOCAL_RECOVERY_TEST_RELEASE'),'requires verified isolated recovery release')
class LocalRecoveryCrashProtocolTests(unittest.TestCase):
    def test_owner_handoff_process_loss_never_replays_prepared_tool(self):
        for fault in ('yield_before','yield_after','handoff_before','handoff_after'):
            for both in (False,True):
                with self.subTest(fault=fault,restart_backend=both):
                    fixture=Harness(self,controller_fault=fault,owner_arrives=True)
                    fixture.wait_file('controller-fault.json');fixture.controller.wait(5)
                    result=fixture.recover(restart_backend=both)
                    self.assertTrue(result['completed'],result)
                    self.assertEqual(result['receipts'][0]['state'],'accepted')
                    self.assertEqual(len(fixture.provider_calls),1);self.assertEqual(fixture.effects,[])
                    self.assertEqual(result['reservations'],0);self.assertEqual(result['held_messages'],1)
                    fixture.close()

    def test_rejection_restart_waits_for_previous_execution_lock(self):
        fixture=Harness(self,controller_fault='rejection_after',rate_limit=True)
        fixture.wait_file('controller-fault.json');fixture.controller.wait(5)
        path=next((fixture.root/'backend-home/backend/local-recovery-v1/conversations').rglob('*.execution'))
        with path.open('a') as lock:
            deadline=time.monotonic()+5
            while True:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    self.assertLess(time.monotonic(),deadline);time.sleep(.01)
            result=fixture.recover(restart_backend=False,recovery_once=True)
            self.assertFalse(result['completed']);self.assertEqual(len(fixture.provider_calls),1)
        result=fixture.recover(restart_backend=False)
        self.assertTrue(result['completed'],result)
        self.assertEqual(len(fixture.provider_calls),3)
        self.assertEqual(len(fixture.effects),1)
        self.assertEqual(result['reservations'],0)
        fixture.close()

    def test_rejection_receipt_process_loss_before_and_after_commit(self):
        for boundary in ('before','after'):
            for restart_backend in (False,True):
                with self.subTest(boundary=boundary,restart_backend=restart_backend):
                    fixture=Harness(self,controller_fault='rejection_'+boundary,rate_limit=True)
                    fixture.wait_file('controller-fault.json');fixture.controller.wait(5)
                    original=fixture.original_input();result=fixture.recover(restart_backend=restart_backend)
                    self.assertEqual(fixture.original_input(),original)
                    self.assertEqual(result['held_messages'],1)
                    if boundary=='before':
                        self.assertFalse(result['completed']);self.assertEqual(len(fixture.provider_calls),1)
                        self.assertEqual(result['reservations'],1);self.assertEqual(fixture.effects,[])
                    else:
                        self.assertTrue(result['completed']);self.assertEqual(len(fixture.provider_calls),3)
                        self.assertEqual(result['reservations'],0);self.assertEqual(len(fixture.effects),1)
                    fixture.close()

    def test_proven_unsent_provider_failure_backs_off_and_completes_without_hold(self):
        fixture=Harness(self,unreached_first=True)
        result=fixture.wait_file('controller-done.json',timeout=40)
        self.assertTrue(result['completed'],result)
        self.assertEqual(result['receipts'][0]['state'],'completed')
        self.assertEqual((len(fixture.provider_calls),len(fixture.effects)),(2,1))
        self.assertEqual(result['reservations'],0);self.assertEqual(result['held_messages'],1)
        import sqlite3
        with sqlite3.connect(fixture.root/'controller.sqlite3') as db:
            self.assertEqual(db.execute('SELECT attempt,http_status,reason FROM recovery_provider_rejections').fetchall(),[(1,None,'provider_not_reached')])
            self.assertIn(db.execute("SELECT value FROM state WHERE key='autonomy_error'").fetchone(),(None,('',)))
            self.assertEqual(db.execute("SELECT count(*) FROM recovery_effects WHERE state='admitted'").fetchone()[0],0)
        fixture.close()

    def test_model_change_between_settled_turns_applies_to_the_next_turn(self):
        fixture=Harness(self)
        self.assertTrue(fixture.wait_file('controller-done.json')['completed'])
        first=len(fixture.provider_calls)
        fixture.controller.kill();fixture.controller.wait(5)
        fixture.model_id='openai/gpt-6-luna';fixture.start_controller('start')
        result=fixture.wait_file('controller-done.json',timeout=40)
        self.assertTrue(result['completed'],result)
        self.assertGreater(len(fixture.provider_calls),first)
        self.assertEqual({call['model'] for call in fixture.provider_calls[first:]},{'openai/gpt-6-luna'})
        import sqlite3
        with sqlite3.connect(fixture.root/'controller.sqlite3') as db:
            state=dict(db.execute("SELECT key,value FROM state WHERE key IN ('letta_model_id','letta_model_handle')"))
            configs=[row[0] for row in db.execute('SELECT DISTINCT json_extract(binding_json,"$.config_sha") FROM recovery_attempts')]
        self.assertEqual(state,{'letta_model_id':'openai/gpt-6-luna','letta_model_handle':'openai-compatible/openai/gpt-6-luna'})
        self.assertEqual(len(configs),2)
        fixture.close()

    def test_model_change_is_refused_while_a_turn_is_held(self):
        fixture=Harness(self,phase='provider_admitted',moment='after')
        fixture.wait_file('backend-fault.json');fixture.backend.wait(5)
        fixture.wait_file('controller-done.json');fixture.controller.kill();fixture.controller.wait(5)
        fixture.environment.pop('NODE_OPTIONS',None);fixture.environment.pop('ANDREW_TEST_FAULT_ROOT',None);fixture.start_backend()
        calls=len(fixture.provider_calls)
        fixture.model_id='openai/gpt-6-luna';fixture.start_controller('recover')
        fixture.controller.wait(30)
        self.assertNotEqual(fixture.controller.returncode,0)
        self.assertIn('Letta model update failed (LettaCommandError)',(fixture.root/'controller.log').read_text())
        self.assertEqual(len(fixture.provider_calls),calls)
        import sqlite3
        with sqlite3.connect(fixture.root/'controller.sqlite3') as db:
            self.assertNotEqual(db.execute("SELECT value FROM state WHERE key='letta_model_id'").fetchone(),('openai/gpt-6-luna',))
        fixture.close()

    def test_system_prompt_change_between_settled_turns_applies_to_the_next_turn(self):
        fixture=Harness(self)
        self.assertTrue(fixture.wait_file('controller-done.json')['completed'])
        first=len(fixture.provider_calls)
        self.assertNotIn('Fixture owner guidance.',fixture.provider_calls[0]['messages'][0]['content'])
        fixture.controller.kill();fixture.controller.wait(5)
        fixture.system_suffix='Fixture owner guidance.';fixture.start_controller('start')
        result=fixture.wait_file('controller-done.json',timeout=40)
        self.assertTrue(result['completed'],result)
        self.assertGreater(len(fixture.provider_calls),first)
        for call in fixture.provider_calls[first:]:
            self.assertIn('Fixture owner guidance.',call['messages'][0]['content'])
        import sqlite3
        with sqlite3.connect(fixture.root/'controller.sqlite3') as db:
            configs=[row[0] for row in db.execute('SELECT DISTINCT json_extract(binding_json,"$.config_sha") FROM recovery_attempts')]
        self.assertEqual(len(configs),2)
        fixture.close()

    def test_system_prompt_change_is_refused_while_a_turn_is_held(self):
        fixture=Harness(self,phase='provider_admitted',moment='after')
        fixture.wait_file('backend-fault.json');fixture.backend.wait(5)
        fixture.wait_file('controller-done.json');fixture.controller.kill();fixture.controller.wait(5)
        fixture.environment.pop('NODE_OPTIONS',None);fixture.environment.pop('ANDREW_TEST_FAULT_ROOT',None);fixture.start_backend()
        calls=len(fixture.provider_calls)
        fixture.system_suffix='Fixture owner guidance.';fixture.start_controller('recover')
        fixture.controller.wait(30)
        self.assertNotEqual(fixture.controller.returncode,0)
        self.assertIn('Letta capability instructions failed (LettaCommandError)',(fixture.root/'controller.log').read_text())
        self.assertEqual(len(fixture.provider_calls),calls)
        fixture.close()

    def test_backend_loss_around_backoff_checkpoint_keeps_rejection_evidence(self):
        for moment in ('before','after'):
            with self.subTest(moment=moment):
                fixture=Harness(self,phase='provider_backoff',moment=moment,rate_limit=True)
                fixture.wait_file('backend-fault.json');fixture.backend.wait(5)
                fixture.wait_file('controller-done.json')
                result=fixture.recover()
                self.assertTrue(result['completed']);self.assertEqual(len(fixture.provider_calls),3)
                self.assertEqual(result['reservations'],0);self.assertEqual(len(fixture.effects),1)
                fixture.close()

    def test_backend_process_loss_matrix(self):
        for phase in ('input_committed','provider_admitted','provider_confirmed','tool_prepared','tool_admitted',
                      'tool_confirmed','continuation_ready','terminal_committed','projection'):
            for moment in ('before','after'):
                with self.subTest(phase=phase,moment=moment):
                    fixture=Harness(self,phase=phase,moment=moment)
                    boundary=fixture.wait_file('backend-fault.json')
                    self.assertEqual(boundary,{'phase':phase,'moment':moment,'client_message_id':boundary['client_message_id']})
                    fixture.backend.wait(5);self.assertEqual(fixture.backend.returncode,-signal.SIGKILL)
                    fixture.wait_file('controller-done.json')
                    original=fixture.original_input();result=fixture.recover()
                    held=(phase=='input_committed' and moment=='before') or (phase in ('provider_admitted','tool_admitted') and moment=='after')
                    self.assertEqual(result['completed'],not held,result)
                    self.assertEqual(result['receipts'][0]['client_message_id'],original)
                    self.assertEqual(result['held_messages'],1)
                    expected=(0 if phase in ('input_committed','provider_admitted') else 1,0) if held else (2,1)
                    self.assertEqual((len(fixture.provider_calls),len(fixture.effects)),expected)
                    print(f'\nverified backend {phase} {moment}: provider={expected[0]} tool={expected[1]} held={held}',file=sys.stderr,flush=True)
                    fixture.close()

    def test_controller_and_combined_loss_after_exact_settlement(self):
        for fault in ('provider_before','provider_after','tool_before','tool_after'):
            for both in (False,True):
                with self.subTest(fault=fault,both=both):
                    fixture=Harness(self,controller_fault=fault)
                    self.assertEqual(fixture.wait_file('controller-fault.json')['boundary'],fault)
                    fixture.controller.wait(5);self.assertEqual(fixture.controller.returncode,-signal.SIGKILL)
                    original=fixture.original_input()
                    result=fixture.recover(restart_backend=both)
                    positive=fault.endswith('after')
                    self.assertEqual(result['completed'],positive,result)
                    self.assertEqual(result['receipts'][0]['client_message_id'],original)
                    self.assertEqual(result['held_messages'],1)
                    expected=(2,1) if positive else (1,1 if fault.startswith('tool') else 0)
                    self.assertEqual((len(fixture.provider_calls),len(fixture.effects)),expected)
                    self.assertEqual(result['reservations'],1 if fault=='provider_before' else 0)
                    print(f'\nverified controller {fault} both={both}: provider={expected[0]} tool={expected[1]}',file=sys.stderr,flush=True)
                    fixture.close()

    def test_confirmed_compaction_survives_full_process_loss_without_another_summary_call(self):
        fixture=Harness(self,controller_fault='provider_after',compaction=True)
        fixture.wait_file('controller-fault.json',timeout=40)
        fixture.controller.wait(5);self.assertEqual(fixture.controller.returncode,-signal.SIGKILL)
        self.assertEqual(len(fixture.provider_calls),2)
        result=fixture.recover()
        self.assertTrue(result['completed'],result)
        self.assertEqual((len(fixture.provider_calls),len(fixture.effects)),(4,1))
        summaries=[call for call in fixture.provider_calls if call['messages'][0]['content'].startswith('Your task is to create a detailed summary')]
        self.assertEqual(len(summaries),1)
        self.assertIn('Exact confirmed compaction summary.',json.dumps(fixture.provider_calls[-1]['messages']))
