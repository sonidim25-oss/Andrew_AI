"""Interactive Desktop OAuth with PKCE and an SSH-forwarded loopback callback."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .gmail_api import (API_BASE, GmailError, VAULT_RECORD, google_request,
                        granted_scopes, token_request)
from .gmail_policy import load_permissions, requested_scopes
from .vault import Vault


def connect():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise GmailError('Gmail authorization requires an interactive SSH terminal')
    credential_dir = os.environ.get('CREDENTIALS_DIRECTORY')
    if not credential_dir:
        raise GmailError('Use the andrew-gmail administrator command')
    try:
        data = (Path(credential_dir) / 'gmail-oauth-client').read_bytes()
        if len(data) > 65536:
            raise ValueError
        client = json.loads(data)['installed']
        if any(not isinstance(client.get(k), str) or not 1 <= len(client[k]) <= 2048
               for k in ('client_id', 'client_secret')):
            raise ValueError
        if not client['client_id'].endswith('.apps.googleusercontent.com'):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise GmailError('Supply the downloaded Desktop OAuth client JSON file') from None
    policy = load_permissions()
    if not policy['enabled']:
        raise GmailError('Enable Gmail in the administrator permissions first')
    scopes = requested_scopes(policy)
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Callback URLs contain the authorization code.

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path != '/oauth2/callback' or len(self.path) > 16384:
                self.send_error(404)
                return
            try:
                fields = parse_qs(parsed.query, max_num_fields=20)
                valid = len(fields.get('state', [])) == 1 and hmac.compare_digest(fields['state'][0], state)
            except ValueError:
                valid = False
            if not valid:
                self.send_error(400, 'Invalid authorization state')
                return
            if len(fields.get('code', [])) == 1 and not fields.get('error'):
                result['code'] = fields['code'][0]
                message = b'Authorization received. Return to your SSH terminal to check the result.'
            else:
                result['denied'] = True
                message = b'Authorization was not completed. Return to your SSH terminal.'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Length', str(len(message)))
            self.end_headers()
            self.wfile.write(message)

    class CallbackServer(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(5)
            return connection, address

        def handle_error(self, request, client_address):
            pass  # Avoid raw callback diagnostics containing OAuth parameters.

    # Use the same port in the owner's local SSH -L forwarding command.
    with CallbackServer(('127.0.0.1', 8765), Callback) as server:
        server.timeout = 1
        redirect = 'http://127.0.0.1:8765/oauth2/callback'
        url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
            'client_id': client['client_id'], 'redirect_uri': redirect,
            'response_type': 'code', 'scope': ' '.join(sorted(scopes)),
            'access_type': 'offline', 'prompt': 'consent', 'include_granted_scopes': 'false',
            'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256',
        })
        print('Requested Gmail permissions: ' + ', '.join(sorted(scopes)), flush=True)
        print('Open this Google URL in your own browser while SSH port 8765 is forwarded:', flush=True)
        print(url, flush=True)
        deadline = time.monotonic() + 300
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result.get('code'):
        raise GmailError('Google authorization was denied or timed out; nothing was stored')
    response = token_request({
        'client_id': client['client_id'], 'client_secret': client['client_secret'],
        'code': result['code'], 'code_verifier': verifier,
        'redirect_uri': redirect, 'grant_type': 'authorization_code',
    })
    actual = granted_scopes(response.get('scope'))
    if actual != scopes:
        raise GmailError('Granted scopes differ from requested permissions; reconnect with exactly the requested scopes')
    for key in ('access_token', 'refresh_token'):
        if not isinstance(response.get(key), str) or not response[key]:
            raise GmailError('Google did not return offline access; revoke the old app grant and reconnect')
    if response.get('token_type', '').lower() != 'bearer':
        raise GmailError('Google returned an unsupported token type')
    try:
        lifetime = int(response['expires_in'])
        if not 0 < lifetime <= 86400:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise GmailError('Google returned an invalid token lifetime') from None
    profile = google_request(API_BASE + 'profile', headers={'Authorization': 'Bearer ' + response['access_token']})
    email = profile.get('emailAddress')
    if not isinstance(email, str) or '@' not in email or len(email) > 254:
        raise GmailError('Google did not confirm a Gmail account; nothing was stored')
    current = load_permissions()
    if not current['enabled'] or requested_scopes(current) != scopes:
        raise GmailError('Permissions changed during consent; reconnect Gmail')
    record = {key: response[key] for key in ('access_token', 'refresh_token')}
    record.update(client_id=client['client_id'], client_secret=client['client_secret'],
                  scope=' '.join(sorted(actual)), expires_at=time.time() + lifetime,
                  email=email)
    vault = Vault('/var/lib/andrew/vault')
    try:
        vault.set_secret(VAULT_RECORD, json.dumps(record))
    finally:
        vault.close()
    print('Connected Gmail account: ' + email)
    print('OAuth tokens saved in the encrypted vault. No email was sent, drafted or deleted.')


if __name__ == '__main__':
    try:
        connect()
    except GmailError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError):
        # Raw OAuth/server exceptions must never reach the terminal or journal.
        print('Gmail connection failed. Check consent, Desktop client, enabled API, requested scopes and SSH port forwarding.', file=sys.stderr)
        raise SystemExit(1)
