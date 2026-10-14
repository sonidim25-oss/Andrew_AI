"""External capabilities require an administrator-owned acceptance record."""
import hashlib
import json
import os
from pathlib import Path

PROOF = Path('/etc/andrew/runtime-proof.json')
POLICY_FILES = [Path('/etc/andrew/runner.conf'),Path('/etc/andrew/egress.conf'),
                Path('/etc/nftables.d/andrew-runtime.nft'),
                Path('/etc/andrew/runner-environment.conf'),Path('/etc/andrew/browser-environment.conf'),
                Path('/etc/systemd/system/andrew-runtime-firewall.service')]
POLICY_FILES += [Path('/etc/systemd/system')/('andrew-'+name+suffix)
                 for name in ('runner','browser','egress') for suffix in ('.service','.socket')]
CHECKS = frozenset({'network','limits','browser','accounts','letta'})

def code_digest(directory):
    digest=hashlib.sha256()
    for path in sorted(Path(directory).glob('*.py')):
        if path.is_symlink(): raise ValueError('symlink in runtime code')
        digest.update(path.name.encode()+b'\0'+path.read_bytes()+b'\0')
    return digest.hexdigest()

def protected_read(path):
    path=Path(path)
    for parent in [path,*path.parents]:
        info=parent.lstat()
        if parent.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('runtime proof inputs must be administrator-owned')
    if path.stat().st_size > 65536: raise ValueError('oversized proof input')
    return path.read_bytes()

def bindings():
    policy=hashlib.sha256()
    for path in POLICY_FILES:
        policy.update(str(path).encode()+b'\0'+protected_read(path)+b'\0')
    config=dict(line.split('=',1) for line in protected_read(POLICY_FILES[0]).decode().splitlines() if '=' in line and not line.startswith('#'))
    return {'code':code_digest(Path('/opt/andrew/prototype/andrewbot')),
            'service_code':code_digest(Path('/opt/andrew/runtime-services/andrewbot')),
            'image':config['IMAGE'],'policy':policy.hexdigest()}

def check_proof(proof, expected):
    if not isinstance(proof,dict) or proof.get('schema') != 1:
        raise ValueError('external runtime acceptance proof missing')
    if any(proof.get(k)!=v for k,v in expected.items()):
        raise ValueError('external runtime acceptance proof is stale')
    if proof.get('checks') != {k:'passed' for k in CHECKS}:
        raise ValueError('external runtime acceptance proof is incomplete')

def validate_runtime_proof():
    try: check_proof(json.loads(protected_read(PROOF)),bindings())
    except (OSError,KeyError,ValueError):
        raise ValueError('external runtime requires current administrator acceptance evidence') from None
