import tempfile
import unittest
import os
from pathlib import Path

from andrewbot.vault import Vault


class VaultTests(unittest.TestCase):
    def test_secret_round_trips_without_plaintext_in_database(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Vault(Path(directory) / "vault")
            secret = "synthetic-test-secret-do-not-log"
            vault.set_secret("telegram_bot", secret)
            self.assertEqual(vault.get_secret("telegram_bot"), secret)
            self.assertNotIn(secret.encode(), vault.db_path.read_bytes())
            self.assertEqual(vault.key_path.stat().st_mode & 0o777, 0o600)

    def test_revoke_removes_record(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Vault(Path(directory) / "vault")
            vault.set_secret("telegram_bot", "synthetic")
            vault.revoke("telegram_bot")
            self.assertEqual(vault.names(), [])

    def test_systemd_credential_key_is_used_without_local_plaintext_key(self):
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "credentials"
            credentials.mkdir()
            (credentials / "andrew-vault-key").write_bytes(b"k" * 32)
            vault_dir = Path(directory) / "vault"
            previous = os.environ.get("CREDENTIALS_DIRECTORY")
            os.environ["CREDENTIALS_DIRECTORY"] = str(credentials)
            try:
                vault = Vault(vault_dir)
                vault.set_secret("test", "secret")
                self.assertEqual(vault.get_secret("test"), "secret")
                self.assertFalse((vault_dir / "vault.key").exists())
                vault.close()
            finally:
                if previous is None:
                    os.environ.pop("CREDENTIALS_DIRECTORY", None)
                else:
                    os.environ["CREDENTIALS_DIRECTORY"] = previous

    def test_site_account_secrets_are_encrypted_and_origin_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Vault(Path(directory) / "vault")
            secret = "synthetic-account-password"
            account_id = vault.save_account("https://example.com", "andrew", secret)
            self.assertEqual(vault.get_account_secret(account_id, "https://example.com"), secret)
            with self.assertRaises(PermissionError):
                vault.get_account_secret(account_id, "https://other.example")
            self.assertNotIn(secret.encode(), vault.db_path.read_bytes())
            self.assertEqual(vault.account_metadata()[0]["username"], "andrew")
            self.assertTrue(vault.revoke_account(account_id))
            with self.assertRaises(PermissionError):
                vault.get_account_secret(account_id, "https://example.com")
            self.assertTrue(vault.delete_account(account_id))


if __name__ == "__main__":
    unittest.main()
