#!/usr/bin/env python3
"""Install uncapped self-managed credits and owner-authorized autonomous media."""
import argparse
from contextlib import closing
import ast
import grp
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BASE = 'd325230'
UPGRADE_BASES = ('bffca82', '239f451', 'f7418d2', 'd02477b', '2faba07', 'c68e128', '0afe2f9', '3ffd87e', '045f7f8', '747ec6b', 'e3af2cd', '387162e', '8f3dc4d', '6d91b06', '0959c63', '5127ce2', '45ab856', 'eb80f5f', '1f60d34', '9e5d3df', 'b0a3439', '40418d9')
TARGET = Path('/opt/andrew/prototype/andrewbot')
DROPIN = Path('/etc/systemd/system/andrew-control.service.d/zz-autonomy-media.conf')
MODULES = ('configuration.py', 'appserver.py', 'budget.py', 'controller.py', 'gateway.py', 'lifecycle_policy.py',
           'dispatch.py', 'gmail_api.py', 'gmail_tools.py', 'main.py', 'media.py', 'memory.py', 'model.py', 'sessions.py', 'skills.py', 'store.py', 'telegram.py', 'telegram_menu.py', 'vault_cli.py','memory_backup.py','recovery.py','recovery_records.py','recovery_dispatch.py','tool_errors.py')
NEW_MODULES=frozenset({'recovery.py','recovery_records.py','recovery_dispatch.py','tool_errors.py'})
SKILLS = ('voice-communication', 'image-creation')
GUIDANCE = ('The owner has limited API credits in general and authorizes me to decide how to use them. '
            'The owner removed all controller daily, monthly, and per-job spending caps and daily media quotas. '
            'I am responsible for checking available provider credits with check_budget, reviewing reasoning and '
            'media spending, allocating credits, and planning my activity. Credit availability may be unknown '
            'when the provider does not expose it; I must not invent a balance or assume unlimited credits. '
            'The owner gives standing permission for me to choose, generate, and send voice notes and images '
            'to the owner, including during autonomous activity. Spontaneous TTS starts on and can be controlled '
            'with /voice_on and /voice_off. Uncertain paid or delivery attempts remain held without automatic replay.')
CONFIG = b'''# Owner authorized self-managed credits and autonomous media.
[Service]
Environment=ANDREW_MEDIA_ENABLED=1
Environment=ANDREW_MEDIA_CAPABILITIES_VERIFIED=1
Environment=ANDREW_MEDIA_VOICE_ENABLED=1
Environment=ANDREW_MEDIA_SPONTANEOUS_VOICE=1
Environment=ANDREW_MEDIA_SPONTANEOUS_IMAGE=1
UnsetEnvironment=ANDREW_MONTHLY_LIMIT_USD ANDREW_MONTHLY_RESERVE_USD ANDREW_DAILY_TARGET_USD ANDREW_MEDIA_MONTHLY_PER_KIND_USD ANDREW_MEDIA_DAILY_PER_KIND_USD ANDREW_MEDIA_VOICE_JOB_USD
'''


def load_helper():
    spec = importlib.util.spec_from_file_location('autonomy_deployment', ROOT / 'scripts/install-p1-fixes.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def idle(*,allowed_running_cycle=None):
    with closing(sqlite3.connect('file:/var/lib/andrew/control/controller.sqlite3?mode=ro', uri=True, timeout=5)) as db:
        state = dict(db.execute("SELECT key,value FROM state WHERE key IN "
                                "('paused','sleeping','autonomy_error','active_session_id','session_start_requested')"))
        if not (state.get('paused') == '1' or state.get('sleeping') == '1' or state.get('autonomy_error')):
            raise ValueError('Send Andrew /pause before installation; no services changed')
        if state.get('active_session_id') or state.get('session_start_requested'):
            raise ValueError('Andrew still has an active session; wait for /pause to settle')
        if db.execute("SELECT count(*) FROM cycles WHERE status='running' AND (? IS NULL OR cycle_id!=?)",
                      (allowed_running_cycle,allowed_running_cycle)).fetchone()[0]:
            raise ValueError('An attempted operation is still settling; installation deferred')
        for query in ("SELECT count(*) FROM messages WHERE queued=2",
                      "SELECT count(*) FROM telegram_claims",
                      "SELECT count(*) FROM media_jobs WHERE state='running' OR delivery='sending'",
                      "SELECT count(*) FROM media_avatar_proposals WHERE state='applying'"):
            if db.execute(query).fetchone()[0]:
                raise ValueError('An attempted operation is still settling; installation deferred')


def merge_skill_catalogs(live, base, proposed):
    """Merge literal catalog entries independently, preserving live-only skills."""
    sources = (live, base, proposed)
    trees = [ast.parse(source) for source in sources]
    catalogs = {}
    for name in ('SKILL_DOCUMENT_HASHES', 'SKILL_SUMMARIES'):
        nodes = []
        values = []
        for tree in trees:
            matches = [node.value for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)]
            if len(matches) != 1:
                raise ValueError('Expected one literal skill catalog: ' + name)
            try:
                value = ast.literal_eval(matches[0])
            except (ValueError, TypeError):
                raise ValueError('Skill catalog is no longer a literal dictionary: ' + name) from None
            if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
                raise ValueError('Unexpected skill catalog structure: ' + name)
            nodes.append(matches[0])
            values.append(value)
        live_values, base_values, proposed_values = values
        missing = object()
        merged = {}
        for key in dict.fromkeys([*base_values, *live_values, *proposed_values]):
            current, previous, candidate = (value.get(key, missing) for value in values)
            if key in SKILLS and candidate is not missing:
                # These two documents are explicitly replaced by this release;
                # their hashes and descriptions must match those documents.
                selected = candidate
            elif current == previous:
                selected = candidate
            elif candidate == previous or current == candidate:
                selected = current
            else:
                raise ValueError('Deployed and proposed skill entries overlap: ' + name + '/' + key)
            if selected is not missing:
                merged[key] = selected
        catalogs[name] = (nodes, repr(merged).encode('utf-8'))
    normalized = []
    for index, source in enumerate(sources):
        lines = source.splitlines(keepends=True)
        edits = []
        for nodes, replacement in catalogs.values():
            node = nodes[index]
            start = sum(map(len, lines[:node.lineno - 1])) + node.col_offset
            end = sum(map(len, lines[:node.end_lineno - 1])) + node.end_col_offset
            edits.append((start, end, replacement))
        for start, end, replacement in sorted(edits, reverse=True):
            source = source[:start] + replacement + source[end:]
        ast.parse(source)
        normalized.append(source)
    return tuple(normalized)


def normalize_gateway_merge(live, base, proposed):
    """Keep the deployed response validator while adding independent diagnostics."""
    sources = (live, base, proposed)
    trees = [ast.parse(source) for source in sources]
    validator_test = ast.parse('not usable_provider_choices(result)', mode='eval').body
    legacy_test = ast.parse("not isinstance(result, dict) or not isinstance(result.get('choices'), list) or not result['choices']",
                            mode='eval').body
    signature = lambda node: ast.dump(node, include_attributes=False)
    preserve_validator = (any(isinstance(node, ast.FunctionDef) and node.name == 'usable_provider_choices'
                              for node in trees[0].body)
                          and any(isinstance(node, ast.If) and signature(node.test) == signature(validator_test)
                                  for node in ast.walk(trees[0])))
    normalized = []
    for source, tree in zip(sources, trees):
        lines = source.splitlines(keepends=True)
        edits = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == '__future__':
                raise ValueError('Unexpected future import in gateway; merge requires review')
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == 'model':
                if any(alias.name == 'ProviderRequestError' for alias in node.names):
                    if len(node.names) != 1:
                        raise ValueError('Unexpected combined provider error import')
                    edits.append((sum(map(len, lines[:node.lineno - 1])),
                                  sum(map(len, lines[:node.end_lineno])), b''))
        if preserve_validator:
            legacy = [node.test for node in ast.walk(tree) if isinstance(node, ast.If)
                      and signature(node.test) == signature(legacy_test)]
            current = [node.test for node in ast.walk(tree) if isinstance(node, ast.If)
                       and signature(node.test) == signature(validator_test)]
            if len(legacy) + len(current) != 1:
                raise ValueError('Unexpected gateway response validator; merge requires review')
            for node in legacy:
                edits.append((sum(map(len, lines[:node.lineno - 1])) + node.col_offset,
                              sum(map(len, lines[:node.end_lineno - 1])) + node.end_col_offset,
                              b'not usable_provider_choices(result)'))
        for start, end, replacement in sorted(edits, reverse=True):
            source = source[:start] + replacement + source[end:]
        # Give all merge inputs the same import. This avoids treating the new
        # import and a live helper added at the import/class boundary as overlap.
        source = b'from .model import ProviderRequestError\n' + source
        ast.parse(source)
        normalized.append(source)
    return tuple(normalized)


def merge_source(live, base, proposed, directory, name):
    if live == proposed:
        return live
    if live == base:
        ast.parse(proposed)
        return proposed
    if name == 'appserver.py':
        # The deployed timeout patch removed this obsolete comment immediately
        # below reply(). Both sides agree on deletion; normalize the ancestor
        # so git can merge the neighbouring new origin parameter independently.
        comment = (b'        # Owner replies can span multiple model/tool rounds just like autonomous\n'
                   b'        # work. Bound network operations separately and retain explicit cancellation.\n')
        if comment not in live and comment not in proposed and base.count(comment) == 1:
            base = base.replace(comment, b'')
    if name == 'skills.py':
        live, base, proposed = merge_skill_catalogs(live, base, proposed)
    if name == 'gateway.py':
        live, base, proposed = normalize_gateway_merge(live, base, proposed)
    paths = [directory / (name + suffix) for suffix in ('.live', '.base', '.proposed')]
    for path, data in zip(paths, (live, base, proposed)):
        path.write_bytes(data)
    result = subprocess.run(['git', 'merge-file', '-p', *map(str, paths)], capture_output=True, timeout=10)
    if result.returncode:
        raise ValueError('Deployed changes overlap the proposed change in ' + name + '; no services changed')
    ast.parse(result.stdout)
    return result.stdout


def prepare(backup):
    tested = json.loads((ROOT / 'scripts/autonomy-media-manifest.json').read_text())
    originals, replacements = {}, {}
    work = backup / 'merge'
    work.mkdir()
    for name in MODULES:
        path = TARGET / name
        if path.is_symlink():
            raise ValueError('Symlink in deployed module: ' + name)
        if name in NEW_MODULES:
            proposed=(ROOT/'prototype/andrewbot'/name).read_bytes()
            if hashlib.sha256(proposed).hexdigest()!=tested['modules'][name]:raise ValueError('Proposed source changed since testing: '+name)
            live=path.read_bytes() if path.exists() else None
            if live is not None and live!=proposed:raise ValueError('Unreviewed recovery module: '+name)
            originals[name]=live;replacements[name]=proposed
            if live is not None:(backup/name).write_bytes(live)
            continue
        live = path.read_bytes()
        base = subprocess.check_output(['git', '-C', str(ROOT), 'show', BASE + ':prototype/andrewbot/' + name])
        reviewed = tested.get('deployed_upgrade_bases', {}).get(name, {}).get(hashlib.sha256(live).hexdigest())
        if reviewed:
            if reviewed not in UPGRADE_BASES:
                raise ValueError('Unrecognized reviewed deployment base for ' + name)
            # The exact deployed hash was reviewed against this release. Its
            # independent live-only changes stay on the live side of the merge.
            base = subprocess.check_output(['git', '-C', str(ROOT), 'show', reviewed + ':prototype/andrewbot/' + name])
        else:
            for revision in UPGRADE_BASES:
                previous = subprocess.check_output(['git', '-C', str(ROOT), 'show', revision + ':prototype/andrewbot/' + name])
                if live == previous:
                    base = previous
                    break
        proposed = (ROOT / 'prototype/andrewbot' / name).read_bytes()
        if hashlib.sha256(proposed).hexdigest() != tested['modules'][name]:
            raise ValueError('Proposed source changed since testing: ' + name)
        originals[name] = live
        replacements[name] = merge_source(live, base, proposed, work, name)
        (backup / name).write_bytes(live)
    return originals, replacements


def snapshot(backup, replacements):
    root = backup / 'acceptance-source'
    package = root / 'prototype/andrewbot'
    package.mkdir(parents=True)
    for path in TARGET.glob('*.py'):
        if path.is_symlink():
            raise ValueError('Symlink in deployed code')
        (package / path.name).write_bytes(replacements.get(path.name, path.read_bytes()))
    for name,data in replacements.items():(package/name).write_bytes(data)
    shutil.copytree(TARGET.parents[1] / 'skills', root / 'skills')
    tested = json.loads((ROOT / 'scripts/autonomy-media-manifest.json').read_text())
    for name in SKILLS:
        source = ROOT / 'skills' / name / 'SKILL.md'
        if hashlib.sha256(source.read_bytes()).hexdigest() != tested['skills'][name]:
            raise ValueError('Skill guidance changed since testing: ' + name)
        shutil.copy2(ROOT / 'skills' / name / 'SKILL.md', root / 'skills' / name / 'SKILL.md')
    tests = root / 'scripts/tests'
    tests.mkdir(parents=True)
    for path in (ROOT / 'scripts/tests').glob('test_*protocol.py'):
        shutil.copy2(path, tests / path.name)
    shutil.copytree(ROOT/'letta_recovery',root/'letta_recovery',ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(ROOT / 'scripts/letta_recovery_metadata.py', root / 'scripts/letta_recovery_metadata.py')
    shutil.copytree(ROOT / 'prototype/tests', root / 'prototype/tests', ignore=shutil.ignore_patterns('__pycache__'))
    # Unit tests import the worker support package from the project root. The
    # pinned production image is independently checked by runtime acceptance.
    shutil.copytree(ROOT / 'runtime', root / 'runtime', ignore=shutil.ignore_patterns('__pycache__'))
    return root


def core_test_environment(root):
    return {**os.environ, 'PYTHONPATH': os.pathsep.join((str(root / 'prototype'), str(root)))}


def record_owner_guidance(helper):
    code = ("import sys; from andrewbot.store import Store; from andrewbot.memory import validate_memory_text; "
            "s=Store('/var/lib/andrew/control/controller.sqlite3'); text=validate_memory_text(sys.argv[1]); "
            "record=s.get_state('owner_credit_guidance_record_id',''); "
            "record=record or s.create_memory_record('experience',text,'owner'); "
            "s.set_state('owner_credit_guidance_record_id',record); "
            "s.set_state('owner_credit_guidance',text); "
            "s.set_state('media_spontaneous_voice','1'); s.close(); print('Owner credit guidance saved to persistent memory.')")
    print(helper.command('runuser', '-u', 'andrew-control', '--', 'env',
                         'PYTHONPATH=/opt/andrew/prototype', '/usr/bin/python3', '-c', code, GUIDANCE), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo in your server terminal.')
    helper = load_helper()
    idle()
    readiness = helper.load_readiness()
    original_proof = readiness.protected_read(readiness.PROOF)
    before = readiness.bindings()
    readiness.check_proof(json.loads(original_proof), before)
    backup = Path(tempfile.mkdtemp(prefix='andrew-autonomy-media-', dir='/var/backups'))
    print('Backup and acceptance evidence: ' + str(backup), flush=True)
    originals, replacements = prepare(backup)
    skills = {}
    for name in SKILLS:
        path = TARGET.parents[1] / 'skills' / name / 'SKILL.md'
        skills[name] = path.read_bytes()
        (backup / (name + '.md')).write_bytes(skills[name])
    if DROPIN.is_symlink():
        raise ValueError('Symlinked configuration override')
    old_config = DROPIN.read_bytes() if DROPIN.exists() else None
    (backup / 'runtime-proof.json').write_bytes(original_proof)
    (backup / 'config-presence.json').write_text(json.dumps({'existed': old_config is not None}))
    if old_config is not None:
        (backup / 'zz-autonomy-media.conf').write_bytes(old_config)
    candidate = snapshot(backup, replacements)
    print('Verified merge preserves deployed changes and removes controller spending caps.', flush=True)
    if not args.apply:
        print('Preview only. Add --apply to run acceptance checks and install.')
        return
    helper.ROOT = candidate
    helper.acceptance(backup, readiness)
    print('Acceptance check: merged-code-core-tests', flush=True)
    result = subprocess.run(['/usr/bin/python3', '-m', 'unittest', 'discover',
                             '-s', str(candidate / 'prototype/tests'), '-q'],
                            env=core_test_environment(candidate),
                            capture_output=True, text=True, timeout=180)
    (backup / 'merged-code-core-tests.txt').write_text(result.stdout + result.stderr)
    if result.returncode:
        raise ValueError('Merged deployed code failed core tests; see backup evidence. No services changed')
    if readiness.bindings() != before:
        raise ValueError('Runtime changed during acceptance; no services changed')
    gid = grp.getgrnam('andrew-control').gr_gid
    stopped = modified = False
    try:
        helper.command('systemctl', 'stop', 'andrew-control.service')
        stopped = True
        idle()
        helper.snapshot_store(backup)
        for name, original in originals.items():
            current=(TARGET/name).read_bytes() if (TARGET/name).exists() else None
            if current != original:
                raise ValueError('Deployed code changed during installation')
        helper.command('systemctl', 'stop', 'andrew-letta.service')
        modified = True
        for name, data in replacements.items():
            helper.replace(TARGET / name, data, gid)
        for name in SKILLS:
            helper.replace(TARGET.parents[1] / 'skills' / name / 'SKILL.md',
                           (ROOT / 'skills' / name / 'SKILL.md').read_bytes(), gid)
        helper.replace(DROPIN, CONFIG, 0, mode=0o644)
        helper.command('systemctl', 'daemon-reload')
        helper.renew_proof(readiness, json.loads(original_proof), before, backup)
        record_owner_guidance(helper)
        helper.command('systemctl', 'start', 'andrew-letta.service')
        helper.wait_for_letta()
        helper.command('systemctl', 'start', 'andrew-control.service')
        helper.verify_controller_startup(backup)
    except Exception:
        if stopped:
            helper.command('systemctl', 'stop', 'andrew-control.service')
            helper.command('systemctl', 'stop', 'andrew-letta.service')
            if modified:
                for name, data in originals.items():
                    if data is None:(TARGET/name).unlink(missing_ok=True)
                    else:helper.replace(TARGET/name,data,gid)
                for name, data in skills.items():
                    helper.replace(TARGET.parents[1] / 'skills' / name / 'SKILL.md', data, gid)
                if old_config is None:
                    DROPIN.unlink(missing_ok=True)
                else:
                    helper.replace(DROPIN, old_config, 0, mode=0o644)
                helper.replace(readiness.PROOF, original_proof, 0, mode=0o644)
                helper.command('systemctl', 'daemon-reload')
                readiness.validate_runtime_proof()
            helper.command('systemctl', 'start', 'andrew-letta.service')
            helper.wait_for_letta()
            helper.command('systemctl', 'start', 'andrew-control.service')
            helper.verify_controller_startup(backup)
            print('Original deployment restored; startup verified.', flush=True)
        raise
    print('Verification: checking account balance with the existing API key.', flush=True)
    try:
        print(helper.command('/usr/bin/bash', str(ROOT / 'scripts/check-existing-credit-access')), flush=True)
    except subprocess.SubprocessError:
        print('Deployment succeeded; the final balance probe did not complete. '
              'Andrew can query check_budget, or rerun scripts/check-existing-credit-access.', flush=True)
    print('Installed and startup verified: no controller spending caps; TTS and spontaneous voice/images enabled. '
          'Owner guidance saved in persistent memory. Existing pause/sleep/error state and uncertain attempts are preserved. '
          'Use /wake when ready; no wake or replay was requested by this installer.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit('Installation stopped: ' + str(error)) from None
