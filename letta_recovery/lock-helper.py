#!/usr/bin/env python3
"""Hold one kernel execution lock until the owning Node process closes its pipe."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--path', required=True)
    args = parser.parse_args()
    path = Path(os.path.abspath(args.path))
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    descriptor = None
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('unsafe')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"error":"owned"}', flush=True)
            return 1
        print('{"state":"owned"}', flush=True)
        # stdin is a private parent pipe; EOF on parent death releases the lock.
        for line in sys.stdin:
            if line.strip() == 'release':
                break
        return 0
    except (OSError, ValueError):
        print('{"error":"unsafe"}', flush=True)
        return 1
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


if __name__ == '__main__':
    raise SystemExit(main())
