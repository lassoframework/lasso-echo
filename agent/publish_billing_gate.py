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
