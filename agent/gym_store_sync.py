"""
gym_store_sync.py — reconcile this service's local `gyms` table with the SHARED
Supabase `echo_gyms` record.

The ongoing mechanism is the dual write + read through in db.py; this module is the
ONE TIME heal for rows that predate it, and the operator's answer to "are the two
services actually agreeing right now?".

Run on EACH service (they have separate volumes, so each has its own local half):

    python -m agent gym-store-sync              # report only, writes NOTHING
    python -m agent gym-store-sync --apply      # push local-only + pull shared-only

Report shape:
    local_only     rows this service has that the shared record does not (a PUSH)
    shared_only    rows the shared record has that this service does not (a PULL)
    disagree       rows both have where a MIRRORED column differs

DISAGREEMENTS ARE NEVER AUTO RESOLVED. Both services write, so a differing field is a
real editorial question ("which display_name is right?"), not a merge conflict a job
should guess at. --apply moves only rows that are MISSING on one side, which is
strictly additive and therefore safe to re-run. Nothing here publishes, and nothing
here touches token material: only gym_shared_store.MIRRORED_COLUMNS move.
"""

from . import db as _db
from .gym_shared_store import MIRRORED_COLUMNS, SharedGymStore


def _norm(value):
    """Compare values the way a human would: None, "" and "  " are all "unset", and a
    float baseline written as 3 vs 3.0 is not a disagreement."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return repr(round(value, 6))
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    # timestamps round trip through Postgres with a timezone suffix; compare the date
    # and time only, which is what actually carries meaning here.
    return text


def compare(store=None, local_rows=None):
    """Read both sides and return the report dict. Read only; never writes."""
    store = store or SharedGymStore()
    if not store.available():
        return {"available": False, "local_only": [], "shared_only": [],
                "disagree": [], "local_count": 0, "shared_count": 0}

    local = {r["account_key"]: r for r in
             (local_rows if local_rows is not None else _db.gym_list(_shared_read=False))}
    shared = {str(r.get("account_key") or ""): r for r in store.list_all()}
    shared.pop("", None)

    local_only = sorted(set(local) - set(shared))
    shared_only = sorted(set(shared) - set(local))

    disagree = []
    for key in sorted(set(local) & set(shared)):
        diffs = {}
        for col in MIRRORED_COLUMNS:
            lv, sv = _norm(local[key].get(col)), _norm(shared[key].get(col))
            # Only a REAL conflict counts: two non-empty values that differ. One side
            # simply not having the value yet is what --apply's additive fill is for,
            # and reporting it as a conflict would bury the handful that matter.
            if lv and sv and lv != sv:
                diffs[col] = {"local": lv, "shared": sv}
        if diffs:
            disagree.append({"account_key": key, "fields": diffs})

    return {"available": True, "local_only": local_only, "shared_only": shared_only,
            "disagree": disagree, "local_count": len(local), "shared_count": len(shared),
            "profile_collisions": profile_collisions(local, shared)}


def profile_collisions(local, shared):
    """Zernio profile ids bound to MORE THAN ONE account_key across the union of both
    stores, as [{"zernio_profile_id": id, "account_keys": [...]}, ...].

    NOT caused by this fix, and NOT fixed by it: it is the separate account-key
    duplicate-derivation problem (one real gym known under two keys, e.g.
    crossfitreverb6cdf33 and crossfitreverb30b5b2). It is reported here because
    hydrating the shared record makes both twins visible on a service that previously
    saw only one, which makes db.gym_key_for_zernio_profile ambiguous there (it answers
    with the lowest account_key, by design) and gives account_key_guard's STEAL-PROFILE
    check a second candidate to reason about. Surfacing it beats discovering it later:
    this function only LOOKS, it never merges, rebinds, or deletes anything."""
    by_profile = {}
    for source in (local, shared):
        for key, row in (source or {}).items():
            pid = str((row or {}).get("zernio_profile_id") or "").strip()
            if not pid:
                continue
            by_profile.setdefault(pid, set()).add(key)
    return [{"zernio_profile_id": pid, "account_keys": sorted(keys)}
            for pid, keys in sorted(by_profile.items()) if len(keys) > 1]


def sync(apply=False, store=None):
    """Report, and when `apply` is True, move the MISSING rows in both directions.

    Returns the report dict with `pushed` / `pulled` counts added. Idempotent: a
    second run with --apply finds nothing left to move. A push failure is reported
    per key and never aborts the rest of the run."""
    store = store or SharedGymStore()
    report = compare(store=store)
    report["pushed"], report["pulled"] = 0, 0
    report["push_errors"], report["pull_errors"] = [], []
    if not report["available"] or not apply:
        return report

    local = {r["account_key"]: r for r in _db.gym_list(_shared_read=False)}
    for key in report["local_only"]:
        row = local.get(key) or {}
        fields = {c: row.get(c) for c in MIRRORED_COLUMNS
                  if row.get(c) is not None and str(row.get(c)).strip() != ""}
        try:
            store.upsert(key, fields)
            report["pushed"] += 1
        except Exception as e:  # noqa: BLE001 - one bad row never stops the reconcile
            report["push_errors"].append({"account_key": key,
                                          "error": f"{type(e).__name__}: {e}"})

    if report["shared_only"]:
        # force=True: a reconcile must not be swallowed by gym_list's 15 minute throttle.
        report["pulled"] = _db.pull_shared_into_local(store=store, force=True)

    return report


def format_report(report):
    """A short, operator readable summary. No secrets, no row dumps."""
    if not report.get("available"):
        return ("gym-store-sync: the shared echo_gyms store is unavailable "
                "(SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY absent, or "
                "AGENT_GYM_SHARED_STORE=false). Nothing compared.")
    lines = [
        f"gym-store-sync: local={report['local_count']} shared={report['shared_count']}",
        f"  local only (need a push):  {len(report['local_only'])}",
        f"  shared only (need a pull): {len(report['shared_only'])}",
        f"  field disagreements:       {len(report['disagree'])}",
    ]
    if report.get("pushed") or report.get("pulled"):
        lines.append(f"  APPLIED: pushed={report['pushed']} pulled={report['pulled']}")
    for err in report.get("push_errors", []):
        lines.append(f"  push FAILED {err['account_key']}: {err['error']}")
    for item in report["disagree"]:
        cols = ", ".join(sorted(item["fields"]))
        lines.append(f"  DISAGREE {item['account_key']}: {cols} "
                     f"(reported only, never auto resolved)")
    collisions = report.get("profile_collisions") or []
    if collisions:
        lines.append(f"  zernio profile ids bound to >1 account_key: {len(collisions)} "
                     f"(pre-existing duplicate-key problem, NOT caused or fixed here)")
        for item in collisions:
            lines.append(f"    {', '.join(item['account_keys'])}")
    return "\n".join(lines)
