"""Read-only proof for the historical oversized research.save admission.

No tool execution, receipt creation, lifecycle changes, or reconciliation occurs.
Only fixed rejection metadata, opaque identities, hashes and counts leave here.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'prototype'))
from andrewbot.memory import ResearchJournal, ResearchValidationError
from andrewbot.recovery_records import canonical, digest, paused_owner_yield, validate_binding
from inspect_owner_handoff import read

class InspectionRefused(ValueError):
    """Fixed, safe diagnostic from an explicit inspection guard."""


TARGET = '997ee1420d4d833fc2c049717b342821ceeb743c7e8ef509ae0586c79b4d0b1f'


def rows_digest(db, query, parameters=()):
    rows = [dict(row) for row in db.execute(query, parameters)]
    return {'count': len(rows), 'sha256': digest(rows)}


def inspect(database, home, effect_id=TARGET):
    database, home = Path(database), Path(home)
    if database.is_symlink() or not database.is_file():
        raise InspectionRefused('Unsafe controller database')
    if (database.parent / 'runner-restore-required.json').exists():
        raise InspectionRefused('Restore fence present')
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise InspectionRefused('Controller integrity failed')
        effect = db.execute('SELECT * FROM recovery_effects WHERE admission_id=?', (effect_id,)).fetchone()
        if (effect is None or effect['kind'] != 'tool' or effect['state'] != 'admitted'
                or effect['reservation_id'] is not None):
            raise InspectionRefused('Requires exact admitted local tool without a result')
        scope = db.execute('SELECT * FROM recovery_attempts WHERE admission_id=?', (effect['scope_id'],)).fetchone()
        if scope is None or scope['origin'] != 'autonomous':
            raise InspectionRefused('Original autonomous scope missing')
        binding = validate_binding(json.loads(scope['binding_json']))
        if effect['binding_sha'] != digest(binding) or scope['binding_sha'] != digest(binding):
            raise InspectionRefused('Controller binding mismatch')
        state = dict(db.execute('SELECT key,value FROM state'))
        if not paused_owner_yield(state, binding):
            raise InspectionRefused('Requires the newer owner pause and unchanged paused lifecycle')
        if db.execute('SELECT 1 FROM recovery_results WHERE admission_id=?', (effect_id,)).fetchone():
            raise InspectionRefused('Tool already has a result')
        if db.execute('SELECT 1 FROM runner_operations WHERE admission_id=?', (effect_id,)).fetchone():
            raise InspectionRefused('Runner mapping cannot be reconciled as a local rejection')
        admitted = db.execute("SELECT admission_id FROM recovery_effects WHERE state='admitted'").fetchall()
        if len(admitted) != 1 or admitted[0][0] != effect_id:
            raise InspectionRefused('Another admitted effect remains uncertain')
        pending = db.execute("SELECT binding_sha FROM recovery_attempts WHERE state IN ('active','held','evidence','yielding')").fetchall()
        if not pending or any(row[0] != digest(binding) for row in pending):
            raise InspectionRefused('Another binding is pending')
        for query in (
            'SELECT 1 FROM telegram_claims LIMIT 1',
            "SELECT 1 FROM telegram_outbox WHERE delivery_state='sending' LIMIT 1",
            'SELECT 1 FROM diary_delivery_claims LIMIT 1',
            "SELECT 1 FROM media_jobs WHERE state='running' OR delivery='sending' LIMIT 1",
            "SELECT 1 FROM media_avatar_proposals WHERE state='applying' LIMIT 1",
        ):
            if db.execute(query).fetchone():
                raise InspectionRefused('Other work is still settling')
        key = digest({k: binding[k] for k in ('agent_id', 'conversation_id')})
        paths = [home / suffix / 'local-recovery-v1' / 'conversations' / (key + '.json')
                 for suffix in ('.letta', '.letta/lc-local-backend')]
        paths = [path for path in paths if path.exists()]
        if len(paths) != 1:
            raise InspectionRefused('Requires one exact backend checkpoint')
        path = paths[0]
        raw = read(path)
        cp = json.loads(raw)
        if (cp.get('binding') != binding or cp.get('phase') != 'held'
                or cp.get('hold_reason') != 'tool_uncertain' or cp.get('held_phase') != 'tool_admitted'
                or cp.get('claim') or cp.get('terminal')):
            raise InspectionRefused('Requires a released tool_uncertain checkpoint')
        tools, steps = cp.get('tools', []), cp.get('steps', [])
        matches = [tool for tool in tools if tool.get('admission_id') == effect_id]
        if len(matches) != 1:
            raise InspectionRefused('Target checkpoint tool missing')
        tool = matches[0]
        if (tool.get('name') != 'research.save' or tool.get('state') != 'admitted'
                or tool.get('request_sha') != effect['request_sha'] or tool.get('effect_id') != effect['effect_id']
                or not steps or steps[-1].get('state') != 'confirmed'
                or tool.get('provider_id') != steps[-1].get('effect_id')):
            raise InspectionRefused('Exact research tool identity mismatch')
        effects = {row['admission_id']: dict(row) for row in db.execute(
            'SELECT * FROM recovery_effects WHERE binding_sha=?', (digest(binding),))}
        items = steps + tools
        if len(items) != len(effects) or {item.get('admission_id') for item in items} != set(effects):
            raise InspectionRefused('Controller and backend effects differ')
        for item in items:
            e = effects[item['admission_id']]
            if e['request_sha'] != item['request_sha'] or e['effect_id'] != item['effect_id']:
                raise InspectionRefused('Prior effect identity differs')
            if item is tool:
                continue
            result = db.execute('SELECT * FROM recovery_results WHERE admission_id=?', (e['admission_id'],)).fetchone()
            if (item.get('state') != 'confirmed' or e['state'] != 'confirmed' or result is None
                    or item.get('result_ref') != result['result_sha']
                    or digest(json.loads(result['result_json'])) != result['result_sha']):
                raise InspectionRefused('Prior effect lacks exact confirmation')
            if e['kind'] in ('provider', 'compaction') and json.loads(result['usage_json'] or '{}').get('settled') is not True:
                raise InspectionRefused('Provider usage is unsettled')
            if e['reservation_id'] and db.execute('SELECT 1 FROM budget_reservations WHERE reservation_id=?', (e['reservation_id'],)).fetchone():
                raise InspectionRefused('Binding provider reservation remains unsettled')
        args_raw = read(path.parents[1] / 'blobs' / key / tool['arguments_ref'])
        args = json.loads(args_raw)
        if (hashlib.sha256(args_raw).hexdigest() != tool['arguments_ref']
                or not isinstance(args, dict) or 'id' in args
                or digest({'name': 'research.save', 'arguments': args}) != effect['request_sha']
                or not isinstance(args.get('findings'), str) or len(args['findings']) != 2116):
            raise InspectionRefused('Requires the exact new-entry oversized findings request')
        try:
            ResearchJournal(None).prepare_save(args)
        except ResearchValidationError as error:
            if error.field != 'findings' or error.maximum != 2000:
                raise InspectionRefused('Rejection is not the established findings limit') from None
            result = json.dumps(error.result(), ensure_ascii=False)
        else:
            raise InspectionRefused('Request does not fail pure pre-write validation')
        guards = {k: state.get(k, '') for k in (
            'owner_control_revision', 'paused', 'sleeping', 'sleep_until', 'autonomy_error',
            'active_session_id', 'active_cycle_id', 'model_turn_origin', 'model_turn_progress',
            'autonomous_owner_id', 'session_start_requested', 'letta_agent_id', 'letta_conversation_id',
            'local_recovery_config_sha', 'restore_recovery_required', 'local_recovery_restore_required')}
        return {'contract': 'historical_research_rejection_inspection_v1', 'read_only': True,
                'tool_admission_id': effect_id, 'scope_id': scope['admission_id'], 'binding': binding,
                'request_sha': effect['request_sha'], 'arguments_sha': tool['arguments_ref'],
                'checkpoint_path': str(path), 'checkpoint_sha256': hashlib.sha256(raw).hexdigest(),
                'backend_revision': cp['revision'], 'guards': guards,
                'findings_characters': len(args['findings']), 'maximum_characters': 2000,
                'validation_result': result, 'validation_result_sha': digest(result),
                'effects': rows_digest(db, 'SELECT * FROM recovery_effects ORDER BY admission_id'),
                'results': rows_digest(db, 'SELECT * FROM recovery_results ORDER BY admission_id'),
                'research': rows_digest(db, 'SELECT * FROM research_entries ORDER BY record_id'),
                'held_messages': rows_digest(db, 'SELECT * FROM messages WHERE queued=3 ORDER BY id'),
                'reservations': rows_digest(db, 'SELECT * FROM budget_reservations ORDER BY reservation_id'),
                'cycle_tools': rows_digest(db, 'SELECT * FROM cycle_tool_attempts ORDER BY attempt_id')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='/var/lib/andrew/control/controller.sqlite3')
    parser.add_argument('--home', default='/var/lib/andrew/letta')
    parser.add_argument('--effect', default=TARGET)
    args = parser.parse_args()
    try:
        report = inspect(args.database, args.home, args.effect)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        # Raw exception text could contain private path/argument/provider data.
        print(json.dumps({'read_only': True, 'eligible': False,
                          'reason': str(error) if isinstance(error, InspectionRefused) else 'Exact historical rejection guards did not match; no changes made.'}))
        return 1
    print(json.dumps({'eligible': True, **report}, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
