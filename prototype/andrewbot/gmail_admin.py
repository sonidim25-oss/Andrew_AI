"""Local owner-only configuration and OAuth maintenance; never a model tool."""
import json
import os
import sys
import tempfile
from urllib.parse import urlencode

from .gmail_api import GmailAPI, GmailError, REVOKE_URL, VAULT_RECORD, google_request
from .gmail_policy import POLICY_PATH, load_permissions
from .vault import Vault


def write_policy(policy):
    if os.geteuid() != 0:
        raise GmailError('Gmail permission changes require administrator access')
    # The installation already provides this root-owned directory.
    parent = POLICY_PATH.parent
    if parent.is_symlink() or parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022:
        raise GmailError('Gmail configuration directory must be administrator-owned')
    fd, temporary = tempfile.mkstemp(prefix='.gmail-permissions-', dir=parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(policy, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, POLICY_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(args):
    if args and args[0] == 'permissions':
        if os.geteuid() != 0:
            raise GmailError('Use sudo for permission management')
        policy = load_permissions()
        if len(args) == 3 and args[1] in {'enabled', 'read', 'draft', 'send', 'delete'} and args[2] in {'on', 'off'}:
            policy[args[1]] = args[2] == 'on'
            write_policy(policy)
        elif len(args) != 1:
            raise GmailError('Use permissions [enabled|read|draft|send|delete on|off]')
        print(json.dumps(policy, indent=2))
        print('Enabling an operation also requires matching OAuth consent. Use connect again to request new scopes.')
        return
    if args == ['prepare-connect']:
        policy = load_permissions()
        policy['enabled'] = True
        write_policy(policy)
        return
    if args not in (['status'], ['disconnect'], ['verify-read']):
        raise GmailError('Unknown Gmail administrator action')
    vault = Vault('/var/lib/andrew/vault')
    try:
        if VAULT_RECORD not in vault.names():
            print('Gmail is not connected.')
            return
        if args == ['disconnect']:
            try:
                record = json.loads(vault.get_secret(VAULT_RECORD))
                google_request(REVOKE_URL, 'POST', urlencode({'token': record['refresh_token']}).encode(),
                               {'Content-Type': 'application/x-www-form-urlencoded'}, limit=65536)
                print('Google token revocation confirmed.')
            except Exception:
                print('Remote revocation could not be confirmed. Remove Andrew Gmail in your Google account connections.')
            finally:
                vault.revoke(VAULT_RECORD)
            print('Local Gmail tokens removed.')
            return
        api = GmailAPI(vault)
        if args == ['verify-read']:
            policy = load_permissions()
            if not policy['enabled'] or not policy['read']:
                raise GmailError('Gmail reading is disabled')
            result = api.request('messages', query={'maxResults': 1})
            print(json.dumps({'gmail_read': 'passed', 'returned_messages': len(result.get('messages', [])),
                              'mutations': 'not performed'}))
        else:
            print(json.dumps({'connected': True, 'permissions': load_permissions(),
                              'granted_scopes': sorted(api.scopes)}))
    finally:
        vault.close()


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except GmailError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError):
        print('Gmail administration failed; check the command, configuration and OAuth connection.', file=sys.stderr)
        raise SystemExit(1)
