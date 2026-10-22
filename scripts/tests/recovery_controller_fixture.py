"""Disposable actual controller process used only by process-loss acceptance."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'prototype'))
from andrewbot.store import Store
from andrewbot.gateway import ModelGateway,GatewayThread,make_server
from andrewbot.appserver import AppServerClient,LettaAgent
from andrewbot.controller import Controller
from andrewbot.recovery import RecoveryCoordinator
from andrewbot.recovery_dispatch import RecoveryDispatch
from andrewbot.recovery_records import RecoveryRecords
from andrewbot.sessions import SessionRunner
from andrewbot.workers import ModelWorker
from andrewbot.model import OpenRouterModel, ProviderRequestError, retry_after_seconds


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--backend-url',required=True);parser.add_argument('--provider-url',required=True)
    parser.add_argument('--mode',choices=['start','recover'],required=True)
    parser.add_argument('--fault',choices=['provider_before','provider_after','tool_before','tool_after','rejection_before','rejection_after','yield_before','yield_after','handoff_before','handoff_after'])
    parser.add_argument('--compaction',action='store_true')
    parser.add_argument('--recovery-once',action='store_true')
    parser.add_argument('--unreached-first',action='store_true')
    args=parser.parse_args();root=args.root
    def save(name,value):
        path=root/name;temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,sort_keys=True));os.replace(temporary,path)
    def post(path,payload):
        request=urllib.request.Request(args.provider_url+path,json.dumps(payload).encode(),{'Content-Type':'application/json'},method='POST')
        with urllib.request.urlopen(request,timeout=10) as response:return json.load(response)
    unreached=[args.unreached_first]
    class Provider:
        def complete(self,payload):
            if unreached.pop() if unreached else False:
                # Real transport against a closed port: proven not sent.
                probe=socket.socket();probe.bind(('127.0.0.1',0));port=probe.getsockname()[1];probe.close()
                model=OpenRouterModel('fixture-key',timeout=5);model.endpoint=f'http://127.0.0.1:{port}/provider'
                return model.complete(payload)
            try:return post('/provider',payload)
            except urllib.error.HTTPError as error:
                raise ProviderRequestError('http_error',error.code,retry_after=retry_after_seconds(error.headers.get('Retry-After'))) from None
    store=Store(root/'controller.sqlite3');gateway=ModelGateway(Provider(),store,'fixture-token')
    server=GatewayThread(make_server(gateway,port=0));server.start()
    def die(boundary):
        save('controller-fault.json',{'boundary':boundary});os.kill(os.getpid(),signal.SIGKILL)
    if args.fault:
        method=('prepare_owner_yield' if args.fault.startswith('yield') else 'complete_scope' if args.fault.startswith('handoff')
                else 'reject_provider' if args.fault.startswith('rejection') else 'confirm_tool' if args.fault.startswith('tool') else 'confirm_provider')
        original=getattr(RecoveryRecords,method)
        def interrupted(self,*values,**keywords):
            if args.compaction:
                kind=self.store.connection.execute('SELECT kind FROM recovery_effects WHERE admission_id=?',(values[0],)).fetchone()[0]
                if kind!='compaction':return original(self,*values,**keywords)
            if args.fault.endswith('before'):die(args.fault)
            result=original(self,*values,**keywords)
            die(args.fault)
            return result
        setattr(RecoveryRecords,method,interrupted)
    agent=LettaAgent(store,'fixture-token',url=args.backend_url)
    controller=Controller(store,agent,17,autonomous_enabled=True)
    coordinator=RecoveryCoordinator(store,controller,enabled=True)
    agent.local_recovery=controller.local_recovery=coordinator;agent.tool_handler=controller.execute_tool
    def tool(name,arguments,*,recovery):
        if name!='recent_activity':raise AssertionError('Unadvertised fixture tool')
        return RecoveryDispatch(store).run_tool(recovery,name,arguments,lambda:post('/effect',{'name':name,'arguments':arguments}))
    gateway.recovery_tool_handler=tool
    def provider(client,token):
        return client.request({'type':'connect_provider','target':'local','provider_id':'openai-compatible',
            'fields':{'apiKey':token,'baseUrl':f'http://127.0.0.1:{server.server.server_port}/v1'}},'connect_provider_response')
    AppServerClient.ensure_provider=provider
    agent.initialize();runner=SessionRunner(store,controller,agent,enabled=True)
    def work():
        if args.mode=='start':
            store.add_message(17,'user','Retained held fixture owner input',queued=3)
            session=runner.start('owner_wake',datetime.now(timezone.utc))
            if args.compaction:
                if not runner.run_cycle(session):raise AssertionError('Compaction warmup failed')
            completed=runner.run_cycle(session)
        else:
            runner.recover();completed=coordinator.reconcile_one()
            # Production schedules another worker pass after transient ownership
            # conflicts. A single pass can race the old connection's release.
            deadline=time.monotonic()+10
            while not completed and not args.recovery_once and time.monotonic()<deadline:
                pending=coordinator._pending()
                if not pending or pending[0]['state']=='held':break
                time.sleep(.05);completed=coordinator.reconcile_one()
        rows=[dict(row) for row in store.connection.execute('SELECT client_message_id,state FROM model_turn_receipts')]
        save('controller-done.json',{'completed':bool(completed),'receipts':rows,
            'reservations':store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],
            'held_messages':store.uncertain_message_count(17)})
    worker=ModelWorker(work,lambda:False);worker.start();worker.wake()
    save('controller-ready.json',{'pid':os.getpid()})
    while True:time.sleep(.05)


if __name__=='__main__':main()
