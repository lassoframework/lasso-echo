"""
Scheduler heartbeat + missed-run alert. NO FLAG: honest observability, always
on, like the audit log. Writes only to the store and (on a miss) one ops alert
line through the existing ops-alerts path.

Every daily draft run writes a heartbeat per account (kv: account + run date +
timestamp). Once per morning the listener checks: an ENABLED account with no
heartbeat by 10:00 ET on a posting day gets ONE alert, debounced to one per
account per day. Skip days and a disarmed agent stay silent.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config, db, ops_alerts, schedule

ALERT_HOUR_ET = 10
_ET = ZoneInfo("America/New_York")


def record_heartbeat(account_key, day_key, now=None):
    """Called by the daily draft run, per account. Idempotent per (account, day)."""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    try:
        db.kv_set(f"heartbeat_{account_key}_{day_key}", ts)
    except Exception as e:
        print(f"[heartbeat] write failed for {account_key}: {type(e).__name__}: {e}")


def heartbeat_at(account_key, day_key):
    return db.kv_get(f"heartbeat_{account_key}_{day_key}", "")


def check_heartbeats(now=None, accounts=None):
    """
    The morning check. Returns the list of account keys alerted (empty when all
    is well or it is not time yet). One alert per account per day, debounced in
    the store so restarts cannot double-alert.
    """
    if not config.master_enabled():
        return []  # a disarmed agent runs nothing; silence is correct
    now = now or datetime.now(timezone.utc)
    now_et = now.astimezone(_ET)
    if now_et.hour < ALERT_HOUR_ET:
        return []  # not deadline time yet
    day_key = now_et.date().isoformat()
    if not schedule.should_post_on(day_key):
        return []  # skip day: no run expected, no alert
    from .accounts import active_accounts
    alerted = []
    for account in (accounts if accounts is not None else active_accounts()):
        if heartbeat_at(account.key, day_key):
            continue  # the run happened; stay silent
        debounce_key = f"hb_alert_{account.key}_{day_key}"
        if db.kv_get(debounce_key):
            continue  # already alerted today
        db.kv_set(debounce_key, now.isoformat())
        ops_alerts.alert(
            f"no daily draft heartbeat for {account.key} by "
            f"{ALERT_HOUR_ET}:00 ET on {day_key}. The scheduled run may have "
            "missed; check the listener.")
        alerted.append(account.key)
    return alerted
