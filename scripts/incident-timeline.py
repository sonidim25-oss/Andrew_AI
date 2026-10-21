#!/usr/bin/env python3
"""Read-only: for each stop needing the owner, print the controller events just before it.

No model calls, no writes, no message content (activity events are fixed controller phrases).
Usage: sudo python3 scripts/incident-timeline.py [--days 14] [--before 12]
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3

STOP = re.compile(r'(Session failed \(category=[a-z_]+\)|Journaled model turn held \(category=[a-z_]+\))')
GATEWAY = re.compile(r'Letta model gateway provider request failed \(category=([a-z_]+), http_status=([0-9a-z_]+)\)')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', default='/var/lib/andrew/control/controller.sqlite3')
    parser.add_argument('--days', type=int, default=14)
    parser.add_argument('--before', type=int, default=12, help='events shown before each stop')
    args = parser.parse_args(argv)
    source = sqlite3.connect(Path(args.database).resolve().as_uri() + '?mode=ro', uri=True)
    db = sqlite3.connect(':memory:')
    source.backup(db)
    source.close()
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    events = db.execute('SELECT id, created_at, event FROM activity WHERE created_at>=? ORDER BY id', (since,)).fetchall()
    gateway = Counter()
    for _, created, event in events:
        match = GATEWAY.search(event or '')
        if match:
            gateway[(created[:10], match.group(1), match.group(2))] += 1
    print('== Gateway provider failures by UTC day (category, http_status)')
    for (day, category, status), count in sorted(gateway.items()):
        print(f'  {day}  {category:<18} {status:<12} x{count}')
    cycles = db.execute('SELECT session_id, cycle_id, status, started_at, ended_at FROM cycles WHERE started_at>=? ORDER BY started_at',
                        (since,)).fetchall()
    print('\n== Stops needing the owner, with preceding controller events')
    for index, (row_id, created, event) in enumerate(events):
        if not STOP.search(event or ''):
            continue
        print(f'\n--- {created}  {STOP.search(event).group(1)}')
        stop = datetime.fromisoformat(created)
        active = [c for c in cycles if c[3] <= created and (c[4] or created) >= (stop - timedelta(minutes=1)).isoformat()]
        for session_id, cycle_id, status, started, ended in active[-1:]:
            duration = ''
            if ended:
                duration = f' duration={int((datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds())}s'
            print(f'    cycle {cycle_id[:12]} session {session_id[:12]} status={status} started={started}{duration}')
        for _, when, previous in events[max(0, index - args.before):index]:
            print(f'    {when}  {(previous or "")[:150]}')


if __name__ == '__main__':
    main()
