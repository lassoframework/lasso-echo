"""
Zernio API client + pure response mappers.

Echo brokers Zernio (the social-posting vendor); the portal NEVER sees the Zernio key. All external
HTTP lives in ZernioClient (mirrors the gbp_check/opus_ingest requests pattern: Bearer auth, 30s
timeout, scrub-on-error). The mappers (map_status, map_pages) are PURE and unit-tested against
real-shape fixtures captured from the live API, so the fold + field-rename logic is provable without
a network call.

Zernio model: Team -> Profile (tenant boundary; one per gym) -> Account (an IG/FB connection).
Field renames Echo owns (portal contract never changes): Zernio `authUrl`->`oauth_url`,
`metadata.profileData.username`->`handle`, page `_id`->`id`. Expiry is DERIVED from
`connectedAt` + `metadata.expires_in`, or an explicit `intentionalDisconnectAt` / `isActive:false`.
"""

import os
from datetime import datetime, timezone

from . import config

PLATFORMS = ("instagram", "facebook")


class ZernioError(Exception):
    def __init__(self, status, detail=""):
        self.status = status
        self.detail = detail
        super().__init__(f"zernio {status}: {detail}")


class ZernioClient:
    """Thin Zernio v1 client. `http` is injectable for tests (defaults to lazy `requests`)."""

    def __init__(self, api_key=None, base=None, http=None):
        self.api_key = api_key if api_key is not None else os.environ.get(config.ZERNIO_API_KEY_ENV, "")
        self.base = (base or config.zernio_api_base()).rstrip("/")
        self._http = http

    def _client(self):
        if self._http is not None:
            return self._http
        import requests  # lazy, matches the repo pattern
        return requests

    def _get(self, path, params=None):
        r = self._client().get(
            self.base + path,
            params=params or {},
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=30,
        )
        if r.status_code >= 400:
            raise ZernioError(r.status_code, (r.text or "")[:200])
        return r.json()

    def _post(self, path, payload):
        r = self._client().post(
            self.base + path,
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=30,
        )
        if r.status_code >= 400:
            raise ZernioError(r.status_code, (r.text or "")[:200])
        return r.json()

    # ---- reads --------------------------------------------------------------
    def connect_url(self, profile_id, platform):
        """GET /v1/connect/{platform}?profileId=... -> {authUrl}."""
        return self._get(f"/v1/connect/{platform}", {"profileId": profile_id})

    def list_accounts(self, profile_id):
        """GET /v1/accounts?profileId=... -> {accounts:[...]}."""
        return self._get("/v1/accounts", {"profileId": profile_id})

    def list_facebook_pages(self, account_id):
        """GET /v1/accounts/{id}/facebook-page -> {pages:[{_id,name}]}."""
        return self._get(f"/v1/accounts/{account_id}/facebook-page")

    # ---- writes (provisioning) ---------------------------------------------
    def create_profile(self, name):
        """POST /v1/profiles {name} -> {..._id}. Per-gym provisioning.

        NOTE: the create-profile request shape is per Zernio's documented convention and is
        UNVERIFIED against the live API (we do not create junk profiles in tests). Verify once
        before fleet provisioning; it is only reached from the connect path, never a read.
        """
        return self._post("/v1/profiles", {"name": name})


# ---------------------------------------------------------------------------
# PURE mappers — no I/O. Provable against real-shape fixtures.
# ---------------------------------------------------------------------------

def _parse_iso(s):
    """Parse a Zernio ISO8601 timestamp (e.g. '2026-07-29T13:14:04.205Z') to aware UTC, or None."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def account_state(acct, now=None):
    """
    Reduce one Zernio account dict to 'connected' | 'expired' | 'not_connected'.
    'expired' == our amber "Needs Reconnect". Precedence: an explicit disconnect or inactive flag
    wins; then a computed token expiry (connectedAt + expires_in); else connected.
    """
    if not isinstance(acct, dict):
        return "not_connected"
    if acct.get("intentionalDisconnectAt"):
        return "expired"
    if acct.get("isActive") is False or acct.get("enabled") is False:
        return "expired"
    md = acct.get("metadata") or {}
    # Require a POSITIVE connection signal — never optimistically claim "connected" on a bare or
    # malformed payload. A real connected account carries at least one of these.
    has_signal = (
        acct.get("isActive") is True
        or bool(acct.get("connectedAt"))
        or bool(md.get("profileData"))
    )
    if not has_signal:
        return "not_connected"
    exp = md.get("expires_in")
    connected_at = _parse_iso(acct.get("connectedAt") or md.get("connectedAt"))
    if isinstance(exp, (int, float)) and connected_at is not None:
        now = now or datetime.now(timezone.utc)
        if (now - connected_at).total_seconds() > float(exp):
            return "expired"
    return "connected"


def _handle_of(acct):
    md = acct.get("metadata") or {}
    pd = md.get("profileData") or {}
    h = pd.get("username") or acct.get("displayName")
    return str(h) if h else None


def map_status(accounts_json, now=None):
    """
    Fold Zernio's flat `accounts[]` into the portal's per-platform shape:
      {platforms: {instagram: {connected, handle, expired}, facebook: {...}}}
    Missing platform -> not connected, no handle (never fabricated). When more than one account of a
    platform exists, a connected one wins over an expired one.
    """
    out = {p: {"connected": False, "handle": None, "expired": False} for p in PLATFORMS}
    for acct in (accounts_json or {}).get("accounts") or []:
        if not isinstance(acct, dict):
            continue
        plat = acct.get("platform")
        if plat not in PLATFORMS:
            continue
        state = account_state(acct, now)
        cur = out[plat]
        # A connected account beats an already-recorded expired/none one.
        if cur["connected"]:
            continue
        if state == "connected":
            out[plat] = {"connected": True, "handle": _handle_of(acct), "expired": False}
        elif state == "expired" and not cur["expired"]:
            out[plat] = {"connected": False, "handle": _handle_of(acct), "expired": True}
    return {"platforms": out}


def map_pages(pages_json):
    """Zernio {pages:[{_id,name}]} -> portal {pages:[{id,name}]}. Drops entries missing id or name."""
    out = []
    for p in (pages_json or {}).get("pages") or []:
        if not isinstance(p, dict):
            continue
        pid = p.get("_id") or p.get("id")
        name = p.get("name")
        if pid and name:
            out.append({"id": str(pid), "name": str(name)})
    return {"pages": out}


def facebook_account_id(accounts_json):
    """The Zernio account _id of the connected Facebook account, or None."""
    for acct in (accounts_json or {}).get("accounts") or []:
        if acct.get("platform") == "facebook" and acct.get("_id"):
            return str(acct["_id"])
    return None
