"""Actual patched SDK with isolated controller, fake provider and exact recovery IDs."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime,timezone
from unittest.mock import Mock
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'prototype'))
from andrewbot.appserver import AppServerClient
from andrewbot.controller import Controller
from andrewbot.sessions import SessionRunner
from andrewbot.recovery_records import RecoveryRecords,digest
spec=importlib.util.spec_from_file_location('local_recovery_fixture',ROOT/'letta_recovery/tests/protocol_fixture.py')
fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)


@unittest.skipUnless(os.environ.get('ANDREW_LOCAL_RECOVERY_TEST_RELEASE'),'requires isolated recovery release')
class LocalRecoveryProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.responses=[{'id':'fixture-response','choices':[{'index':0,'message':{'role':'assistant','content':'Persisted fixture answer.'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}}]
        self.fixture=fixtures.launch_release(os.environ['ANDREW_LOCAL_RECOVERY_TEST_RELEASE'],self.tmp.name,
            lambda payload:self.responses.pop(0),{'ANDREW_LOCAL_RECOVERY_ARTIFACT':'1'})
        self.addCleanup(self.fixture.stop)

    def prepare(self):
        client=self.fixture.client
        client.request({'type':'connect_provider','target':'local','provider_id':'openai-compatible',
            'fields':{'apiKey':'fixture-token','baseUrl':f'http://127.0.0.1:{self.fixture.gateway.server.server_port}/v1'}},'connect_provider_response')
        agent_id=client.create_agent({'name':'Journaled fixture','system':'Use the advertised tools and reply briefly.',
            'model':'openai-compatible/qwen/qwen3.8-flash','include_base_tools':False,'memory_blocks':[]})
        runtime=client.prepare_conversation(agent_id)
        capability=client.request({'type':'recovery_capabilities'},'recovery_capabilities_response')
        self.assertEqual(capability['contract'],'local_durable_recovery_v1')
        store=self.fixture.store;controller=Controller(store,Mock(),17,autonomous_enabled=True)
        session=SessionRunner(store,controller,Mock(),enabled=True).start('owner_wake',datetime.now(timezone.utc))
        store.begin_cycle(session,'cycle-fixture','2026-10-18');store.checkpoint_cycle_turn('cycle-fixture','model_started')
        store.record_model_turn_receipt(session,'cycle-fixture',runtime,{'kind':'prepared','request_id':'consumer-fixture','client_message_id':'input-fixture'})
        input={'model':'qwen/qwen3.8-flash','messages':[{'role':'user','content':'Reply with a short fixture answer.'}], 'tools':[]}
        binding={'store_id':capability['store_id'],**runtime,'session_id':session,'cycle_id':'cycle-fixture',
            'client_message_id':'input-fixture','consumer_id':'consumer-fixture','input_sha':digest(input),'owner_revision':'0','config_sha':'b'*64}
        store.set_state('local_recovery_config_sha','b'*64);scope=RecoveryRecords(store).admit_scope(binding)
        return binding,input,scope

    def command(self,kind,**fields):
        return self.fixture.client.request({'type':kind,**fields},kind+'_response',timeout=10)

    def test_journaled_turn_uses_real_backend_and_preserves_original_input(self):
        binding,input,scope=self.prepare()
        admitted=self.command('recovery_admit',binding=binding,input=input,**scope)
        self.assertTrue(admitted['success'],admitted)
        claimed=self.command('recovery_claim',binding=binding,expected_revision=admitted['revision'],**scope)
        self.assertTrue(claimed['success'],claimed)
        resumed=self.command('recovery_resume',binding=binding,attempt_id='resume-fixture',expected_revision=claimed['revision'],claim_id=claimed['claim_id'],fence=claimed['fence'],**scope)
        self.assertTrue(resumed['success'],resumed)
        import time
        deadline=time.monotonic()+10
        while True:
            state=self.command('recovery_status',binding=binding,attempt_id='resume-fixture',**scope)
            if state.get('phase') in ('terminal_committed','held'):break
            self.assertLess(time.monotonic(),deadline,state);time.sleep(.03)
        self.assertEqual(state['phase'],'terminal_committed',state)
        self.assertEqual(state['binding']['client_message_id'],'input-fixture')
        self.assertEqual(len(self.fixture.provider_calls),1)
        history=self.fixture.client.request({'type':'conversation_messages_list','conversation_id':binding['conversation_id'],'query':{'limit':100,'order':'desc'}},'conversation_messages_list_response')
        self.assertIn('Persisted fixture answer.',json.dumps(history))
        duplicate=self.command('recovery_resume',binding=binding,attempt_id='resume-fixture',expected_revision=claimed['revision'],claim_id=claimed['claim_id'],fence=claimed['fence'],**scope)
        self.assertEqual(duplicate['attempt_id'],'resume-fixture');self.assertEqual(len(self.fixture.provider_calls),1)
        self.fixture.restart()
        restored=self.command('recovery_status',binding=binding,attempt_id='resume-fixture',**scope)
        self.assertEqual(restored['run_id'],state['run_id']);self.assertEqual(restored['phase'],'terminal_committed')
        history=self.fixture.client.request({'type':'conversation_messages_list','conversation_id':binding['conversation_id'],'query':{'limit':100,'order':'desc'}},'conversation_messages_list_response')
        self.assertEqual(json.dumps(history).count('Persisted fixture answer.'),1)
        self.assertEqual(len(self.fixture.provider_calls),1)

    def test_real_tool_is_confirmed_before_one_continuation(self):
        binding,input,scope=self.prepare()
        # A new binding/scope is required when the exact advertised tool set changes.
        input['tools']=[{'type':'function','function':{'name':'recent_activity','parameters':{'type':'object','properties':{}}}}]
        binding['input_sha']=digest(input);scope=RecoveryRecords(self.fixture.store).admit_scope(binding)
        calls=[]
        from andrewbot.recovery_dispatch import RecoveryDispatch
        def tool(name,args,*,recovery):
            return RecoveryDispatch(self.fixture.store).run_tool(recovery,name,args,lambda:calls.append((name,args)) or {'entries':['exact fixture event']})
        self.fixture.model_gateway.recovery_tool_handler=tool
        self.responses.insert(0,{'id':'tool-response','choices':[{'index':0,'message':{'role':'assistant','content':None,'tool_calls':[{'id':'fixture-call','type':'function','function':{'name':'recent_activity','arguments':'{}'}}]},'finish_reason':'tool_calls'}],'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}})
        admitted=self.command('recovery_admit',binding=binding,input=input,**scope)
        claim=self.command('recovery_claim',binding=binding,expected_revision=admitted['revision'],**scope)
        self.command('recovery_resume',binding=binding,attempt_id='tool-attempt',expected_revision=claim['revision'],claim_id=claim['claim_id'],fence=claim['fence'],**scope)
        import time
        deadline=time.monotonic()+10
        while True:
            state=self.command('recovery_status',binding=binding,attempt_id='tool-attempt',**scope)
            if state.get('phase') in ('terminal_committed','held'):break
            self.assertLess(time.monotonic(),deadline,state);time.sleep(.03)
        self.assertEqual(state['phase'],'terminal_committed',state)
        self.assertEqual(calls,[('recent_activity',{})]);self.assertEqual(len(self.fixture.provider_calls),2)
        continuation=self.fixture.provider_calls[1]['messages']
        self.assertEqual(continuation[-1],{'role':'tool','tool_call_id':'fixture-call','content':'{"entries":["exact fixture event"]}'})
