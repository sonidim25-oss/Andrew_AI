#!/usr/bin/env python3
"""Private evidence of unchanged controller IDs, pause/sleep and ledger rows."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

def snapshot(path):
    db=sqlite3.connect('file:'+str(Path(path).resolve())+'?mode=ro',uri=True)
    try:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result={}
        for name in ('usage','budget_reservations','cycles'):
            if name in tables:
                rows=db.execute('SELECT * FROM '+name+' ORDER BY rowid').fetchall()
                result[name]=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()
        keys=('letta_agent_id','letta_conversation_id','paused','sleeping','sleep_until','fallback_wake_day')
        result['state']=dict(db.execute('SELECT key,value FROM state WHERE key IN ('+','.join('?' for _ in keys)+')',keys))
        return result
    finally: db.close()

if __name__=='__main__':
    result=snapshot('/var/lib/andrew/control/controller.sqlite3')
    if sys.argv[1]=='capture': Path(sys.argv[2]).write_text(json.dumps(result,sort_keys=True)+'\n')
    elif sys.argv[1]=='verify':
        assert result==json.loads(Path(sys.argv[2]).read_text()),'Controller identity, owner state or ledger changed during installation'
        print('Controller IDs, owner pause/sleep, cycles and spending rows preserved.')
    else: raise SystemExit('capture or verify required')
