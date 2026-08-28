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
  POST connect/finalize          -> {ok, finalized, platform, selected?|options?} (headless OAuth return leg)
"""

import json as _json

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
    gym_name = row.get("gym_name") or ""
    display_name = row.get("display_name") or ""
    # The name a NEW profile would be created under (first non-empty of gym_name/display_name/key).
    name = gym_name or display_name or account_key

    # 2) reuse an existing profile matching ANY known alias (Zanshin/Pete 2026-08-27): a gym's Zernio
    #    profile is often pre-created by ops under a HUMAN name ("Zanshin Fitness") while Echo looks it
    #    up by the account_key. Trying the account_key AND the display/gym name finds the real,
    #    populated profile so we never create a duplicate empty one that strands connections.
    pid = client.find_profile_id_any(account_key, display_name, gym_name)

    # 3) none found -> create; 4) 409 -> the profile exists, re-find it (by every alias)
    if not pid:
        try:
            pid = _created_profile_id(client.create_profile(name))
        except _z.ZernioError as exc:
            if exc.status == 409:
                pid = client.find_profile_id_any(name, account_key, display_name, gym_name)
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


def _allowed_redirect_origins():
    """The ONLY origins a post-OAuth redirect may land on: the LASSO portal and the
    intake-web service itself. Overridable additions via AGENT_CONNECT_REDIRECT_ORIGINS
    (comma list of https origins), for a future portal domain change without a deploy."""
    import os as _os
    origins = {config.portal_public_base_url().rstrip("/").lower()}
    try:
        from .intake_web import _upload_base_url
        origins.add(_upload_base_url().rstrip("/").lower())
    except Exception:  # noqa: BLE001 - the portal origin alone is a safe allowlist
        pass
    extra = _os.environ.get("AGENT_CONNECT_REDIRECT_ORIGINS", "") or ""
    for o in extra.split(","):
        o = o.strip().rstrip("/").lower()
        if o.startswith("https://"):
            origins.add(o)
    return origins


def _connect_redirect_url(redirect_url):
    """The post-OAuth return URL to hand Zernio. Prefer the caller's (the portal passes its
    own Social page) — but ONLY when its ORIGIN is allowlisted (audit 2026-08-25 MAJOR:
    accepting any http(s) url was an open redirect — a phisher holding a gym's portal link
    could route the owner through a REAL OAuth approval onto an attacker page). Anything
    else falls back to the configured portal origin + /my so the browser NEVER lands on
    the Zernio dashboard or an untrusted site."""
    ru = (redirect_url or "").strip()
    if ru.startswith("http://") or ru.startswith("https://"):
        low = ru.lower()
        for origin in _allowed_redirect_origins():
            if low == origin or low.startswith(origin + "/"):
                return ru
    return f"{config.portal_public_base_url()}/my"


def portal_dest_url(dest):
    """The validated final landing for the post-finalize 302 (the FINALIZE FIX return leg).
    Same allowlist as _connect_redirect_url: only the LASSO portal / intake-web origins are
    honored, so the ?dest= param can never be turned into an open redirect. Anything else
    (absent, off-origin, non-http) falls back to the configured portal /my."""
    return _connect_redirect_url(dest)


def connect_url_for(account_key, platform, client=None, redirect_url=None):
    """OPS: the OAuth CONNECT url for a gym + platform (instagram|facebook|googlebusiness),
    find-or-creating the Zernio profile first. This is the same URL the portal handler returns;
    exposed for the CLI so ops can hand a gym owner a direct connect link (e.g. a Google
    Business connect link) without a portal round-trip. Returns (ok, url_or_error). Requires
    zernio_enabled() (ZERNIO_API_KEY) — run on the worker where the key lives.

    redirect_url is threaded to Zernio so the gym owner returns to the LASSO portal after
    approving, never the Zernio dashboard; a missing one falls back to the portal origin."""
    if not config.zernio_enabled():
        return False, "zernio disabled (ZERNIO_API_KEY not set on this host)"
    if not account_key:
        return False, "missing account_key"
    if platform not in _z.CONNECT_PLATFORMS:
        return False, f"platform must be one of {', '.join(_z.CONNECT_PLATFORMS)}"
    c = _client(client)
    try:
        pid = _ensure_profile_id(account_key, c)
        if not pid:
            return False, "could not resolve a Zernio profile for this gym"
        data = c.connect_url(pid, platform, redirect_url=_connect_redirect_url(redirect_url))
    except _z.ZernioError as exc:
        return False, f"zernio {exc.status}: {exc.detail}"
    except Exception as exc:  # noqa: BLE001 - report, never crash the ops command
        return False, f"{type(exc).__name__}: {exc}"
    auth_url = (data or {}).get("authUrl")
    if not auth_url or not str(auth_url).startswith("http"):
        return False, "zernio returned no authUrl"
    return True, str(auth_url)


def handle_social_connect(account_key, platform, client=None, redirect_url=None,
                          echo_return_url=None):
    """GET /portal/<token>/social-connect?platform=instagram|facebook|googlebusiness[&redirect_url=...]
    -> {oauth_url}. Google Business connects through the SAME find-or-create profile +
    connect_url path as IG/FB (Zernio platform key 'googlebusiness').

    redirect_url is the post-OAuth return target the PORTAL passes (its own Social page) so the
    gym owner lands back in the LASSO portal after approving, never on the Zernio dashboard. When
    the portal does not pass one, _connect_redirect_url falls back to the configured portal
    origin — the redirect is NEVER omitted, so Zernio can never default to its dashboard.

    echo_return_url (the FINALIZE FIX, Zanshin/Pete 2026-08-28): Zernio ALWAYS runs headless
    (connect_url passes headless=true), so after OAuth it bounces the browser back with
    step/tempToken and does NOT create the account. The account is only created when the
    selection endpoints are called (handle_connect_finalize). The portal's /my page has NO
    handshake for that return leg, so every portal-driven Facebook/Google grant was silently
    dropped. When an echo_return_url is supplied (intake_web builds a token-scoped
    /portal/<token>/connect/return that runs the finalize SERVER-SIDE and then 302s to the
    portal), we hand THAT to Zernio instead of the raw portal url — so the finalize always
    happens no matter where the connect was started. The portal's own desired final landing
    rides inside echo_return_url as ?dest=. A missing echo_return_url preserves the old
    behaviour (Echo's own connect page, which has the JS handshake)."""
    if not config.zernio_enabled():
        return _disabled("social-connect")
    if not account_key:
        return 400, {"error": "missing account_key"}
    if platform not in _z.CONNECT_PLATFORMS:
        return 400, {"error": "platform must be instagram, facebook, or googlebusiness"}
    c = _client(client)
    # Instagram is not headless (no page/location select step); it lands the account directly,
    # so it keeps the portal's own redirect. Facebook/Google MUST come back through Echo's
    # finalize return leg or the grant is dropped.
    zernio_redirect = (echo_return_url if (echo_return_url and platform != "instagram")
                       else _connect_redirect_url(redirect_url))
    try:
        pid = _ensure_profile_id(account_key, c)
        if not pid:
            return 502, {"error": "could not resolve a Zernio profile for this gym"}
        data = c.connect_url(pid, platform, redirect_url=zernio_redirect)
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


def headless_params(source):
    """Normalize the Zernio headless-OAuth redirect params from a query string or a
    dict (the connect page forwards them as JSON) into one canonical dict:
      {step, platform, temp_token, user_profile (DECODED dict or None),
       user_profile_raw (JSON string or ''), connect_token, pending_data_token}

    Zernio's redirect (docs 'Standard vs Headless Mode') carries tempToken,
    userProfile (URL-encoded JSON), step, connect_token; Google Business
    additionally carries pendingDataToken (step=select_location). PURE: no I/O,
    never logs — the values are secrets-adjacent tokens."""
    if isinstance(source, str):
        from urllib.parse import urlsplit, parse_qs
        qs = urlsplit(source).query or (source if "=" in source else "")
        parsed = parse_qs(qs)
        src = {k: v[0] for k, v in parsed.items() if v}
    elif isinstance(source, dict):
        src = source
    else:
        src = {}

    def _s(*names):
        for n in names:
            v = src.get(n)
            if v is not None and str(v).strip():
                return str(v).strip()
        return ""

    raw_profile = src.get("userProfile") or src.get("user_profile") or ""
    profile = None
    if isinstance(raw_profile, dict):
        profile = raw_profile
        raw_profile = _json.dumps(raw_profile)
    elif raw_profile:
        raw_profile = str(raw_profile)
        # Tolerate a still URL-encoded value (a client that forwarded the raw param).
        candidate = raw_profile
        if "%" in candidate and "{" not in candidate:
            from urllib.parse import unquote
            candidate = unquote(candidate)
        try:
            decoded = _json.loads(candidate)
            if isinstance(decoded, dict):
                profile = decoded
                raw_profile = candidate
        except (ValueError, TypeError):
            profile = None
    else:
        raw_profile = ""

    return {
        "step": _s("step"),
        "platform": _s("platform"),
        "temp_token": _s("tempToken", "temp_token"),
        "user_profile": profile,
        "user_profile_raw": raw_profile,
        "connect_token": _s("connect_token", "connectToken"),
        "pending_data_token": _s("pendingDataToken", "pending_data_token"),
    }


def _store_fb_page_id(account_key, page_id):
    """Persist gyms.zernio_default_fb_page_id (the publisher requires it). Preserves
    display_name per the gym_upsert contract. Returns True on success."""
    try:
        existing = _db.gym_get(account_key) or {}
        _db.gym_upsert(account_key,
                       display_name=existing.get("display_name") or "",
                       zernio_default_fb_page_id=str(page_id))
        return True
    except Exception:  # noqa: BLE001 - the connection itself succeeded; report, don't lose it
        return False


def _account_row_id(accounts_json, platform):
    """Any Zernio account _id of `platform` under the profile ('' if none) — the
    existence check after a headless finalize (a just-created account counts even
    if Zernio's list momentarily omits status fields)."""
    for a in (accounts_json or {}).get("accounts") or []:
        if isinstance(a, dict) and (a.get("platform") or "").lower() == platform \
                and a.get("_id"):
            return str(a["_id"])
    return ""


def _finalize_facebook(account_key, pid, page, params, c):
    """Finalize ONE chosen Facebook page: select it in Zernio, VERIFY the account
    row now exists, then persist the page id for the publisher. Returns (status, dict)."""
    c.fb_select_page(pid, page["id"], params["temp_token"],
                     user_profile=params["user_profile"],
                     connect_token=params["connect_token"] or None)
    # Only claim success (and only store the page binding) after the account row
    # is REALLY there — the exact class of silent failure this flow fixes.
    acct_id = _account_row_id(c.list_accounts(pid), "facebook")
    if not acct_id:
        return 502, {"error": "the connection did not complete",
                     "detail": "no Facebook account appeared after selection"}
    stored = _store_fb_page_id(account_key, page["id"])
    out = {"ok": True, "finalized": True, "platform": "facebook",
           "selected": {"id": page["id"], "name": page.get("name") or ""}}
    if not stored:
        out["warning"] = "connected, but the page binding could not be stored"
    return 200, out


def _finalize_googlebusiness(account_key, pid, loc, params, c):
    """Finalize ONE chosen Google Business location and verify the account row."""
    c.gbp_select_location(pid, loc["id"], params["pending_data_token"],
                          account_id=loc.get("account_id") or None,
                          connect_token=params["connect_token"] or None)
    acct_id = _account_row_id(c.list_accounts(pid), "googlebusiness")
    if not acct_id:
        return 502, {"error": "the connection did not complete",
                     "detail": "no Google Business account appeared after selection"}
    return 200, {"ok": True, "finalized": True, "platform": "googlebusiness",
                 "selected": {"id": loc["id"], "name": loc.get("name") or ""}}


def handle_connect_finalize(account_key, body, client=None):
    """POST /portal/<token>/connect/finalize — the headless OAuth RETURN leg.

    In headless mode Zernio does not create the account after OAuth; it bounces the
    browser back to the connect page with tempToken/userProfile/step/connect_token
    (GBP: pendingDataToken, step=select_location) and the integrator must call the
    selection endpoints. Echo had ZERO handling for that return leg, so every
    Facebook/Google grant was silently dropped (Hill Country, 2026-08-26). This
    handler closes the loop:

      body = the redirect params (forwarded by the connect page JS) + optionally
             {choice_id, choice_name, choice_account_id} once the owner has picked.

      * no choice + EXACTLY ONE page/location -> auto-select it server-side
        (the common gym case) and return {finalized:true, selected}.
      * no choice + several -> {finalized:false, options:[{id,name}]} for the
        branded picker. ZERO -> {finalized:false, options:[]} (the login used has
        no page access; the page says so honestly).
      * choice_id present -> finalize that one.

    A Zernio 4xx (expired/used tempToken) returns 400 {expired:true} so the page
    can say "that link expired" instead of silently bouncing. On a successful
    Facebook finalize the gym row's zernio_default_fb_page_id is upserted — but
    only after re-checking list_accounts confirms the account was created."""
    if not config.zernio_enabled():
        return _disabled("connect-finalize")
    if not account_key:
        return 400, {"error": "missing account_key"}
    params = headless_params(body if isinstance(body, dict) else {})
    step = params["step"]
    if step not in ("select_page", "select_location"):
        return 400, {"error": "step must be select_page or select_location"}
    if step == "select_page" and not params["temp_token"]:
        return 400, {"error": "missing tempToken"}
    if step == "select_location" and not (params["pending_data_token"]
                                          or params["temp_token"]):
        return 400, {"error": "missing pendingDataToken"}
    if step == "select_location" and not params["pending_data_token"]:
        # Docs say GBP sends pendingDataToken; tolerate a tempToken-only redirect.
        params["pending_data_token"] = params["temp_token"]

    pid = _resolve_profile_id(account_key)
    if not pid:
        return 400, {"error": "no social profile yet; start the connection again"}

    body = body if isinstance(body, dict) else {}
    choice_id = str(body.get("choice_id") or "").strip()
    c = _client(client)
    try:
        if step == "select_page":
            listed = _z.map_pages(c.fb_pages_after_oauth(
                pid, params["temp_token"],
                user_profile=params["user_profile_raw"] or None,
                connect_token=params["connect_token"] or None))
            options = listed["pages"]
            platform, finalize = "facebook", _finalize_facebook
        else:
            listed = _z.map_locations(c.gbp_locations_after_oauth(
                pid, params["pending_data_token"],
                connect_token=params["connect_token"] or None))
            options = listed["locations"]
            platform, finalize = "googlebusiness", _finalize_googlebusiness

        if choice_id:
            chosen = next((o for o in options if o["id"] == choice_id), None)
            if chosen is None:
                return 400, {"error": "that choice is not one of the available "
                                      "options"}
            return finalize(account_key, pid, chosen, params, c)
        if len(options) == 1:
            return finalize(account_key, pid, options[0], params, c)
        return 200, {"ok": True, "finalized": False, "platform": platform,
                     "options": options}
    except _z.ZernioError as exc:
        if 400 <= exc.status < 500:
            # Expired/used tempToken or a rejected selection: honest, retryable.
            return 400, {"error": f"zernio {exc.status}", "expired": True,
                         "detail": exc.detail}
        return 502, {"error": f"zernio {exc.status}", "detail": exc.detail}
    except Exception as exc:  # network/parse: honest, never a silent bounce
        return 502, {"error": f"zernio call failed: {type(exc).__name__}"}


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
