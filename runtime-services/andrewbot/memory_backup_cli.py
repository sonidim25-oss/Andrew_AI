import argparse
import re
import sys

from .main import data_directory, systemd_credential_bytes
from .memory_backup import create_encrypted_snapshot, restore_snapshot
from .store import Store


_SNAPSHOT_NAME = re.compile(r"^memory-\d{8}T\d{6}\.\d{6}Z\.andrewdb$")


def _parser():
    parser = argparse.ArgumentParser(prog="andrew-memory-backup")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("snapshot", help="create an encrypted local database snapshot")
    restore = commands.add_parser("restore", help="restore a named encrypted snapshot")
    restore.add_argument("snapshot_filename")
    return parser


def main(argv=None):
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.action == "restore":
        if (arguments.snapshot_filename != arguments.snapshot_filename.split("/")[-1]
                or not _SNAPSHOT_NAME.fullmatch(arguments.snapshot_filename)):
            parser.error("restore accepts only a generated snapshot filename")

    root = data_directory()
    database_path = root / "controller.sqlite3"
    if database_path.is_symlink():
        print("Controller database is unavailable.", file=sys.stderr)
        return 2
    if arguments.action == "snapshot" and not database_path.is_file():
        print("Controller database is unavailable.", file=sys.stderr)
        return 2
    if database_path.exists() and not database_path.is_file():
        print("Controller database is unavailable.", file=sys.stderr)
        return 2
    try:
        key = systemd_credential_bytes("andrew-memory-backup-key")
        backup_directory = root / "backups"
        if arguments.action == "snapshot":
            store = Store(database_path)
            try:
                create_encrypted_snapshot(store, backup_directory, key)
            finally:
                store.close()
            print("Encrypted memory snapshot created.")
            return 0

        source = backup_directory / arguments.snapshot_filename
        if source.is_symlink() or not source.is_file():
            print("Requested memory snapshot is unavailable.", file=sys.stderr)
            return 2
        restore_snapshot(source, database_path, key)
        print("Memory database restored from encrypted snapshot.")
        return 0
    except Exception as error:
        print(f"Memory backup operation failed ({type(error).__name__}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
