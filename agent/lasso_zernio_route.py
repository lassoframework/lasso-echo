"""
LASSO-via-Zernio routing choke point (AGENT_LASSO_VIA_ZERNIO), shared by EVERY
lane that publishes LASSO content.

WHY (Blake 2026-08-27): metrics_sync ingests Zernio analytics. A LASSO post that
went out Meta-direct reads there as an EXTERNAL / second publisher and taints
LASSO's own months for the learning loop. Blake's ruling: "want everything under
zernio to keep it simple." So when this flag is ARMED, EVERY LASSO publish path
routes through the SAME Zernio lane the client gyms use (zernio_publisher.publish),
and zero LASSO content is ever sent Meta-direct. One publish path = one guard set.

The calendar-row lane (calendar_autopublish.publish_due) was the first surface
brought under the flag (commit 15b4996). This module is the shared choke point so
the OTHER LASSO publish lanes route the same way:

  * runner._claimed_meta_publish  — the auto-approve / welcome-autopublish /
    trust-autopublish draft lanes (book_queue, book_stories_queue, welcome_queue,
    demo_calendar_queue drafts all publish through here).
  * approvals._publisher_for      — the Slack Approve -> publish path.
  * chat_publish._real_publish_fn — the approve-in-chat -> publish path.

Every one of those routed through meta_publisher.publish before this module and so
was a residual second-publisher surface for LASSO.

HARD RULES (identical to the calendar lane):
  - Flag OFF (default): route() is a no-op and the caller uses meta_publisher.publish,
    byte-for-byte today's routing.
  - Flag ON + LASSO account + setup complete: publish through zernio_publisher.publish,
    the client lane. Stories keep contentType=story / empty body (handled inside
    zernio_publisher). Exactly-once is preserved by the caller's own claim
    (socialapi_claims) PLUS Zernio's 24h content-hash dedup (a 409 reads as published).
  - Flag ON + LASSO account + setup MISSING: HOLD with ONE deduped alert. NEVER fall
    back to Meta-direct (a fallback would recreate the second-publisher taint).
"""

from . import config

# Deduped ops-alert key while the armed LASSO-via-Zernio lane is HELD on incomplete
# setup. Shared across all lanes so the whole cutover speaks with ONE alert, not one
# per lane; re-armed by clear_hold() once the setup completes.
HOLD_KEY = "lasso_zernio_hold_alerted"


def is_lasso_account(account_key):
    """True when this account belongs to the LASSO gym (lasso_ig, lasso_fb, ...).
    Mirrors the calendar lane's account.key.startswith('lasso') test."""
    return str(account_key or "").startswith("lasso")


def missing():
    """The setup pieces the LASSO-via-Zernio lane still needs on the 'lasso' gyms
    row ([] when ready): gyms.zernio_profile_id and gyms.zernio_default_fb_page_id,
    both stamped by `python -m agent lasso-zernio-setup`. Fail-OPEN on a read error
    (returns []): the zernio publisher's own resolvers are the hard backstop — a
    genuinely missing profile/page raises there, the caller holds/reverts and
    retries, and nothing is ever dropped or sent Meta-direct."""
    try:
        from . import db
        row = db.gym_get("lasso") or {}
        out = []
        if not str(row.get("zernio_profile_id") or "").strip():
            out.append("gyms.zernio_profile_id")
        if not str(row.get("zernio_default_fb_page_id") or "").strip():
            out.append("gyms.zernio_default_fb_page_id")
        return out
    except Exception:
        return []


def alert_hold(missing_pieces):
    """ONE deduped ops alert while the armed LASSO-via-Zernio lane is HELD on
    incomplete setup (kv-deduped by HOLD_KEY; re-armed by clear_hold once the setup
    completes, so a future regression alerts again). Best effort; never raises."""
    try:
        from . import db, ops_alerts
        if db.kv_get(HOLD_KEY):
            return
        db.kv_set(HOLD_KEY, "1")
        ops_alerts.alert(
            "AGENT_LASSO_VIA_ZERNIO is armed but LASSO's Zernio setup is incomplete "
            f"(missing: {', '.join(missing_pieces)}). LASSO posts are HELD — nothing "
            "is dropped and nothing falls back to Meta-direct (a fallback would "
            "recreate the second-publisher taint in Zernio analytics). Run: "
            "python -m agent lasso-zernio-setup")
    except Exception:
        pass


def clear_hold():
    """Re-arm the deduped setup-hold alert once LASSO's Zernio setup is complete
    (the state changed). Best effort; never raises."""
    try:
        from . import db
        if db.kv_get(HOLD_KEY):
            db.kv_set(HOLD_KEY, "")
    except Exception:
        pass


def should_route(account_key):
    """True when this account's publish must go through the Zernio lane instead of
    Meta-direct: the flag is ARMED and the account is a LASSO account. Flag OFF or a
    client account -> False (the caller keeps its existing Meta-direct/client route)."""
    return is_lasso_account(account_key) and config.lasso_via_zernio_enabled()


def held(account_key):
    """When the armed LASSO lane cannot yet publish because setup is incomplete,
    return the list of missing pieces (truthy) AFTER firing the ONE deduped alert.
    Returns [] (falsy) when the lane is clear to publish (and re-arms the alert).

    Callers MUST treat a truthy return as HOLD: do not publish, do not fall back to
    Meta-direct, leave the draft/row pending for a retry once setup completes.
    Only call this once should_route() is True."""
    m = missing()
    if m:
        alert_hold(m)
        return m
    clear_hold()
    return []


def publish(draft, account, scheduled_for=None):
    """Route a LASSO draft through the SAME Zernio client lane (zernio_publisher.publish).
    Signature mirrors meta_publisher.publish(draft, account) plus an optional
    scheduled_for so callers can drop this in. Stories, empty-body handling, the
    draft-only flag guards, the 24h content-hash dedup (409 -> published), and the
    HARD failure on a missing profile/account/page all live inside zernio_publisher.

    Caller contract: only call this when should_route(account.key) is True AND
    held(account.key) returned []. This never falls back to Meta-direct."""
    from . import zernio_publisher
    return zernio_publisher.publish(draft, account, scheduled_for=scheduled_for)
