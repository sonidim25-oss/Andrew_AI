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
from andrewbot.appserver import AppServerClient, LettaCommandError
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
        capability=client.request({'type':'recovery_capabilities','contract':'local_durable_recovery_v1','version':1},'recovery_capabilities_response')
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
        return self.fixture.client.request({'type':kind,'contract':'local_durable_recovery_v1','version':1,**fields},kind+'_response',timeout=10)

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

    def test_connections_have_separate_claims_and_disconnect_releases_only_owner(self):
        # Opening a second connection before the first input must not create a second store identity.
        peer=AppServerClient(self.fixture.url,timeout=3)
        self.addCleanup(peer.close)
        peer_capability=peer.request({'type':'recovery_capabilities','contract':'local_durable_recovery_v1','version':1},'recovery_capabilities_response')
        binding,input,scope=self.prepare()
        self.assertEqual(peer_capability['store_id'],binding['store_id'])
        admitted=self.command('recovery_admit',binding=binding,input=input,**scope)
        claim=self.command('recovery_claim',binding=binding,expected_revision=admitted['revision'],**scope)
        self.assertTrue(claim['success'],claim)
        for kind in ('recovery_renew','recovery_release','recovery_revoke','recovery_ack','recovery_resume'):
            with self.assertRaises(LettaCommandError):
                peer.request({'type':kind,'contract':'local_durable_recovery_v1','version':1,'binding':binding,**scope,
                    'claim_id':claim['claim_id'],'fence':claim['fence'],
                    **({'attempt_id':'foreign-attempt','expected_revision':claim['revision']} if kind=='recovery_resume' else
                       {'terminal_id':'foreign-terminal','consumer_id':binding['consumer_id'],'expected_revision':claim['revision']} if kind=='recovery_ack' else {})},kind+'_response')
        peer.close()
        # A response from a fresh connection also proves the earlier close reached the server.
        peer=AppServerClient(self.fixture.url,timeout=3)
        self.addCleanup(peer.close)
        peer.request({'type':'recovery_capabilities','contract':'local_durable_recovery_v1','version':1},'recovery_capabilities_response')
        renewed=self.command('recovery_renew',binding=binding,claim_id=claim['claim_id'],fence=claim['fence'],**scope)
        self.assertTrue(renewed['success'],renewed)
        self.fixture.client.close()
        import time
        deadline=time.monotonic()+5
        while True:
            inspected=peer.request({'type':'recovery_inspect','contract':'local_durable_recovery_v1','version':1,'binding':binding},'recovery_inspect_response')
            try:
                replacement=peer.request({'type':'recovery_claim','contract':'local_durable_recovery_v1','version':1,'binding':binding,**scope,'expected_revision':inspected['revision']},'recovery_claim_response')
                break
            except LettaCommandError:
                self.assertLess(time.monotonic(),deadline)
                time.sleep(.03)
        self.assertGreater(replacement['fence'],claim['fence'])
        self.assertEqual(self.fixture.provider_calls,[])

    def test_pristine_rollback_starts_only_on_isolated_pre_admission_snapshot(self):
        import shutil
        from andrewbot.store import Store
        from andrewbot.memory_backup import create_paired_recovery_snapshot
        spec=importlib.util.spec_from_file_location('rollback_installer',ROOT/'scripts/install-letta-recovery.py')
        installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
        binding,input,scope=self.prepare();self.fixture.kill();self.fixture.store.close()
        snapshot=Path(self.tmp.name)/'retained-before'
        create_paired_recovery_snapshot(self.fixture.root/'controller.sqlite3',self.fixture.root/'backend',snapshot,workers_quiescent=True)
        installer.assert_rollback_exclusion(snapshot)
        self.fixture.store=Store(self.fixture.root/'controller.sqlite3');self.fixture.model_gateway.store=self.fixture.store
        self.fixture.restart()
        binding['store_id']=self.command('recovery_capabilities')['store_id']
        scope=RecoveryRecords(self.fixture.store).admit_scope(binding)
        self.command('recovery_admit',binding=binding,input=input,**scope)
        live=self.fixture.root/'backend';before=installer.tree_hashes(live)
        self.fixture.kill()
        isolated=Path(self.tmp.name)/'isolated-old';shutil.copytree(snapshot/'backend',isolated)
        pristine=Path(os.environ.get('ANDREW_LETTA_PRISTINE_BINARY','/tmp/andrew-letta-protocol-runtime/node_modules/@letta-ai/letta-code/letta.js')).parent
        if not (pristine/'letta.js').is_file():self.fail('Pinned pristine runtime required for rollback acceptance')
        self.assertEqual(installer.sha(pristine/'letta.js'),json.loads((ROOT/'letta_recovery/base.json').read_text())['sha256'])
        self.fixture.release=pristine
        self.fixture.environment.update(LETTA_LOCAL_BACKEND_DIR=str(isolated),ANDREW_LOCAL_RECOVERY_ARTIFACT='0')
        self.fixture.restart()
        # Normal handshake and read access cannot pump the retained new-contract input.
        self.fixture.client.request({'type':'conversation_messages_list','conversation_id':binding['conversation_id'],
            'query':{'limit':100,'order':'desc'}},'conversation_messages_list_response')
        self.assertEqual(self.fixture.provider_calls,[])
        self.assertEqual(installer.tree_hashes(live),before)

    def test_coordinator_runs_sequential_autonomy_and_owner_turns_in_same_conversation(self):
        from andrewbot.appserver import LettaAgent
        from andrewbot.recovery import RecoveryCoordinator
        store=self.fixture.store
        agent=LettaAgent(store,'fixture-token',url=self.fixture.url)
        controller=Controller(store,agent,17,autonomous_enabled=True)
        coordinator=RecoveryCoordinator(store,controller,enabled=True)
        agent.local_recovery=controller.local_recovery=coordinator
        agent.tool_handler=controller.execute_tool
        self.fixture.model_gateway.recovery_tool_handler=controller.execute_tool
        # Fixture gateway uses a random loopback port instead of the service port.
        with __import__('unittest.mock',fromlist=['patch']).patch.object(AppServerClient,'ensure_provider',
                lambda client,token:client.request({'type':'connect_provider','target':'local','provider_id':'openai-compatible',
                    'fields':{'apiKey':token,'baseUrl':f'http://127.0.0.1:{self.fixture.gateway.server.server_port}/v1'}},'connect_provider_response')):
            agent.initialize()
            runtime=agent.agent_id,agent.conversation_id
            runner=SessionRunner(store,controller,agent,enabled=True)
            session=runner.start('owner_wake',datetime.now(timezone.utc))
            turn=agent.autonomous_turn;errors=[]
            def checked_turn(*args,**kwargs):
                try:return turn(*args,**kwargs)
                except Exception as error:errors.append((type(error).__name__,str(error)));raise
            agent.autonomous_turn=checked_turn
            self.assertTrue(runner.run_cycle(session),errors)
            self.responses.append({'id':'owner-response','choices':[{'index':0,'message':{'role':'assistant','content':'Owner answer.'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}})
            reply,_=agent.reply([{'role':'user','content':'An ordinary owner question.'}])
            self.assertEqual(reply,'Owner answer.')
            self.responses.append({'id':'next-response','choices':[{'index':0,'message':{'role':'assistant','content':'Next autonomous answer.'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}})
            self.assertTrue(runner.run_cycle(session))
            self.assertEqual((agent.agent_id,agent.conversation_id),runtime)
            self.assertEqual(len(self.fixture.provider_calls),3)
            history=json.dumps(self.fixture.provider_calls[-1]['messages'])
            self.assertEqual(history.count('Owner answer.'),1)
            self.assertEqual(history.count('Persisted fixture answer.'),1)
            self.assertFalse(coordinator.has_pending())
            agent.initialize()  # Exact startup settings remain a no-op on the adopted conversation.

    def coordinated_agent(self):
        from andrewbot.appserver import LettaAgent
        from andrewbot.recovery import RecoveryCoordinator
        from unittest.mock import patch
        agent=LettaAgent(self.fixture.store,'fixture-token',url=self.fixture.url)
        controller=Controller(self.fixture.store,agent,17,autonomous_enabled=True)
        coordinator=RecoveryCoordinator(self.fixture.store,controller,enabled=True)
        agent.local_recovery=controller.local_recovery=coordinator
        agent.tool_handler=controller.execute_tool
        self.fixture.model_gateway.recovery_tool_handler=controller.execute_tool
        provider=patch.object(AppServerClient,'ensure_provider',lambda client,token:client.request(
            {'type':'connect_provider','target':'local','provider_id':'openai-compatible',
             'fields':{'apiKey':token,'baseUrl':f'http://127.0.0.1:{self.fixture.gateway.server.server_port}/v1'}},'connect_provider_response'))
        provider.start();self.addCleanup(provider.stop)
        agent.initialize()
        return agent,controller,coordinator,SessionRunner(self.fixture.store,controller,agent,enabled=True)

    def test_owner_sleep_during_provider_call_does_not_block_later_turns(self):
        # Production 2026-10-22 02:15: /sleep cancelled an autonomous turn whose provider
        # result the controller saved; the backend checkpoint stayed provider_admitted.
        import threading
        agent,controller,coordinator,runner=self.coordinated_agent();store=self.fixture.store
        entered,release=threading.Event(),threading.Event()
        class Held(list):
            first=True
            def pop(self,index=-1):
                if Held.first:
                    Held.first=False;entered.set();release.wait(10)
                return super().pop(index)
        self.responses=Held(self.responses)
        session=runner.start('owner_wake',datetime.now(timezone.utc))
        cycle=threading.Thread(target=lambda:runner.run_cycle(session),daemon=True);cycle.start()
        self.assertTrue(entered.wait(10))
        owner={'from':{'id':17},'chat':{'id':17,'type':'private'}}
        controller.handle_update({'message':{**owner,'text':'/sleep'}})
        release.set();cycle.join(15);self.assertFalse(cycle.is_alive())
        self.assertEqual(len(self.fixture.provider_calls),1)
        controller.handle_update({'message':{**owner,'text':'/wake'}})
        self.responses.append({'id':'owner-response','choices':[{'index':0,'message':{'role':'assistant','content':'Owner answer after sleep.'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}})
        reply,_=agent.reply([{'role':'user','content':'Are you awake?'}])
        self.assertEqual(reply,'Owner answer after sleep.')
        self.assertEqual(len(self.fixture.provider_calls),2)
        self.assertFalse(coordinator.has_pending())

    def test_owner_pause_during_provider_call_is_closed_while_paused(self):
        self.close_paused_provider()

    def test_owner_cancel_closure_retries_after_evidence_transport_failure(self):
        self.close_paused_provider('recovery_reconcile_provider')

    def test_owner_cancel_closure_retries_after_yield_transport_failure(self):
        self.close_paused_provider('recovery_yield')

    def test_owner_cancel_closure_retries_after_terminal_ack_failure(self):
        self.close_paused_provider('ack')

    def test_owner_cancel_closure_recovers_persistent_evidence_scope_after_restart(self):
        self.close_paused_provider(interrupted_scope='evidence')

    def test_owner_cancel_closure_recovers_persistent_yield_scope_after_restart(self):
        self.close_paused_provider(interrupted_scope='yielding')

    def test_owner_cancelled_provider_tool_calls_are_cancelled_before_next_turn(self):
        self.close_paused_provider(tool_calls=True)

    def test_owner_cancel_closure_skips_newer_never_admitted_revoked_binding(self):
        self.close_paused_provider(newer_revoked=True)

    def close_paused_provider(self, fail_command=None, interrupted_scope=None, tool_calls=False, newer_revoked=False):
        import threading
        agent,controller,coordinator,runner=self.coordinated_agent();store=self.fixture.store
        entered,release=threading.Event(),threading.Event()
        class Held(list):
            first=True
            def pop(self,index=-1):
                if Held.first:
                    Held.first=False;entered.set();release.wait(10)
                return super().pop(index)
        if tool_calls:
            self.responses[0]={'id':'cancelled-provider-tools','choices':[{'index':0,'message':{'role':'assistant','content':None,
                'tool_calls':[{'id':'cancelled-activity','type':'function','function':{'name':'recent_activity','arguments':'{}'}}]},
                'finish_reason':'tool_calls'}],'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}}
        self.responses=Held(self.responses)
        session=runner.start('owner_wake',datetime.now(timezone.utc))
        cycle=threading.Thread(target=lambda:runner.run_cycle(session),daemon=True);cycle.start()
        self.assertTrue(entered.wait(10))
        owner={'from':{'id':17},'chat':{'id':17,'type':'private'}}
        controller.handle_update({'message':{**owner,'text':'/pause'}})
        release.set();cycle.join(15);self.assertFalse(cycle.is_alive())
        row=store.connection.execute('SELECT binding_json FROM recovery_attempts ORDER BY rowid DESC LIMIT 1').fetchone()
        binding=json.loads(row[0])
        self.assertEqual(self.command('recovery_inspect',binding=binding)['phase'],'provider_admitted')
        # The dispatch pass closes it while Andrew stays paused; nothing is dispatched.
        if newer_revoked:
            # A later attempt lost admission against this open checkpoint. Its
            # revoked controller-only row must not hide the real backend turn.
            later={**binding,'session_id':'unadmitted-session','cycle_id':'unadmitted-cycle',
                   'client_message_id':'unadmitted-input','consumer_id':'unadmitted-consumer'}
            with store.connection:
                store.connection.execute('''INSERT INTO recovery_attempts
                    (admission_id,binding_sha,binding_json,token_sha,state,created_at,origin)
                    VALUES(?,?,?,?,'revoked',?,'autonomous')''',
                    ('unadmitted-scope',digest(later),json.dumps(later),'0'*64,store.now()))
        if interrupted_scope:
            coordinator.records.admit_scope(binding,evidence_only=interrupted_scope=='evidence',yield_only=interrupted_scope=='yielding')
            from andrewbot.recovery import RecoveryCoordinator
            coordinator=RecoveryCoordinator(store,controller,coordinator.client_factory,enabled=True)
        if fail_command:
            client=coordinator.client_factory()
            original=client.command
            def fail_once(kind, **fields):
                if kind==fail_command:raise TimeoutError('fixture transport interruption')
                return original(kind, **fields)
            if fail_command=='ack':
                def fail_ack(*args, **kwargs):raise TimeoutError('fixture acknowledgement interruption')
                client.ack=fail_ack
            else:client.command=fail_once
            try:
                with self.assertRaises(TimeoutError):coordinator.close_superseded(client)
            finally:client.close()
            self.assertFalse(coordinator.has_pending(), 'Failed evidence/yield scopes must not prevent retry')
        self.assertTrue(coordinator.close_superseded())
        closed=self.command('recovery_inspect',binding=binding)
        self.assertEqual(closed['phase'],'terminal_committed')
        self.assertEqual(closed['terminal']['stop_reason'],'owner_input_pending')
        self.assertTrue(closed['terminal']['acknowledged']);self.assertTrue(closed['terminal']['settled'])
        self.assertEqual(closed['terminal']['cancelled_admission_ids'],[])
        if tool_calls:
            self.assertEqual(store.connection.execute("SELECT count(*) FROM recovery_effects WHERE kind='tool'").fetchone()[0],0)
        self.assertFalse(coordinator.close_superseded())
        self.assertEqual(len(self.fixture.provider_calls),1)
        self.assertEqual(store.get_state('paused'),'1');self.assertFalse(coordinator.has_pending())
        controller.handle_update({'message':{**owner,'text':'/wake'}})
        self.responses.append({'id':'owner-response','choices':[{'index':0,'message':{'role':'assistant','content':'Owner answer after pause.'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}})
        reply,_=agent.reply([{'role':'user','content':'Back?'}])
        self.assertEqual(reply,'Owner answer after pause.');self.assertEqual(len(self.fixture.provider_calls),2)
        if tool_calls:
            results=[message for message in self.fixture.provider_calls[-1]['messages'] if message.get('role')=='tool' and message.get('tool_call_id')=='cancelled-activity']
            self.assertEqual(len(results),1)
            self.assertFalse(json.loads(results[0]['content'])['executed'])

    def test_invalid_legacy_tool_name_can_be_closed_without_retrying_provider(self):
        from andrewbot.model import ProviderRequestError
        agent,controller,coordinator,runner=self.coordinated_agent();store=self.fixture.store
        class InvalidFirst(list):
            first=True
            def pop(self,index=-1):
                if self.first:
                    self.first=False
                    raise ProviderRequestError('http_error',400)
                return super().pop(index)
        self.responses=InvalidFirst(self.responses)
        session=runner.start('owner_wake',datetime.now(timezone.utc))
        self.assertFalse(runner.run_cycle(session))
        self.assertEqual(len(self.fixture.provider_calls),1)
        owner={'from':{'id':17},'chat':{'id':17,'type':'private'}}
        controller.handle_update({'message':{**owner,'text':'/pause'}})
        row=store.connection.execute('SELECT * FROM recovery_attempts ORDER BY rowid DESC LIMIT 1').fetchone()
        binding=json.loads(row['binding_json'])
        effect=store.connection.execute("SELECT * FROM recovery_effects WHERE binding_sha=? AND kind='provider'",(row['binding_sha'],)).fetchone()
        directory=next(self.fixture.root.rglob('local-recovery-v1'))
        checkpoint=next(path for path in (directory/'conversations').glob('*.json') if len(path.stem)==64)
        snapshot=json.loads(checkpoint.read_text())
        request=json.loads((directory/'blobs'/checkpoint.stem/snapshot['steps'][-1]['request_ref']).read_text())
        activity=store.connection.execute("SELECT id FROM activity WHERE event='Letta model gateway provider request failed (category=http_error, http_status=400)' ORDER BY id DESC LIMIT 1").fetchone()[0]
        evidence={'http_status':400,'type':'invalid_request_error','code':'invalid_value','parameter':'tools[9].name',
                  'pattern':'^[a-zA-Z0-9_-]+$','diagnostic_sha':'d'*64,'activity_id':activity,'adapter':'unmapped_tool_names_v1'}
        coordinator.records.revoke(row['admission_id'])
        proof=coordinator.records.reject_invalid_tool_names(effect['admission_id'],request,evidence)
        self.assertFalse(proof['retryable'])
        coordinator.records.revoke(row['admission_id'])
        self.assertTrue(coordinator.close_superseded())
        state=self.command('recovery_inspect',binding=binding)
        self.assertEqual(state['phase'],'terminal_committed');self.assertTrue(state['terminal']['acknowledged'])
        self.assertTrue(state['terminal']['settled']);self.assertFalse(coordinator.has_pending())
        self.assertEqual(len(self.fixture.provider_calls),1)
        controller.handle_update({'message':{**owner,'text':'/wake'}})
        reply,_=agent.reply([{'role':'user','content':'Fresh turn after permanent rejection.'}])
        self.assertEqual(reply,'Persisted fixture answer.');self.assertEqual(len(self.fixture.provider_calls),2)

    def test_owner_arriving_during_autonomy_yields_without_replaying_prepared_tool(self):
        agent,controller,coordinator,runner=self.coordinated_agent();store=self.fixture.store
        self.responses.insert(0,{'id':'interrupted-step','choices':[{'index':0,'message':{'role':'assistant','content':None,
            'tool_calls':[{'id':'not-executed','type':'function','function':{'name':'recent_activity','arguments':'{}'}}]},
            'finish_reason':'tool_calls'}],'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}})
        update={'update_id':991,'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'Answer this owner question'}}
        original=self.fixture.model_gateway.provider.complete
        def owner_arrives(payload):
            result=original(payload)
            if len(self.fixture.provider_calls)==1:store.enqueue_update(update)
            return result
        self.fixture.model_gateway.provider.complete=owner_arrives
        for index in range(10):store.reserve_usage('provider-'+format(index,'064x'),'2026-10','2026-10-17',.01)
        session=runner.start('owner_wake',datetime.now(timezone.utc))
        self.assertFalse(runner.run_cycle(session))
        self.assertEqual(store.get_state('autonomy_error',''),'')
        self.assertFalse(coordinator.has_pending())
        old=store.connection.execute('SELECT * FROM model_turn_receipts').fetchone()
        self.assertEqual(old['state'],'accepted')
        self.assertEqual(store.connection.execute('SELECT status FROM cycles').fetchone()[0],'stopped')
        self.assertEqual(store.connection.execute("SELECT count(*) FROM recovery_effects WHERE kind='tool'").fetchone()[0],0)
        self.assertTrue(store.claim_update(update['update_id']))
        reply=controller.handle_update(update)
        self.assertEqual(reply,'Persisted fixture answer.')
        store.complete_update(update['update_id'])
        self.assertEqual(len(self.fixture.provider_calls),2)
        self.assertEqual(store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],10)
        self.assertIn('owner_input_pending',json.dumps(self.fixture.provider_calls[-1]['messages']))

    def test_owner_diary_without_autonomous_session_returns_rejection_and_answer(self):
        agent,controller,coordinator,runner=self.coordinated_agent();store=self.fixture.store
        self.responses.insert(0,{'id':'owner-diary-request','choices':[{'index':0,'message':{'role':'assistant','content':None,
            'tool_calls':[{'id':'diary-without-session','type':'function','function':{'name':'save_diary_entry',
                'arguments':json.dumps({'content':'A fixture diary that must not be saved.'})}}]},
            'finish_reason':'tool_calls'}],'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}})
        self.assertEqual(store.get_state('active_session_id',''),'')
        reply=controller.handle_update({'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'Please answer my new question.'}})
        self.assertEqual(reply,'Persisted fixture answer.')
        self.assertFalse(coordinator.has_pending())
        self.assertEqual(store.get_state('autonomy_error',''),'')
        self.assertEqual(store.connection.execute('SELECT count(*) FROM diary_entries').fetchone()[0],0)
        self.assertEqual(len(self.fixture.provider_calls),2)
        proof=store.connection.execute("SELECT r.result_json FROM recovery_results r JOIN recovery_effects e ON e.admission_id=r.admission_id WHERE e.kind='tool'").fetchone()
        rejection=json.loads(json.loads(proof[0]))
        self.assertEqual(rejection['status'],'error')
        self.assertEqual(rejection['error'],'diary_requires_autonomous_session')
        self.assertFalse(rejection['executed'])

    def test_owner_arriving_with_final_response_follows_a_genuinely_completed_turn(self):
        agent,controller,coordinator,runner=self.coordinated_agent();store=self.fixture.store
        self.responses.append({'id':'owner-answer','choices':[{'index':0,'message':{'role':'assistant','content':'Answer to owner.'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}})
        update={'update_id':993,'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'Owner question at completion'}}
        original=self.fixture.model_gateway.provider.complete
        def arriving(payload):
            result=original(payload)
            if len(self.fixture.provider_calls)==1:store.enqueue_update(update)
            return result
        self.fixture.model_gateway.provider.complete=arriving
        session=runner.start('owner_wake',datetime.now(timezone.utc))
        self.assertTrue(runner.run_cycle(session))
        self.assertEqual(store.connection.execute('SELECT state FROM model_turn_receipts').fetchone()[0],'completed')
        self.assertFalse(coordinator.has_pending());self.assertEqual(store.get_state('autonomy_error',''),'')
        self.assertTrue(store.claim_update(update['update_id']))
        self.assertEqual(controller.handle_update(update),'Answer to owner.')
        self.assertEqual(len(self.fixture.provider_calls),2)

    def test_existing_owner_blocked_checkpoint_can_handoff_with_legacy_error_preserved(self):
        agent,controller,coordinator,runner=self.coordinated_agent();store=self.fixture.store
        self.responses.insert(0,{'id':'interrupted-step','choices':[{'index':0,'message':{'role':'assistant','content':None,
            'tool_calls':[{'id':'not-executed','type':'function','function':{'name':'recent_activity','arguments':'{}'}}]},
            'finish_reason':'tool_calls'}],'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}})
        update={'update_id':992,'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'Answer this queued question'}}
        original=self.fixture.model_gateway.provider.complete
        def owner_arrives(payload):
            result=original(payload)
            if len(self.fixture.provider_calls)==1:
                attempt=store.begin_cycle_tool(store.research_tool_context(),'python.run')
                store.finish_cycle_tool(attempt,False);store.enqueue_update(update)
            return result
        self.fixture.model_gateway.provider.complete=owner_arrives
        gateway_request=self.fixture.model_gateway.recovery_request
        self.fixture.model_gateway.recovery_request=lambda path,payload: (423,{'error':{'message':'old backend has no handoff'}}) if path.endswith('/owner_yield') else gateway_request(path,payload)
        session=runner.start('owner_wake',datetime.now(timezone.utc))
        self.assertFalse(runner.run_cycle(session))
        self.assertEqual(store.get_state('autonomy_error'),'local_recovery_interrupted')
        with store.connection:store.connection.execute("UPDATE recovery_attempts SET state='held',hold_reason='provider_uncertain'")
        self.fixture.model_gateway.recovery_request=gateway_request
        self.assertTrue(coordinator.reconcile_one())
        self.assertEqual(store.get_state('autonomy_error'),'')
        self.assertFalse(coordinator.has_pending())
        self.assertEqual(store.connection.execute("SELECT outcome FROM cycle_tool_attempts WHERE tool_name='python.run'").fetchone()[0],'uncertain')
        self.assertEqual(store.connection.execute('SELECT state FROM model_turn_receipts').fetchone()[0],'accepted')
        self.assertTrue(store.claim_update(update['update_id']))
        self.assertEqual(controller.handle_update(update),'Persisted fixture answer.')
        self.assertEqual(len(self.fixture.provider_calls),2)

    def test_late_provider_receipt_recovers_without_second_request(self):
        self.late_provider_receipt_scenario()

    def test_late_provider_receipt_import_preserves_newer_pause_without_dispatch(self):
        self.late_provider_receipt_scenario(paused=True)

    def test_owner_handoff_after_late_receipt_keeps_failed_tools_and_pause(self):
        self.late_provider_receipt_scenario(paused=True,owner_handoff=True)

    def late_provider_receipt_scenario(self,*,paused=False,owner_handoff=False):
        import threading
        agent,controller,coordinator,runner=self.coordinated_agent();store=self.fixture.store
        original=self.fixture.model_gateway.complete
        waiting=threading.Event();finish=threading.Event();threads=[]
        self.addCleanup(finish.set)
        provider=self.fixture.model_gateway.provider.complete
        def delayed(payload):
            waiting.set()
            if not finish.wait(10):raise TimeoutError('fixture did not release result')
            return provider(payload)
        self.fixture.model_gateway.provider.complete=delayed
        def detached(authorization,payload,*,recovery=None):
            thread=threading.Thread(target=lambda:original(authorization,payload,recovery=recovery),daemon=True)
            threads.append(thread);thread.start()
            if not waiting.wait(5):raise TimeoutError('provider did not start')
            return 504,{'error':{'message':'fixture response transport timed out'}}
        self.fixture.model_gateway.complete=detached
        session=runner.start('owner_wake',datetime.now(timezone.utc))
        self.assertFalse(runner.run_cycle(session))
        self.assertEqual(store.get_state('autonomy_error'),'local_recovery_interrupted')
        self.assertFalse(coordinator.reconcile_one())
        finish.set()
        for thread in threads:thread.join(5);self.assertFalse(thread.is_alive())
        self.fixture.model_gateway.complete=original
        if paused:
            if owner_handoff:
                for _ in range(3):
                    attempt=store.begin_cycle_tool(store.research_tool_context(),'python.run')
                    store.finish_cycle_tool(attempt,False)
            controller._command('/pause')
            owner_revision=store.get_state('owner_control_revision')
            runner.recover()
        self.assertEqual(len(self.fixture.provider_calls),1)
        self.assertTrue(coordinator.reconcile_one())
        self.assertEqual(len(self.fixture.provider_calls),1)
        if paused:
            self.assertFalse(coordinator.reconcile_one())
            self.assertEqual(store.get_state('paused'),'1')
            self.assertEqual(store.get_state('owner_control_revision'),owner_revision)
            self.assertEqual(store.get_state('active_session_id'),'')
            self.assertTrue(coordinator.has_pending())
            if owner_handoff:
                # A new owner message can retire the old input without
                # declaring any failed program successful or executing it.
                controller.handle_update({'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},
                    'text':'Keep the failed attempts held; answer my new question when I wake.'}})
                self.assertTrue(coordinator.reconcile_one())
                self.assertFalse(coordinator.has_pending())
                self.assertEqual(len(self.fixture.provider_calls),1)
                self.assertEqual(store.get_state('paused'),'1')
                self.assertEqual(store.get_state('owner_control_revision'),owner_revision)
                self.assertEqual(store.get_state('active_session_id'),'')
                self.assertEqual(store.get_state('active_cycle_id',''),'')
                self.assertEqual(store.get_state('autonomy_error'),'local_recovery_interrupted')
                self.assertEqual(store.connection.execute("SELECT count(*) FROM cycle_tool_attempts WHERE tool_name='python.run' AND outcome='uncertain'").fetchone()[0],3)
                self.assertEqual(store.connection.execute('SELECT state FROM model_turn_receipts').fetchone()[0],'accepted')
                self.assertEqual(store.connection.execute('SELECT count(*) FROM recovery_owner_yields').fetchone()[0],1)
            return
        self.assertTrue(coordinator.reconcile_one())
        self.assertFalse(coordinator.has_pending())
        self.assertEqual(store.get_state('autonomy_error'),'')
        self.assertEqual(store.connection.execute('SELECT state FROM model_turn_receipts').fetchone()[0],'completed')
        self.assertEqual(len(self.fixture.provider_calls),1)

    def test_paused_handoff_cancellation_keeps_new_authority_and_makes_no_calls(self):
        agent,controller,coordinator,runner=self.coordinated_agent();store=self.fixture.store
        self.responses.insert(0,{'id':'interrupted-step','choices':[{'index':0,'message':{'role':'assistant','content':None,
            'tool_calls':[{'id':'not-executed','type':'function','function':{'name':'recent_activity','arguments':'{}'}}]},
            'finish_reason':'tool_calls'}],'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}})
        update={'update_id':996,'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'Saved owner question'}}
        original=self.fixture.model_gateway.provider.complete
        def arrives(payload):
            result=original(payload)
            if len(self.fixture.provider_calls)==1:store.enqueue_update(update)
            return result
        self.fixture.model_gateway.provider.complete=arrives
        request=self.fixture.model_gateway.recovery_request
        self.fixture.model_gateway.recovery_request=lambda path,payload: (423,{'error':{'message':'old backend'}}) if path.endswith('/owner_yield') else request(path,payload)
        session=runner.start('owner_wake',datetime.now(timezone.utc))
        self.assertFalse(runner.run_cycle(session))
        self.assertTrue(store.claim_update(996));controller.handle_update(update);store.complete_update(996)
        controller.handle_update({'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'/pause'}})
        self.fixture.model_gateway.recovery_request=request
        runner.recover()
        revision=store.get_state('owner_control_revision')
        self.assertTrue(coordinator.reconcile_one())
        self.assertFalse(coordinator.has_pending())
        self.assertEqual(store.get_state('paused'),'1')
        self.assertEqual(store.get_state('owner_control_revision'),revision)
        self.assertEqual(store.get_state('active_session_id'),'')
        self.assertEqual(store.get_state('active_cycle_id',''),'')
        self.assertEqual(store.get_state('autonomy_error'),'local_recovery_interrupted')
        self.assertEqual(len(self.fixture.provider_calls),1)
        self.assertEqual(store.connection.execute("SELECT count(*) FROM recovery_effects WHERE kind='tool'").fetchone()[0],0)
        self.assertEqual(store.connection.execute('SELECT state FROM model_turn_receipts').fetchone()[0],'accepted')
        controller.handle_update({'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'/wake'}})
        self.assertEqual(controller.flush_queued(),'Persisted fixture answer.')
        self.assertEqual(len(self.fixture.provider_calls),2)

    def test_owner_arrives_between_authorization_and_dispatch(self):
        for kind in ('provider','tool'):
            with self.subTest(kind=kind):
                if kind=='tool':
                    self.fixture.stop();self.setUp()
                agent,controller,coordinator,runner=self.coordinated_agent();store=self.fixture.store
                if kind=='tool':
                    self.responses.insert(0,{'id':'tool-choice','choices':[{'index':0,'message':{'role':'assistant','content':None,
                        'tool_calls':[{'id':'undispatched','type':'function','function':{'name':'recent_activity','arguments':'{}'}}]},'finish_reason':'tool_calls'}],
                        'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}})
                update={'update_id':995,'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'Owner arrives before dispatch'}}
                original=self.fixture.model_gateway.recovery_request
                def arriving(path,payload):
                    result=original(path,payload)
                    if path.endswith('/authorize') and payload['effect']['kind']==kind and result[0]==200:store.enqueue_update(update)
                    return result
                self.fixture.model_gateway.recovery_request=arriving
                session=runner.start('owner_wake',datetime.now(timezone.utc))
                self.assertFalse(runner.run_cycle(session))
                self.assertEqual(store.get_state('autonomy_error',''),'')
                self.assertFalse(coordinator.has_pending())
                self.assertEqual(store.connection.execute("SELECT count(*) FROM recovery_effects WHERE state='cancelled'").fetchone()[0],1)
                self.assertEqual(store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],0)
                self.assertEqual(len(self.fixture.provider_calls),1 if kind=='tool' else 0)
                self.fixture.model_gateway.recovery_request=original
                self.assertTrue(store.claim_update(update['update_id']))
                self.assertEqual(controller.handle_update(update),'Persisted fixture answer.')
                self.assertEqual(len(self.fixture.provider_calls),2 if kind=='tool' else 1)

    def test_first_journaled_turn_preserves_legacy_history_tombstones_costs_and_held_messages(self):
        agent,controller,coordinator,runner=self.coordinated_agent()
        runtime=self.fixture.client.start_runtime(agent.agent_id,agent.conversation_id,controller_tools=False)
        self.assertEqual(self.fixture.client.send_user_turn(runtime,'A historical fixture question.'),'Persisted fixture answer.')
        self.fixture.restart()  # Existing history after the installation's backend restart.
        self.assertEqual(len(self.fixture.provider_calls),1)
        ledgers=list(self.fixture.root.rglob('input-dispositions-v2.json'))
        self.assertEqual(len(ledgers),1)
        before=ledgers[0].read_bytes();self.assertTrue(json.loads(before)['entries'])
        store=self.fixture.store
        with store.connection:
            for index in range(10):
                store.connection.execute('INSERT INTO budget_reservations VALUES(?,?,?,?,?)',('provider-'+format(index,'064x'),'2026-10','2026-10-16',.01,'2026-10-16T00:00:00+00:00'))
        for index in range(15):store.add_message(17,'user','Historical held fixture '+str(index),queued=3)
        self.responses.append({'id':'new-journaled-response','choices':[{'index':0,'message':{'role':'assistant','content':'Distinct new work completed.'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}})
        session=runner.start('owner_wake',datetime.now(timezone.utc))
        errors=[];turn=agent.autonomous_turn
        def checked(*args,**kwargs):
            try:return turn(*args,**kwargs)
            except Exception as error:errors.append((type(error).__name__,str(error)));raise
        agent.autonomous_turn=checked
        self.assertTrue(runner.run_cycle(session),(errors,json.loads(before)))
        self.assertEqual(len(self.fixture.provider_calls),2)
        self.assertIn('Persisted fixture answer.',json.dumps(self.fixture.provider_calls[-1]['messages']))
        self.assertEqual(store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],10)
        self.assertEqual(store.uncertain_message_count(17),15)
        self.assertEqual(ledgers[0].read_bytes(),before)
        self.assertFalse(coordinator.has_pending())
        with self.assertRaises(LettaCommandError):self.fixture.client.start_runtime(agent.agent_id,agent.conversation_id,controller_tools=False)

    def test_first_turn_retains_large_compacted_transcript_outside_active_context(self):
        agent,controller,coordinator,runner=self.coordinated_agent()
        runtime=self.fixture.client.start_runtime(agent.agent_id,agent.conversation_id,controller_tools=False)
        self.fixture.client.send_user_turn(runtime,'Historical active fixture question.')
        self.fixture.kill()
        matches=[]
        for path in (self.fixture.root/'backend/conversations').glob('*/conversation.json'):
            value=json.loads(path.read_text())
            if value.get('agent_id')==agent.agent_id and value.get('id')==agent.conversation_id:matches.append(path)
        self.assertEqual(len(matches),1)
        path=matches[0].with_name('messages.jsonl');rows=[json.loads(line) for line in path.read_text().splitlines()]
        template=next(row['message'] for row in rows if row.get('message',{}).get('role')=='user')
        historical=[]
        for index in range(927):
            message={**template,'id':'historical-'+str(index),'content':[{'type':'text','text':'Inactive historical fixture '+str(index)}]}
            row={'type':'message','id':'entry-'+str(index),'parentId':historical[-1]['id'] if historical else None,
                 'timestamp':datetime.now(timezone.utc).isoformat(),'message':message}
            if index<114:row.update(type='compaction',summary='Historical fixture summary',firstKeptEntryId=None,tokensBefore=100)
            historical.append(row)
        original='\n'.join(json.dumps(row) for row in [rows[0],*historical,*rows[1:]])+'\n';path.write_text(original)
        self.fixture.restart()
        self.responses.append({'id':'fresh-after-compaction','choices':[{'index':0,'message':{'role':'assistant','content':'Fresh work after compaction.'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}})
        session=runner.start('owner_wake',datetime.now(timezone.utc))
        self.assertTrue(runner.run_cycle(session))
        self.assertEqual(len(self.fixture.provider_calls),2)
        self.assertNotIn('Inactive historical fixture',json.dumps(self.fixture.provider_calls[-1]['messages']))
        self.assertTrue(path.read_text().startswith(original))
        self.assertFalse(coordinator.has_pending())
        self.fixture.restart()
        history=self.fixture.client.request({'type':'conversation_messages_list','conversation_id':agent.conversation_id,
            'query':{'limit':100,'order':'desc'}},'conversation_messages_list_response')
        self.assertIn('Fresh work after compaction.',json.dumps(history))
        self.assertEqual(len(self.fixture.provider_calls),2)

    def test_projection_failed_input_is_retained_without_replay_then_fresh_wake_succeeds(self):
        import shutil
        from andrewbot.store import Store
        from andrewbot.memory_backup import create_paired_recovery_snapshot,verify_paired_recovery_snapshot
        spec=importlib.util.spec_from_file_location('retirement_fixture',ROOT/'scripts/retire_unstarted_projection.py')
        repair=importlib.util.module_from_spec(spec);spec.loader.exec_module(repair)
        agent,controller,coordinator,runner=self.coordinated_agent()
        runtime=self.fixture.client.start_runtime(agent.agent_id,agent.conversation_id,controller_tools=False)
        self.fixture.client.send_user_turn(runtime,'Historical active fixture question.')
        self.fixture.kill()
        # Reproduce the deployed writer's missing active-context baseline in isolation.
        fault_directory=tempfile.TemporaryDirectory();self.addCleanup(fault_directory.cleanup)
        fixed=self.fixture.release;broken=Path(fault_directory.name)/'broken-release';broken.mkdir()
        shutil.copy2(fixed/'letta.js',broken/'letta.js');shutil.copytree(fixed/'recovery',broken/'recovery')
        (broken/'node_modules').symlink_to(fixed/'node_modules',target_is_directory=True)
        module=broken/'recovery/transcript-write.mjs'
        module.write_text(module.read_text().replace('baselines.set(path,{messages:structuredClone(messages),text:readTranscript(path)});','// Fault injection: reproduce the previous raw-prefix writer.'))
        home=self.fixture.root;backend=home/'.letta/lc-local-backend';(home/'backend').rename(backend)
        self.fixture.environment['LETTA_LOCAL_BACKEND_DIR']=str(backend)
        paths=[path for path in (backend/'conversations').glob('*/conversation.json')
               if (json.loads(path.read_text()).get('agent_id'),json.loads(path.read_text()).get('id'))==(agent.agent_id,agent.conversation_id)]
        self.assertEqual(len(paths),1)
        transcript=paths[0].with_name('messages.jsonl');rows=[json.loads(line) for line in transcript.read_text().splitlines()]
        historical={**rows[1],'id':'retained-entry','message':{**rows[1]['message'],'id':'retained-message'}}
        original='\n'.join(json.dumps(row) for row in [rows[0],historical,*rows[1:]])+'\n';transcript.write_text(original)
        self.fixture.release=broken;self.fixture.restart()
        store=self.fixture.store
        store.reserve_usage('provider-'+'a'*64,'2026-10','2026-10-16',.01)
        for index in range(15):store.add_message(17,'user','Old held fixture '+str(index),queued=3)
        session=runner.start('owner_wake',datetime.now(timezone.utc))
        self.assertFalse(runner.run_cycle(session));self.assertEqual(len(self.fixture.provider_calls),1)
        row=dict(store.connection.execute("SELECT * FROM recovery_attempts WHERE state='active' ORDER BY rowid DESC LIMIT 1").fetchone())
        controller._command('/pause')
        with store.connection:store.connection.execute("UPDATE recovery_attempts SET state='held',hold_reason='provider_uncertain' WHERE admission_id=?",(row['admission_id'],))
        self.fixture.kill();store.close()
        snapshot_directory=tempfile.TemporaryDirectory();self.addCleanup(snapshot_directory.cleanup)
        snapshot=Path(snapshot_directory.name)/'paired-before-repair'
        create_paired_recovery_snapshot(home/'controller.sqlite3',home,snapshot,workers_quiescent=True)
        verify_paired_recovery_snapshot(snapshot)
        value=repair.plan(home/'controller.sqlite3',home,row['admission_id'])
        repair.retire_backend(value);repair.retire_controller(value)
        self.assertEqual(transcript.read_text(),original)
        self.assertTrue(Path(value['archive']).is_file())
        self.fixture.store=Store(home/'controller.sqlite3');self.fixture.model_gateway.store=self.fixture.store
        self.fixture.release=fixed;self.fixture.restart()
        agent,controller,coordinator,runner=self.coordinated_agent()
        self.assertFalse(coordinator.has_pending());controller._command('/wake')
        self.responses.append({'id':'fresh-after-repair','choices':[{'index':0,'message':{'role':'assistant','content':'Fresh work after repair.'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}})
        new_session=runner.start('owner_wake',datetime.now(timezone.utc))
        self.assertTrue(runner.run_cycle(new_session));self.assertEqual(len(self.fixture.provider_calls),2)
        self.assertTrue(transcript.read_text().startswith(original))
        self.assertEqual(self.fixture.store.uncertain_message_count(17),15)
        self.assertEqual(self.fixture.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],1)
        retained=self.command('recovery_inspect',binding=value['binding'])
        self.assertEqual(retained['phase'],'held');self.assertEqual(retained['hold_reason'],'projection_conflict')
        self.assertEqual(len(self.fixture.provider_calls),2)

    def test_legacy_rate_limit_retirement_then_fresh_wake_preserves_history_and_holds(self):
        from unittest.mock import patch
        from andrewbot.store import Store
        from andrewbot.model import ProviderRequestError
        from andrewbot.memory_backup import create_paired_recovery_snapshot
        spec=importlib.util.spec_from_file_location('rate_retirement_fixture',ROOT/'scripts/retire_rate_limited_input.py')
        repair=importlib.util.module_from_spec(spec);spec.loader.exec_module(repair)
        agent,controller,coordinator,runner=self.coordinated_agent()
        self.fixture.kill()
        home=self.fixture.root;backend=home/'.letta/lc-local-backend';(home/'backend').rename(backend)
        self.fixture.environment['LETTA_LOCAL_BACKEND_DIR']=str(backend);self.fixture.restart()
        store=self.fixture.store
        for index in range(15):store.add_message(17,'user','Old held fixture '+str(index),queued=3)
        for index in range(10):store.reserve_usage('provider-'+format(index,'064x'),'2026-10','2026-10-16',.01)
        session=runner.start('owner_wake',datetime.now(timezone.utc))
        # Reproduce the old gateway: the HTTP error is logged, but no rejection
        # receipt is committed and the reservation remains admitted.
        with patch.object(self.fixture.model_gateway.provider,'complete',side_effect=ProviderRequestError('http_error',429)), \
             patch.object(RecoveryRecords,'reject_provider',side_effect=ValueError('legacy gateway has no rejection receipt')):
            self.assertFalse(runner.run_cycle(session))
        row=dict(store.connection.execute("SELECT * FROM recovery_attempts WHERE state='active' ORDER BY rowid DESC LIMIT 1").fetchone())
        controller._command('/pause')
        with store.connection:store.connection.execute("UPDATE recovery_attempts SET state='held',hold_reason='provider_uncertain' WHERE admission_id=?",(row['admission_id'],))
        self.fixture.kill();store.close()
        transcript=next((backend/'conversations').glob('*/messages.jsonl'));before=transcript.read_bytes()
        snapshot_directory=tempfile.TemporaryDirectory();self.addCleanup(snapshot_directory.cleanup)
        snapshot=Path(snapshot_directory.name)/'paired-before'
        create_paired_recovery_snapshot(home/'controller.sqlite3',home,snapshot,workers_quiescent=True)
        value=repair.plan(home/'controller.sqlite3',home,row['admission_id'])
        repair.retire_backend(value);repair.retire_controller(value)
        self.assertEqual(transcript.read_bytes(),before)
        self.fixture.store=Store(home/'controller.sqlite3');self.fixture.model_gateway.store=self.fixture.store
        self.fixture.restart();agent,controller,coordinator,runner=self.coordinated_agent()
        self.assertFalse(coordinator.has_pending());controller._command('/wake')
        session=runner.start('owner_wake',datetime.now(timezone.utc))
        self.assertTrue(runner.run_cycle(session))
        self.assertEqual(len(self.fixture.provider_calls),1)
        self.assertEqual(self.fixture.store.uncertain_message_count(17),15)
        self.assertEqual(self.fixture.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],10)
        self.assertTrue(transcript.read_bytes().startswith(before))
        self.assertEqual(self.command('recovery_inspect',binding=value['binding'])['phase'],'held')

    def test_confirmed_tool_survives_backend_and_controller_loss_then_one_continuation(self):
        from andrewbot.recovery_dispatch import RecoveryDispatch
        from andrewbot.store import Store
        agent,controller,coordinator,runner=self.coordinated_agent()
        store=self.fixture.store
        held=store.add_message(17,'user','Existing held owner input',queued=3)
        session=runner.start('owner_wake',datetime.now(timezone.utc))
        self.responses.insert(0,{'id':'tool-before-crash','choices':[{'index':0,'message':{'role':'assistant','content':None,
            'tool_calls':[{'id':'crash-call','type':'function','function':{'name':'recent_activity','arguments':'{}'}}]},'finish_reason':'tool_calls'}],
            'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}})
        effects=[]
        def kill_after_confirm(name,args,*,recovery):
            result=RecoveryDispatch(store).run_tool(recovery,name,args,lambda:effects.append(name) or {'entries':['exact confirmed fixture event']})
            self.fixture.process.kill();self.fixture.process.wait(timeout=5)
            return result
        self.fixture.model_gateway.recovery_tool_handler=kill_after_confirm
        self.assertFalse(runner.run_cycle(session))
        original=dict(store.connection.execute("SELECT * FROM recovery_attempts WHERE state='active' ORDER BY rowid DESC LIMIT 1").fetchone())
        binding=json.loads(original['binding_json'])
        self.assertEqual(effects,['recent_activity']);self.assertEqual(len(self.fixture.provider_calls),1)
        # Reopen the actual controller database, losing every in-memory transport token.
        store.close();self.fixture.store=Store(self.fixture.root/'controller.sqlite3')
        self.fixture.model_gateway.store=self.fixture.store
        self.fixture.restart()
        agent,controller,coordinator,runner=self.coordinated_agent()
        runner.recover()
        self.assertEqual(runner.active_session_id,session)
        self.assertTrue(coordinator.reconcile_one())
        self.assertEqual(effects,['recent_activity']);self.assertEqual(len(self.fixture.provider_calls),2)
        receipt=self.fixture.store.connection.execute('SELECT * FROM model_turn_receipts WHERE cycle_id=?',(binding['cycle_id'],)).fetchone()
        self.assertEqual(receipt['client_message_id'],binding['client_message_id']);self.assertEqual(receipt['state'],'completed')
        self.assertEqual(self.fixture.store.connection.execute('SELECT status FROM cycles WHERE cycle_id=?',(binding['cycle_id'],)).fetchone()[0],'complete')
        self.assertEqual(self.fixture.store.connection.execute('SELECT queued FROM messages WHERE id=?',(held,)).fetchone()[0],3)
        continuation=self.fixture.provider_calls[-1]['messages']
        self.assertEqual(continuation[-1],{'role':'tool','tool_call_id':'crash-call','content':'{"entries":["exact confirmed fixture event"]}'})
        self.assertFalse(coordinator.has_pending())
