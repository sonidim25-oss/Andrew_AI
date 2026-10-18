"""Retain and retire only a proven pre-effect projection failure during install."""
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
from andrewbot.recovery_records import digest,validate_binding


def read(path):
    path=Path(path)
    if any(parent.is_symlink() for parent in [path,*path.parents]) or not path.is_file():
        raise ValueError('Projection repair requires regular files')
    if path.stat().st_size>268435456:raise ValueError('Projection repair file too large')
    return path.read_bytes()


def _controller_guard(db,admission):
    db.row_factory=sqlite3.Row
    state=dict(db.execute('SELECT key,value FROM state'))
    if state.get('paused')!='1' or state.get('active_session_id') or state.get('session_start_requested'):
        raise ValueError('Projection retirement requires settled owner pause')
    row=db.execute('SELECT * FROM recovery_attempts WHERE admission_id=?',(admission,)).fetchone()
    if not row or row['state']!='held' or row['origin']!='autonomous':raise ValueError('Projection retirement requires an exact held autonomous input')
    binding=validate_binding(json.loads(row['binding_json']))
    if row['binding_sha']!=digest(binding):raise ValueError('Projection retirement binding changed')
    if db.execute('SELECT 1 FROM recovery_effects WHERE binding_sha=? LIMIT 1',(digest(binding),)).fetchone():
        raise ValueError('Projection input has admitted effects; retirement refused')
    if (state.get('letta_agent_id'),state.get('letta_conversation_id'))!=(binding['agent_id'],binding['conversation_id']):
        raise ValueError('Projection retirement runtime changed')
    return binding


def plan(database,home,admission):
    database=Path(database)
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        binding=_controller_guard(db,admission)
    key=digest({key:binding[key] for key in ('agent_id','conversation_id')})
    roots=[Path(home)/'.letta',Path(home)/'.letta/lc-local-backend']
    checkpoints=[root/'local-recovery-v1/conversations'/f'{key}.json' for root in roots]
    checkpoints=[path for path in checkpoints if path.exists()]
    if len(checkpoints)!=1:raise ValueError('Projection retirement requires one exact backend checkpoint')
    checkpoint=checkpoints[0];snapshot=json.loads(read(checkpoint))
    if (snapshot.get('binding')!=binding or snapshot.get('phase')!='held'
            or snapshot.get('held_phase')!='input_committed' or snapshot.get('hold_reason')!='projection_conflict'
            or snapshot.get('steps')!=[] or snapshot.get('tools')!=[] or snapshot.get('terminal') is not None
            or snapshot.get('context_ref')):
        raise ValueError('Backend does not prove a pre-effect projection-only hold')
    root=checkpoint.parents[1];identity=json.loads(read(root/'identity.json'))
    if identity.get('store_id')!=binding['store_id']:raise ValueError('Projection store identity changed')
    input_path=root/'blobs'/key/snapshot['input_ref'];input_bytes=read(input_path)
    if hashlib.sha256(input_bytes).hexdigest()!=binding['input_sha'] or snapshot['input_ref']!=binding['input_sha']:
        raise ValueError('Projection original input evidence changed')
    operation='input:'+snapshot['run_id'];missing_id='message-'+digest(operation)
    projection=checkpoint.with_name(key+'.projection.json');intent=json.loads(read(projection))
    expected={'operation_id':operation,'kind':'input','value':json.loads(input_bytes),
              'binding_sha':digest(binding),'input_sha':binding['input_sha'],
              'digest':digest({'kind':'input','value':json.loads(input_bytes)})}
    if intent.get('rows')!=[expected]:raise ValueError('Projection intent contains other work')
    matches=[]
    for storage in roots:
        for path in (storage/'conversations').glob('*/conversation.json'):
            conversation=json.loads(read(path))
            if (conversation.get('agent_id'),conversation.get('id'))==(binding['agent_id'],binding['conversation_id']):matches.append((path,conversation))
    if len(matches)!=1:raise ValueError('Projection conversation is ambiguous')
    conversation_path,conversation=matches[0];transcript=conversation_path.with_name('messages.jsonl')
    rows=[json.loads(line) for line in read(transcript).splitlines() if line.strip()]
    messages=[row['message'] for row in rows if isinstance(row.get('message'),dict)]
    if any(message.get('id')==missing_id or message.get('metadata',{}).get('andrew_recovery') for message in messages):
        raise ValueError('Projection input already exists in transcript; retirement refused')
    ids=conversation.get('in_context_message_ids',[]);available={message.get('id') for message in messages}
    if ids.count(missing_id)!=1 or any(item not in available and item!=missing_id for item in ids):
        raise ValueError('Projection active IDs differ from the expected missing input')
    archive=checkpoint.parent/(key+'.history')/(digest(binding)+'.json')
    if any(parent.is_symlink() for parent in [archive,*archive.parents]):raise ValueError('Unsafe projection archive path')
    if archive.exists():raise ValueError('Projection input already has archived evidence')
    files=[checkpoint,projection,conversation_path,transcript,input_path,root/'identity.json']
    return {'admission_id':admission,'binding':binding,'database':str(database),'checkpoint':str(checkpoint),
            'conversation':str(conversation_path),'archive':str(archive),'missing_id':missing_id,
            'files':{str(path):hashlib.sha256(read(path)).hexdigest() for path in files}}


def _atomic(path,data):
    path=Path(path);fd,name=tempfile.mkstemp(prefix='.projection-repair-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)


def retire_backend(value):
    for path,expected in value['files'].items():
        if hashlib.sha256(read(path)).hexdigest()!=expected:raise ValueError('Projection evidence changed before retirement')
    archive=Path(value['archive'])
    if any(parent.is_symlink() for parent in [archive,*archive.parents]):raise ValueError('Unsafe projection archive path')
    archive.parent.mkdir(mode=0o700,exist_ok=True)
    if archive.exists():raise ValueError('Projection archive already exists')
    _atomic(archive,read(value['checkpoint']))
    path=Path(value['conversation']);conversation=json.loads(read(path))
    conversation['in_context_message_ids']=[item for item in conversation['in_context_message_ids'] if item!=value['missing_id']]
    _atomic(path,(json.dumps(conversation,ensure_ascii=False)+'\n').encode())
    Path(value['checkpoint']).unlink()
    directory=os.open(Path(value['checkpoint']).parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(directory)
    finally:os.close(directory)


def retire_controller(value):
    with closing(sqlite3.connect(value['database'])) as db:
        with db:
            db.execute('BEGIN IMMEDIATE')
            if _controller_guard(db,value['admission_id'])!=value['binding']:raise ValueError('Projection controller binding changed')
            db.execute("UPDATE recovery_attempts SET state='retired',hold_reason='projection_conflict' WHERE admission_id=?",(value['admission_id'],))
            db.execute("UPDATE cycles SET status='interrupted',ended_at=coalesce(ended_at,strftime('%Y-%m-%dT%H:%M:%S+00:00','now')) WHERE cycle_id=? AND session_id=? AND status='running'",
                       (value['binding']['cycle_id'],value['binding']['session_id']))
            db.execute("DELETE FROM state WHERE key='active_cycle_id' AND value=?",(value['binding']['cycle_id'],))


def retire(value,snapshot):
    from andrewbot.memory_backup import verify_paired_recovery_snapshot
    verify_paired_recovery_snapshot(snapshot)
    # Each writer runs as the existing store owner, retaining filesystem access.
    for user,phase in [('andrew-letta','backend'),('andrew-control','controller')]:
        result=subprocess.run(['runuser','-u',user,'--','/usr/bin/python3','-c',Path(__file__).read_text(),phase],
                              input=json.dumps(value),text=True,capture_output=True)
        (Path(snapshot).parent/(phase+'-projection-retirement.txt')).write_text(result.stdout+result.stderr)
        if result.returncode:raise ValueError('Projection retirement '+phase+' step failed; services remain stopped; evidence retained beside paired snapshot')


if __name__=='__main__':
    value=json.load(sys.stdin)
    if sys.argv[1]=='backend':retire_backend(value)
    elif sys.argv[1]=='controller':retire_controller(value)
    else:raise SystemExit('Unsupported repair phase')
