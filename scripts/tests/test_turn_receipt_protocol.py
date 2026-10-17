"""Pinned Letta completion receipts survive a failed controller history read."""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'prototype'))
from andrewbot.appserver import AppServerClient, LettaAgent
from andrewbot.controller import Controller
from andrewbot.gateway import ModelGateway, GatewayThread, make_server
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store


@unittest.skipUnless(os.environ.get('ANDREW_LETTA_TEST_BINARY'), 'set pinned Letta JS path')
class TurnReceiptProtocolTests(unittest.TestCase):
    @patch('andrewbot.appserver.gmail_permissions', return_value={'enabled': False})
    def test_completed_turn_recovers_after_final_history_read_fails_without_replay(self, _permissions):
        with tempfile.TemporaryDirectory(prefix='andrew-turn-receipt-') as directory:
            root = Path(directory)
            with socket.socket() as socket_fixture:
                socket_fixture.bind(('127.0.0.1',0))
                port=socket_fixture.getsockname()[1]
            store=Store(root/'state.db')
            self.addCleanup(store.close)
            calls=[]
            class Provider:
                def complete(self, payload):
                    calls.append(payload)
                    return {'id':'fixture','model':'qwen/qwen3.8-flash',
                        'choices':[{'index':0,'message':{'role':'assistant','content':'Saved protocol work.'},'finish_reason':'stop'}],
                        'usage':{'prompt_tokens':20,'completion_tokens':5,'cost':.000001}}
            server_http=make_server(ModelGateway(Provider(),store,'fixture-token'),port=0)
            gateway=GatewayThread(server_http);gateway.start()
            self.addCleanup(gateway.close)
            env=dict(os.environ,HOME=str(root),LETTA_HOME=str(root/'settings'),LETTA_LOCAL_BACKEND_DIR=str(root/'backend'),DO_NOT_TRACK='1')
            with (root/'server.log').open('w') as log:
                server=subprocess.Popen(['node',os.environ['ANDREW_LETTA_TEST_BINARY'],'server','--backend','local',
                    '--listen',f'ws://127.0.0.1:{port}','--openai-api'],cwd=root,env=env,stdout=log,stderr=log)
                try:
                    deadline=time.monotonic()+20
                    url=f'ws://127.0.0.1:{port}/ws'
                    while True:
                        self.assertIsNone(server.poll(),(root/'server.log').read_text()[-1000:])
                        try:
                            client=AppServerClient(url,timeout=3)
                            break
                        except ConnectionRefusedError:
                            self.assertLess(time.monotonic(),deadline)
                            time.sleep(.05)
                    try:
                        client.request({'type':'connect_provider','target':'local','provider_id':'openai-compatible',
                            'fields':{'apiKey':'fixture-token','baseUrl':f'http://127.0.0.1:{server_http.server_port}/v1'}},'connect_provider_response')
                        agent_id=client.create_agent({'name':'Isolated receipt fixture','system':'Reply with a brief text answer.',
                            'model':'openai-compatible/qwen/qwen3.8-flash','include_base_tools':False,'memory_blocks':[]})
                        runtime=client.start_runtime(agent_id)
                    finally: client.close()
                    store.set_state('letta_agent_id',agent_id)
                    store.set_state('letta_conversation_id',runtime['conversation_id'])
                    agent=LettaAgent(store,'fixture-token',url=url)
                    controller=Controller(store,agent,17,autonomous_enabled=True)
                    agent.tool_handler=controller.execute_tool
                    runner=SessionRunner(store,controller,agent,enabled=True)
                    store.set_state('session_start_requested','owner_wake')
                    failed=[]
                    outer=self
                    class FailFinalRead(AppServerClient):
                        def request(self, command, response_type, timeout=None):
                            if command['type']=='conversation_messages_list' and not failed:
                                proof=store.connection.execute('SELECT * FROM model_turn_receipts').fetchone()
                                outer.assertIsNotNone(proof)
                                outer.assertEqual(proof['state'],'completed')
                                outer.assertTrue(proof['run_id'])
                                failed.append(True)
                                raise ConnectionError('Simulated controller history read failure')
                            return super().request(command,response_type,timeout)
                    with patch('andrewbot.appserver.AppServerClient',FailFinalRead):
                        self.assertFalse(runner.tick())
                    self.assertEqual(len(failed),1,'the real pinned protocol must produce a bound receipt')
                    self.assertEqual(len(calls),1)
                    self.assertEqual(store.connection.execute('SELECT status FROM cycles').fetchone()[0],'complete')
                    self.assertEqual(store.get_state('autonomy_error',''),'')
                    self.assertEqual(store.get_state('active_session_id'),runner.active_session_id)
                    first=store.connection.execute('SELECT request_id,run_id FROM model_turn_receipts').fetchone()
                    self.assertTrue(runner.tick())
                    self.assertEqual(len(calls),2,'continuation starts one new turn, never repeats the completed input')
                    self.assertTrue(any(message.get('role') == 'assistant' and message.get('content') == 'Saved protocol work.'
                        for message in calls[-1]['messages']), 'continuation retains the original saved conversation')
                    receipts=store.connection.execute('SELECT * FROM model_turn_receipts').fetchall()
                    self.assertEqual(len(receipts),2)
                    self.assertEqual(len({row['request_id'] for row in receipts}),2)
                    self.assertTrue(all(row['state']=='completed' for row in receipts))
                    self.assertNotEqual(first['run_id'],receipts[-1]['run_id'])
                    self.assertEqual(store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],0)
                finally:
                    server.terminate()
                    try: server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill();server.wait(timeout=5)
