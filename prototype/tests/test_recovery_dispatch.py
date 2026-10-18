from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
from andrewbot.store import Store
from andrewbot.controller import Controller
from andrewbot.sessions import SessionRunner
from andrewbot.gateway import ModelGateway
from andrewbot.recovery_records import RecoveryRecords, canonical, digest
from andrewbot.recovery_dispatch import RecoveryDispatch


class RecoveryDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'state.db';self.store=Store(self.path);self.addCleanup(lambda:self.store.close())
        self.agent=Mock();self.controller=Controller(self.store,self.agent,17,autonomous_enabled=True)
        self.runner=SessionRunner(self.store,self.controller,self.agent,enabled=True)
        session=self.runner.start('owner_wake', datetime.now(timezone.utc));self.store.begin_cycle(session,'cycle-fixture','2026-10-18')
        self.store.checkpoint_cycle_turn('cycle-fixture','model_started')
        runtime={'agent_id':'agent-fixture','conversation_id':'conversation-fixture'}
        self.store.record_model_turn_receipt(session,'cycle-fixture',runtime,{'kind':'prepared','request_id':'consumer-fixture','client_message_id':'input-fixture'})
        self.binding={'store_id':'store-fixture',**runtime,'session_id':session,'cycle_id':'cycle-fixture','client_message_id':'input-fixture',
            'consumer_id':'consumer-fixture','input_sha':'a'*64,'owner_revision':'0','config_sha':'b'*64}
        self.records=RecoveryRecords(self.store);self.store.set_state('local_recovery_config_sha','b'*64)
        self.scope=self.records.admit_scope(self.binding)
        self.claim={'admission_id':self.scope['admission_id'],'fence':1,'claim_id':'claim-fixture','expires_monotonic_ms':time.monotonic()*1000+15000,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
        self.dispatch=RecoveryDispatch(self.store)
        self.provider=Mock();self.response={'id':'fixture','choices':[{'index':0,'message':{'role':'assistant','content':'fixture answer'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':5,'completion_tokens':3,'cost':.001}}
        self.provider.complete.return_value=self.response
        self.gateway=ModelGateway(self.provider,self.store,'fixture-token')
        self.payload={'model':self.gateway.model_id,'messages':[{'role':'user','content':'fixture'}]}

    def metadata(self,kind='provider',effect_id='effect-fixture',payload=None):
        effect={'kind':kind,'effect_id':effect_id,'request_sha':digest(self.payload if payload is None else payload)}
        authorization=self.dispatch.authorize(self.binding,self.claim,effect,self.scope['scope_token'])
        return {'binding':self.binding,'claim':self.claim,'effect':effect,'scope_token':self.scope['scope_token'],**authorization}

    def test_gateway_success_backend_loss_reuses_response(self):
        metadata=self.metadata();status,result=self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)
        self.assertEqual(status,200);self.assertEqual(result,self.response)
        self.store.close();self.store=Store(self.path);self.gateway.store=self.store
        status,repeated=self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)
        self.assertEqual((status,repeated),(200,self.response));self.assertEqual(self.provider.complete.call_count,1)
        proof=RecoveryRecords(self.store).effect_result(metadata['admission_id'])
        self.assertEqual(proof['result'],self.response);self.assertTrue(proof['usage']['settled'])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM usage').fetchone()[0],1)

    def test_unknown_provider_keeps_original_reservation(self):
        metadata=self.metadata();self.provider.complete.side_effect=TimeoutError('PRIVATE')
        self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],422)
        self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],422)
        self.assertEqual(self.provider.complete.call_count,1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],1)
        self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'],'uncertain')

    def test_compaction_uses_scoped_receipts(self):
        self.payload={'model':self.gateway.model_id,'messages':[{'role':'system','content':'Your task is to create a detailed summary of the conversation so far, fixture.'},{'role':'user','content':'fixture history'}]}
        metadata=self.metadata('compaction');self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],200)
        self.assertEqual(self.records.effect_result(metadata['admission_id'])['result'],self.response)

    def test_pause_between_admission_and_dispatch_blocks_call(self):
        for key in ('paused','sleeping','restore_recovery_required'):
            metadata=self.metadata(effect_id='effect-'+key);self.store.set_state(key,'1')
            self.assertNotEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],200)
            self.store.set_state(key,'0')
        self.provider.complete.assert_not_called()

    def test_duplicate_confirmed_tool_returns_exact_result(self):
        args={'limit':1};request={'name':'recent_activity','arguments':args};metadata=self.metadata('tool',payload=request)
        callback=Mock(return_value='saved result')
        first=self.dispatch.run_tool(metadata,'recent_activity',args,callback)
        second=self.dispatch.run_tool(metadata,'recent_activity',args,callback)
        self.assertEqual((first,second),('saved result','saved result'));self.assertEqual(callback.call_count,1)

    def test_unknown_tool_does_not_execute_twice(self):
        args={};metadata=self.metadata('tool',payload={'name':'recent_activity','arguments':args})
        callback=Mock(side_effect=TimeoutError('PRIVATE'))
        for _ in range(2):
            with self.assertRaises(Exception):self.dispatch.run_tool(metadata,'recent_activity',args,callback)
        self.assertEqual(callback.call_count,1)

    def test_changed_content_or_owner_revision_cannot_dispatch(self):
        metadata=self.metadata();self.payload['messages'][0]['content']='changed'
        self.assertNotEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],200)
        self.payload['messages'][0]['content']='fixture';self.store.set_state('owner_control_revision','1')
        self.assertNotEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],200)
        self.provider.complete.assert_not_called()

    def test_http_scope_and_effect_metadata_never_reach_provider(self):
        import urllib.request
        from andrewbot.gateway import make_server,GatewayThread
        server=GatewayThread(make_server(self.gateway,port=0));server.start();self.addCleanup(server.close)
        base='http://127.0.0.1:'+str(server.server.server_port)
        effect={'kind':'provider','effect_id':'http-effect','request_sha':digest(self.payload)}
        def post(path,value,metadata=None):
            headers={'Authorization':'Bearer fixture-token','Content-Type':'application/json'}
            if metadata is not None:headers['X-Andrew-Recovery']=canonical(metadata)
            request=urllib.request.Request(base+path,canonical(value).encode(),headers,method='POST')
            with urllib.request.urlopen(request,timeout=5) as response:return json.load(response)
        admitted=post('/v1/recovery/authorize',{'binding':self.binding,'claim':self.claim,'effect':effect,'scope_token':self.scope['scope_token']})
        metadata={'binding':self.binding,'claim':self.claim,'effect':effect,'scope_token':self.scope['scope_token'],**admitted}
        self.assertEqual(post('/v1/chat/completions',self.payload,metadata),self.response)
        proof=post('/v1/recovery/result',{'binding':self.binding,'claim':self.claim,'scope_token':self.scope['scope_token'],'admission_id':admitted['admission_id']})
        self.assertEqual(proof['result'],self.response)
        self.assertNotIn(self.scope['scope_token'],canonical(self.provider.complete.call_args.args[0]))
        self.assertNotIn('scope_token',canonical(self.provider.complete.call_args.args[0]))

    def test_expired_execution_lease_cannot_dispatch_delayed_request(self):
        metadata=self.metadata();metadata['claim']={**self.claim,'expires_monotonic_ms':0,'boot_id':'expired-boot'}
        self.assertNotEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],200)
        self.provider.complete.assert_not_called()

    def test_missing_usage_does_not_create_recovery_completion_proof(self):
        metadata=self.metadata();self.provider.complete.return_value={key:value for key,value in self.response.items() if key!='usage'}
        self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],422)
        self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'],'uncertain')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],1)

    def test_private_reasoning_and_unknown_metadata_are_not_durable(self):
        metadata=self.metadata()
        self.provider.complete.return_value={**self.response,'reasoning':'PRIVATE-ROOT','headers':{'Authorization':'PRIVATE-HEADER'},
            'choices':[{'index':0,'message':{'role':'assistant','content':'fixture answer','reasoning_content':'PRIVATE-MESSAGE'},'finish_reason':'stop','reasoning':'PRIVATE-CHOICE'}]}
        status,result=self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)
        self.assertEqual(status,200)
        proof=self.records.effect_result(metadata['admission_id'])
        self.assertNotIn('PRIVATE',canonical(proof));self.assertNotIn('PRIVATE',canonical(result))

    def test_unicode_keys_have_the_same_digest_as_javascript(self):
        import subprocess
        value={'\U00010000':'astral','\ue000':'bmp','10':1,'2':.000001,'float':1.25e-7,'negative_zero':-0.0}
        script="import {digest} from './letta_recovery/contracts.mjs';let s='';for await(const part of process.stdin)s+=part;process.stdout.write(digest(JSON.parse(s)));"
        from pathlib import Path
        root=Path(__file__).resolve().parents[2]
        actual=subprocess.check_output(['node','--input-type=module','-e',script],cwd=root,input=json.dumps(value).encode()).decode()
        self.assertEqual(digest(value),actual)
