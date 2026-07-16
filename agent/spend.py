"""
Gemini generation spend surface (visibility only, no auto-reload).

This module reads the per-day generation call counters that creative_studio's
spend gate already writes (buckets: "gemini_calls:<account_key>" per account,
and "gemini_calls" for account-less work). It NEVER bumps a counter, NEVER
reloads a balance, and NEVER invents a dollar figure. It only reports what the
store already knows plus the cap state, so Blake can see how close each account
is to the daily generation cap.

Auto-reload is deliberately not built here. Whether to raise the cap or top up
billing is Blake's call, made in the Google Cloud console.

No em dashes, no en dashes, no hyphens in any human-facing copy (colons only).
"""

import os
from datetime import date as _date

from . import db as _db

# The bucket prefix creative_studio uses for the per account generation counter.
_ACCT_PREFIX = "gemini_calls:"
# The account-less (shared) bucket name.
_SHARED_BUCKET = "gemini_calls"

# A bucket at or above this fraction of the cap trips the digest alert.
LOW_CALL_THRESHOLD = 0.80


def _cap():
    return int(os.environ.get("AGENT_GEMINI_DAILY_CAP", "40"))


def _cap_armed():
    from . import config
    return config.spend_cap_enabled()


def _today(day=None):
    return day or _date.today().isoformat()


def _known_account_keys():
    """Active account keys, so a zero call account still shows in the surface."""
    try:
        from .accounts import active_accounts
        return [a.key for a in active_accounts()]
    except Exception:
        return []


def _bucket_calls(day, db_conn=None):
    """Return {bucket_name: calls} for every gemini_calls bucket seen today,
    UNION the active account buckets and the shared bucket at zero so they
    always appear even before the first generation of the day."""
    def _read(conn):
        rows = conn.execute(
            "SELECT name, count FROM counters WHERE day=? AND "
            "(name=? OR name LIKE ?)",
            (day, _SHARED_BUCKET, _ACCT_PREFIX + "%"),
        ).fetchall()
        return {r["name"]: r["count"] for r in rows}

    if db_conn is not None:
        seen = _read(db_conn)
    else:
        with _db.connect() as conn:
            seen = _read(conn)

    # ensure every active account bucket and the shared bucket are present
    for key in _known_account_keys():
        seen.setdefault(_ACCT_PREFIX + key, 0)
    seen.setdefault(_SHARED_BUCKET, 0)
    return seen


def _bucket_label(bucket):
    """Human label for a bucket name. The shared pool has no account key."""
    if bucket == _SHARED_BUCKET:
        return "shared pool"
    if bucket.startswith(_ACCT_PREFIX):
        return bucket[len(_ACCT_PREFIX):]
    return bucket


def spend_snapshot(account_key=None, day=None, db_conn=None):
    """Today's generation call counts for one account or all accounts.

    account_key None means all buckets (every account plus the shared pool).
    A given account_key narrows to just that account's bucket.
    """
    day = _today(day)
    cap = _cap()
    armed = _cap_armed()
    calls = _bucket_calls(day, db_conn=db_conn)

    if account_key is not None:
        target = _ACCT_PREFIX + account_key
        calls = {target: calls.get(target, 0)}

    buckets = []
    for name in sorted(calls):
        n = calls[name]
        pct = round((n / cap) * 100, 1) if cap else 0.0
        buckets.append({"bucket": _bucket_label(name), "calls": n, "pct": pct})

    return {
        "account_key": account_key,
        "day": day,
        "cap": cap,
        "cap_armed": armed,
        "buckets": buckets,
    }


def spend_status_lines(day=None, db_conn=None):
    """Human readable lines for the spend-status CLI. Colons only, no hyphens."""
    snap = spend_snapshot(day=day, db_conn=db_conn)
    cap = snap["cap"]
    lines = [f"Gemini generation spend  ({snap['day']})"]

    if snap["cap_armed"]:
        lines.append(f"  Cap per account: {cap} calls  [ARMED]")
        for b in snap["buckets"]:
            lines.append(
                f"  {b['bucket']:<12} : {b['calls']:>3} / {cap:>3}  ({b['pct']}%)"
            )
    else:
        lines.append("  Cap: NOT ARMED (AGENT_SPEND_CAP_ENABLED not set)")
        for b in snap["buckets"]:
            lines.append(
                f"  {b['bucket']:<12} : {b['calls']:>3} calls today (no cap enforced)"
            )

    lines.append("")
    lines.append("Auto-reload: not configured (by design, this is Blake's call)")
    if snap["cap_armed"]:
        lines.append(
            "Balance projection: check the Google Cloud console "
            "(billing not tracked here)"
        )
    lines.append("To arm cap: set AGENT_SPEND_CAP_ENABLED=true in Railway.")
    return lines


def _alert_kv_key(account_key, day):
    return f"spend_low_alerted:{account_key}:{day}"


def should_alert_spend(account_key, day=None, db_conn=None):
    """True when this account's bucket is at or above LOW_CALL_THRESHOLD of the
    cap AND the cap is armed AND today's alert for it has not fired yet.

    A not armed cap has nothing to alert on, so this returns False. An account
    already alerted today (dedup kv key) also returns False."""
    if not _cap_armed():
        return False
    day = _today(day)
    if _db.kv_get(_alert_kv_key(account_key, day)) == "1":
        return False
    cap = _cap()
    if cap <= 0:
        return False
    calls = _bucket_calls(day, db_conn=db_conn)
    n = calls.get(_ACCT_PREFIX + account_key, 0)
    return n >= LOW_CALL_THRESHOLD * cap


def mark_spend_alerted(account_key, day=None, db_conn=None):
    """Write the dedup kv key so the alert does not fire again today."""
    day = _today(day)
    _db.kv_set(_alert_kv_key(account_key, day), "1")
