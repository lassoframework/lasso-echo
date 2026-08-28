"""
zernio_reverify.py — re-verify a gym's TRUE Zernio connection state and overwrite the
poisoned portal cache (echo_social_connections).

THE CONNECTION-STATUS BUG (gritx/hillcountry, 2026-08-28): the status route ran on the
volume-less echo-intake-web service, resolved NO local profile id, and answered
not_connected for every platform even though Zernio held the live account. The portal's
6h re-verify cron then WROTE that false not_connected into echo_social_connections and, worse,
flipped ever_connected=false — which defeats the portal's reconcileWithPriorConnection rescue.

This sweep repairs the damage AFTER the resolver fix is deployed: for each gym it reads the
REAL Zernio accounts (via the same read-only resolver the status path now uses) and rewrites
each platform's row to the true state. When a platform is genuinely connected now, it also
repairs ever_connected=true so reconcileWithPriorConnection works again. Read-only against
Zernio (never provisions, never disconnects); idempotent; one gym's failure never blocks the
rest. Runs where BOTH the Zernio key and the Supabase creds live (the worker).

    railway run /opt/venv/bin/python -m agent zernio-reverify --account gritx
    railway run /opt/venv/bin/python -m agent zernio-reverify --all
"""

from . import config
from . import zernio as _z
from . import zernio_routes as _zr


def _state_for_platform(status_map, platform):
    """(portal_state, handle) for one platform from map_status output. portal_state is the
    echo_social_connections vocabulary: 'connected' | 'expired' | 'not_connected'."""
    p = (status_map.get("platforms") or {}).get(platform) or {}
    if p.get("connected"):
        return "connected", p.get("handle")
    if p.get("expired"):
        return "expired", p.get("handle")
    return "not_connected", None


def reverify_gym(base, client=None, store=None, logger=None):
    """Re-verify ONE gym base and rewrite its echo_social_connections rows to the true
    Zernio state. `base` is the tenant/portal slug (gritx, hillcountry). Returns a summary
    dict. Never raises out. Requires zernio_enabled() + Supabase creds; a missing one is
    reported, not a crash."""
    log = logger or (lambda m: print(f"[zernio-reverify] {m}"))
    if not config.zernio_enabled():
        return {"ok": False, "base": base, "reason": "zernio disabled (no key on this host)"}
    if store is None:
        store = _zr._shared_store()
    if store is None:
        return {"ok": False, "base": base, "reason": "supabase creds absent on this host"}

    c = client if client is not None else _z.ZernioClient()
    # Resolve the profile the SAME read-only way the status path now does (shared plane,
    # local db, then find-by-name). No provisioning — a reverify never creates anything.
    pid = _zr._resolve_profile_id(base, client=c, allow_find=True)
    if not pid:
        # Genuinely unresolved: honestly rewrite every platform to not_connected (do NOT
        # touch ever_connected — we have no positive signal to justify repairing it).
        results = []
        for platform in _z.STATUS_PLATFORMS:
            try:
                store.rewrite_social_connection(base, platform, "not_connected", handle=None)
                results.append({"platform": platform, "state": "not_connected"})
            except Exception as exc:  # noqa: BLE001
                results.append({"platform": platform, "error": type(exc).__name__})
        log(f"{base}: no Zernio profile found; wrote not_connected for all platforms")
        return {"ok": True, "base": base, "profile_id": None, "results": results}

    try:
        accounts = c.list_accounts(pid)
    except Exception as exc:  # noqa: BLE001 - a live-read failure must not poison the cache
        log(f"{base}: Zernio list_accounts failed ({type(exc).__name__}); left cache untouched")
        return {"ok": False, "base": base, "profile_id": str(pid),
                "reason": f"list_accounts failed: {type(exc).__name__}"}

    status_map = _z.map_status(accounts)
    results = []
    for platform in _z.STATUS_PLATFORMS:
        state, handle = _state_for_platform(status_map, platform)
        # Repair ever_connected ONLY when the platform is genuinely connected now — that is the
        # positive signal the portal's reconcileWithPriorConnection rescue needs, and the exact
        # bit the poisoning cron cleared.
        mark_ever = state == "connected"
        try:
            store.rewrite_social_connection(base, platform, state, handle=handle,
                                            mark_ever_connected=mark_ever)
            results.append({"platform": platform, "state": state,
                            "handle": handle or "", "ever_connected_repaired": mark_ever})
        except Exception as exc:  # noqa: BLE001 - one platform never blocks the rest
            results.append({"platform": platform, "error": type(exc).__name__})
    log(f"{base}: reverified profile={pid} -> " +
        ", ".join(f"{r.get('platform')}={r.get('state', r.get('error'))}" for r in results))
    return {"ok": True, "base": base, "profile_id": str(pid), "results": results}


def reverify_bases(bases=None, client=None, store=None, logger=None):
    """Re-verify a list of gym bases (default = the client _ig gym bases). Returns a
    summary dict {ok, count, gyms:[...]}. Never raises out."""
    log = logger or (lambda m: print(f"[zernio-reverify] {m}"))
    if bases is None:
        try:
            from .calendar_autopublish import client_gym_bases
            bases = client_gym_bases()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"could not list bases: {type(exc).__name__}"}
    if store is None:
        store = _zr._shared_store()
    c = client if client is not None else (_z.ZernioClient() if config.zernio_enabled() else None)
    gyms = []
    for base in bases:
        gyms.append(reverify_gym(base, client=c, store=store, logger=log))
    return {"ok": True, "count": len(gyms), "gyms": gyms}
