"""
Stripe client: READ-ONLY customer + subscription lookups for the welcome-post
pipeline (agent/welcome_new_clients.py). Two calls only:

  list_new_customers(since_ts)   -> new customers created after since_ts
  subscription_status(customer)  -> the customer's current subscription status

Never touches billing, pixel, or CAPI configuration; never writes anything to
Stripe. The API key is read lazily by NAME (config.STRIPE_API_KEY_ENV), never
logged, never stored on an object, exactly like every other credential in this
codebase (see accounts.py Account.get_token).

Uses stdlib urllib (zero extra dependency), same convention as slack_surface's
_requests() adapter. The client is injectable so tests never hit the network.
"""

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import config


@dataclass
class StripeCustomer:
    id: str
    email: str = ""
    name: str = ""              # Stripe customer.name; often the business name at checkout
    created: int = 0            # unix ts
    metadata: dict = field(default_factory=dict)
    subscription_status: str = ""  # "" = unknown / no subscription found


class _UrllibStripeClient:
    """Minimal Stripe REST client over stdlib urllib. Basic auth: API key as
    the username, empty password (Stripe's documented scheme)."""

    def __init__(self, api_key, api_base=None, timeout=30):
        self._api_key = api_key
        self._api_base = api_base or config.STRIPE_API_BASE
        self._timeout = timeout

    def _get(self, path, params=None):
        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        req = urllib.request.Request(f"{self._api_base}{path}{qs}", method="GET")
        auth = base64.b64encode(f"{self._api_key}:".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read())

    def list_customers(self, created_gte=None, limit=100, starting_after=None):
        params = {"limit": limit}
        if created_gte is not None:
            params["created[gte]"] = int(created_gte)
        if starting_after:
            params["starting_after"] = starting_after
        return self._get("/customers", params)

    def list_subscriptions(self, customer_id, limit=10):
        return self._get("/subscriptions", {"customer": customer_id, "limit": limit})


def default_client():
    """The real Stripe client, or None when the key is not set (fail-quiet, same
    pattern as media_host._default_client). Never raises on a missing key."""
    api_key = os.environ.get(config.STRIPE_API_KEY_ENV)
    if not api_key:
        return None
    return _UrllibStripeClient(api_key)


def _to_customer(raw):
    return StripeCustomer(
        id=raw.get("id", ""),
        email=raw.get("email") or "",
        name=raw.get("name") or "",
        created=int(raw.get("created") or 0),
        metadata=raw.get("metadata") or {},
    )


def list_new_customers(since_ts, client=None, max_pages=20):
    """All Stripe customers created at or after since_ts (unix seconds), oldest
    Stripe page order preserved, paginated. Returns [] (never raises) when there
    is no client (key not set) or the call fails — the caller reports that as
    UNAVAILABLE rather than an empty-but-successful backfill; see
    welcome_new_clients.run_backfill."""
    client = client or default_client()
    if client is None:
        return None
    out = []
    starting_after = None
    for _ in range(max_pages):
        try:
            page = client.list_customers(created_gte=since_ts, limit=100,
                                         starting_after=starting_after)
        except (urllib.error.URLError, ValueError, KeyError, TypeError):
            return None
        data = page.get("data", [])
        out.extend(_to_customer(r) for r in data)
        if not page.get("has_more") or not data:
            break
        starting_after = data[-1].get("id")
    return out


def subscription_status(customer_id, client=None):
    """The customer's most recent subscription status (Stripe's own vocabulary:
    active, trialing, past_due, unpaid, canceled, incomplete, incomplete_expired),
    or "" when there is no client or no subscription is found. Never raises."""
    client = client or default_client()
    if client is None:
        return ""
    try:
        page = client.list_subscriptions(customer_id, limit=10)
    except (urllib.error.URLError, ValueError, KeyError, TypeError):
        return ""
    subs = page.get("data", [])
    if not subs:
        return ""
    # most recently created subscription wins (a resubscribe after a cancel)
    subs.sort(key=lambda s: s.get("created", 0), reverse=True)
    return subs[0].get("status", "") or ""


def is_delinquent(status):
    return status in config.STRIPE_DELINQUENT_STATUSES


def is_active_paying(status):
    return status in config.STRIPE_ACTIVE_STATUSES
