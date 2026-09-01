"""
publish_billing_gate.py — stop PUBLISHING for a gym whose subscription is CANCELED.

Blake's ruling (2026-08-25): a gym that cancels must not keep getting posts published
("idk this needs fixed"). Before this, nothing gated the publish lane on entitlement —
a canceled gym kept auto-publishing until a human revoked its token.

Polarity is deliberately OPPOSITE to the portal's is_social_active (which fails CLOSED
to protect a paid VIEW): blocking a PAYING gym's posts on a flaky Stripe read is worse
than a canceled gym posting one more day, so this gate blocks ONLY on POSITIVE evidence
of cancellation and stays open on any doubt:
  - no stripe_customer_id on the gym       -> OPEN (unknown, never guessed)
  - Stripe key missing / read error        -> OPEN
  - customer HAS a subscription in (active, trialing, past_due) [for the social
    product when configured, else any]     -> OPEN
  - customer exists and has NO such subscription -> CANCELED (publishing holds)

Results are kv-cached (default 6h) so the ~1-min publish tick never hammers Stripe.
Behind AGENT_PUBLISH_BILLING_GATE (default OFF). A newly-blocked gym fires ONE ops
alert (deduped until it re-activates). Read-only against Stripe; never touches billing.

THE INERT-GATE PROBLEM (audit 2026-08-31), and why coverage_report exists below.
The fail-open polarity above is correct and deliberate. But it has a consequence nobody
was told about: the gate's whole decision hangs on gyms.stripe_customer_id, and on the
Echo side NOTHING WRITES THAT COLUMN. A gym with no customer id returns _STATE_OK at
`if not customer_id` — the very first branch — so with the flag armed in production the
gate reads as "on", reports no errors, blocks nothing, and can never block anything. It
looks exactly like protection while being a no-op.

That is worse than the gate being off, because an off gate is honest. So this module now
MEASURES its own reach (coverage_report) and SAYS SO once a day (report_inertness). It
still does not wire billing: writing stripe_customer_id is Blake's call, and this file
deliberately stays read-only. The alert exists so the gap can never be mistaken for cover.
"""

import os

from . import config

_CACHE_TTL_SECONDS = 6 * 3600
_STATE_OK = "ok"
_STATE_CANCELED = "canceled"


def gate_enabled() -> bool:
    """AGENT_PUBLISH_BILLING_GATE: hold a canceled gym's publishing. Default OFF."""
    return (os.environ.get("AGENT_PUBLISH_BILLING_GATE", "false") or "").strip().lower() \
        in ("1", "true", "yes", "on")


def _now_epoch(now=None):
    if now is not None:
        return float(now)
    import time as _t
    return _t.time()


def _cached_state(base, now=None):
    """(state, fresh): the cached gate state and whether it is inside the TTL."""
    try:
        from . import db
        state = db.kv_get(f"billgate_{base}") or ""
        ts = float(db.kv_get(f"billgate_ts_{base}") or 0)
        fresh = state in (_STATE_OK, _STATE_CANCELED) and \
            (_now_epoch(now) - ts) < _CACHE_TTL_SECONDS
        return state, fresh
    except Exception:  # noqa: BLE001
        return "", False


def _store_state(base, state, now=None):
    try:
        from . import db
        db.kv_set(f"billgate_{base}", state)
        db.kv_set(f"billgate_ts_{base}", str(_now_epoch(now)))
    except Exception:  # noqa: BLE001
        pass


def _live_state(base, reader=None):
    """One live Stripe read for this gym. Returns _STATE_OK on ANY doubt."""
    try:
        from . import db
        from .portal_social import StripeSocialReader, social_product_id
        row = db.gym_get(base) or {}
        customer_id = (row.get("stripe_customer_id") or "").strip()
        if not customer_id:
            return _STATE_OK                       # unknown customer: never block
        reader = reader or StripeSocialReader()
        if not reader.available():
            return _STATE_OK
        product_id = social_product_id()
        if product_id:
            active = bool(reader.social_active(customer_id, product_id))
        else:
            active = _any_active_subscription(reader, customer_id)
        return _STATE_OK if active else _STATE_CANCELED
    except Exception:  # noqa: BLE001 - a flaky read never blocks a paying gym
        return _STATE_OK


def _any_active_subscription(reader, customer_id):
    """True when the customer has ANY subscription in an active-billing state (used
    when no social product id is configured). Raises up to the caller's fail-open."""
    import stripe
    stripe.api_key = reader._key
    subs = stripe.Subscription.list(customer=customer_id, status="all", limit=100)
    for s in subs.auto_paging_iter():
        if getattr(s, "status", None) in ("active", "trialing", "past_due"):
            return True
    return False


def publishing_blocked(base, reader=None, now=None, alert=None):
    """True when this gym's publishing must HOLD (positive evidence of cancellation).

    kv-cached; fires ONE deduped ops alert when a gym first flips to canceled, and
    clears the dedup when it re-activates so a re-cancellation alerts again."""
    if not gate_enabled():
        return False
    base = (base or "").strip()
    if not base or base.startswith("lasso"):
        return False
    state, fresh = _cached_state(base, now)
    if not fresh:
        state = _live_state(base, reader=reader)
        _store_state(base, state, now)
    if state != _STATE_CANCELED:
        try:
            from . import db
            if db.kv_get(f"billgate_alerted_{base}"):
                db.kv_set(f"billgate_alerted_{base}", "")   # re-armed for a future flip
        except Exception:  # noqa: BLE001
            pass
        return False
    try:
        from . import db, ops_alerts
        if not db.kv_get(f"billgate_alerted_{base}"):
            db.kv_set(f"billgate_alerted_{base}", "1")
            (alert or ops_alerts.alert)(
                f"gym {base}: subscription shows CANCELED in Stripe — publishing is "
                "now HELD (approved posts stay approved, nothing goes live). If this "
                "gym is actually current, fix its Stripe subscription or clear the "
                f"billgate_{base} kv key.")
    except Exception:  # noqa: BLE001
        pass
    return True


# ---- honest self-reporting: how far does this gate actually reach? ----------------

def coverage_report(bases=None, gym_reader=None):
    """What this gate can ACTUALLY protect right now.

    Returns {enabled, total, with_customer, without_customer, uncovered:[base,...]}.
    A gym with no stripe_customer_id is one the gate can never block, because
    _live_state returns OK at its first branch. Pure aside from the injected readers;
    a read failure reports zeros rather than guessing (an unreadable registry is not
    evidence of coverage OR of a gap)."""
    if bases is None:
        try:
            from .calendar_autopublish import client_gym_bases
            bases = client_gym_bases() or []
        except Exception:  # noqa: BLE001
            bases = []
    if gym_reader is None:
        def gym_reader(b):
            try:
                from . import db
                return db.gym_get(b) or {}
            except Exception:  # noqa: BLE001
                return {}

    # LASSO is excluded from the gate itself (publishing_blocked returns False for any
    # lasso* base), so counting it as uncovered would overstate the gap every day.
    checked = [b for b in bases if b and not str(b).startswith("lasso")]
    uncovered = []
    with_customer = 0
    for base in checked:
        cid = str((gym_reader(base) or {}).get("stripe_customer_id") or "").strip()
        if cid:
            with_customer += 1
        else:
            uncovered.append(base)
    return {
        "enabled": gate_enabled(),
        "total": len(checked),
        "with_customer": with_customer,
        "without_customer": len(uncovered),
        "uncovered": uncovered,
    }


def inertness_message(report):
    """The honest one-line state of the gate, or "" when there is nothing to say.

    Speaks up in exactly two cases, both of which are a lie by omission otherwise:
      * the gate is ARMED and covers NOTHING -> it is a total no-op wearing a flag;
      * the gate is ARMED and covers only some gyms -> it protects fewer than it looks.
    A disarmed gate says nothing (an off gate is honest), and full coverage says nothing
    (there is no gap to report)."""
    if not report.get("enabled"):
        return ""
    total = int(report.get("total") or 0)
    missing = int(report.get("without_customer") or 0)
    if total == 0 or missing == 0:
        return ""
    names = ", ".join(sorted(report.get("uncovered") or [])[:12])
    more = "" if missing <= 12 else f" (+{missing - 12} more)"
    scope = "EVERY client gym" if missing == total else f"{missing} of {total} client gyms"
    return (
        f"billing gate is INERT for {scope}: AGENT_PUBLISH_BILLING_GATE is ON, but "
        f"{scope} carry no stripe_customer_id, and the gate returns OK on an unknown "
        "customer by design. It is therefore blocking NOTHING for them and cannot — a "
        "canceled gym in this set would keep publishing. This is a REPORT, not a "
        "request to wire billing (that needs Blake). Uncovered: "
        f"{names}{more}.")


def report_inertness(alert=None, db=None, today=None, report=None):
    """Say the gate's real reach out loud, at most ONCE PER DAY per distinct message.

    Read-only. Never blocks, never writes billing, never touches a customer id. Returns
    {reported: bool, message: str, report: {...}} and never raises out — a self-report
    that could crash the daily run would be worse than the gap it describes."""
    try:
        rep = coverage_report() if report is None else report
        msg = inertness_message(rep)
        if not msg:
            return {"reported": False, "message": "", "report": rep}
        from datetime import date as _date
        stamp = str(today or _date.today())
        if db is None:
            from . import db as db
        # Dedup on the DAY plus the SHAPE of the gap, so the alert re-fires the day the
        # coverage changes rather than staying silent behind yesterday's stamp.
        key = f"billgate_inert_{rep.get('without_customer')}_{rep.get('total')}"
        try:
            if (db.kv_get(key) or "") == stamp:
                return {"reported": False, "message": msg, "report": rep}
        except Exception:  # noqa: BLE001
            pass
        if alert is None:
            from .ops_alerts import alert as alert
        alert(msg)
        try:
            db.kv_set(key, stamp)
        except Exception:  # noqa: BLE001
            pass
        return {"reported": True, "message": msg, "report": rep}
    except Exception as exc:  # noqa: BLE001
        return {"reported": False, "message": "", "error": type(exc).__name__,
                "report": {}}
