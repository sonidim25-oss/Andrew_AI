import argparse
import os
from pathlib import Path

from .vault import Vault, prompt_for_secret


def default_directory():
    return Path(os.environ.get("ANDREW_VAULT_DIR", Path(os.environ.get("ANDREW_DATA_DIR", Path.home() / ".local/state/andrew")) / "vault"))


def main():
    parser = argparse.ArgumentParser(description="Manage Andrew's encrypted credential vault")
    parser.add_argument("action", choices=("put", "list", "revoke"))
    parser.add_argument("name", nargs="?")
    parser.add_argument("--directory", type=Path, default=default_directory())
    args = parser.parse_args()
    if args.action == "put":
        if args.name not in {"telegram_bot", "openrouter_api", "openrouter_credits", "x_username", "x_password"}:
            parser.error("unsupported credential name")
        prompt_for_secret(args.name, args.directory)
        return
    vault = Vault(args.directory)
    try:
        if args.action == "list":
            for name in vault.names():
                print(name)
        elif args.action == "revoke":
            if not args.name:
                parser.error("revoke requires a credential name")
            vault.revoke(args.name)
            print(f"Revoked {args.name}.")
    finally:
        vault.close()


if __name__ == "__main__":
    main()
