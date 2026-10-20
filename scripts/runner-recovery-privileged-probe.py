#!/usr/bin/env python3
"""Distinct UID acceptance with an inherited root-created listener and zero workers."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time


def probe(package,runner_uid,runner_gid):
    if os.geteuid()!=0 or runner_uid==0:raise ValueError('Distinct runtime UID requires root acceptance')
    with tempfile.TemporaryDirectory(prefix='andrew-receipt-uid-') as directory:
        root=Path(directory);root.chmod(0o755);source=root/'source';source.mkdir(mode=0o755)
        shutil.copytree(package,source/'andrewbot',ignore=shutil.ignore_patterns('__pycache__'))
        for path in (source/'andrewbot').rglob('*'):path.chmod(0o755 if path.is_dir() else 0o644)
        receipts=root/'receipts';receipts.mkdir(mode=0o700);os.chown(receipts,runner_uid,runner_gid)
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as listener:
            path=root/'probe.sock';listener.bind(str(path));listener.listen();path.chmod(0o666)
            program='''import asyncio,json,os,socket,sys
from andrewbot.runner_receipts import RunnerReceiptStore
from andrewbot.runner_service import RunnerSocketServer
key=b'k'*32;s=RunnerReceiptStore(sys.argv[1],key,expected_uid=os.getuid());s.provision(controller_store_id='a'*32)
class NoWorker:
 def run(self,*args):raise AssertionError('privileged acceptance must never execute a worker')
async def main():
 listener=socket.socket(fileno=int(sys.argv[2]));listener.setblocking(False)
 server=await asyncio.start_unix_server(RunnerSocketServer(NoWorker(),receipts=s,controller_uid=0,receipts_enabled=False).handle,sock=listener)
 async with server:await server.serve_forever()
try:asyncio.run(main())
finally:s.close()
'''
            child=subprocess.Popen([sys.executable,'-c',program,str(receipts),str(listener.fileno())],pass_fds=(listener.fileno(),),
                user=runner_uid,group=runner_gid,extra_groups=[],env={**os.environ,'PYTHONPATH':str(source)},stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                sys.path.insert(0,str(Path(package).parent))
                from andrewbot.runner_client import RunnerClient
                deadline=time.monotonic()+15
                while True:
                    try:
                        capability=RunnerClient(str(path)).receipt_capabilities(expected_runner_uid=runner_uid)
                        break
                    except Exception:
                        if child.poll() is not None:raise ValueError('Distinct UID listener process failed')
                        if time.monotonic()>deadline:raise ValueError('Distinct UID sender credential acceptance failed')
                        time.sleep(.05)
                if capability.get('controller_store_id')!='a'*32 or capability.get('execution_enabled') is not False:
                    raise ValueError('Distinct UID receipt capabilities changed')
                # Accepting the root listener UID would indicate a credential bug.
                try:RunnerClient(str(path)).receipt_capabilities(expected_runner_uid=0)
                except Exception:pass
                else:raise ValueError('Listener creator was incorrectly trusted as runtime sender')
                return dict(verified=True,listener_uid=0,sender_uid=runner_uid,sender_pid=child.pid,worker_executions=0,wrong_listener_uid_rejected=True)
            finally:
                child.kill();child.wait(timeout=5);child.stderr.close()


if __name__=='__main__':
    import pwd
    account=pwd.getpwnam('andrew-runtime')
    print(json.dumps(probe(Path(__file__).resolve().parents[1]/'prototype/andrewbot',account.pw_uid,account.pw_gid)))
