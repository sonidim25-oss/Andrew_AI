"""Remove only the deployed owner-turn deadline; preserve all other live code."""
import argparse
import ast
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

TARGET = Path('/opt/andrew/prototype/andrewbot/appserver.py')
EXPECTED = '735153030538f73716dd99c18c5d503dbdb3206d99316a9c6f56be0618453a69'
CONTROL = 'andrew-control.service'
LETTA = 'andrew-letta.service'


def method(tree, class_name, method_name):
    return next(node for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name == class_name
                for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == method_name)


def patch_source(data):
    source = data.decode('utf-8')
    tree = ast.parse(source)
    reply = method(tree, 'LettaAgent', 'reply')
    defaults = dict(zip([arg.arg for arg in reply.args.args][-len(reply.args.defaults):], reply.args.defaults))
    default = defaults.get('timeout')
    if not isinstance(default, ast.Constant) or type(default.value) not in (int, float) or default.value <= 0:
        raise ValueError('Expected a positive fixed owner-turn timeout; no patch applied')
    # Verify the downstream client already supports an unlimited turn and socket read.
    turn = ast.unparse(method(tree, 'AppServerClient', 'send_user_turn'))
    read = ast.unparse(method(tree, 'AppServerConnection', '_read_exactly'))
    if 'deadline = None if timeout is None else' not in turn or 'self.socket.settimeout(None)' not in read:
        raise ValueError('Downstream unlimited-turn support is missing; no patch applied')
    if 'timeout=timeout' not in ast.unparse(reply):
        raise ValueError('Owner reply does not forward its deadline; no patch applied')
    lines = source.splitlines(keepends=True)
    if default.lineno != default.end_lineno:
        raise ValueError('Unexpected timeout syntax')
    index = default.lineno - 1
    line = lines[index].encode('utf-8')
    lines[index] = (line[:default.col_offset] + b'None' + line[default.end_col_offset:]).decode('utf-8')
    patched = ''.join(lines).encode('utf-8')
    old_timeout = default.value
    default.value = None
    if ast.dump(tree, include_attributes=False) != ast.dump(ast.parse(patched), include_attributes=False):
        raise ValueError('Patch changed more than the timeout default')
    return patched, old_timeout


def require_halted():
    with sqlite3.connect('file:/var/lib/andrew/control/controller.sqlite3?mode=ro', uri=True, timeout=5) as db:
        state = dict(db.execute("SELECT key,value FROM state WHERE key IN "
                                "('autonomy_error','active_session_id','session_start_requested')"))
        if state.get('autonomy_error') != 'timeout' or state.get('active_session_id') or state.get('session_start_requested'):
            raise ValueError('Andrew is no longer halted by the reported timeout; no services changed')


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=330).stdout


def deployment_helpers():
    path = Path(__file__).with_name('install-p1-fixes.py')
    spec = importlib.util.spec_from_file_location('owner_timeout_deployment', path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def acceptance_snapshot(backup, patched):
    """Run protocol fixtures against the exact proposed deployed code."""
    root = backup / 'acceptance-source'
    package = root / 'prototype/andrewbot'
    package.mkdir(parents=True)
    for path in TARGET.parent.glob('*.py'):
        if path.is_symlink():
            raise ValueError('Symlink in deployed code; installation refused')
        (package / path.name).write_bytes(patched if path == TARGET else path.read_bytes())
    skills = TARGET.parents[2] / 'skills'
    shutil.copytree(skills, root / 'skills')
    tests = root / 'scripts/tests'
    tests.mkdir(parents=True)
    for path in Path(__file__).with_name('tests').glob('test_*protocol.py'):
        shutil.copy2(path, tests / path.name)
    if not list(tests.glob('test_*protocol.py')):
        raise ValueError('Protocol acceptance fixtures missing')
    return root


def write_module(data, metadata):
    fd, temporary = tempfile.mkstemp(prefix='.owner-timeout-', dir=TARGET.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, metadata.st_mode & 0o777)
        os.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.replace(temporary, TARGET)
    finally:
        Path(temporary).unlink(missing_ok=True)


def start_services(helper, backup):
    command('systemctl', 'start', LETTA)
    deadline = time.monotonic() + 30
    while True:
        try:
            with socket.create_connection(('127.0.0.1', 8283), timeout=1):
                break
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError('Letta did not start') from None
            time.sleep(0.25)
    command('systemctl', 'start', CONTROL)
    helper.verify_controller_startup(backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo to inspect the protected module and session state.')
    if TARGET.is_symlink():
        raise ValueError('Refusing a symlinked module')
    original = TARGET.read_bytes()
    if hashlib.sha256(original).hexdigest() != EXPECTED:
        raise ValueError('Deployed client changed since diagnosis; no patch applied')
    patched, old_timeout = patch_source(original)
    require_halted()
    print(f'Verified: live owner replies have a {old_timeout}-second total deadline.', flush=True)
    if not args.apply:
        print('Preview only. Add --apply to remove that deadline and restart Andrew services.')
        return
    metadata = TARGET.stat()
    helper = deployment_helpers()
    helper.check_idle()
    readiness = helper.load_readiness()
    original_proof = readiness.protected_read(readiness.PROOF)
    proof_metadata = readiness.PROOF.stat()
    before = readiness.bindings()
    readiness.check_proof(json.loads(original_proof), before)
    backup = Path(tempfile.mkdtemp(prefix='andrew-owner-timeout-', dir='/var/backups'))
    shutil.copy2(TARGET, backup / TARGET.name)
    (backup / 'runtime-proof.json').write_bytes(original_proof)
    print('Original module backed up in ' + str(backup), flush=True)
    helper.ROOT = acceptance_snapshot(backup, patched)
    print('Running existing runtime and protocol acceptance checks; this takes several minutes.', flush=True)
    helper.acceptance(backup, readiness)
    if readiness.bindings() != before:
        raise ValueError('Deployed code or runtime policy changed during acceptance; no patch applied')
    stopped = False
    modified = False
    proof_modified = False
    try:
        command('systemctl', 'stop', CONTROL)
        stopped = True
        require_halted()
        helper.check_idle()
        if TARGET.read_bytes() != original:
            raise ValueError('Deployed module changed during patch preparation')
        command('systemctl', 'stop', LETTA)
        write_module(patched, metadata)
        modified = True
        proof_modified = True
        helper.renew_proof(readiness, json.loads(original_proof), before, backup)
        start_services(helper, backup)
    except Exception:
        if stopped:
            command('systemctl', 'stop', CONTROL)
            command('systemctl', 'stop', LETTA)
            if modified:
                write_module(original, metadata)
            if proof_modified:
                helper.replace(readiness.PROOF, original_proof, proof_metadata.st_gid,
                               mode=proof_metadata.st_mode & 0o777)
                readiness.validate_runtime_proof()
            start_services(helper, backup)
            print('Original code and acceptance record restored; Andrew startup verified.', flush=True)
        raise
    print('Installed; Andrew startup checks passed. Error state and held messages are preserved. '
          'No wake, replay, model call, or billing reconciliation was requested.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit('Patch stopped: ' + str(error)) from None
