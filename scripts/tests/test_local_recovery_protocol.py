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
