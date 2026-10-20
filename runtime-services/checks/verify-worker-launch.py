#!/usr/bin/env python3
"""Fixed credential-free worker launch probe, inside a delegated service.

Inspect the real host cgroup while a bounded container runs the production
command. Repeat to catch launcher migration and accidental nested cgroups.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'prototype'))
from andrewbot.process_limits import run_bounded
from andrewbot.runner_service import RunnerService,WorkerConfig


def verify(config):
    runner=RunnerService(config)
    parent=config.cgroup_parent
    assert parent and Path('/proc/self/cgroup').read_text().strip()=='0::'+parent+'/supervisor'
    code="""import json,time
from pathlib import Path
p=Path('/sys/fs/cgroup')
print(json.dumps({k:(p/k).read_text().strip() for k in ('cpu.max','memory.max','pids.max')}))
time.sleep(2)
"""
    payload=json.dumps({'type':'python','code':code}).encode()
    for _ in range(2):
        name='andrew-tool-'+uuid.uuid4().hex
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(run_bounded,runner._podman_args(name),payload=payload,
                                   timeout=25,output_limit=8192)
                contained=False
                while not future.done():
                    inspection=run_bounded([config.podman_binary,'inspect','--format','{{.State.Pid}}',name],
                                           timeout=3,output_limit=64)
                    pid=inspection.stdout.decode().strip()
                    if not inspection.returncode and pid.isdigit() and int(pid)>0:
                        try:
                            cgroup=Path('/proc/'+pid+'/cgroup').read_text().strip()
                            contained=cgroup.startswith('0::'+parent+'/libpod-')
                            break
                        except FileNotFoundError:
                            pass
                    time.sleep(.1)
                result=future.result()
            if result.returncode or result.timed_out or result.output_exceeded:
                # Only the fixed probe above is submitted; no owner data or
                # credential is loaded. Bounded Podman startup stderr is safe.
                print(json.dumps({'podman_launch_status':result.returncode,
                      'podman_launch_error':result.stderr.decode(errors='replace')[:3000]}),flush=True)
                raise RuntimeError('fixed worker launch failed')
            worker=runner._worker_result(result.stdout)
            assert worker['status']=='ok'
            assert json.loads(worker['stdout'])=={'cpu.max':'200000 100000',
                   'memory.max':'2147483648','pids.max':'256'}
            assert contained,'worker escaped the delegated launcher service'
            assert Path('/proc/self/cgroup').read_text().strip()=='0::'+parent+'/supervisor'
        finally:
            assert runner._remove_container(name),'worker probe cleanup failed'
    return {'repeated_worker_launch':'passed','worker_service_containment':'passed','worker_limits':'passed'}


def main():
    values=dict(line.strip().split('=',1) for line in Path('/etc/andrew/runner.conf').read_text().splitlines()
                if '=' in line and not line.startswith('#'))
    config=WorkerConfig(values['IMAGE'],os.environ['ANDREW_PROBE_WORKSPACE'],values['PROXY_SOCKET'],
                        cgroup_parent=os.environ['ANDREW_CGROUP_PARENT'])
    print(json.dumps(verify(config)),flush=True)


if __name__=='__main__': main()
