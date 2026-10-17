#!/usr/bin/env python3
"""Bounded live VPS acceptance, invoked as andrew-control after provisioning.
No model provider calls or production secrets. JSON reports contain categories only.
"""
import json
from pathlib import Path
import sys
sys.path.insert(0,'/opt/andrew/runtime-services')
from andrewbot.runner_client import RunnerClient
from andrewbot.browser_client import BrowserClient

runner=RunnerClient('/run/andrew/runner.sock',timeout_seconds=140)

def run(code):
    result=runner.run({'type':'python','code':code})
    if result['status']!='ok': raise RuntimeError('worker acceptance failed: '+str(result['safe_error']))
    return json.loads(result['stdout'])

def main(strict_workspace=True):
    escaped = runner.run({'type': 'python', 'code': "import os; os.write(1, bytes(16384)); os.write(2, bytes(16384))"})
    assert escaped['status'] == 'ok' and escaped.get('output_truncated') is True
    assert len(json.dumps(escaped, ensure_ascii=False, separators=(',', ':')).encode()) <= 40 * 1024
    kernel=run('''import json,os,socket
from pathlib import Path
cg=Path('/sys/fs/cgroup')
status=Path('/proc/self/status').read_text()
print(json.dumps({'cpu':(cg/'cpu.max').read_text().strip(), 'memory':(cg/'memory.max').read_text().strip(),
'pids':(cg/'pids.max').read_text().strip(),'interfaces':[v for _,v in socket.if_nameindex()],
'capless':'CapEff:\\t0000000000000000' in status,'nnp':'NoNewPrivs:\\t1' in status,
'private_paths':[p for p in ['/etc/andrew','/var/lib/andrew/vault','/run/andrew/browser.sock','/run/podman/podman.sock'] if Path(p).exists()],
'workspace_bytes':os.statvfs('/workspace').f_blocks*os.statvfs('/workspace').f_frsize}))
''')
    assert kernel['cpu']=='200000 100000' and kernel['memory']=='2147483648' and kernel['pids']=='256'
    assert kernel['interfaces']==['lo'] and kernel['capless'] and kernel['nnp'] and not kernel['private_paths']
    if strict_workspace: assert 9*1024**3 < kernel['workspace_bytes'] <= 10*1024**3
    network=run('''import json,socket,os,urllib.request
os.environ['RES_OPTIONS']='attempts:1 timeout:1'
blocked=[]
for host,port in [('1.1.1.1',443),('192.168.1.1',80),('169.254.169.254',80),('127.0.0.1',8284),('2606:4700:4700::1111',443)]:
 try:
  s=socket.create_connection((host,port),timeout=2); s.close()
 except OSError: blocked.append(host)
try: socket.getaddrinfo('example.com',443); dns=False
except OSError: dns=True
with urllib.request.urlopen('https://example.com/',timeout=20) as r: public=r.status==200
try: urllib.request.urlopen('https://127.0.0.1/',timeout=5); private=False
except OSError: private=True
opener=urllib.request.build_opener(urllib.request.ProxyHandler({'https':'http://127.0.0.1:9'}))
try: opener.open('https://example.com/',timeout=3); closed=False
except OSError: closed=True
print(json.dumps({'blocked':len(blocked),'dns':dns,'proxy_public':public,'proxy_private':private,'proxy_unavailable':closed}))
''')
    assert network=={'blocked':5,'dns':True,'proxy_public':True,'proxy_private':True,'proxy_unavailable':True}
    memory=run('''import subprocess,sys,json
from pathlib import Path
child=subprocess.run([sys.executable,'-c',"a=[]\\nwhile True: a.append(bytearray(32*1024*1024))"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
events=dict(line.split() for line in Path('/sys/fs/cgroup/memory.events').read_text().splitlines())
print(json.dumps({'oom':int(events.get('oom_kill','0'))>0,'killed':child.returncode<0}))
''')
    assert memory=={'oom':True,'killed':True}
    pids=run('''import subprocess,json
from pathlib import Path
children=[]; blocked=False
try:
 for _ in range(300):
  try: children.append(subprocess.Popen(['/bin/sleep','20']))
  except OSError: blocked=True; break
finally:
 for p in children: p.terminate()
 for p in children: p.wait()
events=dict(line.split() for line in Path('/sys/fs/cgroup/pids.events').read_text().splitlines())
print(json.dumps({'blocked':blocked,'event':int(events.get('max','0'))>0}))
''')
    assert pids=={'blocked':True,'event':True}
    cpu=run('''import subprocess,sys,json
from pathlib import Path
code="import time; end=time.monotonic()+3\\nwhile time.monotonic()<end: pass"
children=[subprocess.Popen([sys.executable,'-c',code]) for _ in range(4)]
for p in children: p.wait()
stat=dict(line.split() for line in Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())
print(json.dumps({'throttled':int(stat['nr_throttled'])>0}))
''')
    assert cpu['throttled']
    # Owner requested no code/shell wall deadline. Cross the former 120s
    # boundary; the acceptance client alone retains a finite 140s test bound.
    long_execution=runner.run({'type':'python','code':"import time; time.sleep(130); print('finished')"})
    assert long_execution['status']=='ok' and long_execution['stdout'].strip()=='finished'
    browser=BrowserClient()
    result=browser.run({'action':'open','url':'https://example.com/'})
    assert result['status']=='ok' and 'Example' in result['result']['title']
    assert browser.run({'action':'snapshot'})['status']=='ok'
    assert browser.run({'action':'reset'})['status']=='ok'
    print(json.dumps({'network':'passed','limits':'passed','browser_public_https':'passed',
                      'workspace_capacity':'passed' if strict_workspace else 'not checked (local component run)',
                      'production_accounts':'not used','ledger':'not modified (no model calls)'}))

if __name__=='__main__': main()
