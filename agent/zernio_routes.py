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


def _created_profile_id(created):
    """Pull the _id out of a create_profile response ({_id} or {profile:{_id}})."""
    return (created or {}).get("_id") or ((created or {}).get("profile") or {}).get("_id")


def _ensure_profile_id(account_key, client):
    """Stored profile id, else REUSE an existing Zernio profile by name, else create one; persist it.

    LASSO (and other gyms) are already set up in Zernio, so the profile pre-exists and Zernio 409s
    ("profile_name_conflict") on a duplicate create. Order:
      1. stored zernio_profile_id (no Zernio call);
      2. find an existing profile by this gym's name and reuse its _id;
      3. only if none exists, create one;
      4. belt-and-suspenders: if create still 409s (a race, or a name we did not match), fall back
         to find-by-name.
    Only the connect path calls this; reads never provision, so a passive status poll never mutates
    Zernio.
    """
    pid = _resolve_profile_id(account_key)
    if pid:
        return pid
    row = _db.gym_get(account_key) or {}
    name = row.get("gym_name") or row.get("display_name") or account_key

    # 2) reuse an existing profile of this name (the common case: LASSO already provisioned)
    pid = client.find_profile_id(name)

    # 3) none found -> create; 4) 409 -> the profile exists, re-find it
    if not pid:
        try:
            pid = _created_profile_id(client.create_profile(name))
        except _z.ZernioError as exc:
            if exc.status == 409:
                pid = client.find_profile_id(name)
            else:
                raise
            if not pid:
                raise

    if pid:
        # Preserve the gym's existing display_name (gym_upsert rewrites it every call).
        _db.gym_upsert(account_key, display_name=row.get("display_name") or "", zernio_profile_id=str(pid))
    return pid


def provision_gym(account_key, client=None):
    """OPS: find-or-create this gym's Zernio PROFILE so it can connect Google Business Profile
    / GBM (or any platform). Provisioning normally happens as a side effect of the FIRST IG/FB
    connect (handle_social_connect -> _ensure_profile_id); this is the direct path for a
    GBP-first gym whose owner goes straight to Google Business and hits "not provisioned in
    Zernio yet". Find-first (reuses an existing profile by name; only creates when none exists),
    so it is safe to re-run. Returns (ok, profile_id_or_error_string). Requires
    zernio_enabled() (ZERNIO_API_KEY) — run on the worker where the key lives:
        railway run /opt/venv/bin/python -m agent zernio-provision --account <key>
    """
    if not config.zernio_enabled():
        return False, "zernio disabled (ZERNIO_API_KEY not set on this host)"
    if not account_key:
        return False, "missing account_key"
    try:
        pid = _ensure_profile_id(account_key, _client(client))
    except _z.ZernioError as exc:
        return False, f"zernio {exc.status}: {exc.detail}"
    except Exception as exc:  # noqa: BLE001 - report, never crash the ops command
        return False, f"{type(exc).__name__}: {exc}"
    if not pid:
        return False, "no profile id returned (check the gym name against Zernio)"
    return True, str(pid)


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


def handle_social_disconnect(account_key, platform, client=None,
                             snapshot_clear=None):
    """POST /portal/<token>/social-disconnect?platform=instagram|facebook -> {ok, disconnected}.

    For a gym owner who connected the WRONG account (e.g. a personal or a spouse's
    Instagram): finds that platform's connected account under the gym's Zernio profile
    and DELETES it (Zernio DELETE /v1/accounts/{id}), so they can reconnect the right
    one. Also clears the portal's connection snapshot (echo_social_connections) for that
    platform so the LASSO dashboard reflects the disconnect immediately, not the stale
    account. FB additionally forgets the stored page binding (a new page must be picked
    on reconnect). Idempotent: nothing connected for that platform -> {ok, disconnected:0}.
    Token-scoped: account_key is validated upstream and every read/write is keyed to it."""
    if not config.zernio_enabled():
        return _disabled("social-disconnect")
    if not account_key:
        return 400, {"error": "missing account_key"}
    if platform not in _z.PLATFORMS:
        return 400, {"error": "platform must be instagram or facebook"}
    pid = _resolve_profile_id(account_key) or _client(client).find_profile_id(account_key)
    if not pid:
        return 200, {"ok": True, "disconnected": 0, "detail": "nothing connected"}
    c = _client(client)
    try:
        accounts = c.list_accounts(pid)
        acct_id = _z.account_id_for(accounts, platform)
        # account_id_for returns only a CONNECTED account; if none, also try any
        # account row of that platform (a wrong/expired one still needs removing).
        if not acct_id:
            for a in (accounts or {}).get("accounts") or []:
                if a.get("platform") == platform and a.get("_id"):
                    acct_id = str(a["_id"])
                    break
        if not acct_id:
            return 200, {"ok": True, "disconnected": 0, "detail": "nothing connected"}
        try:
            c.disconnect_account(acct_id)
        except _z.ZernioError as exc:
            # 404 = already gone (double-click race: the first click removed it).
            # That IS the desired end state — fall through to the clears, not a 502.
            if exc.status != 404:
                raise
    except _z.ZernioError as exc:
        return 502, {"error": f"zernio {exc.status}", "detail": exc.detail}
    except Exception as exc:
        return 502, {"error": f"zernio call failed: {type(exc).__name__}"}
    # forget the FB page binding so a reconnect picks a fresh page (best effort)
    if platform == "facebook":
        try:
            existing = _db.gym_get(account_key) or {}
            _db.gym_upsert(account_key, display_name=existing.get("display_name") or "",
                           zernio_default_fb_page_id="")
        except Exception:
            pass
    # clear the portal's snapshot so the dashboard updates now (best effort; the
    # portal's own status poll would also correct it on the next read)
    clearer = snapshot_clear or _default_snapshot_clear
    try:
        clearer(account_key, platform)
    except Exception as exc:
        print(f"[zernio] disconnect snapshot clear failed for {account_key}/"
              f"{platform}: {type(exc).__name__}")
    return 200, {"ok": True, "disconnected": 1, "platform": platform}


def _default_snapshot_clear(account_key, platform):
    """Set echo_social_connections.state='not_connected' (handle null) for this gym's
    platform so the LASSO dashboard reflects a disconnect immediately. Keyed by the
    gym's Supabase uuid resolved from its slug (the tenant base). No-op when creds are
    absent or the gym/row is missing."""
    from .portal_calendar_store import SupabaseCalendarStore
    base = account_key
    for suf in ("_ig", "_fb"):
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    store = SupabaseCalendarStore()
    if not getattr(store, "available", lambda: True)():
        return
    store.clear_social_connection(base, platform)


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

    OWNERSHIP VALIDATED: the page id must be one of the pages the gym's OWN connected
    Facebook account actually manages (list_facebook_pages). A typo'd/stale/foreign id
    is refused with 400 at selection time instead of surfacing days later as a silent
    publish failure (or a post landing on the wrong page).
    """
    if not config.zernio_enabled():
        return _disabled("facebook-page-select")
    if not account_key:
        return 400, {"error": "missing account_key"}
    if not page_id or not str(page_id).strip():
        return 400, {"error": "page_id required"}
    page_id = str(page_id).strip()
    c = _client(client)
    try:
        pid = _resolve_profile_id(account_key)
        if not pid:
            return 400, {"error": "no social profile yet; connect Facebook first"}
        fb_id = _z.facebook_account_id(c.list_accounts(pid))
        if not fb_id:
            return 400, {"error": "no Facebook account connected; connect it first"}
        owned = {p["id"] for p in _z.map_pages(c.list_facebook_pages(fb_id))["pages"]}
        if page_id not in owned:
            return 400, {"error": "page_id does not belong to this gym's connected "
                                  "Facebook account"}
    except _z.ZernioError as exc:
        return 502, {"error": f"zernio {exc.status}", "detail": exc.detail}
    except Exception as exc:
        return 502, {"error": f"zernio call failed: {type(exc).__name__}"}
    try:
        existing = _db.gym_get(account_key) or {}
        _db.gym_upsert(
            account_key,
            display_name=existing.get("display_name") or "",
            zernio_default_fb_page_id=page_id,
        )
    except Exception as exc:
        return 500, {"error": f"db error: {type(exc).__name__}"}
    return 200, {"ok": True}
