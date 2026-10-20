#!/usr/bin/env python3
"""Read-only phase, package and schema inspection for the interrupted rollout."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.dont_write_bytecode = True
if os.geteuid() != 0:
    raise SystemExit('Run with sudo to inspect private installation evidence.')
directory = Path('/var/backups/andrew-runner-recovery-candidate-v2')
candidate = json.loads((directory / 'candidate.json').read_text())
spec = importlib.util.spec_from_file_location('accepted_runner_installer', directory / 'source/scripts/install-runner-recovery.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module._trusted_directory(directory)
module.verify_candidate(candidate, require_acceptance=True)
installation = json.loads((directory / 'installation.json').read_text())
paths = {**module.DEFAULT_PATHS, **installation.get('paths', {})}
database = Path(paths['database'])
with sqlite3.connect(database.resolve().as_uri() + '?mode=ro&immutable=1', uri=True) as db:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    state = dict(db.execute('SELECT key,value FROM state'))
    counts = {name: db.execute('SELECT count(*) FROM ' + name).fetchone()[0]
              for name in ('runner_operations', 'runner_imports', 'runner_research_results') if name in tables}
root = Path(paths['runner_root'])
with sqlite3.connect((root / 'receipts.sqlite3').resolve().as_uri() + '?mode=ro&immutable=1', uri=True) as db:
    receipts = db.execute('SELECT count(*) FROM receipts').fetchone()[0]
print(json.dumps(dict(accepted_candidate=True, phase=installation['phase'],
    packages_match={role: module.tree(paths[role]) == candidate[role + '_files'] for role in ('controller', 'runner')},
    owner_preserved=module._owner(database) == installation['owner'],
    paused=state.get('paused'), runner_enabled=state.get('runner_receipts_enabled'),
    runner_tables=counts, receipt_rows=receipts,
    controller_pin_present=bool(state.get('runner_receipt_store_id')),
    adoption_exists=Path(paths['adoption']).exists(),
    code_backup_exists=(directory / 'code-before').exists(),
    paired_snapshot_exists=(directory / 'paired-before/paired-recovery.json').exists(),
    runtime_before_exists=(directory / 'runtime-proof-before.json').exists(),
    incomplete_snapshot_exists=(directory / 'three-store-before-adoption').exists(),
    final_snapshot_metadata_exists=(directory / 'three-store-before-adoption/runner-recovery.json').exists(),
    controller_wal_bytes=Path(str(database) + '-wal').stat().st_size if Path(str(database) + '-wal').exists() else 0)), flush=True)
