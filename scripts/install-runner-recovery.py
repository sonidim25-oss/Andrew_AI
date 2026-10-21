#!/usr/bin/env python3
"""Build and verify both packages before a coordinated, initially disabled rollout."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from contextlib import contextmanager

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'prototype'))
from andrewbot.memory_backup import create_paired_recovery_snapshot,create_runner_recovery_snapshot,_atomic_write
from andrewbot.runtime_readiness import code_digest

CONTRACT='runner_result_recovery_v1'
MANIFEST=ROOT/'scripts/runner-recovery-manifest.json'
UNITS=('andrew-control.service','andrew-letta.service','andrew-runner.service','andrew-runner.socket')
DEFAULT_PATHS=dict(controller='/opt/andrew/prototype/andrewbot',runner='/opt/andrew/runtime-services/andrewbot',
    database='/var/lib/andrew/control/controller.sqlite3',backend_home='/var/lib/andrew/letta',runner_root='/var/lib/andrew/runtime/runner-receipts',
    adoption='/etc/andrew/runner-receipt-adoption.json',installing='/etc/andrew/runner-recovery-installing.json',
    artifacts='/opt/andrew-letta-recovery/releases',overrides='/etc/systemd/system',guard='/opt/andrew-runner-recovery/check-runner-adoption.py',credential_dir='/etc/credstore.encrypted')


def helper(name):
    spec=importlib.util.spec_from_file_location(name.replace('-','_'),ROOT/'scripts'/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def tree(root):
    return helper('install-letta-recovery').tree_hashes(root,ignore_bytecode=True)


def _write(path,data,mode=0o600):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
    _atomic_write(path,data if isinstance(data,bytes) else (json.dumps(data,sort_keys=True,indent=2)+'\n').encode())
    path.chmod(mode)


def _package(role,live,target,manifest):
    live,target=Path(live),Path(target)
    originals=tree(live)
    shutil.copytree(live,target,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name,entry in manifest[role].items():
        proposed=ROOT/'prototype/andrewbot'/name
        if sha(proposed)!=entry['tested']:raise ValueError('Reviewed source changed: '+name)
        actual=originals.get(name)
        if actual not in {entry.get('baseline'),entry['tested']} and not (actual is None and entry['allow_missing']):
            raise ValueError('Unknown deployed '+role+' module: '+name)
        (target/name).write_bytes(proposed.read_bytes())
    return originals


def build_candidate(backend_release,controller_sources,runner_sources,directory):
    manifest=json.loads(MANIFEST.read_text());directory=Path(directory)
    if manifest.get('contract')!=CONTRACT or manifest.get('version')!=1:raise ValueError('Unsupported deployment contract')
    if any(sha(ROOT/name)!=expected for name,expected in manifest.get('assets',{}).items()):raise ValueError('Reviewed deployment asset changed')
    release_hash=helper('install-letta-recovery').verify_release(backend_release)
    if directory.exists() or directory.is_symlink():raise ValueError('Candidate destination exists')
    directory.mkdir(parents=True,mode=0o700);source=directory/'source';source.mkdir()
    originals={}
    for role,live,target in [('controller',controller_sources,source/'prototype/andrewbot'),('runner',runner_sources,source/'runtime-services/andrewbot')]:
        target.parent.mkdir(parents=True,exist_ok=True)
        originals[role]=_package(role,live,target,manifest)
    for name in ('scripts','letta_recovery','runtime','skills','systemd'):
        shutil.copytree(ROOT/name,source/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copytree(ROOT/'prototype/tests',source/'prototype/tests',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    if (ROOT/'docs').exists():shutil.copytree(ROOT/'docs',source/'docs')
    # Legacy installer tests validate their own current source bindings too.
    for name,field in [('p1-fix-manifest.json',None),('autonomy-media-manifest.json','modules'),('letta-recovery-manifest.json','controller')]:
        path=source/'scripts'/name;document=json.loads(path.read_text());entries=document[field] if field else document
        for module,entry in entries.items():
            proposed=source/'prototype/andrewbot'/module
            if proposed.is_file():
                if isinstance(entry,dict):entry['tested']=sha(proposed)
                else:entries[module]=sha(proposed)
        _write(path,document)
    # copytree preserves developer write bits; accepted root sources must be protected.
    for path in [source,*source.rglob('*')]:
        if path.is_symlink():raise ValueError('Unsafe candidate source link')
        path.chmod(path.stat().st_mode & ~0o022)
    candidate=dict(contract=CONTRACT,version=1,directory=str(directory.resolve()),source=str(source.resolve()),
        backend_release=str(Path(backend_release).resolve()),backend_release_sha256=release_hash,manifest_sha256=sha(MANIFEST),
        originals=originals,controller_files=tree(source/'prototype/andrewbot'),runner_files=tree(source/'runtime-services/andrewbot'),source_files=tree(source))
    _write(directory/'candidate.json',candidate)
    return candidate


def verify_candidate(candidate,*,require_acceptance=False,require_privileged=True):
    if (candidate.get('contract'),candidate.get('version'))!=(CONTRACT,1):raise ValueError('Invalid runner candidate')
    directory=Path(candidate['directory']);source=Path(candidate['source'])
    if json.loads((directory/'candidate.json').read_text())!=candidate:raise ValueError('Candidate record changed')
    if sha(MANIFEST)!=candidate['manifest_sha256'] or tree(source)!=candidate['source_files']:raise ValueError('Candidate sources changed')
    if helper('install-letta-recovery').verify_release(candidate['backend_release'])!=candidate['backend_release_sha256']:
        raise ValueError('Candidate backend changed')
    if require_acceptance:
        record=json.loads((directory/'acceptance.json').read_text())
        required={'node','core','scripts','pristine_protocol','service_imports'}
        if require_privileged:required.add('distinct_uid')
        if record.get('candidate_sha256')!=sha(directory/'candidate.json') or any(record.get('checks',{}).get(k)!='passed' for k in required):
            raise ValueError('Candidate acceptance incomplete')
        for name,expected in record.get('logs',{}).items():
            if sha(directory/name)!=expected:raise ValueError('Candidate acceptance evidence changed')
    return dict(verified=True,automatic_resume=False)


def core_acceptance_inputs(source):
    source=Path(source);result={}
    for prefix in ('prototype/andrewbot','prototype/tests','skills','letta_recovery','runtime'):
        if not (source/prefix).is_dir():raise ValueError('Core acceptance inputs are missing')
        result.update({prefix+'/'+name:value for name,value in tree(source/prefix).items()})
    return result


def reuse_core_acceptance(source,evidence_path):
    # Evidence is selected explicitly by the operator. Bind the saved success
    # to every controller/test/skill/backend input and the same Python binary.
    read=helper('inspect_owner_handoff').read
    evidence_path=Path(evidence_path);proof=json.loads(read(evidence_path))
    if (not isinstance(proof,dict) or proof.get('contract')!='runner_core_acceptance_evidence_v1'
            or proof.get('python_version')!=sys.version or proof.get('python_sha256')!=sha(sys.executable)):
        raise ValueError('Core acceptance evidence runtime differs')
    if proof.get('inputs')!=core_acceptance_inputs(source):raise ValueError('Core acceptance inputs differ')
    if proof.get('command')!=['python3','-m','unittest','discover','-s','prototype/tests','-q']:
        raise ValueError('Core acceptance command differs')
    raw=read(evidence_path.parent/'core.txt')
    if (hashlib.sha256(raw).hexdigest()!=proof.get('log_sha256')
            or not re.search(rb'(?m)^Ran [1-9][0-9]* tests in [0-9.]+s\r?$',raw)
            or not re.search(rb'(?m)^OK(?: \(skipped=[0-9]+\))?\s*\Z',raw)):
        raise ValueError('Core acceptance log is changed or unsuccessful')
    return raw,proof


def run_acceptance(candidate,pristine_binary,*,privileged=False,core_evidence=None):
    verify_candidate(candidate);directory=Path(candidate['directory']);source=Path(candidate['source']);release=Path(candidate['backend_release'])
    pin=json.loads((ROOT/'letta_recovery/base.json').read_text())
    if sha(pristine_binary)!=pin['sha256']:raise ValueError('Unexpected pristine backend')
    environment={**os.environ,'PYTHONPATH':os.pathsep.join(map(str,[source/'prototype',source/'scripts',source/'scripts/tests'])),
        'ANDREW_LOCAL_RECOVERY_TEST_RELEASE':str(release),'ANDREW_LETTA_TEST_BINARY':str(release/'letta.js'),
        'ANDREW_LETTA_PRISTINE_BINARY':str(Path(pristine_binary).resolve())}
    environment.pop('ANDREW_LOCAL_RECOVERY_ARTIFACT',None)
    checks={};logs={};commands={};reused={}
    saved_core=reuse_core_acceptance(source,core_evidence) if core_evidence is not None else None
    jobs=[('node',['node','--test',*map(str,sorted((source/'letta_recovery/tests').glob('*.test.mjs')))]),
        ('core',[sys.executable,'-m','unittest','discover','-s','prototype/tests','-q']),
        ('scripts',[sys.executable,'-m','unittest','discover','-s','scripts/tests','-q'])]
    for name,command in jobs:
        if name=='core' and saved_core is not None:
            print('Reusing passing core evidence for identical code and test inputs',flush=True)
            raw,proof=saved_core
            _write(directory/'core-acceptance.txt',raw)
            _write(directory/'core-reused-evidence.json',proof)
            checks[name]='passed';logs['core-acceptance.txt']=sha(directory/'core-acceptance.txt')
            logs['core-reused-evidence.json']=sha(directory/'core-reused-evidence.json')
            commands[name]=proof['command'];reused[name]={'evidence':'core-reused-evidence.json','tested_commit':proof.get('tested_commit')}
            continue
        print('Running candidate '+name+' checks',flush=True)
        with (directory/(name+'-acceptance.txt')).open('w') as output:
            result=subprocess.run(command,cwd=source,env=environment,stdout=output,stderr=subprocess.STDOUT,timeout=1200)
        if result.returncode:raise ValueError('Candidate '+name+' acceptance failed; production unchanged')
        checks[name]='passed';logs[name+'-acceptance.txt']=sha(directory/(name+'-acceptance.txt'));commands[name]=command
    service_env={**environment,'PYTHONPATH':str(source/'runtime-services')}
    command=[sys.executable,'-c','from andrewbot.runner_service import RunnerSocketServer; from andrewbot.runner_adoption import startup_record; from andrewbot.runner_receipts import RunnerReceiptStore']
    result=subprocess.run(command,cwd=source,env=service_env,capture_output=True,text=True,timeout=30)
    _write(directory/'service-imports.txt',(result.stdout+result.stderr).encode())
    if result.returncode:raise ValueError('Runner service dependency acceptance failed')
    checks['service_imports']='passed';logs['service-imports.txt']=sha(directory/'service-imports.txt');commands['service_imports']=command
    environment.pop('ANDREW_LOCAL_RECOVERY_TEST_RELEASE',None);environment['ANDREW_LETTA_TEST_BINARY']=str(Path(pristine_binary).resolve())
    command=[sys.executable,'-m','unittest','discover','-s','scripts/tests','-p','test_*protocol.py','-q']
    with (directory/'pristine-protocol-acceptance.txt').open('w') as output:
        result=subprocess.run(command,cwd=source,env=environment,stdout=output,stderr=subprocess.STDOUT,timeout=600)
    if result.returncode:raise ValueError('Pristine compatibility acceptance failed')
    checks['pristine_protocol']='passed';logs['pristine-protocol-acceptance.txt']=sha(directory/'pristine-protocol-acceptance.txt');commands['pristine_protocol']=command
    if privileged:
        result=helper('runner-recovery-privileged-probe').probe(source/'runtime-services/andrewbot',pwd.getpwnam('andrew-runtime').pw_uid,pwd.getpwnam('andrew-runtime').pw_gid)
        if result.get('verified') is not True:raise ValueError('Distinct UID credential acceptance failed')
        _write(directory/'distinct-uid-acceptance.json',result);checks['distinct_uid']='passed';logs['distinct-uid-acceptance.json']=sha(directory/'distinct-uid-acceptance.json')
    verify_candidate(candidate)
    evidence=dict(candidate_sha256=sha(directory/'candidate.json'),checks=checks,logs=logs,commands=commands,reused_checks=reused,production_changed=False)
    _write(directory/'acceptance.json',evidence)
    return evidence


def _service(action,unit=None):
    if action=='assert-stopped':
        result=subprocess.run(['systemctl','show',unit,'-p','ActiveState','--value'],check=True,capture_output=True,text=True)
        if result.stdout.strip() not in {'inactive','failed'}:raise ValueError('Service is not stopped')
    else:subprocess.run(['systemctl',action,*([unit] if unit else [])],check=True,capture_output=True)


@contextmanager
def _database(path,*,uri=False):
    db=sqlite3.connect(path,uri=uri)
    try:
        with db:yield db
    finally:db.close()


def _owner(database):
    with _database(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as db:
        state=dict(db.execute('SELECT key,value FROM state'))
    return {k:state.get(k,'') for k in ('owner_control_revision','paused','sleeping','sleep_until','session_start_requested','active_session_id','active_cycle_id','autonomous_owner_id','model_turn_origin')}


def _checkpoint(database):
    helper('install-letta-recovery').checkpoint_controller_wal(Path(database))


def _record(directory,phase,**fields):
    _write(Path(directory)/'installation.json',dict(phase=phase,**fields))


def _provision(paths,controller_uid,runner_uid,runner_gid):
    credentials=helper('provision-runner-receipts-key');root=Path(paths['runner_root'])
    credentials.provision(paths['credential_dir'],root)
    with _database(paths['database']) as db:
        state=dict(db.execute('SELECT key,value FROM state'));controller_id=state.get('local_recovery_controller_store_id') or uuid.uuid4().hex
    key=credentials.decrypt(Path(paths['credential_dir'])/credentials.NAME)
    root.parent.mkdir(parents=True,exist_ok=True);root.mkdir(mode=0o700,exist_ok=True);os.chown(root,runner_uid,runner_gid)
    program='''import json,os,sys
from andrewbot.runner_receipts import RunnerReceiptStore
s=RunnerReceiptStore(sys.argv[1],sys.stdin.buffer.read(),expected_uid=os.getuid())
try:
 if s._identity is None:s.provision(controller_store_id=sys.argv[2])
 print(json.dumps(s._identity))
finally:s.close()
'''
    result=subprocess.run([sys.executable,'-c',program,str(root),controller_id],input=key,check=True,capture_output=True,
        env={**os.environ,'PYTHONPATH':str(Path(paths['runner']).parent)},cwd=str(Path(paths['runner']).parent),user=runner_uid,group=runner_gid,extra_groups=[])
    identity=json.loads(result.stdout)
    if identity['controller_store_id']!=controller_id:raise ValueError('Existing runner controller pin changed')
    with _database(paths['database']) as db:
        for k,v in dict(local_recovery_controller_store_id=controller_id,runner_receipt_store_id=identity['runner_store_id'],runner_receipts_enabled='0',runner_receipt_backend_capability='0').items():
            db.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,v))
    _checkpoint(paths['database'])
    return identity


def _runtime_snapshot(paths):
    spec=importlib.util.spec_from_file_location('runner_runtime_readiness',Path(paths['controller'])/'runtime_readiness.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    proof=json.loads(module.protected_read(module.PROOF));bindings=module.bindings();module.check_proof(proof,bindings)
    return dict(proof=proof,policy={str(p):sha(p) for p in module.POLICY_FILES},image=bindings['image'])


def _renew_runtime(paths,before):
    spec=importlib.util.spec_from_file_location('runner_runtime_readiness_after',Path(paths['controller'])/'runtime_readiness.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    if any(sha(name)!=value for name,value in before['policy'].items()):raise ValueError('Runtime policy changed during rollout')
    bindings=module.bindings()
    if bindings['image']!=before['image']:raise ValueError('Worker image changed during rollout')
    proof={**before['proof'],**bindings};module.check_proof(proof,bindings);_write(module.PROOF,proof,0o644)


def _remove_fence(path):
    path=Path(path);path.unlink();fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def _no_workers(paths,runner_uid):
    account=pwd.getpwuid(runner_uid)
    environment={**os.environ,'HOME':str(Path(paths['runner_root']).parent),'XDG_RUNTIME_DIR':'/run/user/'+str(runner_uid),
        'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(runner_uid)+'/bus'}
    result=subprocess.run(['/usr/bin/podman','ps','-aq','--filter','label=org.andrew.runtime=managed'],
        env=environment,cwd=str(Path(paths['runner']).parent),user=runner_uid,group=account.pw_gid,extra_groups=os.getgrouplist(account.pw_name,account.pw_gid),
        check=True,capture_output=True,timeout=20)
    if result.stdout.strip():raise ValueError('Disposable worker still exists; installation remains stopped')


def _migrate_controller(paths,controller_uid):
    account=pwd.getpwuid(controller_uid)
    program="""import sys
from andrewbot.store import Store
store=Store(sys.argv[1])
try:
 for table in ('runner_operations','runner_imports','runner_research_results'):store.connection.execute('SELECT 1 FROM '+table+' LIMIT 1')
 if store.connection.execute('PRAGMA foreign_key_check').fetchone() is not None:raise ValueError('Controller schema has invalid links')
finally:store.close()
"""
    identity=dict(user=controller_uid,group=account.pw_gid)
    if os.geteuid()==0:identity['extra_groups']=os.getgrouplist(account.pw_name,account.pw_gid)
    subprocess.run([sys.executable,'-c',program,str(paths['database'])],check=True,capture_output=True,timeout=60,
        env={**os.environ,'PYTHONPATH':str(Path(paths['controller']).parent)},cwd=str(Path(paths['controller']).parent),**identity)
    _checkpoint(paths['database'])


def activate(candidate,*,paths=None,service=_service,provision=None,uid=None,runtime_snapshot=_runtime_snapshot,renew_runtime=_renew_runtime,no_workers=_no_workers):
    verify_candidate(candidate,require_acceptance=True)
    paths={**DEFAULT_PATHS,**(paths or {})};directory=Path(candidate['directory']);source=Path(candidate['source'])
    for role in ('controller','runner'):
        if tree(paths[role])!=candidate['originals'][role]:raise ValueError('Deployed '+role+' changed; production unchanged')
    if (Path(paths['overrides'])/'andrew-letta.service.d/zzz-local-recovery-rollback.conf').exists():raise ValueError('Retained rollback override requires reconciliation')
    if uid is None:
        control=pwd.getpwnam('andrew-control');runner=pwd.getpwnam('andrew-runtime');uid=(control.pw_uid,runner.pw_uid,runner.pw_gid)
    before_owner=_owner(paths['database'])
    if before_owner['paused']!='1':raise ValueError('Pause Andrew before coordinated installation; owner controls are preserved')
    runtime_before=runtime_snapshot(paths)
    _write(directory/'runtime-proof-before.json',runtime_before)
    _write(paths['installing'],dict(contract=CONTRACT,reason='installation_incomplete'),0o644)
    # No exception path restarts a partially installed package.
    try:
        _record(directory,'stopping',owner=before_owner,paths=paths)
        for unit in UNITS:service('stop',unit)
        for unit in UNITS:service('assert-stopped',unit)
        _checkpoint(paths['database'])
        no_workers(paths,uid[1])
        _write(paths['installing'],dict(contract=CONTRACT,reason='installation_incomplete'),0o644)
        _record(directory,'stopped',owner=before_owner)
        for role in ('controller','runner'):
            shutil.copytree(paths[role],directory/'code-before'/role,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        _write(directory/'adoption-before.json',dict(content=Path(paths['adoption']).read_text() if Path(paths['adoption']).exists() else None))
        if (Path(paths['runner_root'])/'identity.json').exists():
            create_runner_recovery_snapshot(paths['database'],paths['backend_home'],paths['runner_root'],directory/'three-store-before',workers_quiescent=True)
        create_paired_recovery_snapshot(paths['database'],paths['backend_home'],directory/'paired-before',workers_quiescent=True)
        _write(paths['guard'],(source/'scripts/check-runner-adoption.py').read_bytes(),0o555)
        overrides={}
        overrides_before={}
        for unit in UNITS[:-1]:
            path=Path(paths['overrides'])/(unit+'.d/zz-runner-recovery.conf')
            overrides_before[str(path)]=path.read_text() if path.exists() else None
            _write(directory/'overrides-before.json',overrides_before)
            text='[Service]\nExecStartPre=+/usr/bin/python3 '+paths['guard']+'\n'
            if unit=='andrew-runner.service':text+='Environment=ANDREW_RUNNER_RECEIPTS_ENABLED=0\nLoadCredentialEncrypted=andrew-runner-receipts-key:'+paths['credential_dir']+'/andrew-runner-receipts-key\n'
            if unit=='andrew-control.service':text+='Environment=ANDREW_RUNNER_RECEIPTS_ENABLED=0\nEnvironment=ANDREW_LOCAL_RECOVERY_ENABLED=1\n'
            if unit=='andrew-letta.service':text+='ExecStart=\nExecStart=/usr/bin/node '+candidate['backend_release']+'/letta.js server --backend local --listen ws://127.0.0.1:8283 --openai-api\nEnvironment=ANDREW_LOCAL_RECOVERY_ARTIFACT=1\n'
            _write(path,text.encode(),0o644);overrides[str(path)]=sha(path)
        _record(directory,'guards_installed',owner=before_owner)
        installed_release=Path(paths['artifacts'])/candidate['backend_release_sha256']
        if not installed_release.exists():
            installed_release.parent.mkdir(parents=True,exist_ok=True)
            shutil.copytree(candidate['backend_release'],installed_release,symlinks=True)
            for item in [installed_release,*installed_release.rglob('*')]:
                if not item.is_symlink():item.chmod(0o755 if item.is_dir() or item.stat().st_mode&0o111 else 0o644)
        if helper('install-letta-recovery').verify_release(installed_release)!=candidate['backend_release_sha256']:raise ValueError('Installed backend release differs')
        backend_override=Path(paths['overrides'])/'andrew-letta.service.d/zz-runner-recovery.conf'
        _write(backend_override,backend_override.read_bytes().replace(candidate['backend_release'].encode(),str(installed_release).encode()),0o644)
        overrides[str(backend_override)]=sha(backend_override)
        for role,subdir in [('controller','prototype'),('runner','runtime-services')]:
            package=source/subdir/'andrewbot';target=Path(paths[role])
            for path in package.glob('*.py'):_write(target/path.name,path.read_bytes(),0o644)
            for cache in target.rglob('__pycache__'):
                if cache.is_symlink():raise ValueError('Unsafe installed bytecode cache')
                shutil.rmtree(cache)
            if tree(target)!=candidate[role+'_files']:raise ValueError('Installed package differs from accepted candidate')
            _record(directory,role+'_installed',owner=before_owner)
        _migrate_controller(paths,uid[0])
        _record(directory,'controller_schema_ready',owner=before_owner)
        identity=(provision or _provision)(paths,*uid)
        create_runner_recovery_snapshot(paths['database'],paths['backend_home'],paths['runner_root'],directory/'three-store-before-adoption',workers_quiescent=True)
        record=dict(contract=CONTRACT,version=1,mode='disabled',backend_store_id=json.loads((directory/'three-store-before-adoption/runner-recovery.json').read_text())['backend_store_id'],controller_code=code_digest(paths['controller']),service_code=code_digest(paths['runner']),
            controller_uid=uid[0],runner_uid=uid[1],backend_release=str(installed_release),backend_release_sha256=candidate['backend_release_sha256'],service_overrides=overrides,**{k:identity[k] for k in ('controller_store_id','runner_store_id','key_fingerprint')})
        if _owner(paths['database'])!=before_owner:raise ValueError('Owner controls changed during installation')
        _write(paths['adoption'],record,0o644)
        # Existing network acceptance is retained only for byte-identical policy
        # inputs; coordinated code/adoption acceptance is separately bound here.
        renew_runtime(paths,runtime_before)
        _record(directory,'installed_disabled',owner=before_owner,adoption_sha256=sha(paths['adoption']),paths=paths)
        _remove_fence(paths['installing'])
        service('daemon-reload',None)
        for unit in ('andrew-letta.service','andrew-runner.socket','andrew-runner.service'):service('start',unit)
        return dict(installed=True,automatic_resume=False,execution_enabled=False,evidence=str(directory))
    except BaseException:
        _write(paths['installing'],dict(contract=CONTRACT,reason='installation_interrupted'),0o644)
        for unit in UNITS:
            try:service('stop',unit)
            except Exception:pass
        _record(directory,'interrupted',owner=before_owner,paths=paths)
        raise


def live_peers(paths,record):
    control=pwd.getpwnam('andrew-control')
    program="""import json,sys
from andrewbot.runner_client import RunnerClient
from andrewbot.appserver import AppServerClient
from andrewbot.recovery import LocalRecoveryClient
record=json.loads(sys.argv[1]);runner=RunnerClient('/run/andrew/runner.sock')
receiver=runner.receipt_capabilities(expected_runner_uid=record['runner_uid'])
client=LocalRecoveryClient(AppServerClient('ws://127.0.0.1:8283/ws'))
try:backend=client.capabilities()
finally:client.close()
print(json.dumps(dict(backend_contract='runner_result_recovery_v1' if backend.get('runner_result_recovery_v1')==1 else None,
 backend_store_id=backend.get('store_id'),held_tool_result_v1=backend.get('held_tool_result_v1'),receiver=receiver)))
"""
    result=subprocess.run([sys.executable,'-c',program,json.dumps(record)],check=True,capture_output=True,text=True,
        env={**os.environ,'PYTHONPATH':str(Path(paths['controller']).parent)},cwd=str(Path(paths['controller']).parent),user=control.pw_uid,group=control.pw_gid,
        extra_groups=os.getgrouplist(control.pw_name,control.pw_gid),timeout=30)
    return json.loads(result.stdout)


def enable(candidate,*,paths=None,service=_service,peers=None,runtime_snapshot=_runtime_snapshot,renew_runtime=_renew_runtime):
    verify_candidate(candidate,require_acceptance=True)
    directory=Path(candidate['directory']);installed=json.loads((directory/'installation.json').read_text())
    if installed.get('phase')!='installed_disabled':raise ValueError('Installation is incomplete')
    paths={**DEFAULT_PATHS,**(paths or installed.get('paths',{}))}
    record=json.loads(Path(paths['adoption']).read_text())
    if sha(paths['adoption'])!=installed['adoption_sha256'] or _owner(paths['database'])!=installed['owner']:
        raise ValueError('Owner or adoption state changed')
    for role in ('controller','runner'):
        if tree(paths[role])!=candidate[role+'_files']:raise ValueError('Installed package changed')
    for name,expected in record['service_overrides'].items():
        if sha(name)!=expected:raise ValueError('Service overrides changed')
    if Path(paths['installing']).exists():raise ValueError('Installation is fenced')
    runtime_before=runtime_snapshot(paths)
    peers=live_peers(paths,record) if peers is None else peers
    receiver=peers.get('receiver',{})
    if (peers.get('backend_contract')!=CONTRACT or peers.get('backend_store_id')!=record['backend_store_id'] or peers.get('held_tool_result_v1')!=1
            or receiver.get('contract')!=CONTRACT or receiver.get('version')!=1 or receiver.get('formatter')!='runner_text_v1'
            or receiver.get('runner_store_id')!=record['runner_store_id'] or receiver.get('controller_store_id')!=record['controller_store_id']
            or receiver.get('execution_enabled') is not False):
        raise ValueError('Recovery peers do not match')
    _write(paths['installing'],dict(contract=CONTRACT,reason='enable_incomplete'),0o644)
    try:
        _record(directory,'enabling_stopping',owner=installed['owner'],paths=paths)
        for unit in ('andrew-control.service','andrew-runner.service','andrew-runner.socket'):service('stop',unit)
        for unit in ('andrew-control.service','andrew-runner.service','andrew-runner.socket'):service('assert-stopped',unit)
        if _owner(paths['database'])!=installed['owner']:raise ValueError('Owner controls changed')
        with _database(paths['database']) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("INSERT INTO state VALUES('runner_receipts_enabled','1') ON CONFLICT(key) DO UPDATE SET value='1'")
        _checkpoint(paths['database'])
        for unit in ('andrew-control.service','andrew-runner.service'):
            path=Path(paths['overrides'])/(unit+'.d/zz-runner-recovery.conf')
            _write(path,path.read_bytes().replace(b'ANDREW_RUNNER_RECEIPTS_ENABLED=0',b'ANDREW_RUNNER_RECEIPTS_ENABLED=1'),0o644)
        record['mode']='enabled';record['service_overrides']={name:sha(name) for name in record['service_overrides']}
        _write(paths['adoption'],record,0o644);renew_runtime(paths,runtime_before)
        if _owner(paths['database'])!=installed['owner']:raise ValueError('Owner controls changed')
        _record(directory,'enabled',owner=installed['owner'],paths=paths,adoption_sha256=sha(paths['adoption']))
        _remove_fence(paths['installing']);service('daemon-reload',None)
        for unit in ('andrew-runner.socket','andrew-runner.service','andrew-control.service'):service('start',unit)
        return dict(enabled=True,owner_controls_preserved=True,wake_requested=False)
    except BaseException:
        _write(paths['installing'],dict(contract=CONTRACT,reason='enable_interrupted'),0o644)
        for unit in UNITS:
            try:service('stop',unit)
            except Exception:pass
        _record(directory,'enable_interrupted',owner=installed['owner'],paths=paths)
        raise


def rollback(candidate,*,paths=None):
    paths={**DEFAULT_PATHS,**(paths or {})}
    if Path(paths['adoption']).exists() or (Path(paths['runner_root'])/'identity.json').exists():
        raise ValueError('Adopted runner stores cannot roll back to feature-unaware code')
    raise ValueError('Pre-adoption rollback requires retaining current stores and an explicit offline restore; automatic rollback is disabled')


def _trusted_directory(directory):
    path=Path(directory).absolute()
    for part in [path,*path.parents]:
        info=part.lstat()
        if part.is_symlink() or info.st_uid!=0 or info.st_mode&0o022:raise ValueError('Deployment candidate must be administrator-owned')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--backend-release');parser.add_argument('--directory',required=True)
    parser.add_argument('--controller-sources',default=DEFAULT_PATHS['controller']);parser.add_argument('--runner-sources',default=DEFAULT_PATHS['runner'])
    parser.add_argument('--core-evidence',help='Reuse a saved passing core log only for identical validated inputs')
    parser.add_argument('--pristine-binary');parser.add_argument('--prepare',action='store_true');parser.add_argument('--apply',action='store_true');parser.add_argument('--enable',action='store_true')
    args=parser.parse_args(argv)
    if args.apply or args.enable:
        if os.geteuid()!=0:parser.error('Deployment requires root')
        _trusted_directory(args.directory)
        candidate=json.loads((Path(args.directory)/'candidate.json').read_text())
        print(json.dumps(enable(candidate) if args.enable else activate(candidate)))
        return
    if not args.backend_release or not args.pristine_binary:parser.error('--backend-release and --pristine-binary are required')
    candidate=build_candidate(args.backend_release,args.controller_sources,args.runner_sources,args.directory)
    print(json.dumps(dict(candidate=str(Path(args.directory)/'candidate.json'),production_changed=False)),flush=True)
    if args.prepare:print(json.dumps(run_acceptance(candidate,args.pristine_binary,privileged=os.geteuid()==0,core_evidence=args.core_evidence)))


if __name__=='__main__':
    try:main()
    except Exception as error:raise SystemExit('Runner recovery preparation stopped: '+str(error))
