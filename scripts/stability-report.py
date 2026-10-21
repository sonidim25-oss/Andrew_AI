#!/usr/bin/env python3
"""Daily stability summary from the controller ledger: no model calls, no content output, no writes."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

CATEGORY = re.compile(r'[a-z_]{1,64}')
PATTERNS = {
    'sessions_started': re.compile(r'Autonomous session started \(([a-z_]+)\)$'),
    'session_failures': re.compile(r'Session failed \(category=([a-z_]+)\); automatic model calls stopped$'),
    'journaled_holds': re.compile(r'Journaled model turn held \(category=([a-z_]+)\)$'),
    'provider_retries': re.compile(r'Provider unreachable before any request was sent; autonomous retry \d+ scheduled$'),
    'owner_wakes': re.compile(r'Owner woke Andrew$'),
    'owner_replays': re.compile(r'Owner explicitly authorized uncertain message retry$'),
}


def safe(value):
    return value if isinstance(value, str) and CATEGORY.fullmatch(value) else 'other'


def snapshot(path):
    """Copy a consistent read-only snapshot into memory; the source is never written."""
    source = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        copy = sqlite3.connect(':memory:')
        source.backup(copy)
    finally:
        source.close()
    copy.row_factory = sqlite3.Row
    return copy


def has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def local_day(value, zone):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(zone).date().isoformat()


def report(db, days, zone, now):
    today = now.astimezone(zone).date()
    wanted = [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
    daily = {day: {'sessions_started': Counter(), 'cycles': Counter(), 'stops': Counter(),
                   'session_failures': Counter(), 'journaled_holds': Counter(),
                   'provider_retries': 0, 'owner_wakes': 0, 'owner_replays': 0} for day in wanted}
    earliest = datetime.combine(today - timedelta(days=days), datetime.min.time(), zone).astimezone(timezone.utc).isoformat()
    for row in db.execute('SELECT event, created_at FROM activity WHERE created_at>=?', (earliest,)):
        day = daily.get(local_day(row['created_at'], zone))
        if day is None:
            continue
        for key, pattern in PATTERNS.items():
            match = pattern.match(row['event'] or '')
            if not match:
                continue
            if isinstance(day[key], Counter):
                day[key][safe(match.group(1))] += 1
            else:
                day[key] += 1
            break
    if has_table(db, 'cycles'):
        marks = ','.join('?' for _ in wanted)
        for row in db.execute(f'SELECT local_day, status, count(*) AS n FROM cycles WHERE local_day IN ({marks}) GROUP BY 1, 2', wanted):
            daily[row['local_day']]['cycles'][safe(row['status'])] += row['n']
    if has_table(db, 'research_checkpoints'):
        for row in db.execute('SELECT stop_type, created_at FROM research_checkpoints WHERE created_at>=?', (earliest,)):
            day = daily.get(local_day(row['created_at'], zone))
            if day is not None:
                day['stops'][safe(row['stop_type'])] += 1
    days_out = []
    for day in wanted:
        entry = daily[day]
        owner_needed = (sum(entry['session_failures'].values()) + sum(entry['journaled_holds'].values()))
        days_out.append({'day': day, **{key: dict(value) if isinstance(value, Counter) else value for key, value in entry.items()},
                         'stops_needing_owner': owner_needed})
    state = dict(db.execute('SELECT key, value FROM state').fetchall()) if has_table(db, 'state') else {}
    current = {
        'autonomy_error': safe(state['autonomy_error']) if state.get('autonomy_error') else '',
        'paused': state.get('paused', '0') == '1', 'sleeping': state.get('sleeping', '0') == '1',
        'active_session': bool(state.get('active_session_id')),
        'session_start_requested': safe(state['session_start_requested']) if state.get('session_start_requested') else '',
    }
    if has_table(db, 'recovery_attempts'):
        columns = {row[1] for row in db.execute('PRAGMA table_info(recovery_attempts)')}
        reason = 'hold_reason' if 'hold_reason' in columns else "''"
        current['held_journal_attempts'] = {safe(row[0] or 'unspecified'): row[1] for row in db.execute(
            f"SELECT {reason}, count(*) FROM recovery_attempts WHERE state='held' GROUP BY 1")}
    if has_table(db, 'recovery_effects') and has_table(db, 'recovery_results'):
        current['admitted_effects_without_result'] = db.execute(
            "SELECT count(*) FROM recovery_effects e WHERE e.state='admitted' AND NOT EXISTS "
            "(SELECT 1 FROM recovery_results r WHERE r.admission_id=e.admission_id)").fetchone()[0]
    if has_table(db, 'budget_reservations'):
        current['unsettled_reservations'] = db.execute('SELECT count(*) FROM budget_reservations').fetchone()[0]
    if has_table(db, 'messages'):
        current['uncertain_owner_messages'] = db.execute(
            "SELECT count(*) FROM messages WHERE role='user' AND queued=3").fetchone()[0]
    totals = {'cycles': Counter(), 'stops_needing_owner': 0, 'failure_categories': Counter()}
    for entry in days_out:
        totals['cycles'].update(entry['cycles'])
        totals['stops_needing_owner'] += entry['stops_needing_owner']
        totals['failure_categories'].update(entry['session_failures'])
        totals['failure_categories'].update(entry['journaled_holds'])
    return {'generated_at': now.astimezone(timezone.utc).isoformat(timespec='seconds'), 'timezone': str(zone),
            'days': days_out, 'current': current,
            'totals': {'cycles': dict(totals['cycles']), 'stops_needing_owner': totals['stops_needing_owner'],
                       'failure_categories': dict(totals['failure_categories'].most_common())}}


def render(result):
    lines = [f"Andrew stability report ({result['timezone']}, generated {result['generated_at']})", '',
             f"{'day':<11} {'sessions':>8} {'cycles ok':>9} {'failed':>6} {'owner-stops':>11} {'retries':>7} {'wakes':>5}  categories"]
    for entry in result['days']:
        cycles = entry['cycles']
        failed = sum(count for status, count in cycles.items() if status in {'failed', 'interrupted'})
        categories = Counter(entry['session_failures']) + Counter(entry['journaled_holds'])
        lines.append(f"{entry['day']:<11} {sum(entry['sessions_started'].values()):>8} {cycles.get('complete', 0):>9} {failed:>6} "
                     f"{entry['stops_needing_owner']:>11} {entry['provider_retries']:>7} {entry['owner_wakes']:>5}  "
                     + (', '.join(f'{name}×{count}' for name, count in categories.most_common()) or '-'))
    totals = result['totals']
    complete = totals['cycles'].get('complete', 0)
    failed = sum(count for status, count in totals['cycles'].items() if status in {'failed', 'interrupted'})
    lines += ['', f"Totals: {complete} cycles completed, {failed} failed/interrupted, "
                  f"{totals['stops_needing_owner']} stops needing the owner.",
              'Top stop categories: ' + (', '.join(f'{k}×{v}' for k, v in totals['failure_categories'].items()) or 'none'),
              '', 'Current state: ' + json.dumps(result['current'], sort_keys=True)]
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='/var/lib/andrew/control/controller.sqlite3')
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--timezone', default='America/Toronto')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--now', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 366:
        parser.error('--days must be from 1 to 366')
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    db = snapshot(args.database)
    try:
        result = report(db, args.days, ZoneInfo(args.timezone), now)
    finally:
        db.close()
    print(json.dumps(result, indent=1, sort_keys=True) if args.json else render(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
