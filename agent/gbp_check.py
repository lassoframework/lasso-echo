"""
gbp-check: READ-ONLY Google Business Profile probe. RUN BY HAND:

    /opt/venv/bin/python -m agent gbp-check

One honest status line: READY (auth ok, quota above zero) or NOT READY with
the specific reason: quota zero pending Google case 3-8465000040674, auth
failure, wrong project, or configuration missing. Never posts, never writes;
one GET against the configured location. The token is read at call time and
never printed.
"""

import os

from . import config

QUOTA_CASE = "3-8465000040674"


def gbp_check(http=None):
    """Probe and print the one status line. Returns {"ready": bool, "reason": str}."""
    def _out(ready, reason):
        line = "gbp-check: READY" if ready else f"gbp-check: NOT READY: {reason}"
        print(line + (f" ({reason})" if ready and reason else ""))
        return {"ready": ready, "reason": reason}

    token = os.environ.get(config.GBP_TOKEN_ENV)
    if not token:
        return _out(False, f"no token: set {config.GBP_TOKEN_ENV} by hand")
    if not config.GBP_ACCOUNT_ID or not config.GBP_LOCATION_ID:
        return _out(False, "missing AGENT_GBP_ACCOUNT_ID or AGENT_GBP_LOCATION_ID")

    if http is None:
        import requests  # lazy
        http = requests
    url = (f"{config.GBP_API_BASE}/accounts/{config.GBP_ACCOUNT_ID}"
           f"/locations/{config.GBP_LOCATION_ID}")
    try:
        r = http.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    except Exception as e:
        return _out(False, f"request failed: {type(e).__name__}: {e}")

    status = getattr(r, "status_code", 0)
    try:
        body_text = str(r.json())
    except Exception:
        body_text = getattr(r, "text", "") or ""

    if status == 429 or "RESOURCE_EXHAUSTED" in body_text or "Quota exceeded" in body_text:
        return _out(False, f"quota zero, pending Google case {QUOTA_CASE}; "
                           "wait for the quota grant, nothing to fix in code")
    if status == 401:
        return _out(False, "auth failure: the access token was rejected; "
                           "refresh AGENT_GBP_ACCESS_TOKEN")
    if status == 403:
        if "project" in body_text.lower() or "PERMISSION_DENIED" in body_text:
            return _out(False, "wrong project: the API is not enabled for the "
                               "project this token belongs to; check the Google "
                               "Cloud project and API enablement")
        return _out(False, "auth failure: permission denied for this location")
    if status >= 400:
        return _out(False, f"HTTP {status} from the API")
    return _out(True, "auth ok, quota above zero")
