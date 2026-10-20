"""Read-only eligibility for updating one halted owner-handoff checkpoint."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing
import stat
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'prototype'))
from andrewbot.recovery_records import paused_owner_yield


PHASES={'input_committed','provider_confirmed','provider_backoff','tool_prepared','tool_confirmed','continuation_ready'}


def read(path):
    path=Path(path);directory=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.absolute().parts[1:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory);os.close(directory);directory=child
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        with os.fdopen(fd,'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):raise ValueError('Unsafe checkpoint')
            value=stream.read(1024*1024+1)
            if len(value)>1024*1024:raise ValueError('Checkpoint too large')
            return value
    finally:os.close(directory)


def plan(database,home,admission,*,late_provider=False):
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN')
        state=dict(db.execute('SELECT key,value FROM state'))
        pending=db.execute("SELECT * FROM recovery_attempts WHERE state IN ('active','held','evidence','yielding')").fetchall()
        if len(pending)!=1 or pending[0]['admission_id']!=admission:raise ValueError('Requires one exact pending handoff')
        scope=pending[0];binding=json.loads(scope['binding_json'])
        # Receipt-only maintenance may ignore stale reply progress only after
        # the effect/result and no-dispatch checks below prove settlement.
        paused=paused_owner_yield({**state,'model_turn_progress':''} if late_provider else state,binding)
        if scope['origin']!='autonomous' or (not paused and (state.get('autonomy_error')!='local_recovery_interrupted'
                or state.get('paused','0')!='0' or state.get('sleeping','0')!='0'
                or state.get('active_session_id')!=binding['session_id'] or state.get('active_cycle_id')!=binding['cycle_id']
                or state.get('owner_control_revision','0')!=binding['owner_revision']
                or any(state.get(key) for key in (('model_turn_origin','autonomous_owner_id','session_start_requested') if late_provider else ('model_turn_origin','model_turn_progress','autonomous_owner_id','session_start_requested')))
                or any(state.get(key)=='1' for key in ('restore_recovery_required','local_recovery_restore_required')))):raise ValueError('Owner state changed or work is not halted')
        if db.execute("SELECT 1 FROM recovery_effects WHERE state='admitted' LIMIT 1").fetchone():raise ValueError('An effect is still uncertain')
        for sql in ("SELECT 1 FROM messages WHERE queued=2 LIMIT 1","SELECT 1 FROM telegram_claims LIMIT 1",
                    "SELECT 1 FROM telegram_outbox WHERE delivery_state='sending' LIMIT 1",'SELECT 1 FROM diary_delivery_claims LIMIT 1',
                    "SELECT 1 FROM media_jobs WHERE state='running' OR delivery='sending' LIMIT 1",
                    "SELECT 1 FROM media_avatar_proposals WHERE state='applying' LIMIT 1"):
            if db.execute(sql).fetchone():raise ValueError('Other work is still settling')
        if paused:
            if db.execute("SELECT 1 FROM cycles WHERE status='running' AND cycle_id!=? LIMIT 1",(binding['cycle_id'],)).fetchone():raise ValueError('Unexpected running cycles')
            cycle=db.execute('SELECT * FROM cycles WHERE cycle_id=? AND session_id=?',(binding['cycle_id'],binding['session_id'])).fetchone()
            if cycle is None or cycle['status'] not in ('running','interrupted','stopped'):raise ValueError('Original cycle differs')
        elif db.execute("SELECT count(*) FROM cycles WHERE status='running'").fetchone()[0]!=1:raise ValueError('Unexpected running cycles')
        receipt=db.execute('SELECT * FROM model_turn_receipts WHERE cycle_id=?',(binding['cycle_id'],)).fetchone()
        if receipt is None or receipt['state']!='accepted' or receipt['client_message_id']!=binding['client_message_id']:raise ValueError('Input receipt differs')
        if not late_provider and db.execute("SELECT 1 FROM messages WHERE owner_id=? AND role='user' AND queued=1 LIMIT 1",(int(state['local_recovery_owner_id']),)).fetchone() is None:raise ValueError('No saved owner input waits')
        for row in db.execute('SELECT reservation_id FROM budget_reservations'):
            if (not re.fullmatch(r'(?:provider-[a-f0-9]{64}|[a-f0-9]{32})',row[0])
                    or db.execute('SELECT 1 FROM recovery_effects WHERE reservation_id=?',(row[0],)).fetchone()):raise ValueError('Unsettled journaled provider request')
        effects={row['admission_id']:dict(row) for row in db.execute('SELECT * FROM recovery_effects WHERE binding_sha=?',(scope['binding_sha'],))}
        results={row['admission_id']:row['result_sha'] for row in db.execute('SELECT * FROM recovery_results')}
        if not effects or any(row['state']!='confirmed' or row['admission_id'] not in results for row in effects.values()):raise ValueError('Requires confirmed invocation results')
        key=hashlib.sha256(json.dumps({k:binding[k] for k in ('agent_id','conversation_id')},sort_keys=True,separators=(',',':')).encode()).hexdigest()
        paths=[Path(home)/suffix/'local-recovery-v1/conversations'/(key+'.json') for suffix in ('.letta','.letta/lc-local-backend')]
        paths=[path for path in paths if path.exists()]
        if len(paths)!=1:raise ValueError('Requires one backend checkpoint')
        raw=read(paths[0]);checkpoint=json.loads(raw)
        if (checkpoint.get('binding')!=binding or checkpoint.get('phase')!='held' or checkpoint.get('hold_reason')!=('provider_uncertain' if late_provider else 'owner_blocked')
                or checkpoint.get('held_phase') not in ({'provider_admitted'} if late_provider else PHASES) or checkpoint.get('terminal') is not None or checkpoint.get('claim')):raise ValueError('Backend is not a released owner-blocked checkpoint')
        items=checkpoint['steps']+checkpoint['tools']
        if any(item['state'] not in (('confirmed','prepared','admitted') if late_provider else ('confirmed','prepared')) for item in items):raise ValueError('An invocation is still uncertain')
        recorded={item['admission_id']:item for item in items if item.get('admission_id')}
        if set(recorded)!=set(effects):raise ValueError('Backend and controller effects differ')
        late=checkpoint['steps'][-1] if late_provider and checkpoint['steps'] else None
        if late_provider:
            if not late or late.get('state')!='admitted' or late.get('kind') not in ('provider','compaction'):raise ValueError('Requires one last admitted provider step')
            proof=db.execute('SELECT * FROM recovery_results WHERE admission_id=?',(late.get('admission_id'),)).fetchone()
            if proof is None or json.loads(proof['usage_json'] or '{}').get('settled') is not True:raise ValueError('Provider usage is not settled')
            from andrewbot.recovery_records import digest
            if digest(json.loads(proof['result_json']))!=proof['result_sha']:raise ValueError('Provider result digest differs')
            if effects.get(late.get('admission_id'),{}).get('kind')!=late['kind']:raise ValueError('Provider kind differs')
        for key,item in recorded.items():
            if late_provider and item is late:
                if item.get('request_sha')!=effects[key]['request_sha']:raise ValueError('Provider request identity differs')
                continue
            if item.get('state')!='confirmed' or item.get('request_sha')!=effects[key]['request_sha'] or item.get('result_ref')!=results[key]:raise ValueError('Saved result identities differ')
        return {'eligible':True,'mode':'late_provider_receipt' if late_provider else 'paused_cancellation' if paused else 'active_handoff','owner_revision':state.get('owner_control_revision','0'),'admission_id':admission,'binding':binding,'backend_revision':checkpoint['revision'],
                'checkpoint_sha256':hashlib.sha256(raw).hexdigest(),'confirmed_effects':len(effects),**({'late_admission_id':late['admission_id'],'late_result_sha256':proof['result_sha'],'owner_paused':state.get('paused','0')} if late_provider else {})}
