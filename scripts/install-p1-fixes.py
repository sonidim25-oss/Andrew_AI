#!/usr/bin/env python3
"""Install the tested incident, owner-control, and complete-delivery fixes."""
import ast
import grp
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import pwd
from datetime import datetime, timezone


_asset_spec = importlib.util.spec_from_file_location('work_limit_assets', Path(__file__).with_name('work_limit_assets.py'))
_assets = importlib.util.module_from_spec(_asset_spec)
_asset_spec.loader.exec_module(_assets)
plan_assets = _assets.plan_assets
prepare_images = _assets.prepare_images


ROOT = Path(__file__).resolve().parents[1]
TARGET = Path('/opt/andrew/prototype/andrewbot')
SERVICE = 'andrew-control.service'
RUNTIME_PACKAGE = Path('/opt/andrew/runtime-services/andrewbot')


def runtime_patch(manifest):
    """Predict the complete reviewed runtime dependency replacement."""
    files = []
    replacements = {}
    for name in ('runner_service.py', 'process_limits.py', 'runner_client.py'):
        if name not in manifest:
            continue
        path = RUNTIME_PACKAGE / name
        if path.is_symlink():
            raise RuntimeError('Symlink in runtime service package; installation refused')
        original = path.read_bytes()
        hashes = manifest[name]
        if hashlib.sha256(original).hexdigest() not in {hashes['original'], hashes['tested'], *hashes.get('reviewed', {})}:
            raise RuntimeError('Installed runtime module differs from reviewed versions: ' + name)
        tested = (ROOT / 'prototype/andrewbot' / name).read_bytes()
        if hashlib.sha256(tested).hexdigest() != hashes['tested']:
            raise RuntimeError('Runtime source changed since testing: ' + name)
        if original != tested:
            replacements[path] = tested
            files.append({'path': path, 'original': original, 'tested': tested, 'backup': 'runtime-' + name})
    if not files:
        return None
    digest = hashlib.sha256()
    for module in sorted(RUNTIME_PACKAGE.glob('*.py')):
        if module.is_symlink():
            raise RuntimeError('Symlink in runtime service package; installation refused')
        digest.update(module.name.encode() + b'\0' + replacements.get(module, module.read_bytes()) + b'\0')
    return {**files[0], 'files': files, 'expected_service_code': digest.hexdigest()}


def runtime_files(plan):
    return plan.get('files', [plan]) if plan else []


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=300, cwd="/").stdout.strip()


def load_readiness():
    spec = importlib.util.spec_from_file_location('installed_readiness', TARGET / 'runtime_readiness.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def acceptance(backup, readiness):
    """Repeat the installed runtime's acceptance checks without reprovisioning it."""
    def check(label, *args):
        print('Acceptance check: ' + label, flush=True)
        try:
            output = command(*args)
        except subprocess.CalledProcessError as error:
            (backup / (label + '.txt')).write_text((error.stdout or '') + '\n' + (error.stderr or ''))
            raise
        (backup / (label + '.txt')).write_text(output + '\n')
    check('kernel-network-browser', 'systemd-run', '--wait', '--pipe', '--collect', '--quiet',
          '--unit=andrew-incident-acceptance', '--uid=andrew-control', '--gid=andrew-control',
          '--property=WorkingDirectory=/opt/andrew/runtime-services',
          '--property=Environment=PYTHONPATH=/opt/andrew/runtime-services',
          '/usr/bin/python3', '/opt/andrew/runtime-services/checks/verify-runtime.py')
    config = dict(line.split('=', 1) for line in readiness.protected_read('/etc/andrew/runner.conf').decode().splitlines()
                  if '=' in line and not line.startswith('#'))
    browser = pwd.getpwnam('andrew-browser')
    check('browser-accounts', 'runuser', '-u', 'andrew-browser', '--', 'env',
          'HOME=' + browser.pw_dir, 'XDG_RUNTIME_DIR=/run/user/' + str(browser.pw_uid),
          'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/' + str(browser.pw_uid) + '/bus',
          'podman', '--runtime=/usr/bin/crun', 'run', '--rm', '--timeout=120', '--log-driver=none',
          '--pull=never', '--network=none', '--sysctl', 'net.ipv4.ip_unprivileged_port_start=0',
          '--read-only', '--user=1000:1000', '--cap-drop=ALL', '--security-opt=no-new-privileges',
          '--memory=2g', '--cpus=2', '--pids-limit=256', '--env', 'HOME=/tmp',
          '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m', '--mount',
          'type=bind,src=/opt/andrew/runtime-services/checks/verify-browser-fixture.py,dst=/opt/andrew/verify-browser-fixture.py,ro=true',
          '--entrypoint', 'python3', config['IMAGE'], '/opt/andrew/verify-browser-fixture.py')
    probe = ('import socket\ntry: socket.create_connection(("1.1.1.1",443),2)\n'
             'except OSError: raise SystemExit(0)\nraise SystemExit("UID networking was not blocked")')
    for user in ('andrew-runtime', 'andrew-browser'):
        check(user + '-network', 'runuser', '-u', user, '--', '/usr/bin/python3', '-c', probe)
    check('letta-protocol', 'env',
          'ANDREW_LETTA_TEST_BINARY=/var/lib/andrew/letta-app/node_modules/@letta-ai/letta-code/letta.js',
          '/usr/bin/python3', '-m', 'unittest', 'discover', '-s', str(ROOT / 'scripts/tests'),
          '-p', 'test_*protocol.py', '-q')


def renew_proof(readiness, original, before, backup, expected_service_code=None, expected_bindings=None):
    after = readiness.bindings()
    expected = {**before}
    if expected_service_code is not None:
        expected['service_code'] = expected_service_code
    if expected_bindings:
        if set(expected_bindings) - {'image', 'policy'}:
            raise RuntimeError('Unexpected runtime binding override')
        expected.update(expected_bindings)
    if any(after.get(key) != value for key, value in expected.items() if key != 'code'):
        raise RuntimeError('Runtime service code, image, or policy changed during installation')
    proof = {**original, **after, 'verified_at': datetime.now(timezone.utc).isoformat(), 'evidence': str(backup)}
    readiness.check_proof(proof, after)
    replace(readiness.PROOF, (json.dumps(proof, indent=2) + '\n').encode(), 0, mode=0o644)
    readiness.validate_runtime_proof()


def check_idle(database=Path('/var/lib/andrew/control/controller.sqlite3')):
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as db:
        state = dict(db.execute("SELECT key,value FROM state WHERE key IN "
                               "('sleeping','active_session_id','session_start_requested',"
                               "'active_session_status','autonomy_error')"))
        halted = (state.get('active_session_status') == 'error' and bool(state.get('autonomy_error')))
        if (state.get('sleeping') != '1' and not halted) or state.get('active_session_id') or state.get('session_start_requested'):
            raise RuntimeError('Andrew must be idle and asleep or halted by error; installation refused')
        for query in ("SELECT count(*) FROM cycles WHERE status='running'",
                      "SELECT count(*) FROM messages WHERE queued=2",
                      "SELECT count(*) FROM telegram_claims",
                      "SELECT count(*) FROM telegram_inbox",
                      "SELECT count(*) FROM telegram_outbox",
                      "SELECT count(*) FROM telegram_delivery_progress WHERE chunk_attempts>0"):
            if db.execute(query).fetchone()[0]:
                raise RuntimeError('Pending work or uncertain delivery prevents safe installation')
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diary_delivery_claims'").fetchone():
            if db.execute('SELECT count(*) FROM diary_delivery_claims').fetchone()[0]:
                raise RuntimeError('Active diary delivery prevents safe installation')


def snapshot_store(backup, database=Path('/var/lib/andrew/control/controller.sqlite3')):
    destination = backup / 'controller-before.sqlite3'
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(destination) as target:
            source.backup(target)
    destination.chmod(0o600)


def wait_for_letta(timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection(('127.0.0.1', 8283), timeout=1):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError('Letta socket did not become ready') from None
            time.sleep(0.25)


def verify_controller_startup(backup, timeout=180):
    deadline = time.monotonic() + timeout
    while True:
        invocation = command('systemctl', 'show', SERVICE, '-p', 'InvocationID', '--value')
        logs = command('journalctl', '--quiet', '_SYSTEMD_INVOCATION_ID=' + invocation, '--no-pager', '-o', 'cat') if invocation else ''
        (backup / 'controller-startup.txt').write_text(logs + '\n')
        if 'Andrew Telegram controller started' in logs:
            command('systemctl', 'is-active', SERVICE)
            return
        state = command('systemctl', 'show', SERVICE, '-p', 'ActiveState', '--value')
        if state in {'failed', 'inactive'}:
            raise RuntimeError('Controller exited before completing startup')
        if time.monotonic() >= deadline:
            raise TimeoutError('Controller startup did not complete')
        time.sleep(0.5)


def replace(path, data, gid, mode=0o640):
    descriptor, name = tempfile.mkstemp(prefix='.incident-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), 0, gid)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run this installer with sudo in your server terminal.')
    manifest = json.loads((ROOT / 'scripts/p1-fix-manifest.json').read_text())
    if set(manifest) != {'appserver.py', 'browser_history.py', 'configuration.py', 'controller.py', 'diary.py', 'dispatch.py', 'latency.py', 'main.py', 'memory.py', 'runner_service.py', 'sessions.py', 'store.py', 'telegram.py', 'telegram_menu.py', 'workers.py', 'model.py', 'runner_client.py', 'process_limits.py'}:
        raise SystemExit('Unexpected module manifest; installation refused.')
    replacements = {}
    originals = {}
    for name, hashes in manifest.items():
        data = (ROOT / 'prototype/andrewbot' / name).read_bytes()
        ast.parse(data)
        if hashlib.sha256(data).hexdigest() != hashes['tested']:
            raise SystemExit('Source changed since testing; installation refused: ' + name)
        path = TARGET / name
        if path.is_symlink():
            raise SystemExit('Symlink in installed module; installation refused: ' + name)
        original = path.read_bytes() if path.exists() else None
        installed_hash = hashlib.sha256(original).hexdigest() if original is not None else None
        if installed_hash is None and (name not in {'latency.py', 'workers.py'} or hashes['original'] is not None):
            raise SystemExit('Required installed module missing; installation refused: ' + name)
        reviewed_hashes = {hashes['original'], hashes['tested'], *hashes.get('reviewed', {}).keys()}
        if installed_hash not in reviewed_hashes:
            raise SystemExit('Installed code differs from every reviewed version; installation refused: '
                             + name + ' (SHA-256 ' + installed_hash + ')')
        replacements[name] = data
        originals[name] = original
    check_idle()
    command('systemctl', 'is-active', SERVICE)
    readiness = load_readiness()
    original_proof_bytes = readiness.protected_read(readiness.PROOF)
    original_proof = json.loads(original_proof_bytes)
    before = readiness.bindings()
    readiness.check_proof(original_proof, before)
    runtime = runtime_patch(manifest)
    assets = plan_assets(ROOT, readiness, before)
    # Protocol checks use the checkout; require it to match every unchanged installed module.
    for path in TARGET.glob('*.py'):
        if path.name not in replacements and path.read_bytes() != (ROOT / 'prototype/andrewbot' / path.name).read_bytes():
            raise SystemExit('Unchanged module differs from checkout; installation refused: ' + path.name)
    gid = grp.getgrnam('andrew-control').gr_gid
    backup = Path(tempfile.mkdtemp(prefix='andrew-incident-', dir='/var/backups'))
    for name, original in originals.items():
        if original is not None:
            shutil.copy2(TARGET / name, backup / name)
    (backup / 'module-presence.json').write_text(json.dumps({name: data is not None for name, data in originals.items()}) + '\n')
    (backup / 'runtime-proof.json').write_bytes(original_proof_bytes)
    for item in runtime_files(runtime):
        (backup / item.get('backup', 'runtime-' + item['path'].name)).write_bytes(item['original'])
    if assets:
        for item in assets['files']:
            (backup / item['backup']).write_bytes(item['original'])
        (backup / 'image-release.json').write_text(json.dumps(assets['release'], indent=2) + '\n')
    print('Original modules backed up in ' + str(backup), flush=True)
    controller_stopped = False
    runtime_changed = False
    assets_changed = False
    new_controller_attempted = False
    stage = 'acceptance'
    try:
        if assets:
            stage = 'image_preload'
            prepare_images(assets)
        if runtime or assets:
            # No autonomous work may run against an unaccepted service binding.
            command('systemctl', 'stop', SERVICE)
            controller_stopped = True
            check_idle()
            stage = 'runtime_work_limits_patch'
            command('systemctl', 'stop', 'andrew-runner.service')
            if assets:
                command('systemctl', 'stop', 'andrew-browser.service')
            if runtime:
                runtime_changed = True
                for item in runtime_files(runtime):
                    replace(item['path'], item['tested'], 0, mode=0o644)
            if assets:
                assets_changed = True
                for item in assets['files']:
                    replace(item['path'], item['tested'], 0, mode=0o644)
            command('systemctl', 'start', 'andrew-runner.service')
            command('systemctl', 'is-active', 'andrew-runner.service')
            if assets:
                command('systemctl', 'start', 'andrew-browser.service')
                command('systemctl', 'is-active', 'andrew-browser.service')
        stage = 'acceptance'
        acceptance(backup, readiness)
        expected_before = {**before, **({'service_code': runtime['expected_service_code']} if runtime else {}),
                           **(assets['bindings'] if assets else {})}
        if readiness.bindings() != expected_before:
            raise RuntimeError('Runtime bindings changed unexpectedly during acceptance')
        check_idle()
        if not controller_stopped:
            command('systemctl', 'stop', SERVICE)
            controller_stopped = True
        stage = 'idle_recheck'
        check_idle()
        stage = 'database_snapshot'
        snapshot_store(backup)
        # The local backend keeps active-run bookkeeping in memory. A controller
        # restart alone cannot clear an orphaned run left by an old transport.
        stage = 'backend_restart'
        command('systemctl', 'restart', 'andrew-letta.service')
        command('systemctl', 'is-active', 'andrew-letta.service')
        stage = 'backend_readiness'
        wait_for_letta()
        stage = 'module_installation'
        for name, data in replacements.items():
            replace(TARGET / name, data, gid)
        stage = 'proof_renewal'
        if assets:
            renew_proof(readiness, original_proof, before, backup,
                        expected_service_code=runtime['expected_service_code'] if runtime else None,
                        expected_bindings=assets['bindings'])
        elif runtime:
            renew_proof(readiness, original_proof, before, backup, expected_service_code=runtime['expected_service_code'])
        else:
            renew_proof(readiness, original_proof, before, backup)
        stage = 'controller_start'
        new_controller_attempted = True
        command('systemctl', 'start', SERVICE)
        stage = 'controller_startup_verification'
        verify_controller_startup(backup)
        print('Reviewed fixes installed; Letta backend restarted and controller startup verified. Stored messages were preserved; no wake or replay was requested.')
    except Exception as error:
        (backup / 'installation-failure.json').write_text(json.dumps({'stage': stage, 'error_type': type(error).__name__}) + '\n')
        if not controller_stopped:
            raise SystemExit('Acceptance failed before installation; controller left running unchanged. Evidence: ' + str(backup))
        command('systemctl', 'stop', SERVICE)
        if new_controller_attempted:
            # Disable boot startup before restoring older sender code. A crash
            # during rollback must not revive automatic uncertain-message retries.
            command('systemctl', 'disable', SERVICE)
        for name, original in originals.items():
            if original is None:
                (TARGET / name).unlink(missing_ok=True)
            else:
                replace(TARGET / name, (backup / name).read_bytes(), gid)
        if runtime_changed or assets_changed:
            command('systemctl', 'stop', 'andrew-runner.service')
            if assets_changed:
                command('systemctl', 'stop', 'andrew-browser.service')
            if runtime_changed:
                for item in runtime_files(runtime):
                    replace(item['path'], item['original'], 0, mode=0o644)
            if assets_changed:
                for item in assets['files']:
                    replace(item['path'], item['original'], 0, mode=0o644)
            command('systemctl', 'start', 'andrew-runner.service')
            if assets_changed:
                command('systemctl', 'start', 'andrew-browser.service')
        replace(readiness.PROOF, original_proof_bytes, 0, mode=0o644)
        if new_controller_attempted:
            # A new sender may have persisted held/claimed deliveries. The older
            # sender ignores them and must not run automatically, including at boot.
            (backup / 'rollback-state.json').write_text(json.dumps({
                'controller': 'stopped', 'boot_start': 'disabled',
                'reason': 'delivery_state_requires_compatible_sender'}) + '\n')
            raise SystemExit('Installation failed at ' + stage + ' (' + type(error).__name__ + '); '
                             'code and proof restored, database preserved. Controller left stopped and disabled '
                             'to protect uncertain deliveries; do not start the older sender. Evidence: ' + str(backup))
        command('systemctl', 'start', SERVICE)
        raise SystemExit('Installation failed at ' + stage + ' (' + type(error).__name__ + '); '
                         'backed-up modules and acceptance record restored, controller restarted. Evidence: ' + str(backup))


if __name__ == '__main__':
    main()
