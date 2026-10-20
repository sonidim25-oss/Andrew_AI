"""Administrator-owned Gmail permissions, independent of Google's broad scopes."""
import json
from pathlib import Path

from .runtime_readiness import protected_read

POLICY_PATH = Path('/etc/andrew/gmail-permissions.json')
SCOPES = {
    'read': 'https://www.googleapis.com/auth/gmail.readonly',
    'draft': 'https://www.googleapis.com/auth/gmail.compose',
    'send': 'https://www.googleapis.com/auth/gmail.send',
    'delete': 'https://www.googleapis.com/auth/gmail.modify',
}
DEFAULT_POLICY = {'schema': 1, 'enabled': False, 'read': True,
                  'draft': False, 'send': False, 'delete': False}


def load_permissions():
    if not POLICY_PATH.exists():
        return dict(DEFAULT_POLICY)
    try:
        value = json.loads(protected_read(POLICY_PATH))
        if (not isinstance(value, dict) or set(value) != set(DEFAULT_POLICY)
                or type(value['schema']) is not int or value['schema'] != 1
                or any(type(value[k]) is not bool for k in DEFAULT_POLICY if k != 'schema')):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError):
        raise ValueError('Gmail permission configuration is invalid') from None


def requested_scopes(policy):
    # Reading is part of every connection; mutations remain individually gated.
    return {SCOPES['read']} | {scope for name, scope in SCOPES.items() if policy[name]}


def scope_allows(scopes, permission):
    # compose and modify confer more rights at Google than Andrew's local gates.
    accepted = {
        'read': {SCOPES['read'], SCOPES['delete']},
        'draft': {SCOPES['draft'], SCOPES['delete']},
        'send': {SCOPES['send'], SCOPES['draft'], SCOPES['delete']},
        'delete': {SCOPES['delete']},
    }
    return bool(set(scopes) & accepted[permission])
