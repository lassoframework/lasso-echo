"""Verified client Slack routes for the media bridge.

A Slack-shaped ID is never enough evidence to contact a client.  A route is usable
only when a real, non-test ``support_tickets`` row ties that exact conversation to
the resolved Echo tenant, and no other tenant has used that conversation.  This
module performs reads only and deliberately has no Slack client.
"""
from dataclasses import dataclass
import json
import re

from . import config, echo_clients, portal_gyms

_CHANNEL_RE = re.compile(r"^[CG][A-Z0-9]+$")
_PAGE = 1000
_MAX_PAGES = 20
_TIMEOUT = 15


@dataclass(frozen=True)
class Route:
    """A verified destination, or a reason it could not be verified."""
    channel: str = ""
    reason: str = ""
    gym_id: str = ""

    @property
    def ok(self):
        return bool(self.channel)


def _channel(value):
    value = str(value or "").strip()
    return value if _CHANNEL_RE.fullmatch(value) else ""


def _tenant_id(base, clients):
    key = echo_clients.normalize_key(base)
    if not key or not clients.ok or not clients.is_client(key):
        return ""
    if key in clients.gym_ids:
        return key
    return clients.key_to_gym.get(key, "")


def _is_test(row):
    return row.get("is_test") is True


def verify_rows(base, clients, gym_rows, ticket_rows, *, revoked=False,
                ops_channel=None):
    """Pure route proof used by the live reader and offline tests.

    ``ticket_rows`` must contain only the fields read from support_tickets.  A
    duplicate channel across client IDs is treated as a shared route and refused.
    """
    if revoked:
        return Route(reason="account revoked")
    gym_id = _tenant_id(base, clients)
    if not gym_id:
        return Route(reason="tenant not verified")
    matching_gyms = [r for r in (gym_rows or [])
                     if echo_clients.normalize_key(r.get("id")) == gym_id]
    if len(matching_gyms) != 1 or portal_gyms.is_excluded(matching_gyms[0]):
        return Route(reason="tenant inactive", gym_id=gym_id)

    tenants_by_channel = {}
    for row in ticket_rows or []:
        if _is_test(row):
            continue
        channel = _channel(row.get("slack_channel_id"))
        tenant = echo_clients.normalize_key(row.get("client_id"))
        if channel and tenant:
            tenants_by_channel.setdefault(channel, set()).add(tenant)

    candidates = [channel for channel, tenants in tenants_by_channel.items()
                  if tenants == {gym_id}]
    if not candidates:
        return Route(reason="no tenant-bound client channel", gym_id=gym_id)
    # A tenant having multiple historic destinations needs an explicit human
    # resolution. Selecting the most recent ticket would make recency an unsafe
    # identity rule.
    if len(candidates) != 1:
        return Route(reason="ambiguous client channels", gym_id=gym_id)
    channel = candidates[0]
    if channel == _channel(ops_channel or config.SLACK_CHANNEL_ID):
        return Route(reason="ops channel refused", gym_id=gym_id)
    return Route(channel=channel, gym_id=gym_id)


class RouteReader:
    """Read-only PostgREST loader.  Credentials are lazy and never logged."""

    def __init__(self, url=None, service_key=None, http=None):
        self._url = url if url is not None else config.supabase_url()
        self._key = service_key if service_key is not None else config.supabase_service_key()
        self._http = http

    def available(self):
        return bool(self._url and self._key)

    def _client(self):
        if self._http is not None:
            return self._http
        import requests
        return requests

    def _read_all(self, table, select):
        if not self.available():
            return None
        rows = []
        headers = {"apikey": self._key, "Authorization": f"Bearer {self._key}",
                   "Accept": "application/json"}
        for page in range(_MAX_PAGES):
            try:
                response = self._client().get(
                    f"{self._url}/rest/v1/{table}",
                    params={"select": select, "limit": str(_PAGE),
                            "offset": str(page * _PAGE)}, headers=headers,
                    timeout=_TIMEOUT)
                if getattr(response, "status_code", 599) >= 400:
                    return None
                batch = response.json()
            except Exception:  # network and malformed responses are unknown, never empty
                return None
            if not isinstance(batch, list):
                return None
            rows.extend(batch)
            if len(batch) < _PAGE:
                return rows
        return None

    def resolve(self, base, *, clients=None, revoked=False):
        """Return a verified route or fail closed without contacting Slack."""
        if revoked:
            return Route(reason="account revoked")
        try:
            clients = clients or echo_clients.snapshot(http=self._http)
            gym_id = _tenant_id(base, clients)
            if not gym_id:
                return Route(reason="tenant not verified")
            gyms = self._read_all("gyms", "id,status,is_demo,load_test,is_verification")
            tickets = self._read_all("support_tickets",
                                     "client_id,slack_channel_id,is_test")
            if gyms is None or tickets is None:
                return Route(reason="route evidence unavailable", gym_id=gym_id)
            return verify_rows(base, clients, gyms, tickets, revoked=False)
        except Exception:
            return Route(reason="route evidence unavailable")


def resolve_client_route(base, *, reader=None, clients=None, r2=None):
    """Live resolver for ``media_bridge``.  Returns a Route, never raises.

    Callers must use ``.channel`` only when ``.ok`` is true; ``account.slack_channel``
    and environment mappings are intentionally not consulted here.
    """
    try:
        from .intake_web import _default_r2, _DENYLIST_KEY
        reader_r2 = r2 if r2 is not None else _default_r2()
        if reader_r2 is None:
            return Route(reason="revocation unavailable")
        raw = reader_r2.get_bytes(_DENYLIST_KEY)
        data = json.loads(raw) if raw else {"revoked": []}
        if not isinstance(data, dict) or not isinstance(data.get("revoked"), list):
            return Route(reason="revocation unavailable")
        revoked = base.strip().lower() in {
            str(key).strip().lower() for key in data["revoked"]}
    except Exception:
        # A failed denylist read is unsafe for an outbound client alert.
        return Route(reason="revocation unavailable")
    return (reader or RouteReader()).resolve(base, clients=clients, revoked=revoked)
