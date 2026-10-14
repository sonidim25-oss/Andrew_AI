import os
import sys

from .telegram import TelegramAPI
from .vault import Vault


def main():
    directory = os.environ.get("ANDREW_VAULT_DIR", "/var/lib/andrew/vault")
    vault = Vault(directory)
    try:
        token = vault.get_secret("telegram_bot")
    finally:
        vault.close()
    owner_id = int(os.environ.get("ANDREW_OWNER_ID", ""))
    telegram = TelegramAPI(token)
    try:
        telegram.get_me()
        telegram.send_message(owner_id, "Andrew’s trusted controller is online and listening. Send /status or a message to start our conversation.")
    except Exception as error:
        print(f"Telegram test failed ({type(error).__name__}).", file=sys.stderr)
        return 1
    print("Telegram test message sent to the configured owner.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
