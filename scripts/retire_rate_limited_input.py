"""Explicit operator retirement of one legacy, uniquely evidenced HTTP 429 input.

This is an installation migration, not an automatic inference or retry. Both
services must be stopped and a paired snapshot verified before either writer.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1] if '__file__' in globals() else Path('/opt/andrew')
sys.path.insert(0,str(ROOT/'prototype'))
RATE_EVENT='Letta model gateway provider request failed (category=http_error, http_status=429)'


def read(path):
    path=Path(path)
    if any(parent.is_symlink() for parent in [path,*path.parents]) or not path.is_file() or path.stat().st_size>268435456:
        raise ValueError('Rate-limit retirement requires bounded regular evidence files')
    return path.read_bytes()


def controller_evidence(db,admission):
    from andrewbot.recovery_records import digest,validate_binding
    db.row_factory=sqlite3.Row
    state=dict(db.execute('SELECT key,value FROM state'))
    if state.get('paused')!='1' or state.get('active_session_id') or state.get('session_start_requested'):
        raise ValueError('Rate-limit retirement requires settled owner pause')
    pending=db.execute("SELECT * FROM recovery_attempts WHERE state IN ('active','evidence','held')").fetchall()
    if len(pending)!=1 or pending[0]['admission_id']!=admission or pending[0]['state']!='held' or pending[0]['hold_reason']!='provider_uncertain' or pending[0]['origin']!='autonomous':
        raise ValueError('Requires exactly the selected held autonomous input')
    scope=pending[0];binding=validate_binding(json.loads(scope['binding_json']))
    if scope['binding_sha']!=digest(binding) or (state.get('letta_agent_id'),state.get('letta_conversation_id'))!=(binding['agent_id'],binding['conversation_id']):
        raise ValueError('Retirement binding changed')
    for completed in db.execute("SELECT binding_json FROM recovery_attempts WHERE state='complete'"):
        prior=validate_binding(json.loads(completed['binding_json']))
        if (prior['agent_id'],prior['conversation_id'])==(binding['agent_id'],binding['conversation_id']):
            raise ValueError('This migration supports the first journaled input only; completed predecessor requires separate reconciliation')
    effects=db.execute('SELECT * FROM recovery_effects WHERE binding_sha=?',(digest(binding),)).fetchall()
    if len(effects)!=1 or effects[0]['kind']!='provider' or effects[0]['state']!='admitted' or effects[0]['scope_id']!=admission:
        raise ValueError('Requires exactly one admitted provider request and no tools')
    effect=dict(effects[0])
    if db.execute('SELECT 1 FROM recovery_results WHERE admission_id=?',(effect['admission_id'],)).fetchone():raise ValueError('Provider already has a result')
    checkpoint=db.execute('SELECT * FROM cycle_recovery_checkpoints WHERE cycle_id=?',(binding['cycle_id'],)).fetchone()
    cycle=db.execute('SELECT * FROM cycles WHERE cycle_id=? AND session_id=?',(binding['cycle_id'],binding['session_id'])).fetchone()
    receipt=db.execute('SELECT * FROM model_turn_receipts WHERE cycle_id=?',(binding['cycle_id'],)).fetchone()
    if not checkpoint or checkpoint['phase']!='model_started' or not cycle or cycle['status'] not in ('running','interrupted') or not receipt or receipt['state']!='accepted':
        raise ValueError('Original interrupted cycle evidence missing')
    if any(receipt[key]!=binding[key] for key in ('session_id','agent_id','conversation_id','client_message_id','owner_revision')) or receipt['request_id']!=binding['consumer_id']:
        raise ValueError('Original model input evidence changed')
    # The legacy gateway serialized calls but did not bind its error to an
    # admission ID. Require a single request, a single failure, and no later work.
    events=[dict(row) for row in db.execute("SELECT id,created_at,event FROM activity WHERE id>=? AND event LIKE 'Letta model gateway %' ORDER BY id",(checkpoint['activity_id'],))]
    if len(events)!=1 or events[0]['event']!=RATE_EVENT:raise ValueError('No unique explicit HTTP 429 evidence; automatic inference refused')
    if db.execute('SELECT 1 FROM usage WHERE created_at>=?',(scope['created_at'],)).fetchone():raise ValueError('Usage after admission is ambiguous')
    if db.execute('SELECT 1 FROM recovery_effects WHERE created_at>=? AND admission_id!=?',(scope['created_at'],effect['admission_id'])).fetchone():raise ValueError('Other effect admissions make legacy event ambiguous')
    if effect['reservation_id']!='local-recovery-'+effect['admission_id']:raise ValueError('Reservation binding mismatch')
    reservation=db.execute('SELECT * FROM budget_reservations WHERE reservation_id=?',(effect['reservation_id'],)).fetchone()
    if not reservation:raise ValueError('Original reservation missing')
    if db.execute("SELECT 1 FROM cycle_tool_attempts WHERE cycle_id=?",(binding['cycle_id'],)).fetchone():raise ValueError('Tool attempts prohibit retirement')
    return {'binding':binding,'effect':effect,'reservation':dict(reservation),'event':events[0]}


def plan(database,home,admission):
    from andrewbot.recovery_records import digest
    database=Path(database)
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as db:evidence=controller_evidence(db,admission)
    binding=evidence['binding'];effect=evidence['effect'];key=digest({key:binding[key] for key in ('agent_id','conversation_id')})
    paths=[Path(home)/suffix/'local-recovery-v1/conversations'/f'{key}.json' for suffix in ('.letta','.letta/lc-local-backend')]
    paths=[path for path in paths if path.exists()]
    if len(paths)!=1:raise ValueError('Requires one exact backend checkpoint')
    checkpoint=paths[0];snapshot=json.loads(read(checkpoint));root=checkpoint.parents[1]
    steps=snapshot.get('steps')
    if snapshot.get('binding')!=binding or snapshot.get('phase')!='held' or snapshot.get('held_phase')!='provider_admitted' or snapshot.get('hold_reason')!='provider_uncertain' or snapshot.get('tools')!=[] or snapshot.get('terminal') is not None or not isinstance(steps,list) or len(steps)!=1:
        raise ValueError('Backend does not prove a first-provider-only hold')
    step=steps[0]
    if step.get('state')!='admitted' or step.get('kind','provider')!='provider' or any(step.get(key)!=effect[key] for key in ('effect_id','admission_id','request_sha')) or step.get('result_ref'):
        raise ValueError('Backend request differs from controller admission')
    if json.loads(read(root/'identity.json')).get('store_id')!=binding['store_id']:raise ValueError('Backend store identity changed')
    files=[checkpoint,root/'identity.json'];refs=snapshot.get('payload_refs',[])
    if snapshot.get('input_ref')!=binding['input_sha'] or step.get('request_ref')!=step['request_sha'] or not {binding['input_sha'],step['request_sha']}<=set(refs):raise ValueError('Original request evidence missing')
    for ref in refs:
        if not isinstance(ref,str) or len(ref)!=64 or any(c not in '0123456789abcdef' for c in ref):raise ValueError('Unsafe payload reference')
        path=root/'blobs'/key/ref
        if hashlib.sha256(read(path)).hexdigest()!=ref:raise ValueError('Payload evidence changed')
        files.append(path)
    archive=checkpoint.parent/(key+'.history')/(digest(binding)+'.json')
    if archive.exists() or any(p.is_symlink() for p in [archive,*archive.parents]):raise ValueError('Existing or unsafe retirement archive')
    return {'admission_id':admission,'database':str(database),'binding':binding,'evidence':evidence,
            'checkpoint':str(checkpoint),'archive':str(archive),'files':{str(p):hashlib.sha256(read(p)).hexdigest() for p in files}}


def retire_backend(value):
    # Stdlib only: the backend service cannot import the controller's package.
    for path,expected in value['files'].items():
        if hashlib.sha256(read(path)).hexdigest()!=expected:raise ValueError('Retirement evidence changed')
    archive=Path(value['archive'])
    if archive.exists() or any(p.is_symlink() for p in [archive,*archive.parents]):raise ValueError('Existing or unsafe archive')
    archive.parent.mkdir(mode=0o700,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.rate-limit-retirement-',dir=archive.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(read(value['checkpoint']));stream.flush();os.fsync(stream.fileno())
        os.replace(name,archive)
        directory=os.open(archive.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
        Path(value['checkpoint']).unlink()
        directory=os.open(Path(value['checkpoint']).parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)
    # The original transcript and projection remain byte-for-byte intact.


def retire_controller(value):
    with closing(sqlite3.connect(value['database'])) as db:
        with db:
            db.execute('BEGIN IMMEDIATE')
            if controller_evidence(db,value['admission_id'])!=value['evidence']:raise ValueError('Controller retirement evidence changed')
            db.execute('CREATE TABLE IF NOT EXISTS recovery_operator_retirements(admission_id TEXT PRIMARY KEY,evidence_json TEXT NOT NULL,created_at TEXT NOT NULL)')
            db.execute("INSERT INTO recovery_operator_retirements VALUES(?,?,strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))",(value['admission_id'],json.dumps(value,sort_keys=True)))
            effect=value['evidence']['effect']
            db.execute('DELETE FROM budget_reservations WHERE reservation_id=?',(effect['reservation_id'],))
            db.execute("UPDATE recovery_effects SET state='retired_rejected' WHERE admission_id=?",(effect['admission_id'],))
            db.execute("UPDATE recovery_attempts SET state='retired',hold_reason='provider_rate_limited' WHERE admission_id=?",(value['admission_id'],))
            db.execute("UPDATE cycles SET status='interrupted',ended_at=coalesce(ended_at,strftime('%Y-%m-%dT%H:%M:%S+00:00','now')) WHERE cycle_id=? AND session_id=?",(value['binding']['cycle_id'],value['binding']['session_id']))
            db.execute("DELETE FROM state WHERE key='active_cycle_id' AND value=?",(value['binding']['cycle_id'],))


def retire(value,snapshot):
    from andrewbot.memory_backup import verify_paired_recovery_snapshot
    verify_paired_recovery_snapshot(snapshot)
    for user,phase in [('andrew-letta','backend'),('andrew-control','controller')]:
        result=subprocess.run(['runuser','-u',user,'--','/usr/bin/python3','-c',Path(__file__).read_text(),phase],input=json.dumps(value),text=True,capture_output=True)
        (Path(snapshot).parent/(phase+'-rate-limit-retirement.txt')).write_text(result.stdout+result.stderr)
        if result.returncode:raise ValueError('Rate-limit retirement '+phase+' failed; services remain stopped; inspect paired snapshot and evidence')


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--inspect':
        import argparse
        parser=argparse.ArgumentParser(description='Read-only eligibility check; no state or service changes')
        parser.add_argument('--inspect',required=True)
        parser.add_argument('--database',default='/var/lib/andrew/control/controller.sqlite3')
        parser.add_argument('--home',default='/var/lib/andrew/letta')
        args=parser.parse_args()
        value=plan(args.database,args.home,args.inspect)
        print(json.dumps({'eligible':True,'admission_id':value['admission_id'],'cycle_id':value['binding']['cycle_id'],
                          'http_status':429,'event_id':value['evidence']['event']['id'],
                          'reservation_usd':value['evidence']['reservation']['cost_usd']}))
        raise SystemExit(0)
    value=json.load(sys.stdin)
    if sys.argv[1]=='backend':retire_backend(value)
    elif sys.argv[1]=='controller':retire_controller(value)
    else:raise SystemExit('Unsupported repair phase')
