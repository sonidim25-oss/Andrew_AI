#!/usr/bin/env python3
"""Provision missing controller data keys without rotating existing keys."""

import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


KEY_NAMES = ("andrew-memory-backup-key", "andrew-history-key")


def decrypted_key(path, name):
    try:
        result = subprocess.run(
            ["systemd-creds", "decrypt", "--newline=no", f"--name={name}", str(path), "-"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError:
        raise RuntimeError(f"{name}: existing credential cannot decrypt; refusing replacement") from None
    if len(result.stdout) != 32:
        raise RuntimeError(f"{name}: existing credential is not a 32-byte key")


def provision(credential_dir, state_dir):
    credential_dir, state_dir = Path(credential_dir), Path(state_dir)
    if credential_dir.is_symlink():
        raise RuntimeError("credential directory cannot be a symlink")
    credential_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    missing = []
    for name in KEY_NAMES:
        path = credential_dir / name
        if path.is_symlink():
            raise RuntimeError(f"{name}: credential cannot be a symlink")
        if path.exists():
            decrypted_key(path, name)
        else:
            missing.append(name)

    # Check all protected data before generating either key.
    if "andrew-memory-backup-key" in missing:
        if any((state_dir / "backups").glob("memory-*.andrewdb")):
            raise RuntimeError("Backup key missing while encrypted snapshots exist; recover the original key")
    history = state_dir / "browser-history/history.sqlite3"
    if "andrew-history-key" in missing and history.exists():
        try:
            with sqlite3.connect(history.resolve().as_uri() + "?mode=ro", uri=True) as db:
                rows = db.execute("SELECT count(*) FROM visits").fetchone()[0]
        except sqlite3.Error:
            raise RuntimeError("Cannot verify that browser history is empty; refusing a new key") from None
        if rows:
            raise RuntimeError("History key missing while browser history exists; recover the original key")

    for name in missing:
        with tempfile.TemporaryDirectory(prefix=".andrew-key-", dir=credential_dir) as temporary:
            encrypted = Path(temporary) / name
            try:
                subprocess.run(
                    ["systemd-creds", "encrypt", f"--name={name}", "--with-key=host", "-", str(encrypted)],
                    input=secrets.token_bytes(32), check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            except subprocess.CalledProcessError:
                raise RuntimeError(f"{name}: host-key encryption failed") from None
            encrypted.chmod(0o600)
            decrypted_key(encrypted, name)
            with encrypted.open("rb") as stream:
                os.fsync(stream.fileno())
            # An exclusive link prevents overwriting a credential created concurrently.
            os.link(encrypted, credential_dir / name)
            directory_fd = os.open(credential_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        print(f"{name}: created and verified")
    for name in KEY_NAMES:
        if name not in missing:
            print(f"{name}: existing key verified")


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("Run this helper with sudo; credentials are never printed")
    os.umask(0o077)
    try:
        provision(Path("/etc/credstore.encrypted"), Path("/var/lib/andrew/control"))
    except (RuntimeError, OSError) as error:
        sys.exit(str(error))
