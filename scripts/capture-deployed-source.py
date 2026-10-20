#!/usr/bin/env python3
"""Copy only application source from /opt/andrew for a private Git reconciliation."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo to read the private application sources.')
    uid, gid = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
    if uid == 0:raise SystemExit('Invoke with sudo from the project owner account.')
    source = Path('/opt/andrew')
    target = Path('/home/daniel/andrew-deployed-project-source-20261020')
    target.mkdir(mode=0o700)
    allowed = {'.py', '.sh', '.md', '.js', '.mjs'}
    files = {}
    for path in sorted(source.rglob('*')):
        if any(part in {'__pycache__', '.git', '.venv', 'node_modules'} for part in path.relative_to(source).parts):continue
        info = path.lstat()
        if path.is_symlink():raise ValueError('Source link requires manual review: ' + str(path.relative_to(source)))
        if not stat.S_ISREG(info.st_mode) or path.suffix not in allowed:continue
        name = path.relative_to(source)
        output = target / name
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = path.read_bytes()
        output.write_bytes(data)
        output.chmod(0o600)
        files[name.as_posix()] = hashlib.sha256(data).hexdigest()
    (target / 'source-manifest.json').write_text(json.dumps(files, sort_keys=True, indent=2) + '\n')
    for path in [*target.rglob('*'), target]:
        os.chown(path, uid, gid)
    print(json.dumps(dict(captured=True, directory=str(target), source_files=len(files), production_changed=False)))


if __name__ == '__main__':main()
