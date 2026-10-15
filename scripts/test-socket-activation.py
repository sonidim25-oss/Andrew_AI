#!/usr/bin/env python3
"""Exercise the real runner entrypoint using temporary native user socket units.

Development identity only. No root files, production state or provider calls.
"""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'prototype'))
from andrewbot.runner_client import RunnerClient
from andrewbot.browser_client import BrowserClient


def main(image, full=False):
    name='andrew-socket-proof-'+uuid.uuid4().hex[:12]
    units=Path(os.environ.get('XDG_RUNTIME_DIR',f'/run/user/{os.getuid()}'))/'systemd/user'
    units.mkdir(parents=True,exist_ok=True)
    service=units/(name+'.service'); activation=units/(name+'.socket')
    manager=subprocess.check_output(['systemctl','show',f'user@{os.getuid()}.service',
                                     '--property=ControlGroup','--value'],text=True).strip()
    assert manager.startswith('/')
    parent=manager+'/app.slice/'+name+'.service'
    def systemctl(*args,check=True):
        return subprocess.run(['systemctl','--user',*args],check=check,capture_output=True,text=True)
    with tempfile.TemporaryDirectory(prefix='andrew-socket-') as directory:
        root=Path(directory); (root/'projects').mkdir(); (root/'proxy.sock').touch()
        config=root/'runner.conf'
        config.write_text(f'IMAGE={image}\nWORKSPACE={root}/projects\nPROXY_SOCKET={root}/proxy.sock\n')
        service.write_text(f'''[Service]
Type=simple
Slice=app.slice
WorkingDirectory={PROJECT}
Environment=PYTHONPATH={PROJECT}/prototype
Environment=ANDREW_RUNNER_CONFIG={config}
Environment=ANDREW_CGROUP_PARENT={parent}
ExecStart=/usr/bin/python3 -m andrewbot.runner_service
Delegate=yes
DelegateSubgroup=supervisor
MemoryMax=3G
CPUQuota=250%
TasksMax=512
UMask=0077
''')
        activation.write_text(f'''[Socket]
ListenStream={root}/runner.sock
SocketMode=0600
RemoveOnStop=yes
Service={name}.service
''')
        extra_units=[]
        # These fixture entrypoints use the production activated servers with
        # temporary paths; protected production config/credentials stay untouched.
        for role in ('browser','proxy'):
            role_name=name+'-'+role
            role_parent=manager+'/app.slice/'+role_name+'.service'
            entry=root/(role+'.py')
            if role=='browser':
                entry.write_text(f"""import asyncio,os
from andrewbot.browser_service import BrowserProcess,serve
from andrewbot.runner_service import WorkerConfig,RunnerService
config=WorkerConfig({image!r},{str(root/'projects')!r},{str(root/'proxy.sock')!r},cgroup_parent=os.environ['ANDREW_CGROUP_PARENT'])
RunnerService(config).recover()
asyncio.run(serve(BrowserProcess(config)))
""")
            else:
                entry.write_text("import asyncio; from andrewbot.egress_proxy import serve_systemd_socket; asyncio.run(serve_systemd_socket())\n")
            role_service=units/(role_name+'.service'); role_socket=units/(role_name+'.socket')
            extra_units.extend((role_service,role_socket))
            role_service.write_text(service.read_text().replace(
                'ExecStart=/usr/bin/python3 -m andrewbot.runner_service','ExecStart=/usr/bin/python3 '+str(entry)).replace(parent,role_parent))
            role_socket.write_text(f'[Socket]\nListenStream={root}/{role}.sock\nSocketMode=0600\nRemoveOnStop=yes\nService={role_name}.service\n')
        try:
            systemctl('daemon-reload')
            systemctl('start',name+'.socket',name+'-proxy.socket',name+'-browser.socket')
            client=RunnerClient(str(root/'runner.sock'),timeout_seconds=40)
            for _ in range(2):
                result=client.run({'type':'python','code':"print('activated runner works')"})
                assert result['status']=='ok',f'actual activated runner returned {result["safe_error"]}'
                assert result['stdout'].strip()=='activated runner works'
            browser=BrowserClient(str(root/'browser.sock'))
            result=browser.run({'action':'open','url':'https://example.com/'})
            assert result['status']=='ok',result
            assert 'Example' in result['result']['title']
            assert browser.run({'action':'snapshot'})['status']=='ok'
            assert browser.run({'action':'reset'})['status']=='ok'
            if full:
                spec=importlib.util.spec_from_file_location('acceptance',PROJECT/'scripts/verify-runtime.py')
                acceptance=importlib.util.module_from_spec(spec); spec.loader.exec_module(acceptance)
                acceptance.runner=RunnerClient(str(root/'runner.sock'),timeout_seconds=140)
                acceptance.BrowserClient=lambda: BrowserClient(str(root/'browser.sock'))
                acceptance.main(strict_workspace=False)
            print(json.dumps({'native_socket_activation':'passed','real_runner_entrypoint':'passed',
                              'repeated_worker_requests':'passed','activated_browser_and_proxy':'passed','model_calls':0}))
        finally:
            systemctl('stop',name+'.socket',name+'.service',name+'-browser.socket',name+'-browser.service',name+'-proxy.socket',name+'-proxy.service',check=False)
            systemctl('reset-failed',name+'.service',check=False)
            service.unlink(missing_ok=True); activation.unlink(missing_ok=True)
            for unit in extra_units: unit.unlink(missing_ok=True)
            systemctl('daemon-reload')


if __name__=='__main__': main(sys.argv[1],full='--full' in sys.argv[2:])
