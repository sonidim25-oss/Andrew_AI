import getpass
import os
import sqlite3
import sys
from pathlib import Path
from secrets import token_bytes
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .site_policy import canonical_origin


class Vault:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        configured_key = os.environ.get("ANDREW_VAULT_KEY_FILE")
        credential_directory = os.environ.get("CREDENTIALS_DIRECTORY")
        credential_key = Path(credential_directory) / "andrew-vault-key" if credential_directory else None
        self.key_path = Path(configured_key) if configured_key else (
            credential_key if credential_key else self.directory / "vault.key"
        )
        self.db_path = self.directory / "credentials.sqlite3"
        if not self.key_path.exists() and configured_key:
            raise FileNotFoundError("configured vault key is unavailable")
        if not self.key_path.exists() and credential_key and self.key_path == credential_key:
            raise FileNotFoundError("systemd vault credential is unavailable")
        if self.key_path == self.directory / "vault.key" and not self.key_path.exists():
            descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as key_file:
                key_file.write(token_bytes(32))
                key_file.flush()
                os.fsync(key_file.fileno())
        if self.key_path.is_symlink():
            raise ValueError("vault key path cannot be a symlink")
        if self.key_path == self.directory / "vault.key":
            self.key_path.chmod(0o600)
        self.key = self.key_path.read_bytes()
        if len(self.key) != 32:
            raise ValueError("vault key must be 32 bytes")
        self.db = sqlite3.connect(self.db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS credentials (name TEXT PRIMARY KEY, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS site_accounts (
            account_id TEXT PRIMARY KEY, origin TEXT NOT NULL, username_nonce BLOB NOT NULL,
            username_ciphertext BLOB NOT NULL, secret_nonce BLOB NOT NULL,
            secret_ciphertext BLOB NOT NULL, created_at TEXT NOT NULL,
            revoked_at TEXT NOT NULL DEFAULT '')""")
        self.db.execute("CREATE INDEX IF NOT EXISTS site_accounts_origin ON site_accounts(origin, account_id)")
        self.db.commit()
        self.db_path.chmod(0o600)

    def set_secret(self, name, secret):
        if not name or not isinstance(secret, str) or not secret:
            raise ValueError("credential name and secret are required")
        nonce = token_bytes(12)
        ciphertext = AESGCM(self.key).encrypt(nonce, secret.encode("utf-8"), name.encode("utf-8"))
        self.db.execute(
            "INSERT INTO credentials(name,nonce,ciphertext,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(name) DO UPDATE SET nonce=excluded.nonce,ciphertext=excluded.ciphertext,updated_at=CURRENT_TIMESTAMP",
            (name, nonce, ciphertext),
        )
        self.db.commit()

    def get_secret(self, name):
        row = self.db.execute("SELECT nonce,ciphertext FROM credentials WHERE name=?", (name,)).fetchone()
        if row is None:
            raise KeyError(name)
        return AESGCM(self.key).decrypt(row[0], row[1], name.encode("utf-8")).decode("utf-8")

    def names(self):
        return [row[0] for row in self.db.execute("SELECT name FROM credentials ORDER BY name")]

    def revoke(self, name):
        self.db.execute("DELETE FROM credentials WHERE name=?", (name,))
        self.db.commit()

    @staticmethod
    def _account_aad(account_id, field):
        return f"site-account:v1:{account_id}:{field}".encode("utf-8")

    def save_account(self, origin, username, secret, account_id=None):
        if not isinstance(origin, str) or canonical_origin(origin) != origin or not isinstance(username, str) or not username or len(username) > 512 or not isinstance(secret, str) or not secret:
            raise ValueError("account fields are invalid")
        account_id = account_id or uuid4().hex
        if not isinstance(account_id, str) or not account_id or len(account_id) > 128:
            raise ValueError("account ID is invalid")
        username_nonce, secret_nonce = token_bytes(12), token_bytes(12)
        username_cipher = AESGCM(self.key).encrypt(username_nonce, username.encode("utf-8"), self._account_aad(account_id, "username"))
        secret_cipher = AESGCM(self.key).encrypt(secret_nonce, secret.encode("utf-8"), self._account_aad(account_id, "secret"))
        try:
            self.db.execute("INSERT INTO site_accounts(account_id,origin,username_nonce,username_ciphertext,secret_nonce,secret_ciphertext,created_at) VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                            (account_id, origin, username_nonce, username_cipher, secret_nonce, secret_cipher))
            self.db.commit()
        except sqlite3.IntegrityError:
            self.db.rollback()
            raise ValueError("account ID already exists") from None
        return account_id

    def account_metadata(self):
        rows = self.db.execute("SELECT account_id,origin,username_nonce,username_ciphertext,created_at,revoked_at FROM site_accounts ORDER BY created_at,account_id").fetchall()
        result = []
        for account_id, origin, nonce, ciphertext, created_at, revoked_at in rows:
            username = AESGCM(self.key).decrypt(nonce, ciphertext, self._account_aad(account_id, "username")).decode("utf-8")
            result.append({"account_id": account_id, "origin": origin, "username": username,
                           "created_at": created_at, "revoked": revoked_at != ''})
        return result

    def get_account_secret(self, account_id, origin):
        row = self.db.execute("SELECT origin,secret_nonce,secret_ciphertext,revoked_at FROM site_accounts WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            raise KeyError(account_id)
        if row[0] != origin:
            raise PermissionError("account origin does not match")
        # Only the schema's exact empty-text sentinel means active.
        if row[3] != '':
            raise PermissionError("account is revoked")
        return AESGCM(self.key).decrypt(row[1], row[2], self._account_aad(account_id, "secret")).decode("utf-8")

    def revoke_account(self, account_id):
        cursor = self.db.execute("UPDATE site_accounts SET revoked_at=CURRENT_TIMESTAMP WHERE account_id=? AND revoked_at=''", (account_id,))
        self.db.commit()
        return cursor.rowcount > 0

    def delete_account(self, account_id):
        cursor = self.db.execute("DELETE FROM site_accounts WHERE account_id=?", (account_id,))
        self.db.commit()
        return cursor.rowcount > 0

    def close(self):
        self.db.close()


def prompt_for_secret(name, directory):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("Secure credential import requires an interactive SSH terminal (TTY).")
    value = getpass.getpass(f"Enter {name} (input hidden): ")
    confirmation = getpass.getpass("Confirm (input hidden): ")
    if not value or value != confirmation:
        raise SystemExit("Values were empty or did not match; nothing was stored.")
    vault = Vault(directory)
    try:
        vault.set_secret(name, value)
    finally:
        vault.close()
    print(f"Stored {name} securely.")
