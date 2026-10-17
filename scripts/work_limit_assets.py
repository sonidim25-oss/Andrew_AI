"""Reviewed image/config/check replacements; no controller state mutations."""
import ast
import hashlib
import json
from pathlib import Path
import pwd
import re
import subprocess

CHECK_BASELINE_HASHES = frozenset({
    'f48cdee1869c651bdcfd3325b9f43170ff3604a80492762510fd6641bb5be64a',
    # verify-runtime.py at 22c80e0: commissioned unlimited-runtime check.
    'f7e07c99fce3f57c99856125781a57e773b2ee1e4f6c7792576bc1f6455891c8',
})
IMAGE_SOURCES = frozenset({'runtime/Containerfile.release', 'runtime/browser_entry.py',
                           'runtime/worker_entry.py', 'prototype/andrewbot/browser_protocol.py'})


def plan_assets(root, readiness, before, config_path=Path('/etc/andrew/runner.conf'),
                checks_path=Path('/opt/andrew/runtime-services/checks/verify-runtime.py')):
    release = json.loads((root / 'runtime/browser-release.json').read_text())
    if (set(release['sources']) != IMAGE_SOURCES
            or not re.fullmatch(r'localhost/[a-z0-9-]+@sha256:[0-9a-f]{64}', release['image'])
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', release['image_config_id'])
            or not re.fullmatch(r'[0-9a-f]{64}', release['archive_sha256'])):
        raise RuntimeError('Invalid reviewed image release metadata')
    for name, digest in release['sources'].items():
        path = root / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError('Image source differs from reviewed release: ' + name)
    original_config = readiness.protected_read(config_path)
    config = dict(line.split('=', 1) for line in original_config.decode().splitlines()
                  if '=' in line and not line.startswith('#'))
    if config.get('IMAGE') != before['image']:
        raise RuntimeError('Installed image binding changed')
    new_config, count = re.subn(rb'^IMAGE=[^\r\n]*', ('IMAGE=' + release['image']).encode(),
                               original_config, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError('Ambiguous runner image configuration')
    original_check = readiness.protected_read(checks_path)
    new_check = (root / 'scripts/verify-runtime.py').read_bytes()
    ast.parse(new_check)
    if (hashlib.sha256(original_check).hexdigest() not in CHECK_BASELINE_HASHES
            and original_check != new_check):
        raise RuntimeError('Installed acceptance check differs from reviewed versions')
    files = [{'path': config_path, 'original': original_config, 'tested': new_config, 'backup': 'runner.conf'},
             {'path': checks_path, 'original': original_check, 'tested': new_check, 'backup': 'verify-runtime.py'}]
    files = [item for item in files if item['original'] != item['tested']]
    if not files:
        return None
    digest = hashlib.sha256()
    for path in readiness.POLICY_FILES:
        digest.update(str(path).encode() + b'\0' +
                      (new_config if Path(path) == config_path else readiness.protected_read(path)) + b'\0')
    return {'files': files, 'bindings': {'image': release['image'], 'policy': digest.hexdigest()},
            'release': release}


def podman_prefix(user):
    uid = pwd.getpwnam(user).pw_uid
    return ['runuser', '-u', user, '--', 'env', 'XDG_RUNTIME_DIR=/run/user/' + str(uid),
            'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/' + str(uid) + '/bus', 'podman']


def prepare_images(plan):
    """Root opens the private archive; service identities receive only its stdin."""
    release = plan['release']
    path = Path(release['archive_path'])
    if path.is_symlink() or not path.is_absolute():
        raise RuntimeError('Invalid image archive path')
    with path.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != release['archive_sha256']:
            raise RuntimeError('Image archive differs from reviewed release')
        for user in ('andrew-runtime', 'andrew-browser'):
            prefix = podman_prefix(user)
            stream.seek(0)
            subprocess.run(prefix + ['load'], stdin=stream, check=True, capture_output=True,
                           text=True, timeout=300, cwd='/')
            result = subprocess.run(prefix + ['image', 'inspect', release['image']], check=True,
                                    capture_output=True, text=True, timeout=30, cwd='/')
            image = json.loads(result.stdout)[0]
            if (image.get('Id') not in {release['image_config_id'], release['image_config_id'].removeprefix('sha256:')}
                    or image.get('Digest') != release['image'].split('@', 1)[1]):
                raise RuntimeError('Loaded image identity differs from reviewed release')
