"""Exact-origin approval policy for narrowly scoped browser actions."""

from datetime import datetime, timezone
import ipaddress
from urllib.parse import urlsplit


def canonical_origin(url, allowed_ports=()):
    if not isinstance(url, str):
        raise ValueError("site origin is invalid")
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if not hostname or ":" in hostname or ".." in hostname:
            raise ValueError
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ValueError
        port = parsed.port
        if port is not None and port != 443 and port not in allowed_ports:
            raise ValueError
        if port == 443:
            port = None
        netloc = hostname if port is None else f"{hostname}:{port}"
        return f"https://{netloc}"
    except (UnicodeError, ValueError):
        raise ValueError("site origin is invalid") from None


class SitePolicy:
    ACTIONS = frozenset({"read", "signup", "login"})

    def __init__(self):
        self._approved = {}

    def approve(self, origin, actions, reviewed_at):
        canonical = canonical_origin(origin)
        actions = frozenset(actions)
        if not actions or not actions <= self.ACTIONS:
            raise ValueError("site actions are invalid")
        if not isinstance(reviewed_at, datetime) or reviewed_at.tzinfo is None:
            raise ValueError("review timestamp must be timezone-aware")
        self._approved[canonical] = (actions, reviewed_at.astimezone(timezone.utc))

    def permits(self, origin, action):
        try:
            canonical = canonical_origin(origin)
        except ValueError:
            return False
        approval = self._approved.get(canonical)
        return action in self.ACTIONS and approval is not None and action in approval[0]
