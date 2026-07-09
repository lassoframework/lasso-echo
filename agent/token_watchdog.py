"""
Token watchdog: warn BEFORE a Meta long-lived token dies, not after posts fail.

OFF BY DEFAULT (`config.token_watchdog_enabled()`). Armed, it runs once per daily
cycle (wired in runner.run_daily) and by hand via `python -m agent check-tokens`.
For each active account with a token set it calls the Graph debug_token endpoint
(a READ) and posts one ops alert when expiry is within config.token_warn_days()
(env AGENT_TOKEN_WARN_DAYS, default 7).

NO SECRETS: the token is read at call time, passed only to the Graph call, and
NEVER printed, logged, returned, or included in an alert. Alerts and the CLI
summary carry only the account key (which credential) and days remaining.
"""

import time

from . import config, ops_alerts
from .accounts import active_accounts


def _requests():
    import requests
    return requests


def _check_one(client, account, now, warn_days, poster):
    """One account's expiry check. Returns a result dict with NO token in it."""
    token = account.get_token()
    if not token:
        return {"account": account.key, "status": "no_token", "days_remaining": None}

    try:
        r = client.get(
            f"{config.GRAPH_API_BASE}/debug_token",
            params={"input_token": token, "access_token": token},
            timeout=30,
        )
        if getattr(r, "status_code", 200) >= 400:
            return {"account": account.key, "status": f"error_http_{r.status_code}",
                    "days_remaining": None}
        data = (r.json() or {}).get("data") or {}
    except Exception as e:
        return {"account": account.key, "status": f"error_{type(e).__name__}",
                "days_remaining": None}

    expires_at = data.get("expires_at") or 0
    if not expires_at:
        # 0 / absent = a token Meta reports as never expiring. Nothing to warn about.
        return {"account": account.key, "status": "never_expires", "days_remaining": None}

    days = int((expires_at - now) // 86400)
    result = {"account": account.key, "status": "ok", "days_remaining": days}
    if days <= warn_days:
        # force=True: this module's own default-OFF flag is the gate, so a warning
        # still lands even when the general ops alerts flag is not armed.
        ops_alerts.alert(
            f"Meta token for {account.key} expires in {days} day(s). "
            "Refresh the long lived token by hand.",
            poster=poster, force=True,
        )
        result["status"] = "expiring_soon"
    return result


def check_tenant_tokens(poster=None, base_dir=None):
    """
    Stage 2 tenant upload tokens (Part 9): for every tenant whose media lanes
    include the upload endpoint, warn when AGENT_INTAKE_TOKEN_<KEY> is not set
    (its texted link would be a dead URL). READS env presence only; the token
    VALUE is never printed, logged, or included in an alert. Returns one result
    dict per upload-lane tenant.
    """
    import os as _os
    from . import tenants
    results = []
    for key in tenants.list_tenants(base_dir=base_dir):
        rec = tenants.load_tenant(key, base_dir=base_dir) or {}
        if "upload" not in (rec.get("media_lanes") or []):
            continue
        present = bool(_os.environ.get(f"AGENT_INTAKE_TOKEN_{key.upper()}"))
        results.append({"tenant": key,
                        "status": "ok" if present else "missing_token"})
        if not present:
            ops_alerts.alert(
                f"upload token for tenant {key} is NOT set "
                f"(AGENT_INTAKE_TOKEN_{key.upper()}): its upload link is dead. "
                "Set the token by hand in env.",
                poster=poster, force=True,
            )
    return results


def check_tokens(http=None, poster=None, accounts=None, now=None, base_dir=None):
    """
    Check every active account's token expiry, plus every upload-lane tenant's
    upload token presence (Part 9). Returns {"status": "disabled"|"checked",
    "results": [...], "tenant_results": [...]}; flag OFF -> disabled, no
    network, no client touched.
    """
    if not config.token_watchdog_enabled():
        return {"status": "disabled", "results": [], "tenant_results": []}

    client = http or _requests()
    now = now if now is not None else time.time()
    warn_days = config.token_warn_days()
    results = [_check_one(client, a, now, warn_days, poster)
               for a in (accounts if accounts is not None else active_accounts())]
    tenant_results = check_tenant_tokens(poster=poster, base_dir=base_dir)
    return {"status": "checked", "results": results,
            "tenant_results": tenant_results}
