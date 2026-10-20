#!/usr/bin/env python3
"""One-time encrypted credential; never replace a key protecting receipt history."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile

NAME='andrew-runner-receipts-key'


def decrypt(path):
    result=subprocess.run(['systemd-creds','decrypt','--newline=no','--name='+NAME,str(path),'-'],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if len(result.stdout)!=32:raise ValueError('Invalid runner receipt credential')
    return result.stdout


def provision(credential_dir,runner_root):
    credential_dir,runner_root=Path(credential_dir),Path(runner_root)
    if credential_dir.is_symlink() or runner_root.is_symlink():raise ValueError('Unsafe credential path')
    credential_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
    target=credential_dir/NAME
    if target.is_symlink():raise ValueError('Unsafe runner credential')
    if target.exists():
        if not target.is_file() or target.stat().st_nlink!=1 or target.stat().st_mode&0o077:raise ValueError('Unsafe runner credential permissions')
        key=decrypt(target)
    else:
        if runner_root.exists() and any(runner_root.iterdir()):raise ValueError('Receipt history exists; recover the original credential')
        key=secrets.token_bytes(32)
        with tempfile.TemporaryDirectory(prefix='.runner-key-',dir=credential_dir) as directory:
            staged=Path(directory)/NAME
            subprocess.run(['systemd-creds','encrypt','--with-key=host','--name='+NAME,'-',str(staged)],input=key,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            staged.chmod(0o600)
            if decrypt(staged)!=key:raise ValueError('Credential verification failed')
            with staged.open('rb') as stream:os.fsync(stream.fileno())
            os.link(staged,target)
        fd=os.open(credential_dir,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    fingerprint=hashlib.sha256(key).hexdigest()
    if (runner_root/'identity.json').exists():
        if json.loads((runner_root/'identity.json').read_text()).get('key_fingerprint')!=fingerprint:
            raise ValueError('Receipt credential differs from stored identity')
    return dict(verified=True,key_fingerprint=fingerprint)


if __name__=='__main__':
    if os.geteuid()!=0:raise SystemExit('Run as root; credentials are never printed')
    os.umask(0o077)
    try:print(json.dumps(provision('/etc/credstore.encrypted','/var/lib/andrew/runtime/runner-receipts')))
    except Exception as error:raise SystemExit('Runner credential provisioning refused ('+type(error).__name__+')')
