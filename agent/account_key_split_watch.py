"""
account_key_split_watch.py — catch a gym whose portal key and content key have DRIFTED APART.

THE SPLIT-BRAIN CLASS (Swift River CrossFit + CrossFit Sunnyside, live 2026-08-31).
Both gyms onboarded the same day and ended up with TWO keys each:

    portal token row : swiftrivercrossfite5c9db   (0 calendar rows, 0 media, 0 sources)
    content + Zernio : swiftrivercrossfitd23567   (14 pending rows, the Zernio profile)

Every read that starts from the PORTAL roster (the intake status page, the client portal,
the source readers, anything keyed off echo_intake_tokens.echo_account_key) looked at the
empty key and saw a gym with nothing. Every lane that starts from the CONTENT key built a
month nobody could see. Neither side errored. Neither side alerted. The gyms would simply
have never posted, and the first signal would have been the owner asking why.

WHY THE EXISTING DETECTORS ALL MISS IT:
  * account_key_reconcile compares the token key to canonical_account_key, but its
    idempotency rule treats ANY non-collided current key as "issued" and returns it
    verbatim — so it grades a split gym OK. It is also CLI-only and never scheduled.
  * account_key_doctor asks "does this base resolve to exactly one gyms row" — both keys
    resolve fine, to the SAME gym. That is precisely the split, and it reads as healthy.
  * onboarding_watch compares the portal token key to the INTAKE submission key. When both
    sides of the split agree with each other (they did), key_mismatch never fires.

So this watch asks the one question none of them ask: DOES THE KEY THE PORTAL RECORDED
ACTUALLY OWN THIS GYM'S CONTENT? A gym whose portal key holds nothing while another key
resolving to the SAME gym uuid holds a calendar is split, full stop.

WHAT IT REPORTS (each with the exact next command, because a watchdog that names a problem
without naming the fix just moves the work):
  orphan_portal_key  the portal key owns NO calendar rows while another key for the same
                     gym owns them. This is the Swift River shape. The portal is pointing
                     at a ghost.
  two_live_keys      BOTH keys own calendar rows. Worse: the gym is being built twice, and
                     whichever key the publisher iterates decides what actually posts.

RAILS: READ-ONLY. It never writes a key, never repoints a token, never touches content. It
reads three tables and emits one throttled ops alert per gym per day. Repointing is a
deliberate human act through account_key_reconcile (whose own writer is flag-gated and
refuses to strand data) — this module only makes the drift impossible to MISS.

Behind AGENT_ACCOUNT_KEY_SPLIT_WATCH, default OFF (house rule: every new capability ships
dark). All I/O is injectable so the whole watch runs offline in tests.

    python -m agent account-key-split-watch          # read-only report
"""

from datetime import date

from . import config

REASON_ORPHAN_PORTAL_KEY = "orphan_portal_key"
REASON_TWO_LIVE_KEYS = "two_live_keys"

_FIX = {
    REASON_ORPHAN_PORTAL_KEY:
        "the portal's key owns NO calendar rows while {content_key} owns {n} for the SAME "
        "gym, so every portal-side read sees an empty gym and the built month is invisible. "
        "Re-point the token onto the key that holds the content: "
        "AGENT_ACCOUNT_KEY_RECONCILE=true python -m agent account-key-reconcile "
        "--gym {gym_id} --apply  (it refuses if the CURRENT key owns data, so verify the "
        "plan first with the same command minus --apply).",
    REASON_TWO_LIVE_KEYS:
        "BOTH {portal_key} and {content_key} own calendar rows for the SAME gym, so the "
        "month is being built twice and whichever key the publisher iterates decides what "
        "posts. Do NOT re-point blind — that moves the pointer and strands the data. "
        "Reconcile the two calendars by hand first, then "
        "python -m agent account-key-reconcile --gym {gym_id}.",
}


def enabled():
    return config.account_key_split_watch_enabled()


# ---- injectable live readers (all read-only) --------------------------------------

def _supabase_get(path, params):
    """One bounded read against the shared plane. [] on absent creds or any failure: a
    reader fault must make this watch a NO-OP, never a fleet-wide false alarm."""
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        return []
    try:
        import requests  # lazy, matches the repo pattern
        r = requests.get(f"{url.rstrip('/')}/rest/v1/{path}", params=params,
                         headers={"apikey": key, "Authorization": f"Bearer {key}",
                                  "Accept": "application/json"},
                         timeout=30)
        if r.status_code >= 400:
            return []
        return r.json() or []
    except Exception:  # noqa: BLE001 - never raise out of a watchdog
        return []


def _default_portal_roster():
    """[(gym_uuid, portal_key, gym_name)] — every gym the PORTAL has minted an Echo key
    for. This is the authoritative roster (same source onboarding_watch trusts): a gym
    cannot hide from it by being absent from Echo's own registry."""
    tokens = _supabase_get("echo_intake_tokens", {"select": "gym_id,echo_account_key"})
    gyms = _supabase_get("gyms", {"select": "id,name,slug"})
    names = {str(g.get("id") or ""): (str(g.get("name") or "").strip()
                                      or str(g.get("slug") or "").strip())
             for g in gyms}
    out = []
    for t in tokens:
        gid = str(t.get("gym_id") or "").strip()
        key = str(t.get("echo_account_key") or "").strip()
        if gid and key:
            out.append((gid, key, names.get(gid, "")))
    return out


def _default_content_counts():
    """{content_key: row_count} over the whole calendar. One read; the table is small
    enough per gym that counting client-side is cheaper than a query per key."""
    rows = _supabase_get("content_calendar", {"select": "gym_id"})
    counts = {}
    for r in rows:
        k = str(r.get("gym_id") or "").strip()
        if k:
            counts[k] = counts.get(k, 0) + 1
    return counts


def _default_resolver():
    """base -> gym uuid, via the shared store's resolver (the same one every other lane
    uses, so this watch agrees with production about which keys mean which gym). Returns
    a callable; a store that cannot read yields a resolver that always answers None, which
    makes the watch a no-op rather than a guesser."""
    try:
        from .portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
        if not store.available():
            return lambda _base: None
        return store.resolve_gym_uuid
    except Exception:  # noqa: BLE001
        return lambda _base: None


# ---- the pure classifier ----------------------------------------------------------

def build_report(roster, content_counts, resolve_uuid):
    """Pure. Returns [{gym_id, gym_name, portal_key, content_key, portal_rows,
    content_rows, reason, fix}] — one row per SPLIT gym. A healthy gym yields nothing.

    A content key counts as the same gym's ONLY when it resolves to the same gym uuid.
    Resolution is the authority, never a string-prefix guess: two gyms can share a name
    stem, and guessing here would invent a split that does not exist."""
    # Which uuid does each content-owning key belong to? Resolve each key ONCE.
    owner = {}
    for content_key in content_counts:
        try:
            owner[content_key] = resolve_uuid(content_key)
        except Exception:  # noqa: BLE001 - an unresolvable key is simply not attributable
            owner[content_key] = None

    findings = []
    for gym_id, portal_key, gym_name in roster:
        portal_rows = int(content_counts.get(portal_key, 0) or 0)
        # Every OTHER key that resolves to this same gym and actually owns rows.
        others = [(k, n) for k, n in content_counts.items()
                  if k != portal_key and n > 0 and owner.get(k) and str(owner[k]) == gym_id]
        if not others:
            continue                      # no rival key -> not split, whatever the count
        # Report the biggest rival: if a gym somehow has several, the one holding the most
        # content is the one a human needs to look at first.
        others.sort(key=lambda kv: kv[1], reverse=True)
        content_key, content_rows = others[0]
        reason = (REASON_TWO_LIVE_KEYS if portal_rows > 0
                  else REASON_ORPHAN_PORTAL_KEY)
        findings.append({
            "gym_id": gym_id,
            "gym_name": gym_name,
            "portal_key": portal_key,
            "content_key": content_key,
            "portal_rows": portal_rows,
            "content_rows": content_rows,
            "reason": reason,
            "fix": _FIX[reason].format(gym_id=gym_id, portal_key=portal_key,
                                       content_key=content_key, n=content_rows),
        })
    return findings


def _alert_text(f):
    label = f["gym_name"] or f["portal_key"]
    return (f"account-key SPLIT on {label}: the portal records "
            f"{f['portal_key']} ({f['portal_rows']} calendar row(s)) but "
            f"{f['content_key']} holds {f['content_rows']}. {f['fix']}")


# ---- the sweep --------------------------------------------------------------------

def run(roster=None, content_counts=None, resolve_uuid=None, alert=None, db=None,
        today=None):
    """READ-ONLY sweep. Returns {ok, enabled, findings:[...], alerted:[...]}.
    One alert per gym per distinct reason per DAY (kv-stamped), so a nightly re-run on a
    still-split gym never storms the channel. Never raises out."""
    if not enabled():
        return {"ok": True, "enabled": False, "findings": [], "alerted": []}

    if db is None:
        from . import db as db
    if alert is None:
        from .ops_alerts import alert as alert
    roster = _default_portal_roster() if roster is None else roster
    content_counts = _default_content_counts() if content_counts is None else content_counts
    resolve_uuid = _default_resolver() if resolve_uuid is None else resolve_uuid

    findings = build_report(roster, content_counts, resolve_uuid)
    stamp = str(today or date.today())
    alerted = []
    for f in findings:
        key = f"acctkey_split_{f['gym_id']}_{f['reason']}"
        try:
            if (db.kv_get(key) or "") == stamp:
                continue                       # already said this today
        except Exception:  # noqa: BLE001 - a kv fault must not silence a real split
            pass
        try:
            alert(_alert_text(f))
            alerted.append(f["gym_id"])
        except Exception:  # noqa: BLE001 - one alert failure never blocks the rest
            continue
        try:
            db.kv_set(key, stamp)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "enabled": True, "findings": findings, "alerted": alerted}


def print_report(summary, printer=print):
    """Readable CLI table (mirrors account_key_reconcile.print_plan)."""
    if not summary.get("enabled"):
        printer("account-key-split-watch: flag OFF (AGENT_ACCOUNT_KEY_SPLIT_WATCH); "
                "nothing swept.")
        return
    findings = summary.get("findings") or []
    if not findings:
        printer("account-key-split-watch: no split gyms — every portal key owns its own "
                "content.")
        return
    printer(f"account-key-split-watch: {len(findings)} SPLIT gym(s):")
    for f in findings:
        printer(f"  {f['reason']:18} {f['gym_name'] or f['gym_id']}")
        printer(f"    portal key : {f['portal_key']}  ({f['portal_rows']} row(s))")
        printer(f"    content key: {f['content_key']}  ({f['content_rows']} row(s))")
        printer(f"    fix        : {f['fix']}")
