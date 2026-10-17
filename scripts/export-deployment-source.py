#!/usr/bin/env python3
"""Export deployed Python and skill source for offline merge review; no state files."""
from pathlib import Path
import sys
import tarfile


def main():
    root = Path('/opt/andrew')
    paths = sorted(path for pattern in ('prototype/andrewbot/*.py', 'skills/*/SKILL.md')
                   for path in root.glob(pattern))
    if not paths:
        raise SystemExit('No deployed source files found')
    for path in paths:
        if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
            raise SystemExit('Refusing unexpected source path')
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
        for path in paths:
            archive.add(path, arcname=str(path.relative_to(root)), recursive=False)


if __name__ == '__main__':
    main()
