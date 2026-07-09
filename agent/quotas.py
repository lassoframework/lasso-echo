"""
Per-tenant quotas (Stage 2 Part 9): storage cap + monthly recreate budget.

The FIELDS live on tenant.json (written by intake-create with defaults):
    storage_quota_mb          cap on intake storage for the tenant
    monthly_recreate_budget   how many creative re-generations a month may burn

The ENFORCEMENT lives here, pure and offline-testable:
  - storage_quota_bytes(key): the byte cap, or None when the tenant has no
    record (legacy env-token clients keep working uncapped, honestly).
  - over_quota(key, used, incoming): the upload gate intake_web calls. Unknown
    usage (storage that cannot report a total) NEVER blocks an upload; a real
    measured total over the cap does.
  - spend_recreate(key): one unit of the month's recreate budget, kv-counted
    per calendar month. False when the budget is exhausted (the caller must
    then card a human ask instead of burning more). A missing tenant record
    reads as no budget: nothing to spend, False.

Nothing here reads env secrets; quota numbers are tenant data.
"""

from datetime import datetime, timezone

from . import db, tenants


def storage_quota_bytes(tenant_key, base_dir=None):
    """The tenant's storage cap in BYTES, or None (no record = no cap, honest)."""
    rec = tenants.load_tenant(tenant_key, base_dir=base_dir)
    if not rec:
        return None
    try:
        return int(rec.get("storage_quota_mb", 0)) * 1024 * 1024 or None
    except (TypeError, ValueError):
        return None


def over_quota(tenant_key, used_bytes, incoming_bytes, base_dir=None):
    """
    True ONLY when a real measured total would exceed the tenant's cap.
    Unknown usage (used_bytes None) or an uncapped tenant never blocks: quota
    is a resource control, not a safety gate, so it fails open with the
    measurement, never guesses.
    """
    cap = storage_quota_bytes(tenant_key, base_dir=base_dir)
    if cap is None or used_bytes is None:
        return False
    return (int(used_bytes) + int(incoming_bytes)) > cap


def monthly_recreate_budget(tenant_key, base_dir=None):
    """The tenant's monthly recreate budget (0 when no record)."""
    rec = tenants.load_tenant(tenant_key, base_dir=base_dir)
    if not rec:
        return 0
    try:
        return max(0, int(rec.get("monthly_recreate_budget", 0)))
    except (TypeError, ValueError):
        return 0


def _spend_key(tenant_key, now=None):
    month = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
    return f"recreate_spent_{tenant_key}_{month}"


def recreate_spent(tenant_key, now=None):
    try:
        return int(db.kv_get(_spend_key(tenant_key, now)) or 0)
    except ValueError:
        return 0


def spend_recreate(tenant_key, now=None, base_dir=None):
    """
    Burn one unit of this month's recreate budget. Returns True and counts the
    spend, or False when the budget is exhausted (or the tenant has none) —
    the caller must then ask a human instead of burning more.
    """
    budget = monthly_recreate_budget(tenant_key, base_dir=base_dir)
    spent = recreate_spent(tenant_key, now)
    if spent >= budget:
        return False
    db.kv_set(_spend_key(tenant_key, now), str(spent + 1))
    return True
