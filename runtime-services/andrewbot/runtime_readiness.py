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
POLICY_FILES += [Path('/etc/systemd/system')/('andrew-'+role+'.service.d/50-rootless-runtime.conf')
                 for role in ('runner','browser')]
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
    if RUNNER_ADOPTION.exists():
        runner_adoption()
        policy.update(str(RUNNER_ADOPTION).encode()+b'\0'+protected_read(RUNNER_ADOPTION)+b'\0')
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


RUNNER_ADOPTION = Path('/etc/andrew/runner-receipt-adoption.json')


def runner_adoption(*, controller_directory=Path('/opt/andrew/prototype/andrewbot'),
                    service_directory=Path('/opt/andrew/runtime-services/andrewbot')):
    """An adopted store cannot start under an older or partial code rollout."""
    if not RUNNER_ADOPTION.exists():
        if RUNNER_ADOPTION.is_symlink():raise ValueError('Invalid runner adoption record')
        return None
    record=json.loads(protected_read(RUNNER_ADOPTION))
    if (record.get('contract')!='runner_result_recovery_v1' or record.get('version')!=1
            or record.get('mode') not in {'disabled','enabled'}
            or controller_directory is not None and record.get('controller_code')!=code_digest(controller_directory)
            or service_directory is not None and record.get('service_code')!=code_digest(service_directory)):
        raise ValueError('Runner adoption requires its exact coordinated packages')
    for key in ('controller_store_id','runner_store_id'):
        import uuid
        if str(uuid.UUID(record[key])).replace('-','')!=record[key].replace('-',''):
            raise ValueError('Invalid runner adoption identity')
    for field in ('key_fingerprint','backend_release_sha256'):
        if not isinstance(record.get(field),str) or len(record[field])!=64 or any(c not in '0123456789abcdef' for c in record[field]):
            raise ValueError('Invalid runner adoption digest')
    for key in ('controller_uid','runner_uid'):
        if type(record.get(key)) is not int or record[key]<0:raise ValueError('Invalid runner adoption UID')
    return record
