"""
billing_customer_sync.py — give the publish billing gate the customer ids it reads.

THE DEFECT (found by the 2026-08-31 capability audit, ruled fixable by Blake):
`agent/publish_billing_gate.py` decides on `gyms.stripe_customer_id` in Echo's own
registry, and NOTHING on the Echo side ever wrote that column. Every gym answered
"unknown customer" at the gate's first branch, so with AGENT_PUBLISH_BILLING_GATE armed
in production the gate protected nothing at all while reporting healthy. The customer ids
exist upstream the whole time, in the portal's `gym_billing` table.

WHAT THIS DOES: copies `gym_billing.stripe_customer_id` (keyed by the portal gym uuid)
onto the matching Echo registry row, so the gate can actually reach Stripe. It is a
one-way READ of the portal plane and a write to ECHO'S OWN column. It never touches a
client's Stripe account, subscription, or billing setup, and it never decides anything:
the gate keeps its own contract (fail OPEN, block only on positive cancellation
evidence).

RAILS:
  * Flag AGENT_BILLING_CUSTOMER_SYNC (default OFF, house rule). Arming it is the
    deliberate act that turns a decorative gate into a real one.
  * Never blanks a stored id: an upstream row with no customer id is reported, not
    written, so a partial portal read can never erase Echo's mapping.
  * Never invents a mapping: a gym whose uuid resolves to no Echo account is skipped
    and named in the report.
  * The shared plane being unavailable is an honest no-op, never a crash: the gate then
    keeps behaving exactly as it does today.
"""
from __future__ import annotations

from .. import config, db as _db, ops_alerts


def _store():
    """The Supabase shared plane, or None when creds are absent (durable-or-skip)."""
    try:
        from ..portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
        return store if store.available() else None
    except Exception:  # noqa: BLE001 - no shared plane is a no-op, never a crash
        return None


def fetch_customer_ids(store):
    """{gym_uuid: stripe_customer_id} for every portal gym that has one. Rows without a
    customer id are omitted (never returned as an empty string, which would blank a
    stored mapping downstream)."""
    r = store._client().get(
        store._rest("gym_billing"),
        params={"select": "gym_id,stripe_customer_id"},
        headers=store._headers(),
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"gym_billing read failed: {r.status_code}")
    out = {}
    for row in (r.json() or []):
        cid = str(row.get("stripe_customer_id") or "").strip()
        gid = str(row.get("gym_id") or "").strip()
        if cid and gid:
            out[gid] = cid
    return out


def run(apply=True, store=None, accounts=None, alert=None):
    """Sync customer ids onto the Echo registry. Returns a summary dict; never raises."""
    alert = alert or ops_alerts.alert
    if not config.billing_customer_sync_enabled():
        return {"ok": True, "skipped": "flag off"}

    store = store or _store()
    if store is None:
        return {"ok": True, "skipped": "shared plane unavailable"}

    try:
        by_uuid = fetch_customer_ids(store)
    except Exception as e:  # noqa: BLE001 - an upstream hiccup leaves the gate as it was
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if accounts is None:
        from .. import accounts as _accounts
        bases = sorted({_base(a) for a in _accounts.all_accounts()})
    else:
        bases = sorted(set(accounts))

    written, already, unmapped, no_customer = [], [], [], []
    for base in bases:
        try:
            gym_uuid = store.resolve_gym_uuid(base)
        except Exception:  # noqa: BLE001
            gym_uuid = None
        if not gym_uuid:
            unmapped.append(base)
            continue
        cid = by_uuid.get(str(gym_uuid))
        if not cid:
            no_customer.append(base)
            continue
        row = _db.gym_get(base) or {}
        current = str(row.get("stripe_customer_id") or "").strip()
        if current == cid:
            already.append(base)
            continue
        if apply:
            _db.gym_upsert(base, display_name=row.get("display_name") or "",
                           stripe_customer_id=cid)
        written.append(base)

    summary = {"ok": True, "apply": apply, "written": len(written),
               "written_gyms": written, "already": len(already),
               "no_customer": no_customer, "unmapped": unmapped}

    # One honest line when the gate's reach CHANGES, so arming this is never silent.
    if written and apply:
        alert(
            f"billing gate: {len(written)} gym(s) now carry a Stripe customer id "
            f"({', '.join(written[:8])}{'...' if len(written) > 8 else ''}), so the "
            "publish billing gate can reach Stripe for them. The gate still fails OPEN "
            "and blocks only on positive cancellation evidence."
        )
    return summary


def _base(account):
    key = getattr(account, "key", "") or ""
    for suf in ("_ig", "_fb"):
        if key.endswith(suf):
            return key[: -len(suf)]
    return key
