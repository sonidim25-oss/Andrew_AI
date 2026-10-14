"""Real pinned Letta RPC with browser/account controller callbacks, no paid provider."""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'prototype'))
from andrewbot.appserver import AppServerClient, LettaAgent
from andrewbot.browser_tools import BrowserTools
from andrewbot.account_broker import AccountBroker
from andrewbot.tool_runtime import ToolRuntime,ToolPolicy
from andrewbot.controller import Controller
from andrewbot.vault import Vault
from andrewbot.store import Store
from andrewbot.gateway import GatewayThread,ModelGateway,make_server

@unittest.skipUnless(os.environ.get('ANDREW_LETTA_TEST_BINARY'),'requires isolated pinned Letta harness')
class BrowserProtocolTests(unittest.TestCase):
    def test_real_harness_browser_account_callbacks(self):
        with tempfile.TemporaryDirectory(prefix='andrew-browser-protocol-') as directory:
            root=Path(directory)
            with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
            env=dict(os.environ,LETTA_HOME=str(root/'settings'),LETTA_LOCAL_BACKEND_DIR=str(root/'backend'),DO_NOT_TRACK='1')
            with (root/'server.log').open('w') as log:
                process=subprocess.Popen(['node',os.environ['ANDREW_LETTA_TEST_BINARY'],'server','--backend','local',
                    '--listen',f'ws://127.0.0.1:{port}','--openai-api'],cwd=root,env=env,stdout=log,stderr=log)
                gateway=None; store=Store(root/'state.sqlite3'); vault=Vault(root/'vault')
                try:
                    deadline=time.monotonic()+30
                    while True:
                        try: client=AppServerClient(f'ws://127.0.0.1:{port}/ws'); break
                        except ConnectionRefusedError:
                            self.assertIsNone(process.poll()); self.assertLess(time.monotonic(),deadline); time.sleep(.1)
                    calls=[]; browser_calls=[]
                    class Browser:
                        def run(self,request):
                            browser_calls.append(request)
                            return {'status':'ok','result':{'url':'https://example.com/','title':'fixture','text':'Browser callback succeeded'}}
                    browser=Browser(); broker=AccountBroker(vault,browser)
                    tools=BrowserTools(browser,broker,root,root/'artifacts',lambda *args:None)
                    test=self
                    class Provider:
                        def complete(self,payload):
                            calls.append(payload)
                            test.assertEqual({t['function']['name'] for t in payload['tools']},
                                             AppServerClient.SAFE_CONTROLLER_TOOL_NAMES|AppServerClient.EXTERNAL_TOOL_NAMES)
                            steps=[('browser.control','{"action":"open","url":"https://example.com/"}'),
                                   ('browser.account','{"operation":"list"}')]
                            if len(calls)<=len(steps):
                                name,args=steps[len(calls)-1]
                                message={'role':'assistant','content':None,'tool_calls':[{'id':f'call-{len(calls)}','type':'function','function':{'name':name,'arguments':args}}]}
                                stop='tool_calls'
                            else: message={'role':'assistant','content':'Browser and account callbacks completed.'}; stop='stop'
                            return {'id':f'local-{len(calls)}','model':'qwen/qwen3.8-flash',
                                'choices':[{'index':0,'message':message,'finish_reason':stop}],
                                'usage':{'prompt_tokens':20,'completion_tokens':20,'cost':.000002}}
                    gateway=GatewayThread(make_server(ModelGateway(Provider(),store,'fixture-token'),port=0)); gateway.start()
                    client.request({'type':'connect_provider','target':'local','provider_id':'openai-compatible',
                        'fields':{'apiKey':'fixture-token','baseUrl':f'http://127.0.0.1:{gateway.server.server_address[1]}/v1'}},'connect_provider_response')
                    agent_id=client.create_agent({'name':'Browser protocol fixture','system':'Use advertised browser tools.',
                        'model':'openai-compatible/qwen/qwen3.8-flash','model_settings':{'provider_type':'openai-compatible','max_tokens':512,'parallel_tool_calls':False},'include_base_tools':False,'memory_blocks':[]})
                    client.close()
                    agent=LettaAgent(store,'fixture-token',url=f'ws://127.0.0.1:{port}/ws'); agent.agent_id=agent_id
                    agent.external_tools_enabled=True
                    controller=Controller(store,agent,17,tool_runtime=ToolRuntime(None,ToolPolicy(True),browser_tools=tools))
                    agent.tool_handler=controller.execute_tool
                    reply,_=agent.reply([{'role':'user','content':'Use browser.control then browser.account list, then report completion.'}])
                    self.assertEqual(reply,'Browser and account callbacks completed.')
                    self.assertEqual(len(browser_calls),1); self.assertEqual(len(calls),3)
                    self.assertEqual(store.connection.execute('SELECT count(*) FROM usage').fetchone()[0],3)
                    store.set_state('paused','1')
                    with self.assertRaises(ValueError): controller.execute_tool('browser.control',{'action':'snapshot'})
                    self.assertEqual(len(browser_calls),1)
                finally:
                    if gateway: gateway.close()
                    process.terminate(); process.wait(timeout=10); vault.close(); store.close()
