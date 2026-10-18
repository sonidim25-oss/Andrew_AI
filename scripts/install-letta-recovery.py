#!/usr/bin/env python3
"""Prepare an exact local recovery candidate; initial activation keeps resume off."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import grp
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid

ROOT=Path(__file__).resolve().parents[1]
TARGET=Path('/opt/andrew/prototype/andrewbot')
DATABASE=Path('/var/lib/andrew/control/controller.sqlite3')
BACKEND_HOME=Path('/var/lib/andrew/letta')
ARTIFACTS=Path('/opt/andrew-letta-recovery/releases')
BACKEND_DROPIN=Path('/etc/systemd/system/andrew-letta.service.d/zz-local-recovery.conf')
CONTROL_DROPIN=Path('/etc/systemd/system/andrew-control.service.d/zz-local-recovery.conf')
ROLLBACK_DROPIN=BACKEND_DROPIN.with_name('zzz-local-recovery-rollback.conf')
MANIFEST=ROOT/'scripts/letta-recovery-manifest.json'
NEW_MODULES={'recovery.py','recovery_dispatch.py','recovery_records.py'}
CONTRACT='local_durable_recovery_v1'
CHECKPOINT_PROGRAM='''import json,sqlite3,sys
db=sqlite3.connect(sys.argv[1],timeout=5)
try:
    db.execute('PRAGMA synchronous=FULL')
    if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
        raise SystemExit('Controller database integrity check failed')
    result=db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
    if result not in ((0,0,0),(0,-1,-1)):
        raise SystemExit('Controller WAL checkpoint is busy')
    print(json.dumps(result))
finally:
    db.close()
'''
sys.path.insert(0,str(ROOT/'prototype'))
from andrewbot.memory_backup import create_paired_recovery_snapshot,verify_paired_recovery_snapshot,_recovery_tree


def helper(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def tree_hashes(root,*,ignore_bytecode=False):
    result={};root=Path(root)
    for path in sorted(root.rglob('*')):
        relative=path.relative_to(root).as_posix()
        if ignore_bytecode and ('__pycache__' in path.parts or path.suffix=='.pyc'):continue
        if path.is_symlink():
            target=os.readlink(path)
            if os.path.isabs(target) or not path.resolve().is_relative_to(root.resolve()):raise ValueError('Unsafe artifact symlink')
            result[relative]='link:'+target
        elif path.is_file():result[relative]=sha(path)
        elif not path.is_dir():raise ValueError('Unsafe artifact file')
    return result


def verify_release(release):
    release=Path(release)
    if release.is_symlink() or not release.is_dir():raise ValueError('Recovery release unavailable')
    manifest=json.loads((release/'recovery-release.json').read_text())
    pin=json.loads((ROOT/'letta_recovery/base.json').read_text())
    if (manifest.get('contract'),manifest.get('version'),manifest.get('base_sha256'))!=(CONTRACT,1,pin['sha256']):raise ValueError('Recovery artifact contract mismatch')
    source=(release/'letta.js').read_text()
    for patch in reversed(json.loads((ROOT/'letta_recovery/patches.json').read_text())):
        if source.count(patch['replacement'])!=1:raise ValueError('Unexpected recovery bundle patch')
        source=source.replace(patch['replacement'],patch['anchor'],1)
    if hashlib.sha256(source.encode()).hexdigest()!=pin['sha256']:raise ValueError('Recovery bundle cannot be verified against the pinned base')
    actual=tree_hashes(release);actual.pop('recovery-release.json',None)
    if actual!=manifest.get('files'):raise ValueError('Recovery artifact file hash mismatch')
    # Imported extension hashes matter even when bundle hooks have not changed.
    for name,value in manifest['files'].items():
        if name.startswith('recovery/'):
            path=ROOT/'letta_recovery'/name.removeprefix('recovery/')
            if not path.is_file() or value!=sha(path):raise ValueError('Recovery artifact differs from reviewed sources')
    return sha(release/'recovery-release.json')


def build_candidate(backend_release,deployed_sources,backup_dir):
    manifest=json.loads(MANIFEST.read_text());deployed_sources=Path(deployed_sources);backup_dir=Path(backup_dir)
    release_hash=verify_release(backend_release)
    if manifest.get('contract')!=CONTRACT or manifest.get('version')!=1:raise ValueError('Unsupported deployment manifest')
    if backup_dir.exists():raise ValueError('Candidate destination exists')
    backup_dir.mkdir(parents=True,mode=0o700)
    original=backup_dir/'original';original.mkdir(mode=0o700)
    source=backup_dir/'source';package=source/'prototype/andrewbot';package.mkdir(parents=True)
    merge=backup_dir/'merge';merge.mkdir()
    media=helper('install-autonomy-media')
    originals={};replacements={}
    for name,entry in manifest['controller'].items():
        proposed=ROOT/'prototype/andrewbot'/name
        if sha(proposed)!=entry['tested']:raise ValueError('Controller source changed since validation: '+name)
        target=deployed_sources/name
        if target.is_symlink():raise ValueError('Symlink in deployed controller')
        live=target.read_bytes() if target.exists() else None
        live_hash=hashlib.sha256(live).hexdigest() if live is not None else None
        if live is None:
            if name not in NEW_MODULES:raise ValueError('Required deployed module missing: '+name)
            replacement=proposed.read_bytes()
        elif live_hash==entry['tested']:replacement=live
        else:
            revision=entry.get('deployed_bases',{}).get(live_hash)
            if revision is None:raise ValueError('Unknown deployment source: '+name)
            base=subprocess.check_output(['git','-C',str(ROOT),'show',revision+':prototype/andrewbot/'+name])
            replacement=media.merge_source(live,base,proposed.read_bytes(),merge,name)
        originals[name]=live_hash;replacements[name]=replacement
        if live is not None:(original/name).write_bytes(live)
    for path in deployed_sources.rglob('*'):
        if path.is_symlink():raise ValueError('Symlink in deployed controller')
    shutil.copytree(deployed_sources,package,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name,data in replacements.items():(package/name).write_bytes(data)
    for directory in ('scripts','letta_recovery','runtime','skills','systemd'):
        live_skills=deployed_sources.parents[1]/'skills'
        origin=live_skills if directory=='skills' and live_skills.is_dir() else ROOT/directory
        tree_hashes(origin)
        shutil.copytree(origin,source/directory,ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT/'prototype/tests',source/'prototype/tests',ignore=shutil.ignore_patterns('__pycache__'))
    # Candidate-local manifests bind the exact reviewed merge, including preserved live changes.
    for filename in ('p1-fix-manifest.json','autonomy-media-manifest.json','letta-recovery-manifest.json'):
        path=source/'scripts'/filename;copied=json.loads(path.read_text())
        entries=copied['modules'] if filename.startswith('autonomy') else copied['controller'] if filename.startswith('letta') else copied
        for name,entry in list(entries.items()):
            if (package/name).is_file():
                if isinstance(entry,dict):entry['tested']=sha(package/name)
                else:entries[name]=sha(package/name)
        path.write_text(json.dumps(copied,sort_keys=True,indent=2)+'\n')
    # Original hashes, exact merged files and the complete backend manifest bind this candidate.
    candidate={'version':1,'contract':CONTRACT,'directory':str(backup_dir.resolve()),'source':str(source.resolve()),
        'backend_release':str(Path(backend_release).resolve()),'backend_manifest_sha256':release_hash,
        'deployed_files':tree_hashes(deployed_sources,ignore_bytecode=True),
        'deployment_manifest_sha256':sha(MANIFEST),'originals':originals,'controller_files':tree_hashes(package,ignore_bytecode=True),'source_files':tree_hashes(source,ignore_bytecode=True)}
    (backup_dir/'candidate.json').write_text(json.dumps(candidate,sort_keys=True,indent=2)+'\n')
    return candidate


def verify_candidate(candidate,*,require_acceptance=False):
    directory=Path(candidate['directory']);source=Path(candidate['source'])
    if (candidate.get('contract'),candidate.get('version'))!=(CONTRACT,1):raise ValueError('Unsupported candidate')
    if verify_release(candidate['backend_release'])!=candidate['backend_manifest_sha256']:raise ValueError('Candidate backend changed')
    if sha(MANIFEST)!=candidate['deployment_manifest_sha256']:raise ValueError('Deployment manifest changed')
    if tree_hashes(source,ignore_bytecode=True)!=candidate['source_files']:raise ValueError('Candidate validation inputs changed')
    if tree_hashes(source/'prototype/andrewbot',ignore_bytecode=True)!=candidate['controller_files']:raise ValueError('Merged controller candidate changed')
    if require_acceptance:
        evidence=json.loads((directory/'acceptance.json').read_text())
        if evidence.get('candidate_sha256')!=sha(directory/'candidate.json') or evidence.get('checks')!={'node':'passed','core':'passed','scripts':'passed','pristine_protocol':'passed'}:raise ValueError('Candidate acceptance incomplete')
    return {'verified':True,'automatic_resume':False,'backend_manifest_sha256':candidate['backend_manifest_sha256']}


def run_acceptance(candidate,pristine_binary):
    verify_candidate(candidate);source=Path(candidate['source']);directory=Path(candidate['directory'])
    pin=json.loads((ROOT/'letta_recovery/base.json').read_text())
    if sha(pristine_binary)!=pin['sha256']:raise ValueError('Unexpected pristine runtime; no services changed')
    environment={**os.environ,'PYTHONPATH':os.pathsep.join((str(source/'prototype'),str(source)))}
    environment['ANDREW_LETTA_PRISTINE_BINARY']=str(Path(pristine_binary).resolve())
    environment.pop('ANDREW_LOCAL_RECOVERY_ARTIFACT',None)
    release=str(Path(candidate['backend_release']))
    environment.update(ANDREW_LOCAL_RECOVERY_TEST_RELEASE=release,ANDREW_LETTA_TEST_BINARY=release+'/letta.js')
    checks=[('node',['node','--test',*map(str,sorted((source/'letta_recovery/tests').glob('*.test.mjs')))]),
        ('core',[sys.executable,'-m','unittest','discover','-s',str(source/'prototype/tests'),'-q']),
        ('scripts',[sys.executable,'-m','unittest','discover','-s',str(source/'scripts/tests'),'-q'])]
    for name,command in checks:
        result=subprocess.run(command,cwd=source,env=environment,capture_output=True,text=True,timeout=600)
        (directory/(name+'-acceptance.txt')).write_text(result.stdout+result.stderr)
        if result.returncode:raise ValueError('Candidate '+name+' checks failed; no services changed')
    environment.pop('ANDREW_LOCAL_RECOVERY_TEST_RELEASE',None);environment['ANDREW_LETTA_TEST_BINARY']=str(pristine_binary)
    result=subprocess.run([sys.executable,'-m','unittest','discover','-s',str(source/'scripts/tests'),'-p','test_*protocol.py','-q'],
        cwd=source,env=environment,capture_output=True,text=True,timeout=600)
    (directory/'pristine-protocol-acceptance.txt').write_text(result.stdout+result.stderr)
    if result.returncode:raise ValueError('Pristine protocol checks failed; no services changed')
    evidence={'candidate_sha256':sha(directory/'candidate.json'),
        'checks':{'node':'passed','core':'passed','scripts':'passed','pristine_protocol':'passed'}}
    (directory/'acceptance.json').write_text(json.dumps(evidence,sort_keys=True)+'\n')
    return evidence


def assert_rollback_exclusion(snapshot):
    """An older runtime may start only on an isolated, proven pre-admission snapshot."""
    metadata=verify_paired_recovery_snapshot(snapshot)
    home=Path(snapshot)/'backend'
    if any('local-recovery-v1/adopted/' in name for name in metadata['backend_files']):raise ValueError('Older runtime cannot read adopted snapshot; services remain stopped')
    for path in home.rglob('*'):
        if not path.is_file():continue
        relative=path.relative_to(home)
        if path.name.startswith('input-dispositions'):
            value=json.loads(path.read_text())
            if (not isinstance(value,dict) or value.get('version') not in (2,3,4)
                    or not isinstance(value.get('entries'),dict) or not isinstance(value.get('reservations'),dict)
                    or value['entries'] or value['reservations']):raise ValueError('Rollback input exclusion unproven; services remain stopped')
        elif 'state' in relative.parts:
            if not (path.name.endswith('.lock') and path.stat().st_size==0):raise ValueError('Rollback recovery metadata cannot be excluded; services remain stopped')
    return metadata


def _service(action,unit):
    h=helper('install-p1-fixes')
    if action=='assert-stopped':
        if h.command('systemctl','show',unit,'-p','ActiveState','--value') not in {'inactive','failed'}:raise ValueError('Service has not stopped')
        return 'stopped'
    return h.command('systemctl',action,*([unit] if unit else []))


def checkpoint_controller_wal(database):
    database=Path(database)
    if database.is_symlink() or not database.is_file():raise ValueError('Controller database unavailable')
    try:
        result=helper('install-p1-fixes').command('runuser','-u','andrew-control','--',
            '/usr/bin/python3','-c',CHECKPOINT_PROGRAM,str(database))
        if json.loads(result) not in ([0,0,0],[0,-1,-1]):raise ValueError()
    except (subprocess.CalledProcessError,ValueError,TypeError):
        raise ValueError('Controller WAL checkpoint did not finish; services remain stopped') from None


def load_accepted_candidate(directory):
    directory=Path(directory)
    info=directory.lstat()
    if directory.is_symlink() or not directory.is_dir() or info.st_uid!=os.geteuid() or info.st_mode&0o022:
        raise ValueError('Accepted candidate directory must be private and owned by the installer user')
    directory=directory.resolve();candidate=json.loads((directory/'candidate.json').read_text())
    if candidate.get('directory')!=str(directory) or candidate.get('source')!=str(directory/'source'):
        raise ValueError('Accepted candidate path changed')
    verify_candidate(candidate,require_acceptance=True)
    return candidate


def activate(candidate,enable_resume=False,*,service=_service,retire_projection=None):
    if enable_resume:raise ValueError('Initial activation must keep automatic resume disabled')
    verify_candidate(candidate,require_acceptance=True)
    directory=Path(candidate['directory']);h=helper('install-p1-fixes')
    if ROLLBACK_DROPIN.exists():raise ValueError('A retained rollback override requires reconciliation; no services changed')
    if tree_hashes(TARGET,ignore_bytecode=True)!=candidate['deployed_files']:raise ValueError('Deployed package changed; no services changed')
    for name,expected in candidate['originals'].items():
        path=TARGET/name;actual=sha(path) if path.exists() else None
        if path.is_symlink() or actual!=expected:raise ValueError('Deployed controller changed; no services changed')
    for name,path in [('backend',BACKEND_DROPIN),('controller',CONTROL_DROPIN)]:
        if path.is_symlink():raise ValueError('Symlink in service override')
        (directory/(name+'-override-before.json')).write_text(json.dumps({'content':path.read_text() if path.exists() else None}))
    service('stop','andrew-control.service');service('stop','andrew-letta.service')
    try:
        for unit in ('andrew-control.service','andrew-letta.service'):service('assert-stopped',unit)
        checkpoint_controller_wal(DATABASE)
        create_paired_recovery_snapshot(DATABASE,BACKEND_HOME,directory/'paired-before',workers_quiescent=True)
        if retire_projection:
            repair=helper('retire_unstarted_projection')
            retirement=repair.plan(DATABASE,BACKEND_HOME,retire_projection)
            repair.retire(retirement,directory/'paired-before')
            (directory/'retired-projection.json').write_text(json.dumps({'admission_id':retire_projection,
                'archive':retirement['archive'],'checkpoint_sha256':retirement['files'][retirement['checkpoint']]}))
        if tree_hashes(TARGET,ignore_bytecode=True)!=candidate['deployed_files']:raise ValueError('Deployed package changed during activation; services remain stopped')
        for name,expected in candidate['originals'].items():
            path=TARGET/name
            if path.is_symlink() or (sha(path) if path.exists() else None)!=expected:raise ValueError('Controller changed during activation; services remain stopped')
        release=ARTIFACTS/candidate['backend_manifest_sha256']
        if release.exists():
            if verify_release(release)!=candidate['backend_manifest_sha256']:raise ValueError('Installed recovery artifact differs')
        else:
            release.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
            for parent in [release.parent,*release.parent.parents]:
                info=parent.lstat()
                if parent.is_symlink() or info.st_uid!=0 or info.st_mode&0o022:raise ValueError('Recovery artifact parents must be root owned')
            shutil.copytree(candidate['backend_release'],release,symlinks=True)
            for path in [release,*release.rglob('*')]:
                os.lchown(path,0,0)
                if not path.is_symlink():path.chmod(0o755 if path.is_dir() or path.stat().st_mode&0o111 else 0o644)
        gid=grp.getgrnam('andrew-control').gr_gid
        for name in candidate['originals']:h.replace(TARGET/name,(Path(candidate['source'])/'prototype/andrewbot'/name).read_bytes(),gid)
        for path in (BACKEND_DROPIN,CONTROL_DROPIN):path.parent.mkdir(parents=True,exist_ok=True)
        backend_config=('[Service]\nExecStart=\nExecStart=/usr/bin/node '+str(release)+'/letta.js server --backend local --listen ws://127.0.0.1:8283 --openai-api\nEnvironment=ANDREW_LOCAL_RECOVERY_ARTIFACT=1\n')
        h.replace(BACKEND_DROPIN,backend_config.encode(),0,mode=0o644)
        h.replace(CONTROL_DROPIN,b'[Service]\nEnvironment=ANDREW_LOCAL_RECOVERY_ENABLED=0\n',0,mode=0o644)
        service('daemon-reload','')
        return {'installed':True,'automatic_resume':False,'paired_snapshot':str(directory/'paired-before'),'release':str(release)}
    except Exception:
        # No unsafe fallback startup. The journals and paired snapshot remain available.
        service('stop','andrew-control.service');service('stop','andrew-letta.service')
        raise


def rollback(candidate,*,service=_service):
    directory=Path(candidate['directory']);h=helper('install-p1-fixes')
    service('stop','andrew-control.service');service('stop','andrew-letta.service')
    for unit in ('andrew-control.service','andrew-letta.service'):service('assert-stopped',unit)
    snapshot=directory/'paired-before';assert_rollback_exclusion(snapshot)
    isolated=BACKEND_HOME.with_name('letta-rollback-'+uuid.uuid4().hex)
    shutil.copytree(snapshot/'backend',isolated,symlinks=False)
    owner=pwd.getpwnam('andrew-letta')
    for path in [isolated,*isolated.rglob('*')]:os.chown(path,owner.pw_uid,owner.pw_gid)
    gid=grp.getgrnam('andrew-control').gr_gid
    for name,original in candidate['originals'].items():
        if original is None:(TARGET/name).unlink(missing_ok=True)
        else:
            saved=directory/'original'/name
            if sha(saved)!=original:raise ValueError('Rollback controller evidence changed; services remain stopped')
            h.replace(TARGET/name,saved.read_bytes(),gid)
    for name,path in [('backend',BACKEND_DROPIN),('controller',CONTROL_DROPIN)]:
        old=json.loads((directory/(name+'-override-before.json')).read_text())['content']
        if old is None:path.unlink(missing_ok=True)
        else:h.replace(path,old.encode(),0,mode=0o644)
    fence=ROLLBACK_DROPIN
    text='[Service]\nEnvironment=ANDREW_LOCAL_RECOVERY_ARTIFACT=0\nEnvironment=HOME='+str(isolated)+'\nEnvironment=LETTA_LOCAL_BACKEND_DIR='+str(isolated/'.letta')+'\nEnvironment=LETTA_HOME='+str(isolated/'.letta')+'\nReadWritePaths='+str(isolated)+'\n'
    h.replace(fence,text.encode(),0,mode=0o644)
    h.replace(CONTROL_DROPIN,b'[Service]\nEnvironment=ANDREW_LOCAL_RECOVERY_ENABLED=0\n',0,mode=0o644)
    # Preserve all live ledgers. The old code must see a pause even after an interrupted activation.
    pause="import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); db.execute(\"INSERT INTO state VALUES('paused','1') ON CONFLICT(key) DO UPDATE SET value='1'\"); db.commit(); db.close()"
    h.command('runuser','-u','andrew-control','--','/usr/bin/python3','-c',pause,str(DATABASE))
    readiness=h.load_readiness()
    h.replace(readiness.PROOF,(directory/'runtime-proof-before.json').read_bytes(),0,mode=0o644)
    readiness.validate_runtime_proof()
    service('daemon-reload','');service('start','andrew-letta.service');h.wait_for_letta();service('start','andrew-control.service')
    h.verify_controller_startup(directory)
    return {'rolled_back':True,'automatic_resume':False,'isolated_backend_home':str(isolated)}


def enable(candidate,*,service=_service):
    directory=Path(candidate['directory'])
    if not (directory/'activated.json').is_file():raise ValueError('Initial disabled activation must complete before enabling recovery')
    verify_candidate(candidate,require_acceptance=True)
    activated=json.loads((directory/'activated.json').read_text())
    if verify_release(activated['release'])!=candidate['backend_manifest_sha256']:raise ValueError('Installed backend changed')
    if tree_hashes(TARGET,ignore_bytecode=True)!=candidate['controller_files']:raise ValueError('Installed controller changed')
    h=helper('install-p1-fixes');readiness=h.load_readiness();readiness.validate_runtime_proof()
    helper('install-autonomy-media').idle()
    code="from andrewbot.appserver import AppServerClient; from andrewbot.recovery import LocalRecoveryClient; c=LocalRecoveryClient(AppServerClient()); c.capabilities(); c.close()"
    h.command('runuser','-u','andrew-control','--','env','PYTHONPATH=/opt/andrew/prototype','/usr/bin/python3','-c',code)
    service('stop','andrew-control.service');service('assert-stopped','andrew-control.service')
    h.replace(CONTROL_DROPIN,b'[Service]\nEnvironment=ANDREW_LOCAL_RECOVERY_ENABLED=1\n',0,mode=0o644)
    service('daemon-reload','')
    try:
        service('start','andrew-control.service');h.verify_controller_startup(directory)
    except Exception:
        service('stop','andrew-control.service')
        h.replace(CONTROL_DROPIN,b'[Service]\nEnvironment=ANDREW_LOCAL_RECOVERY_ENABLED=0\n',0,mode=0o644)
        service('daemon-reload','')
        raise
    return {'enabled':True,'owner_controls_preserved':True,'wake_requested':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend-release',type=Path)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--resume-apply',type=Path,help='Retry activation of an unchanged accepted candidate after a pre-snapshot failure')
    parser.add_argument('--retire-unstarted-projection',help='Retain one exact projection-failed input proven to have no effects; requires --apply')
    parser.add_argument('--rollback',type=Path,help='Candidate directory from the installation')
    parser.add_argument('--enable',type=Path,help='Verified candidate directory after initial disabled activation')
    args=parser.parse_args()
    if args.resume_apply and (args.backend_release or args.enable or args.rollback):parser.error('--resume-apply cannot be combined with another action')
    if args.retire_unstarted_projection and (not args.apply or not args.backend_release or args.enable or args.rollback or args.resume_apply):
        parser.error('--retire-unstarted-projection requires --backend-release and --apply')
    if os.geteuid()!=0:raise SystemExit('Run with sudo in your server terminal; no services changed.')
    if args.enable:
        candidate=json.loads((args.enable/'candidate.json').read_text());print(json.dumps(enable(candidate)));return
    if args.rollback:
        candidate=json.loads((args.rollback/'candidate.json').read_text());print(json.dumps(rollback(candidate)));return
    if not args.backend_release and not args.resume_apply:parser.error('--backend-release or --resume-apply is required')
    h=helper('install-p1-fixes');readiness=h.load_readiness()
    proof=readiness.protected_read(readiness.PROOF);before=readiness.bindings();readiness.check_proof(json.loads(proof),before)
    if args.retire_unstarted_projection:
        retirement=helper('retire_unstarted_projection').plan(DATABASE,BACKEND_HOME,args.retire_unstarted_projection)
        helper('install-autonomy-media').idle(allowed_running_cycle=retirement['binding']['cycle_id'])
    else:helper('install-autonomy-media').idle()
    if args.resume_apply:
        candidate=load_accepted_candidate(args.resume_apply);directory=Path(candidate['directory'])
        if (directory/'paired-before').exists() or (directory/'activated.json').exists():raise ValueError('Candidate has already reached the snapshot stage; create a fresh candidate')
        if (directory/'runtime-proof-before.json').read_bytes()!=proof:raise ValueError('Runtime proof changed; create a fresh candidate')
        print('Resuming accepted candidate: '+str(directory),flush=True)
    else:
        directory=Path('/var/backups')/('andrew-local-recovery-'+uuid.uuid4().hex)
        candidate=build_candidate(args.backend_release,TARGET,directory)
        (directory/'runtime-proof-before.json').write_bytes(proof)
        print('Candidate and evidence: '+str(directory),flush=True)
        pristine=Path('/var/lib/andrew/letta-app/node_modules/@letta-ai/letta-code/letta.js')
        run_acceptance(candidate,pristine)
    if readiness.bindings()!=before:raise ValueError('Runtime changed during candidate verification; no services changed')
    if not args.apply and not args.resume_apply:
        print('Candidate verified; preview only, no services changed.');return
    result=activate(candidate,retire_projection=args.retire_unstarted_projection) if args.retire_unstarted_projection else activate(candidate)
    try:
        h.renew_proof(readiness,json.loads(proof),before,directory)
        _service('start','andrew-letta.service');h.wait_for_letta()
        code="from andrewbot.appserver import AppServerClient; from andrewbot.recovery import LocalRecoveryClient; c=LocalRecoveryClient(AppServerClient()); c.capabilities(); c.close(); print('Recovery capability verified; no input admitted')"
        h.command('runuser','-u','andrew-control','--','env','PYTHONPATH=/opt/andrew/prototype','/usr/bin/python3','-c',code)
        _service('start','andrew-control.service');h.verify_controller_startup(directory)
    except Exception:
        _service('stop','andrew-control.service');_service('stop','andrew-letta.service')
        raise
    (directory/'activated.json').write_text(json.dumps(result,sort_keys=True)+'\n')
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':
    try:main()
    except ValueError as error:raise SystemExit('Recovery installation stopped: '+str(error)) from None
    except Exception as error:raise SystemExit('Recovery installation stopped: '+type(error).__name__+'; services were not automatically restarted after a failed activation.') from None
