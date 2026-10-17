"""Bounded Gmail transport. Tokens stay inside the trusted controller."""
import json
import time
import hashlib
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .gmail_policy import SCOPES

API_BASE = 'https://gmail.googleapis.com/gmail/v1/users/me/'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
REVOKE_URL = 'https://oauth2.googleapis.com/revoke'
VAULT_RECORD = 'gmail_oauth_v1'


class GmailError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def google_request(url, method='GET', body=None, headers=None, limit=2 * 1024 * 1024, timeout=20):
    if not (url.startswith(API_BASE) or url in {TOKEN_URL, REVOKE_URL}):
        raise GmailError('Google endpoint is not allowed')
    request = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise GmailError('Google response exceeds the size limit')
            result = json.loads(data) if data else {}
            if not isinstance(result, dict):
                raise GmailError('Google response is invalid')
            return result
    except HTTPError as error:
        code = error.code
        error.close()
        raise GmailError(f'Google API rejected the request (HTTP {code})') from None
    except (URLError, TimeoutError, OSError, ValueError):
        # No response body, request URL, code or OAuth token in exceptions.
        raise GmailError('Google request failed or returned an invalid response') from None


def token_request(fields, timeout=20):
    return google_request(TOKEN_URL, 'POST', urlencode(fields).encode(),
                          {'Content-Type': 'application/x-www-form-urlencoded'}, limit=65536, timeout=timeout)


def granted_scopes(value):
    if not isinstance(value, str):
        raise GmailError('Google did not confirm the granted scopes; reconnect Gmail')
    scopes = set(value.split())
    if not scopes or not scopes <= set(SCOPES.values()):
        raise GmailError('Google granted unsupported scopes; revoke this grant and reconnect')
    return scopes


class GmailAPI:
    def __init__(self, vault):
        self.vault = vault
        self.deadline = time.monotonic() + 60
        try:
            self.credentials = json.loads(vault.get_secret(VAULT_RECORD))
            for key in ('client_id', 'client_secret', 'refresh_token'):
                if not isinstance(self.credentials.get(key), str) or not self.credentials[key]:
                    raise ValueError
            self.scopes = granted_scopes(self.credentials.get('scope'))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise GmailError('Gmail is not connected; use the terminal OAuth setup') from None

    def timeout(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise GmailError('Gmail operation timed out')
        return min(20, remaining)

    def account_fingerprint(self):
        # Stable across access-token refresh; a different authorization grant
        # must never reconcile an old mailbox's action.
        return hashlib.sha256(json.dumps([self.credentials['client_id'], self.credentials['refresh_token']],
                                         separators=(',', ':')).encode()).hexdigest()

    def access_token(self):
        record = self.credentials
        try:
            fresh = float(record.get('expires_at', 0)) > time.time() + 60
        except (ValueError, TypeError):
            fresh = False
        if not fresh or not record.get('access_token'):
            response = token_request({
                'client_id': record['client_id'], 'client_secret': record['client_secret'],
                'refresh_token': record['refresh_token'], 'grant_type': 'refresh_token',
            }, timeout=self.timeout())
            token = response.get('access_token')
            if not isinstance(token, str) or not token or response.get('token_type', '').lower() != 'bearer':
                raise GmailError('Google did not return a valid access token; reconnect Gmail')
            try:
                lifetime = int(response['expires_in'])
                if not 0 < lifetime <= 86400:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise GmailError('Google returned an invalid token lifetime') from None
            # A refresh must not silently expand the stored consent grant.
            scopes = granted_scopes(response.get('scope', record['scope']))
            if scopes != self.scopes:
                raise GmailError('Google scopes changed; reconnect Gmail')
            record.update(access_token=token, expires_at=time.time() + lifetime)
            # Refreshed access tokens live only for this operation. Do not write
            # an old grant back over a concurrent disconnect or reauthorization.
        return record['access_token']

    def request(self, path, method='GET', payload=None, query=None):
        url = API_BASE + path
        if query:
            url += '?' + urlencode(query, doseq=True)
        headers = {'Authorization': 'Bearer ' + self.access_token()}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        # Never automatically retry mutations: a timeout can hide a successful send.
        return google_request(url, method, body, headers, timeout=self.timeout())
