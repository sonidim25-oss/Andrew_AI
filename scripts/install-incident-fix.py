#!/usr/bin/env python3
"""Install the tested incident mitigation, backing up only the four changed modules."""
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
import time
import pwd
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
TARGET = Path('/opt/andrew/prototype/andrewbot')
SERVICE = 'andrew-control.service'


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=300).stdout.strip()


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


def renew_proof(readiness, original, before, backup):
    after = readiness.bindings()
    if any(after.get(key) != value for key, value in before.items() if key != 'code'):
        raise RuntimeError('Runtime service code, image, or policy changed during installation')
    proof = {**original, **after, 'verified_at': datetime.now(timezone.utc).isoformat(), 'evidence': str(backup)}
    readiness.check_proof(proof, after)
    replace(readiness.PROOF, (json.dumps(proof, indent=2) + '\n').encode(), 0, mode=0o644)
    readiness.validate_runtime_proof()


def check_idle():
    with sqlite3.connect('file:/var/lib/andrew/control/controller.sqlite3?mode=ro', uri=True) as db:
        state = dict(db.execute("SELECT key,value FROM state WHERE key IN "
                               "('sleeping','active_session_id','session_start_requested')"))
    if state.get('sleeping') != '1' or state.get('active_session_id') or state.get('session_start_requested'):
        raise RuntimeError('Andrew is no longer idle and asleep; installation refused')


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
    manifest = json.loads((ROOT / 'scripts/incident-fix-manifest.json').read_text())
    if set(manifest) != {'appserver.py', 'configuration.py', 'gateway.py', 'sessions.py'}:
        raise SystemExit('Unexpected module manifest; installation refused.')
    replacements = {}
    for name, hashes in manifest.items():
        data = (ROOT / 'prototype/andrewbot' / name).read_bytes()
        ast.parse(data)
        if hashlib.sha256(data).hexdigest() != hashes['tested']:
            raise SystemExit('Source changed since testing; installation refused: ' + name)
        if hashlib.sha256((TARGET / name).read_bytes()).hexdigest() not in {hashes['original'], hashes['tested']}:
            raise SystemExit('Installed code differs from the reviewed version; installation refused: ' + name)
        replacements[name] = data
    check_idle()
    command('systemctl', 'is-active', SERVICE)
    readiness = load_readiness()
    original_proof_bytes = readiness.protected_read(readiness.PROOF)
    original_proof = json.loads(original_proof_bytes)
    before = readiness.bindings()
    readiness.check_proof(original_proof, before)
    # Protocol checks use the checkout; require it to match every unchanged installed module.
    for path in TARGET.glob('*.py'):
        if path.name not in replacements and path.read_bytes() != (ROOT / 'prototype/andrewbot' / path.name).read_bytes():
            raise SystemExit('Unchanged module differs from checkout; installation refused: ' + path.name)
    gid = grp.getgrnam('andrew-control').gr_gid
    backup = Path(tempfile.mkdtemp(prefix='andrew-incident-', dir='/var/backups'))
    for name in replacements:
        shutil.copy2(TARGET / name, backup / name)
    (backup / 'runtime-proof.json').write_bytes(original_proof_bytes)
    print('Original modules backed up in ' + str(backup), flush=True)
    try:
        acceptance(backup, readiness)
        readiness.check_proof(original_proof, readiness.bindings())
        check_idle()
    except Exception:
        raise SystemExit('Acceptance failed before installation; controller left running unchanged. Evidence: ' + str(backup))
    command('systemctl', 'stop', SERVICE)
    try:
        check_idle()
        for name, data in replacements.items():
            replace(TARGET / name, data, gid)
        renew_proof(readiness, original_proof, before, backup)
        command('systemctl', 'start', SERVICE)
        for _ in range(40):
            invocation = command('systemctl', 'show', SERVICE, '-p', 'InvocationID', '--value')
            logs = command('journalctl', '--quiet', '_SYSTEMD_INVOCATION_ID=' + invocation, '--no-pager', '-o', 'cat') if invocation else ''
            if 'Andrew Telegram controller started' in logs:
                command('systemctl', 'is-active', SERVICE)
                print('Installed and startup verified. No wake was requested; lifecycle and spending records were preserved.')
                return
            time.sleep(0.5)
        raise RuntimeError('Controller startup did not complete')
    except Exception:
        command('systemctl', 'stop', SERVICE)
        for name in replacements:
            replace(TARGET / name, (backup / name).read_bytes(), gid)
        replace(readiness.PROOF, original_proof_bytes, 0, mode=0o644)
        command('systemctl', 'start', SERVICE)
        raise SystemExit('Installation failed; backed-up modules and acceptance record restored, controller restarted. Inspect the service journal.')


if __name__ == '__main__':
    main()
