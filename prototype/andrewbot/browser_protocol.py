"""Small typed browser protocol; never accepts JavaScript or host paths."""
import ipaddress
import json
from urllib.parse import urlsplit, urlunsplit, quote, quote_plus

MAX_WIRE = 2 * 1024 * 1024
MAX_FILE = 512 * 1024
FIELDS = {
    'open': {'url'}, 'new_tab': {'url'}, 'snapshot': set(), 'tabs': set(),
    'switch_tab': {'tab'}, 'close_tab': set(), 'click': {'selector'},
    'fill': {'selector', 'value'}, 'select': {'selector', 'value'}, 'key': {'key'},
    'scroll': {'pixels'}, 'screenshot': set(), 'reset': set(),
    'download': {'selector'}, 'upload': {'selector', 'name', 'data'},
}

def origin(url):
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None,443):
        raise ValueError('public HTTPS URL required')
    host = p.hostname.encode('idna').decode('ascii').lower().rstrip('.')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if '.' not in host or any(not s or not all(c.isalnum() or c == '-' for c in s) for s in host.split('.')):
            raise ValueError('invalid public hostname') from None
    else:
        raise ValueError('browser URLs require public hostnames')
    return 'https://' + host

def safe_url(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, '', ''))[:2048]

def validate_action(value, private=False):
    if not isinstance(value, dict) or not isinstance(value.get('action'), str):
        raise ValueError('invalid browser action')
    action = value['action']
    fields = FIELDS.get(action)
    if private and action == 'account_submit':
        fields = {'url','username','secret','username_selector','password_selector','submit_selector'}
        if 'submit_url' in value:
            fields = fields | {'submit_url'}
    if fields is None or set(value) != fields | {'action'}:
        raise ValueError('unsupported browser action or fields')
    if len(json.dumps(value).encode()) > MAX_WIRE:
        raise ValueError('browser request too large')
    for key in fields:
        v = value[key]
        if key in {'tab','pixels'}:
            if type(v) is not int or not (0 <= v < 8 if key == 'tab' else -5000 <= v <= 5000):
                raise ValueError('invalid numeric browser argument')
        elif not isinstance(v, str) or len(v.encode()) > (800000 if key == 'data' else 16000):
            raise ValueError('invalid browser text argument')
        elif key in {'selector','username_selector','password_selector','submit_selector','key','url','name','secret'} and not v:
            raise ValueError('empty browser argument')
    if 'url' in value:
        if len(value['url']) > 2048: raise ValueError('URL too long')
        origin(value['url'])
    if 'submit_url' in value and origin(value['submit_url']) != origin(value['url']):
        raise ValueError('credential endpoint must have the exact account origin')
    if 'name' in value and ('/' in value['name'] or '\\' in value['name'] or value['name'] in {'.','..'}):
        raise ValueError('invalid upload name')
    return value

def redact(value, secrets):
    if isinstance(value, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                for encoded in {secret, quote(secret, safe=''), quote_plus(secret)}:
                    value = value.replace(encoded, '[REDACTED]')
        return value
    if isinstance(value, list): return [redact(v,secrets) for v in value]
    if isinstance(value, dict): return {k:redact(v,secrets) for k,v in value.items()}
    return value
