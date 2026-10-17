#!/usr/bin/env python3
"""Install controller modules and version-bound skill documents as one package."""
import argparse
import ast
import base64
import grp
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from skill_assets import install_skill_assets, plan_skill_assets


def safe_path(path):
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError('Symlink in controller package destination')


def ensure_directory(path, gid):
    safe_path(path)
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o750)
        os.chown(directory, 0, gid)


def replace_file(path, data, gid, mode=0o640, uid=0):
    path = Path(path)
    safe_path(path)
    ensure_directory(path.parent, gid)
    descriptor, name = tempfile.mkstemp(prefix='.controller-package-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), uid, gid)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def snapshot(path):
    safe_path(path)
    if not path.exists():
        return {'path': str(path), 'data': None}
    stat = path.stat()
    return {'path': str(path), 'data': base64.b64encode(path.read_bytes()).decode(),
            'uid': stat.st_uid, 'gid': stat.st_gid, 'mode': stat.st_mode & 0o777}


def restore_records(records):
    for record in reversed(records):
        path = Path(record['path'])
        safe_path(path)
        if record['data'] is None:
            path.unlink(missing_ok=True)
        else:
            replace_file(path, base64.b64decode(record['data'], validate=True), record['gid'],
                         mode=record['mode'], uid=record['uid'])


def restore_backup(path):
    path = Path(path)
    safe_path(path)
    records = json.loads(path.read_text())
    if not isinstance(records, list) or any(not Path(row['path']).is_absolute() for row in records):
        raise ValueError('Invalid package backup')
    restore_records(records)


def install_package(source_root, target_package, gid, backup_file=None):
    source_root, target_package = Path(source_root).resolve(), Path(target_package).absolute()
    safe_path(target_package)
    skills_root = target_package.parents[1] / 'skills'
    skills = plan_skill_assets(source_root, skills_root)
    modules = []
    for path in sorted((source_root / 'prototype/andrewbot').glob('*.py')):
        if path.is_symlink():
            raise ValueError('Symlink in controller source')
        data = path.read_bytes()
        ast.parse(data)
        modules.append((target_package / path.name, data))
    if not modules:
        raise ValueError('Controller package is empty')
    records = [snapshot(path) for path, _ in modules]
    records.extend(snapshot(item['path']) for item in skills)
    if backup_file is not None:
        backup_file = Path(backup_file)
        safe_path(backup_file)
        with backup_file.open('xb') as backup:
            os.fchmod(backup.fileno(), 0o600)
            backup.write(json.dumps(records).encode())
            backup.flush()
            os.fsync(backup.fileno())
    try:
        for path, data in modules:
            replace_file(path, data, gid)
        install_skill_assets(skills, gid, replace_file)
        if any(path.read_bytes() != data for path, data in modules):
            raise ValueError('Controller module verification failed')
    except BaseException:
        restore_records(records)
        raise
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source_root', nargs='?')
    parser.add_argument('target_package', nargs='?')
    parser.add_argument('--group', default='andrew-control')
    parser.add_argument('--backup')
    parser.add_argument('--restore-backup')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run this package installer with sudo')
    if args.restore_backup:
        restore_backup(args.restore_backup)
        return
    if not args.source_root or not args.target_package:
        parser.error('source_root and target_package are required')
    backup = args.backup or str(Path(tempfile.mkdtemp(prefix='andrew-package-', dir='/var/backups')) / 'package.json')
    install_package(args.source_root, args.target_package, grp.getgrnam(args.group).gr_gid, backup)
    print('Controller modules and trusted skill documents verified. Backup: ' + backup)


if __name__ == '__main__':
    main()
