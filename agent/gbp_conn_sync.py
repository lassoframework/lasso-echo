"""
gbp_conn_sync.py — populate gym_gbp_connections from the LIVE Zernio connection.

THE GAP THIS CLOSES (Dale / ENG, 2026-08-19): the GBP publish lane
(gbp_worker.publish_due_gbp -> store.connections_for -> resolve_connection) routes every
approved googlebusiness row through a gym_gbp_connections row. But nothing in Echo wrote
that table — the portal was specced to upsert it on the OAuth callback (GBP_BUILD_SPEC
§71) and does not — so the table was EMPTY for every gym and GBP could never publish
(0 connections -> RoutingError -> failed).

This module reads each client gym's live Google Business connection from Zernio
(list_accounts) and upserts its gym_gbp_connections row: zernio account + GBP location id
+ status. It NEVER publishes and never touches content_calendar. Run once per loop so a
gym that connects (or reconnects) has a routable connection by the next cycle, and a gym
whose Zernio account goes inactive is flipped to 'needs_reconnect' (the publish lane then
holds its posts silently, never a failed-post storm).

Behind AGENT_GBP_CONN_SYNC (config.gbp_conn_sync_enabled(), default OFF). Flag off ->
sync_gbp_connections returns {ok:False} and touches nothing. Fully isolated per gym: one
gym's Zernio read failing never blocks the others.

TIMEZONE: Zernio does not return the location's timezone, so a NEW row is created with a
best-effort tz inferred from the location address (US state -> tz), else a flagged default
(a staff alert fires to set it). An EXISTING row's tz is PRESERVED (a human may have
corrected it) — see GbpStore.upsert_connection.
"""

from . import config

PLATFORM = "googlebusiness"
DEFAULT_TZ = "America/Indianapolis"      # LASSO HQ default; flagged for staff when used

# Best-effort US state -> IANA tz for the common case (single-tz states). A state that
# straddles zones (or an unrecognized address) falls back to DEFAULT_TZ + a staff flag,
# so we never silently guess wrong on a split-zone state.
_STATE_TZ = {
    "FL": "America/New_York", "GA": "America/New_York", "VA": "America/New_York",
    "NJ": "America/New_York", "NY": "America/New_York", "CT": "America/New_York",
    "MA": "America/New_York", "PA": "America/New_York", "NC": "America/New_York",
    "SC": "America/New_York", "OH": "America/New_York", "MI": "America/New_York",
    "ME": "America/New_York", "MD": "America/New_York", "DC": "America/New_York",
    "IL": "America/Chicago", "MN": "America/Chicago", "WI": "America/Chicago",
    "MO": "America/Chicago", "IA": "America/Chicago", "AR": "America/Chicago",
    "LA": "America/Chicago", "MS": "America/Chicago", "AL": "America/Chicago",
    "OK": "America/Chicago", "WA": "America/Los_Angeles", "OR": "America/Los_Angeles",
    "CA": "America/Los_Angeles", "NV": "America/Los_Angeles", "AZ": "America/Phoenix",
    "UT": "America/Denver", "CO": "America/Denver", "NM": "America/Denver",
    "MT": "America/Denver", "WY": "America/Denver", "HI": "Pacific/Honolulu",
    "AK": "America/Anchorage",
    # SPLIT-ZONE STATES, mapped to their DOMINANT zone (audit 2026-08-31: omitting
    # them meant 'Dripping Springs, TX' DEFAULTED to Indianapolis and every such
    # connection demanded a human VERIFY — the opposite of self-running). The rare
    # off-zone metros are corrected by _CITY_TZ below; a one-hour edge case for an
    # unlisted border town beats a human task on every new connection.
    "TX": "America/Chicago", "TN": "America/Chicago", "KY": "America/New_York",
    "KS": "America/Chicago", "NE": "America/Chicago", "ND": "America/Chicago",
    "SD": "America/Chicago", "ID": "America/Boise", "IN": "America/Indiana/Indianapolis",
    "NH": "America/New_York", "VT": "America/New_York", "RI": "America/New_York",
    "DE": "America/New_York", "WV": "America/New_York",
}

# Off-dominant-zone metros inside split states: checked BEFORE the state map so the
# well-known exceptions land right without any human step.
_CITY_TZ = {
    "EL PASO": "America/Denver",              # TX (Mountain)
    "PENSACOLA": "America/Chicago",           # FL panhandle
    "PANAMA CITY": "America/Chicago",         # FL panhandle
    "KNOXVILLE": "America/New_York",          # East TN
    "CHATTANOOGA": "America/New_York",        # East TN
    "JOHNSON CITY": "America/New_York",       # East TN
    "BOWLING GREEN": "America/Chicago",       # West KY
    "PADUCAH": "America/Chicago",             # West KY
    "OWENSBORO": "America/Chicago",           # West KY
    "EVANSVILLE": "America/Chicago",          # SW IN
    "GARY": "America/Chicago",                # NW IN
    "HAMMOND": "America/Chicago",             # NW IN
    "COEUR D'ALENE": "America/Los_Angeles",   # North ID
}


_STATE_NAMES = {
    "FLORIDA": "FL", "GEORGIA": "GA", "VIRGINIA": "VA", "CALIFORNIA": "CA",
    "ARIZONA": "AZ", "ILLINOIS": "IL", "OHIO": "OH", "MICHIGAN": "MI",
    "INDIANA": "IN", "COLORADO": "CO", "WASHINGTON": "WA", "OREGON": "OR",
    "NEVADA": "NV", "CONNECTICUT": "CT", "MASSACHUSETTS": "MA", "PENNSYLVANIA": "PA",
    "MARYLAND": "MD", "MINNESOTA": "MN", "WISCONSIN": "WI", "MISSOURI": "MO",
    "IOWA": "IA", "ARKANSAS": "AR", "LOUISIANA": "LA", "MISSISSIPPI": "MS",
    "ALABAMA": "AL", "OKLAHOMA": "OK", "UTAH": "UT", "MAINE": "ME",
    "HAWAII": "HI", "ALASKA": "AK", "MONTANA": "MT", "WYOMING": "WY",
    "NEWMEXICO": "NM", "NORTHCAROLINA": "NC", "SOUTHCAROLINA": "SC", "NEWYORK": "NY",
    "NEWJERSEY": "NJ",
    "TEXAS": "TX", "TENNESSEE": "TN", "KENTUCKY": "KY", "KANSAS": "KS",
    "NEBRASKA": "NE", "IDAHO": "ID", "VERMONT": "VT", "DELAWARE": "DE",
}


def _tz_from_address(address):
    """Best-effort IANA tz from a US location address, or None when the state is
    ambiguous / not found (caller then defaults + flags). PRECISE by design to avoid a
    token collision (a street/city word like 'IN', 'OR', 'OK' must NOT be read as a state):

      1. a FULL state name anywhere (word match, spaces stripped for two-word states); then
      2. a 2-letter state code ONLY when it stands alone as a comma-delimited part
         (optionally followed by a ZIP), i.e. the real 'state' segment of the address.

    Pure. Returns the IANA tz or None."""
    import re
    if not address:
        return None
    parts = [p.strip() for p in str(address).split(",")]
    # 0. off-dominant-zone metros inside split states (checked first, most specific).
    upper = str(address).upper()
    for city, tz in _CITY_TZ.items():
        if city in upper:
            return tz
    # 1. full state name (e.g. "... Cape Coral, Florida") — word-boundary, space-collapsed.
    words = {re.sub(r"[^A-Z]", "", w.upper()) for p in parts for w in p.split()}
    for name, st in _STATE_NAMES.items():
        if name in words and st in _STATE_TZ:
            return _STATE_TZ[st]
    # 2. a 2-letter code that IS a whole state segment ("FL", "FL 33904", "FL 33904-1234").
    for p in reversed(parts):
        m = re.match(r"^([A-Za-z]{2})(?:\s+\d{5}(?:-\d{4})?)?$", p.strip())
        if m and m.group(1).upper() in _STATE_TZ:
            return _STATE_TZ[m.group(1).upper()]
    return None


def _accounts_list(accts):
    """Normalize Zernio list_accounts output to a list of account dicts."""
    if isinstance(accts, list):
        return [a for a in accts if isinstance(a, dict)]
    if isinstance(accts, dict):
        for k in ("accounts", "data", "items"):
            v = accts.get(k)
            if isinstance(v, list):
                return [a for a in v if isinstance(a, dict)]
    return []


def _gbp_account(accts):
    """The googlebusiness account dict from a Zernio accounts list, or None."""
    for a in _accounts_list(accts):
        if str(a.get("platform") or a.get("type") or "").lower() == PLATFORM:
            return a
    return None


def _client_bases(clients=None):
    """The gym bases to sync. Explicit `clients` wins; else the client _ig account bases
    (reusing the same discovery client_media_sync uses), so LASSO/personal are excluded."""
    from .client_media_sync import _client_bases as _cmb
    return _cmb(clients)


def sync_gbp_connections(store=None, zernio=None, clients=None, logger=None, alert=None):
    """Read each client gym's live Google Business connection from Zernio and upsert its
    gym_gbp_connections row. Behind AGENT_GBP_CONN_SYNC. Returns
    {ok, synced, connected, needs_reconnect, skipped, results}.

    store   injectable GbpStore (upsert_connection + connections_for). Default: the live one.
    zernio  injectable Zernio client (find_profile_id + list_accounts). Default: the live one.
    clients optional explicit gym bases; else discovered from the client account registry.
    """
    log = logger or (lambda m: print(f"[gbp-conn-sync] {m}"))
    if not config.gbp_conn_sync_enabled():
        return {"ok": False, "reason": "AGENT_GBP_CONN_SYNC off", "synced": 0}

    if store is None:
        from .gbp_store import GbpStore
        store = GbpStore()
    if not getattr(store, "available", lambda: True)():
        return {"ok": False, "reason": "portal store unavailable", "synced": 0}
    if zernio is None:
        from .zernio import ZernioClient
        zernio = ZernioClient()

    from . import zernio as _z

    bases = _client_bases(clients)
    synced = connected = needs_reconnect = skipped = 0
    results = []
    for base in bases:
        try:
            pid = zernio.find_profile_id(base)
            if not pid:
                skipped += 1
                results.append({"gym": base, "status": "no_profile"})
                continue
            acct = _gbp_account(zernio.list_accounts(pid))
            if acct is None:
                # No Google account connected under this profile. If a row already exists,
                # flip it to needs_reconnect so the publish lane holds; else nothing to do.
                existing = store.connections_for(base) or []
                for c in existing:
                    if str(c.get("status", "")).lower() == "connected":
                        store.upsert_connection({
                            "portal_gym_key": base,
                            "gbp_location_id": c.get("gbp_location_id"),
                            "zernio_profile_id": c.get("zernio_profile_id") or pid,
                            "zernio_account_id": c.get("zernio_account_id"),
                            "status": "needs_reconnect",
                        })
                        needs_reconnect += 1
                results.append({"gym": base, "status": "no_gbp_account"})
                continue

            md = acct.get("metadata") or {}
            loc = str(md.get("selectedLocationId") or "").strip()
            if not loc:
                skipped += 1
                results.append({"gym": base, "status": "no_selected_location"})
                log(f"{base}: Google connected but no selectedLocationId; cannot route yet")
                continue

            state = _z.account_state(acct)
            status = "connected" if state == "connected" else "needs_reconnect"

            conn = {
                "portal_gym_key": base,
                "zernio_profile_id": pid,
                "zernio_account_id": acct.get("_id"),
                "gbp_location_id": loc,
                "location_name": md.get("selectedLocationName") or "",
                "status": status,
                "connected_by": "gbp_conn_sync",
            }
            if md.get("connectedAt"):
                conn["connected_at"] = md["connectedAt"]
            # tz only lands on INSERT (upsert preserves an existing/corrected tz).
            already = store.connections_for(base) or []
            has_row = any(c.get("gbp_location_id") == loc for c in already)
            if not has_row:
                tz = _tz_from_address(md.get("locationAddress"))
                # SELF-RUNNING (Blake 2026-08-31: no staff verification tasks): a
                # confident address inference lands SILENTLY — the city/state maps now
                # cover every state (split-zone states by their dominant zone, known
                # off-zone metros corrected). Only a truly unparseable address falls
                # back — first to the gym's own posting_timezone (set from its verified
                # website), then the HQ default — with ONE informational alert (no
                # action demanded; a wrong hour is self-evident and correctable).
                if not tz:
                    try:
                        gym_tz = config.posting_timezone_for(base)
                        tz_fallback = gym_tz if gym_tz != config.POSTING_TIMEZONE \
                            else DEFAULT_TZ
                    except Exception:  # noqa: BLE001
                        tz_fallback = DEFAULT_TZ
                    conn["timezone"] = tz_fallback
                    if alert:
                        alert(f"GBP conn sync: new {base} connection address was "
                              f"unparseable ({md.get('locationAddress')!r}); tz set to "
                              f"{tz_fallback} (the gym's own posting tz when known). "
                              "No action needed unless posts land at an odd hour.")
                else:
                    conn["timezone"] = tz

            store.upsert_connection(conn)
            synced += 1
            if status == "connected":
                connected += 1
            else:
                needs_reconnect += 1
            results.append({"gym": base, "status": status, "location_id": loc,
                            "location_name": conn["location_name"]})
        except Exception as e:  # noqa: BLE001 - one gym never blocks the others
            skipped += 1
            log(f"{base}: sync failed: {type(e).__name__}: {e}")
            results.append({"gym": base, "status": "error", "error": type(e).__name__})
            continue

    log(f"synced {synced} connection(s): {connected} connected, "
        f"{needs_reconnect} needs_reconnect, {skipped} skipped")
    return {"ok": True, "synced": synced, "connected": connected,
            "needs_reconnect": needs_reconnect, "skipped": skipped, "results": results}
