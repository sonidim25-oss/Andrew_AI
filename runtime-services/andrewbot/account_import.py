"""Interactive SSH credential import; never through chat, argv or environment."""
import getpass
import sys
from .browser_protocol import origin
from .vault import Vault

def main():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit('Use an interactive SSH terminal.')
    url=input('Site HTTPS URL: ').strip()
    expected=origin(url)
    username=getpass.getpass('Site username/email (hidden): ')
    secret=getpass.getpass('Site password (hidden): ')
    confirm=getpass.getpass('Confirm password (hidden): ')
    if not username or not secret or secret!=confirm: raise SystemExit('Values empty or mismatched; nothing stored.')
    vault=Vault('/var/lib/andrew/vault')
    try: account=vault.save_account(expected,username,secret)
    finally: vault.close()
    print('Stored exact-origin account ID: '+account)

if __name__=='__main__': main()
