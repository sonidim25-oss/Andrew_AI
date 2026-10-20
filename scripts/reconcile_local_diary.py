"""Offline destination reconciliation for one exact failed owner diary call.

No callback, model call, diary creation or message replay is permitted here.
The administrator installer stops writers and snapshots both stores first.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
import sys
if '__file__' in globals():sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'prototype'))
from andrewbot.recovery_records import RecoveryRecords,canonical,digest,validate_binding
from andrewbot.store import Store

RESULT=json.dumps({'status':'error','error':'diary_not_committed','committed':False,'evidence':'quiescent_local_diary_ledger'},separators=(',',':'))


def identity(database):
    path=Path(database)
    if path.is_symlink() or not path.is_file():raise ValueError('Unsafe controller database')
    info=path.stat()
    # Ordinary WAL checkpoints update ctime. Linux file birth time still
    # distinguishes the supported restore's newly created replacement file.
    fields=subprocess.check_output(['stat','--format=%d:%i:%W','--',str(path)],text=True).strip().split(':')
    if len(fields)!=3 or (int(fields[0]),int(fields[1]))!=(info.st_dev,info.st_ino):raise ValueError('Database changed during identity check')
    return {'device':info.st_dev,'inode':info.st_ino,'ctime_ns':str(info.st_ctime_ns),'birth_time_seconds':int(fields[2])}


def plan(database,home,admission,*,continuity_before=None,now=None):
    from inspect_owner_handoff import read
    database=Path(database);physical=identity(database)
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Controller integrity failed')
        pending=db.execute("SELECT * FROM recovery_attempts WHERE state IN ('active','held','evidence','yielding')").fetchall()
        if len(pending)!=1 or pending[0]['admission_id']!=admission:raise ValueError('Requires one exact pending diary input')
        scope=pending[0];binding=validate_binding(json.loads(scope['binding_json']));state=dict(db.execute('SELECT key,value FROM state'))
        if (scope['binding_sha']!=digest(binding) or scope['origin']!='owner' or binding['session_id']!=binding['cycle_id'] or not binding['session_id'].startswith('owner-')
                or state.get('owner_control_revision','0')!=binding['owner_revision'] or state.get('autonomy_error')!='turn_failed'
                or any(state.get(k) for k in ('active_session_id','active_cycle_id','model_turn_origin','model_turn_progress','autonomous_owner_id','session_start_requested'))
                or any(state.get(k,'0')=='1' for k in ('sleeping','paused','restore_recovery_required','local_recovery_restore_required'))
                or any(state.get('letta_'+k)!=binding[k] for k in ('agent_id','conversation_id'))
                or state.get('local_recovery_config_sha')!=binding['config_sha']):raise ValueError('Owner state changed or request not halted')
        for query in ("SELECT 1 FROM cycles WHERE status='running' LIMIT 1","SELECT 1 FROM messages WHERE queued IN (1,2) LIMIT 1",
                      'SELECT 1 FROM telegram_claims LIMIT 1',"SELECT 1 FROM telegram_outbox WHERE delivery_state='sending' LIMIT 1",
                      'SELECT 1 FROM diary_delivery_claims LIMIT 1',"SELECT 1 FROM media_jobs WHERE state='running' OR delivery='sending' LIMIT 1",
                      "SELECT 1 FROM media_avatar_proposals WHERE state='applying' LIMIT 1"):
            if db.execute(query).fetchone():raise ValueError('Other work is still settling')
        key=digest({k:binding[k] for k in ('agent_id','conversation_id')})
        paths=[Path(home)/s/'local-recovery-v1/conversations'/(key+'.json') for s in ('.letta','.letta/lc-local-backend')]
        paths=[p for p in paths if p.exists()]
        if len(paths)!=1:raise ValueError('Requires one backend checkpoint')
        path=paths[0];raw=read(path);checkpoint=json.loads(raw)
        if (checkpoint.get('binding')!=binding or checkpoint.get('phase')!='held' or checkpoint.get('hold_reason')!='tool_uncertain'
                or checkpoint.get('held_phase')!='tool_admitted' or checkpoint.get('claim') or checkpoint.get('terminal')
                or len(checkpoint.get('tools',[]))!=1 or len(checkpoint.get('steps',[]))!=1):raise ValueError('Requires exact released diary checkpoint')
        tool=checkpoint['tools'][0];provider=checkpoint['steps'][0]
        if tool.get('name')!='save_diary_entry' or tool.get('state')!='admitted' or provider.get('state')!='confirmed' or provider.get('kind','provider')!='provider':raise ValueError('Unexpected checkpoint effects')
        effects=db.execute('SELECT * FROM recovery_effects WHERE binding_sha=?',(scope['binding_sha'],)).fetchall()
        if len(effects)!=2:raise ValueError('Unexpected controller effects')
        by_id={r['admission_id']:r for r in effects}
        e=by_id.get(tool.get('admission_id'));p=by_id.get(provider.get('admission_id'))
        if (e is None or p is None or any(r['binding_sha']!=digest(binding) or r['scope_id']!=admission for r in effects) or e['kind']!='tool' or e['state']!='admitted' or e['request_sha']!=tool['request_sha']
                or e['effect_id']!=tool['effect_id'] or p['kind']!='provider' or p['state']!='confirmed' or p['request_sha']!=provider['request_sha']
                or p['effect_id']!=provider['effect_id']):raise ValueError('Controller and backend differ')
        admitted=db.execute("SELECT admission_id FROM recovery_effects WHERE state='admitted'").fetchall()
        if len(admitted)!=1 or admitted[0][0]!=e['admission_id']:raise ValueError('Another effect is uncertain')
        proof=db.execute('SELECT * FROM recovery_results WHERE admission_id=?',(p['admission_id'],)).fetchone()
        if (proof is None or proof['result_sha']!=provider.get('result_ref') or digest(json.loads(proof['result_json']))!=proof['result_sha']
                or json.loads(proof['usage_json'] or '{}').get('settled') is not True):raise ValueError('Provider proof differs')
        if db.execute('SELECT 1 FROM recovery_results WHERE admission_id=?',(e['admission_id'],)).fetchone():raise ValueError('Diary already has a result')
        if db.execute('SELECT 1 FROM recovery_effects e JOIN budget_reservations b ON e.reservation_id=b.reservation_id LIMIT 1').fetchone():raise ValueError('Journaled provider request is unsettled')
        args_raw=read(path.parents[1]/'blobs'/key/tool['arguments_ref']);args=json.loads(args_raw)
        if (hashlib.sha256(args_raw).hexdigest()!=tool['arguments_ref'] or not isinstance(args,dict) or not {'content'}<=set(args)<= {'content','event_ids'}
                or not isinstance(args['content'],str) or digest({'name':'save_diary_entry','arguments':args})!=e['request_sha']):raise ValueError('Diary arguments differ')
        start=datetime.fromisoformat(e['created_at']);current=now or datetime.now(timezone.utc)
        if start.tzinfo is None or current.tzinfo is None or current<start:raise ValueError('Unbounded execution dates')
        first=start.astimezone(ZoneInfo('America/Toronto')).date()
        last=current.astimezone(ZoneInfo('America/Toronto')).date()
        if (last-first).days>6:raise ValueError('Execution dates exceed the seven-day inspection bound')
        dates=[(first+timedelta(days=n)).isoformat() for n in range((last-first).days+1)]
        # Supported restore creates a new main database file. A pre-admission
        # birth-time (or older ctime) witness and the SAME inode after quiescence
        # exclude replacement without mistaking a WAL checkpoint for a restore.
        witness=continuity_before or physical
        older_birth=type(witness.get('birth_time_seconds')) is int and 0<witness['birth_time_seconds']<int(start.timestamp())
        older_ctime=int(witness.get('ctime_ns','0'))>0 and int(witness['ctime_ns'])<=int(start.timestamp()*1_000_000_000)
        continuity=older_birth if witness.get('birth_time_seconds',0)>0 else older_ctime
        if (set(witness)!={'device','inode','ctime_ns','birth_time_seconds'} or not continuity
                or any(physical[k]!=witness[k] for k in ('device','inode'))):raise ValueError('Database continuity is not proven')
        if db.execute("SELECT 1 FROM diary_entries WHERE local_date BETWEEN ? AND ? AND record_type='diary' LIMIT 1",(dates[0],dates[-1])).fetchone():raise ValueError('Diary destination is not empty')
        if db.execute("SELECT 1 FROM activity WHERE created_at>=? AND event LIKE 'Diary record created (%' LIMIT 1",(e['created_at'],)).fetchone():raise ValueError('Diary creation history conflicts')
        return {'contract':'local_diary_destination_reconciliation_v1','admission_id':admission,'binding':binding,'binding_sha':scope['binding_sha'],
            'backend_root':str(path.parents[1]),'checkpoint_digest':digest(checkpoint),'checkpoint_sha256':hashlib.sha256(raw).hexdigest(),
            'tool_admission_id':e['admission_id'],'request_sha':e['request_sha'],'arguments_sha':tool['arguments_ref'],
            'provider_admission_id':p['admission_id'],'provider_result_sha':proof['result_sha'],'destination_date':dates[0],'destination_dates':dates,
            'continuity_before':witness,'result':RESULT,'result_sha':digest(RESULT)}


def controller_phase(payload,phase):
    proof=payload['proof'];store=Store(payload['database'])
    try:
        with store._lock,store.connection:
            store.connection.execute('BEGIN IMMEDIATE')
            if phase=='record':
                effect=store.connection.execute('SELECT * FROM recovery_effects WHERE admission_id=?',(proof['tool_admission_id'],)).fetchone()
                scope=store.connection.execute('SELECT * FROM recovery_attempts WHERE admission_id=?',(proof['admission_id'],)).fetchone()
                if (effect is None or effect['state']!='admitted' or effect['request_sha']!=proof['request_sha'] or effect['binding_sha']!=proof['binding_sha'] or effect['scope_id']!=proof['admission_id']
                        or scope is None or scope['origin']!='owner' or scope['binding_sha']!=digest(proof['binding']) or json.loads(scope['binding_json'])!=proof['binding']):raise ValueError('Diary admission changed')
                RecoveryRecords(store)._confirm(proof['tool_admission_id'],canonical(RESULT),{'local_diary_reconciliation':{'contract':proof['contract'],'evidence_sha':payload['evidence_sha'],'committed':False}})
            else:raise ValueError('Invalid repair phase')
    finally:store.close()


def record_result(database,home,proof,snapshot,*,run_as_service=True):
    from andrewbot.memory_backup import verify_paired_recovery_snapshot
    metadata=verify_paired_recovery_snapshot(snapshot)
    current=plan(database,home,proof['admission_id'],continuity_before=proof['continuity_before'])
    if current!=proof:raise ValueError('Diary repair proof changed')
    if Path(str(database)+'-wal').exists() and Path(str(database)+'-wal').stat().st_size:raise ValueError('Controller WAL is not quiescent')
    if hashlib.sha256(Path(database).read_bytes()).hexdigest()!=metadata['controller_sha256']:raise ValueError('Snapshot controller differs')
    key=digest({k:proof['binding'][k] for k in ('agent_id','conversation_id')})
    relative=(Path(proof['backend_root']).relative_to(home)/'conversations'/(key+'.json')).as_posix()
    if metadata['backend_files'].get(relative)!=proof['checkpoint_sha256']:raise ValueError('Snapshot checkpoint differs')
    evidence_sha=digest(proof);payload={'proof':proof,'evidence_sha':evidence_sha,'database':str(database)}
    if run_as_service:
        result=subprocess.run(['runuser','-u','andrew-control','--','/usr/bin/python3','-c',Path(__file__).read_text(),'record'],
            input=json.dumps(payload),capture_output=True,text=True,timeout=30,env={**os.environ,'PYTHONPATH':'/opt/andrew/prototype'})
        if result.returncode:raise ValueError('Diary controller repair failed; services must remain stopped')
    else:controller_phase(payload,'record')
    return {'result_recorded':True,'committed':False,'automatic_resume':False,'evidence_sha':evidence_sha}


def finalize(database,proof,*,client=None):
    from andrewbot.appserver import AppServerClient
    from andrewbot.recovery import LocalRecoveryClient
    store=Store(database);own_client=client is None
    client=client or LocalRecoveryClient(AppServerClient())
    try:
        records=RecoveryRecords(store);binding=proof['binding']
        saved=records.effect_result(proof['tool_admission_id'])
        receipt=(saved.get('usage') or {}).get('local_diary_reconciliation',{})
        if saved.get('result')!=RESULT or receipt.get('evidence_sha')!=digest(proof):raise ValueError('Saved diary reconciliation differs')
        if client.capabilities().get('local_diary_reconciliation')!=1:raise ValueError('Diary reconciliation capability missing')
        scope=records.admit_scope(binding,origin='owner',evidence_only=True)
        state=client.inspect(binding)
        claim=client.command('recovery_reconcile_local_diary',binding=binding,expected_revision=state['revision'],effect_admission_id=proof['tool_admission_id'],**scope)
        state=client.inspect(binding);terminal=state['terminal']
        records.complete_scope(scope['admission_id'],binding,terminal)
        client.ack(binding,scope,claim,terminal);client.release(binding,scope,claim)
        current=client.inspect(binding)
        with store._lock,store.connection:
            store.connection.execute("UPDATE recovery_attempts SET state='complete',backend_revision=? WHERE admission_id=?",(current['revision'],scope['admission_id']))
            store.add_activity('Local diary destination reconciled as not committed; failed owner turn retired without replay')
        return {'reconciled':True,'committed':False,'automatic_resume':False,'evidence_sha':digest(proof),'backend_revision':current['revision']}
    finally:
        if own_client:client.close()
        store.close()


def verify_recorded(database,home,proof,snapshot):
    from andrewbot.memory_backup import verify_paired_recovery_snapshot
    from inspect_owner_handoff import read
    metadata=verify_paired_recovery_snapshot(snapshot)
    key=digest({k:proof['binding'][k] for k in ('agent_id','conversation_id')})
    relative=(Path(proof['backend_root']).relative_to(home)/'conversations'/(key+'.json')).as_posix()
    if metadata['backend_files'].get(relative)!=proof['checkpoint_sha256']:raise ValueError('Original diary snapshot differs')
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN');state=dict(db.execute('SELECT key,value FROM state'))
        if (state.get('owner_control_revision','0')!=proof['binding']['owner_revision'] or state.get('autonomy_error')!='turn_failed'
                or any(state.get(k) for k in ('active_session_id','active_cycle_id','model_turn_origin','model_turn_progress','autonomous_owner_id','session_start_requested'))
                or any(state.get(k,'0')=='1' for k in ('paused','sleeping','restore_recovery_required','local_recovery_restore_required'))):raise ValueError('Owner controls changed after diary receipt')
        if db.execute("SELECT 1 FROM recovery_effects WHERE state='admitted' LIMIT 1").fetchone():raise ValueError('An effect remains admitted')
        if db.execute("SELECT 1 FROM recovery_attempts WHERE state IN ('active','held','evidence','yielding') AND binding_sha!=? LIMIT 1",(proof['binding_sha'],)).fetchone():raise ValueError('Another recovery binding is pending')
        row=db.execute('SELECT r.*,e.state,e.binding_sha,e.request_sha FROM recovery_results r JOIN recovery_effects e ON r.admission_id=e.admission_id WHERE e.admission_id=?',(proof['tool_admission_id'],)).fetchone()
        if (row is None or row['state']!='confirmed' or row['binding_sha']!=proof['binding_sha'] or row['request_sha']!=proof['request_sha']
                or json.loads(row['result_json'])!=RESULT or row['result_sha']!=digest(RESULT)
                or json.loads(row['usage_json'])['local_diary_reconciliation']['evidence_sha']!=digest(proof)):raise ValueError('Saved diary receipt differs')
    cp=json.loads(read(Path(proof['backend_root'])/'conversations'/(key+'.json')))
    if cp.get('binding')!=proof['binding']:raise ValueError('Current diary checkpoint binding differs')
    if cp.get('phase')=='held':
        original=json.loads(read(Path(snapshot)/'backend'/relative))
        ownership={'revision','fence','claim'}
        if ({k:v for k,v in cp.items() if k not in ownership}!={k:v for k,v in original.items() if k not in ownership}
                or type(cp.get('revision')) is not int or cp['revision']<original['revision']
                or type(cp.get('fence')) is not int or cp['fence']<original['fence']):raise ValueError('Held diary checkpoint changed')
        claim=cp.get('claim')
        if claim and claim.get('boot_id')==Path('/proc/sys/kernel/random/boot_id').read_text().strip() and claim.get('expires_monotonic_ms',float('inf'))>time.monotonic()*1000:raise ValueError('Diary maintenance claim is still live; wait for release')
    elif cp.get('phase')=='terminal_committed':
        if (cp['terminal'].get('maintenance_receipt_sha')!=digest(proof) or cp['terminal'].get('error_category')!='diary_not_committed'
                or cp['tools'][0].get('result_ref')!=digest(RESULT)):raise ValueError('Reconciled checkpoint differs')
    else:raise ValueError('Unexpected diary checkpoint phase')
    return {'result_recorded':True,'committed':False,'automatic_resume':False,'evidence_sha':digest(proof)}


def verify_finalized(database,home,proof,marker):
    from inspect_owner_handoff import read
    if marker.get('reconciled') is not True or marker.get('committed') is not False or marker.get('automatic_resume') is not False or marker.get('evidence_sha')!=digest(proof):raise ValueError('Diary finalization marker differs')
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN');state=dict(db.execute('SELECT key,value FROM state'))
        if (state.get('owner_control_revision','0')!=proof['binding']['owner_revision'] or state.get('autonomy_error')!='turn_failed'
                or any(state.get(k) for k in ('active_session_id','active_cycle_id','model_turn_origin','model_turn_progress','autonomous_owner_id','session_start_requested'))
                or any(state.get(k,'0')=='1' for k in ('paused','sleeping','restore_recovery_required','local_recovery_restore_required'))):raise ValueError('Owner controls changed after diary repair')
        if db.execute("SELECT 1 FROM recovery_attempts WHERE state IN ('active','held','evidence','yielding') LIMIT 1").fetchone() or db.execute("SELECT 1 FROM recovery_effects WHERE state='admitted' LIMIT 1").fetchone():raise ValueError('Unresolved diary recovery remains')
        row=db.execute('SELECT r.result_json,r.result_sha,r.usage_json,e.state,e.binding_sha,e.request_sha FROM recovery_results r JOIN recovery_effects e ON r.admission_id=e.admission_id WHERE e.admission_id=?',(proof['tool_admission_id'],)).fetchone()
        if (row is None or row['state']!='confirmed' or row['binding_sha']!=proof['binding_sha'] or row['request_sha']!=proof['request_sha']
                or json.loads(row['result_json'])!=RESULT or row['result_sha']!=digest(RESULT)
                or json.loads(row['usage_json'])['local_diary_reconciliation']['evidence_sha']!=digest(proof)):raise ValueError('Diary completion evidence differs')
    root=Path(proof['backend_root'])
    if not root.is_relative_to(Path(home)):raise ValueError('Backend home changed')
    key=digest({k:proof['binding'][k] for k in ('agent_id','conversation_id')})
    cp=json.loads(read(root/'conversations'/(key+'.json')));terminal=cp.get('terminal') or {}
    if (cp.get('binding')!=proof['binding'] or cp.get('phase')!='terminal_committed' or cp.get('claim')
            or terminal.get('stop_reason')!='error' or terminal.get('error_category')!='diary_not_committed'
            or terminal.get('maintenance_receipt_sha')!=digest(proof) or terminal.get('acknowledged') is not True or terminal.get('settled') is not True
            or cp['tools'][0].get('result_ref')!=digest(RESULT)):raise ValueError('Backend diary completion differs')
    return True


if __name__=='__main__':
    payload=json.load(sys.stdin)
    if sys.argv[1]=='record':controller_phase(payload,'record')
    elif sys.argv[1]=='finalize':print(json.dumps(finalize(payload['database'],payload['proof'])))
    else:raise SystemExit('Unsupported diary repair phase')
