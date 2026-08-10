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
from datetime import datetime, timedelta, timezone

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

    def list_profiles(self):
        """GET /v1/profiles -> {profiles:[{_id,name,...}], total, skip, limit}.

        LASSO is already provisioned in Zernio, so profiles pre-exist; the connect path must
        REUSE an existing profile, never re-create it (Zernio 409s on a duplicate name). Verified
        live 2026-08-06 against api.zernio.com: `{"profiles":[{"_id"(24 char),"name",...}]}`.
        """
        return self._get("/v1/profiles", {"limit": 100})

    def find_profile_id(self, name):
        """The `_id` of the existing Zernio profile whose name matches `name`, or None.

        Match is exact first, then case-insensitive (Zernio names are user-set, e.g. "lasso").
        Pure over the list response so it stays testable with a fake http client.
        """
        if not name:
            return None
        profiles = (self.list_profiles() or {}).get("profiles") or []
        want = str(name).strip()
        want_lower = want.lower()
        fallback = None
        for p in profiles:
            if not isinstance(p, dict):
                continue
            pid = p.get("_id") or p.get("id")
            pname = p.get("name")
            if not pid or not pname:
                continue
            if str(pname) == want:
                return str(pid)
            if fallback is None and str(pname).strip().lower() == want_lower:
                fallback = str(pid)
        return fallback

    def list_facebook_pages(self, account_id):
        """GET /v1/accounts/{id}/facebook-page -> {pages:[{_id,name}]}."""
        return self._get(f"/v1/accounts/{account_id}/facebook-page")

    def analytics(self, profile_id, skip=0, limit=50):
        """GET /v1/analytics?profileId=... -> the analytics JSON (read-only add-on).

        Shape (probed live): {hasAnalyticsAccess, overview, accounts:[...], posts:[...],
        pagination}. `posts` is a page of up to `limit` (newest first); pass `skip` to page.
        """
        params = {"profileId": profile_id, "skip": int(skip), "limit": int(limit)}
        return self._get("/v1/analytics", params)

    def analytics_window(self, profile_id, days, page_limit=50, max_pages=20):
        """Fetch ONE merged analytics JSON whose `posts` cover the last `days`.

        Pages through `posts` (newest first) accumulating until a page's OLDEST post is
        already before the window start, or `pagination.total` is reached, or a page comes
        back empty, or `max_pages` is hit (defensive cap; flagged as `_pages_capped`). The
        first page's top-level fields (hasAnalyticsAccess, overview, accounts, pagination)
        are kept as is; only `posts` accumulate. Read-only: no writes ever issued.

        A post with no parseable publishedAt is KEPT (never dropped by the pager) so the
        pure mapper's in-window filter is the single place inclusion is decided.
        """
        cutoff = None
        if isinstance(days, (int, float)) and days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=float(days))

        first = self.analytics(profile_id, skip=0, limit=page_limit) or {}
        merged = dict(first)
        posts = list(first.get("posts") or [])
        pagination = first.get("pagination") or {}
        pages_capped = False

        def _page_oldest(page_posts):
            oldest = None
            for p in page_posts:
                if not isinstance(p, dict):
                    continue
                ts = _parse_iso(p.get("publishedAt"))
                if ts is not None and (oldest is None or ts < oldest):
                    oldest = ts
            return oldest

        last_posts = posts
        page = 1
        while True:
            # Stop once the newest-first stream has crossed the window boundary.
            if cutoff is not None:
                oldest = _page_oldest(last_posts)
                if oldest is not None and oldest < cutoff:
                    break
            total = pagination.get("total")
            if not isinstance(total, (int, float)) or len(posts) >= int(total):
                break
            if page >= max_pages:
                pages_capped = True
                break
            nxt = self.analytics(profile_id, skip=len(posts), limit=page_limit) or {}
            more = list(nxt.get("posts") or [])
            if not more:
                break
            posts.extend(more)
            last_posts = more
            pagination = nxt.get("pagination") or pagination
            page += 1

        merged["posts"] = posts
        merged["_pages_capped"] = pages_capped
        return merged

    # ---- writes (provisioning) ---------------------------------------------
    def create_profile(self, name):
        """POST /v1/profiles {name} -> {..._id}. Per-gym provisioning.

        Only reached when find_profile_id found NO existing profile of that name — Zernio 409s
        ("profile_name_conflict") on a duplicate, so the connect path finds-before-create and
        falls back to find on a 409. Never a read path.
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
    # Token expiry is a NEGATIVE and takes precedence: connectedAt + expires_in in the past -> expired.
    exp = md.get("expires_in")
    connected_at = _parse_iso(acct.get("connectedAt") or md.get("connectedAt"))
    if isinstance(exp, (int, float)) and connected_at is not None:
        now = now or datetime.now(timezone.utc)
        if (now - connected_at).total_seconds() > float(exp):
            return "expired"
    # A real account ROW present in Zernio's list IS the connection: the OAuth callback wrote it, and
    # an intentional disconnect / inactive flag / token expiry (all handled above) are the only things
    # that make it not connected. We do NOT require a positive signal like profileData/connectedAt/
    # isActive: Zernio's list momentarily omits those fields, which used to flap a live connection
    # (especially Instagram) to "not_connected" and force a reconnect every session. This matches the
    # bar facebook_account_id() uses (platform + _id). The only guard is a bare or malformed payload
    # with no account id, which is never optimistically called connected.
    if not acct.get("_id"):
        return "not_connected"
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
