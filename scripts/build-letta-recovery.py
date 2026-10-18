#!/usr/bin/env python3
"""Build a separate, pinned local-recovery Letta release; never modify npm in place."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PIN = json.loads((ROOT / 'letta_recovery/base.json').read_text())
BASE_SHA = PIN['sha256']


def tree_hashes(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            result[path.relative_to(root).as_posix()] = 'link:' + os.readlink(path)
        elif path.is_file():
            with path.open('rb') as source:
                result[path.relative_to(root).as_posix()] = hashlib.file_digest(source, 'sha256').hexdigest()
    return result


def build_release(base_package, output):
    base_package, output = Path(base_package).resolve(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('Output must not exist')
    if output.is_relative_to(base_package):
        raise ValueError('Output must be separate from the base package')
    source = (base_package / 'letta.js').read_bytes()
    if hashlib.sha256(source).hexdigest() != BASE_SHA:
        raise ValueError('Unexpected base hash')
    package = json.loads((base_package / 'package.json').read_text())
    if (package.get('name'), package.get('version')) != (PIN['name'], PIN['version']):
        raise ValueError('Unsupported base package')
    patches = json.loads((ROOT / 'letta_recovery/patches.json').read_text())
    proposed = source.decode()
    for patch in patches:
        if proposed.count(patch['anchor']) != 1:
            raise ValueError('Missing or ambiguous patch anchor')
        proposed = proposed.replace(patch['anchor'], patch['replacement'], 1)
    for path in base_package.rglob('*'):
        if path.is_symlink():
            if os.path.isabs(os.readlink(path)) or not path.resolve().is_relative_to(base_package):
                raise ValueError('Unsafe dependency symlink')
        elif not (path.is_dir() or stat.S_ISREG(path.stat().st_mode)):
            raise ValueError('Unsafe dependency file')
    if not (base_package / 'node_modules').is_dir():
        raise ValueError('Self-contained dependencies are required')
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='.letta-recovery-build-', dir=output.parent))
    try:
        shutil.copytree(base_package, work, dirs_exist_ok=True, symlinks=True)
        (work / 'letta.js').write_text(proposed)
        shutil.copytree(ROOT / 'letta_recovery', work / 'recovery',
                        ignore=shutil.ignore_patterns('tests', '__pycache__'))
        manifest = {'contract': PIN['contract'], 'version': 1, 'base_sha256': BASE_SHA,
                    'files': tree_hashes(work)}
        (work / 'recovery-release.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
        os.rename(work, output)
        return manifest
    finally:
        if work.exists():
            shutil.rmtree(work)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-package', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    manifest = build_release(args.base_package, args.output)
    print(json.dumps({'contract':manifest['contract'], 'files':len(manifest['files']),
                      'bundle_sha256':manifest['files']['letta.js']}))


if __name__ == '__main__':
    main()
