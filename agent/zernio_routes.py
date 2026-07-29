"""
Zernio social-connect portal endpoints (brokered for the LASSO portal).

All routes gated by config.zernio_enabled() (ZERNIO_API_KEY present, else dark). Token->account
resolution happens in intake_web.py before these handlers, so each receives a validated account_key.
Each returns (status_code, dict). External Zernio calls go through zernio.ZernioClient (injectable
for tests); the portal never sees the Zernio key — Echo brokers it here.

Portal contract (these return exactly what the portal expects Echo to return):
  GET  social-connect?platform=  -> {oauth_url}
  GET  social-status             -> {platforms:{instagram:{connected,handle,expired}, facebook:{...}}}
  GET  facebook-pages            -> {pages:[{id,name}]}
  POST facebook-page-select      -> {ok}
"""

from . import config, db as _db
from . import zernio as _z


def _disabled(route):
    return 403, {"error": "zernio is not configured", "route": route}


def _client(client):
    return client if client is not None else _z.ZernioClient()


def _resolve_profile_id(account_key):
    """The gym's stored Zernio profile id, or None. Read-only (never provisions)."""
    if not account_key:
        return None
    row = _db.gym_get(account_key)
    if not row:
        return None
    return row.get("zernio_profile_id") or None


def _ensure_profile_id(account_key, client):
    """Stored profile id, or provision a new Zernio profile for this gym and store it.

    Only the connect path calls this (connecting implies the gym needs a profile). Reads never
    provision, so a passive status poll never mutates Zernio.
    """
    pid = _resolve_profile_id(account_key)
    if pid:
        return pid
    row = _db.gym_get(account_key) or {}
    name = row.get("gym_name") or row.get("display_name") or account_key
    created = client.create_profile(name)
    pid = (created or {}).get("_id") or ((created or {}).get("profile") or {}).get("_id")
    if pid:
        # Preserve the gym's existing display_name (gym_upsert rewrites it every call).
        _db.gym_upsert(account_key, display_name=row.get("display_name") or "", zernio_profile_id=str(pid))
    return pid


def handle_social_connect(account_key, platform, client=None):
    """GET /portal/<token>/social-connect?platform=instagram|facebook -> {oauth_url}."""
    if not config.zernio_enabled():
        return _disabled("social-connect")
    if not account_key:
        return 400, {"error": "missing account_key"}
    if platform not in _z.PLATFORMS:
        return 400, {"error": "platform must be instagram or facebook"}
    c = _client(client)
    try:
        pid = _ensure_profile_id(account_key, c)
        if not pid:
            return 502, {"error": "could not resolve a Zernio profile for this gym"}
        data = c.connect_url(pid, platform)
    except _z.ZernioError as exc:
        return 502, {"error": f"zernio {exc.status}", "detail": exc.detail}
    except Exception as exc:  # network/parse: honest, never a fabricated URL
        return 502, {"error": f"zernio call failed: {type(exc).__name__}"}
    auth_url = (data or {}).get("authUrl")
    if not auth_url or not str(auth_url).startswith("http"):
        return 502, {"error": "zernio returned no authUrl"}
    return 200, {"oauth_url": auth_url}


def handle_social_status(account_key, client=None):
    """GET /portal/<token>/social-status -> {platforms:{instagram,facebook}}."""
    if not config.zernio_enabled():
        return _disabled("social-status")
    if not account_key:
        return 400, {"error": "missing account_key"}
    pid = _resolve_profile_id(account_key)
    if not pid:
        # Not provisioned yet: honest not-connected, never a fabricated connection.
        return 200, _z.map_status({})
    c = _client(client)
    try:
        accounts = c.list_accounts(pid)
    except _z.ZernioError as exc:
        return 502, {"error": f"zernio {exc.status}", "detail": exc.detail}
    except Exception as exc:
        return 502, {"error": f"zernio call failed: {type(exc).__name__}"}
    return 200, _z.map_status(accounts)


def handle_facebook_pages(account_key, client=None):
    """GET /portal/<token>/facebook-pages -> {pages:[{id,name}]}."""
    if not config.zernio_enabled():
        return _disabled("facebook-pages")
    if not account_key:
        return 400, {"error": "missing account_key"}
    pid = _resolve_profile_id(account_key)
    if not pid:
        return 200, {"pages": []}
    c = _client(client)
    try:
        accounts = c.list_accounts(pid)
        fb_id = _z.facebook_account_id(accounts)
        if not fb_id:
            return 200, {"pages": []}  # no FB account connected yet: honest empty picker
        pages = c.list_facebook_pages(fb_id)
    except _z.ZernioError as exc:
        return 502, {"error": f"zernio {exc.status}", "detail": exc.detail}
    except Exception as exc:
        return 502, {"error": f"zernio call failed: {type(exc).__name__}"}
    return 200, _z.map_pages(pages)


def handle_facebook_page_select(account_key, page_id, client=None):
    """POST /portal/<token>/facebook-page-select {page_id} -> {ok}.

    Echo owns the Page binding: it persists the gym's chosen page id and injects it per post
    (Zernio's platformSpecificData.pageId). The portal stores nothing.
    """
    if not config.zernio_enabled():
        return _disabled("facebook-page-select")
    if not account_key:
        return 400, {"error": "missing account_key"}
    if not page_id or not str(page_id).strip():
        return 400, {"error": "page_id required"}
    try:
        existing = _db.gym_get(account_key) or {}
        _db.gym_upsert(
            account_key,
            display_name=existing.get("display_name") or "",
            zernio_default_fb_page_id=str(page_id).strip(),
        )
    except Exception as exc:
        return 500, {"error": f"db error: {type(exc).__name__}"}
    return 200, {"ok": True}
