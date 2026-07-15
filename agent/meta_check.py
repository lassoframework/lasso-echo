"""
Meta asset verification: token validity, scopes, target reachability, and
publishable status for every active account.

NO SECRETS SURFACE HERE. Token values are passed only in query params or
Authorization headers to the Graph API. They are never logged, printed, or
returned in any result dict.

Run via:
  python -m agent meta-check
  python -m agent meta-check --account lasso_ig
"""

import os

from . import config
from .accounts import active_accounts, get_account
from .accounts import Platform


def _requests():
    import requests
    return requests


def check_account(account, http=None):
    """
    Verify one account against the Meta Graph API.

    Returns:
        {
            "account": key,
            "ready": bool,
            "checks": [{"name": str, "status": "pass"|"fail"|"warn"|"skip", "detail": str}],
            "missing": [str],   # names of failed checks
        }

    Checks run in order; token absence short-circuits all network checks.
    Token values are NEVER included in the result.
    """
    client = http or _requests()
    checks = []
    base = config.GRAPH_API_BASE
    platform = account.platform

    def _check(name, status, detail=""):
        checks.append({"name": name, "status": status, "detail": detail})

    def _skip_remaining(names):
        for n in names:
            _check(n, "skip")

    # a. token_set
    token = account.get_token()
    if not token:
        _check("token_set", "fail", "token env not set")
        _skip_remaining(["token_valid", "scopes", "target_reachable",
                         "business_connected", "publishable"])
        return _result(account.key, checks)

    _check("token_set", "pass", "set")

    # Determine access token for debug_token call.
    # Use app_id|app_secret when both are present (proper validation).
    app_id = os.environ.get("AGENT_META_APP_ID", "")
    app_secret = os.environ.get("AGENT_META_APP_SECRET", "")
    if app_id and app_secret:
        access_token_for_debug = f"{app_id}|{app_secret}"
    else:
        access_token_for_debug = token  # self-validation, less reliable

    # b. token_valid
    debug_data = {}
    try:
        r = client.get(
            f"{base}/debug_token",
            params={"input_token": token, "access_token": access_token_for_debug},
            timeout=30,
        )
        body = r.json() if callable(getattr(r, "json", None)) else {}
        debug_data = (body or {}).get("data") or {}
        is_valid = bool(debug_data.get("is_valid"))
        if is_valid:
            _check("token_valid", "pass")
        else:
            error = (debug_data.get("error") or {}).get("message", "is_valid=false")
            _check("token_valid", "fail", error)
            _skip_remaining(["scopes", "target_reachable", "business_connected",
                             "publishable"])
            return _result(account.key, checks)
    except Exception as exc:
        _check("token_valid", "fail", str(exc))
        _skip_remaining(["scopes", "target_reachable", "business_connected",
                         "publishable"])
        return _result(account.key, checks)

    # c. scopes
    scopes = debug_data.get("scopes") or []
    if not scopes:
        _check("scopes", "warn", "scopes not returned (self-validation may omit them)")
    else:
        if platform == Platform.INSTAGRAM:
            required = {"instagram_basic", "pages_read_engagement"}
        else:
            required = {"pages_manage_posts", "pages_read_engagement"}
        missing_scopes = required - set(scopes)
        if missing_scopes:
            _check("scopes", "fail",
                   f"missing required scopes: {', '.join(sorted(missing_scopes))}")
        else:
            _check("scopes", "pass")

    # d. target_reachable
    target_id = account.get_target_id()
    if not target_id:
        _check("target_reachable", "fail", "target id env not set")
        _skip_remaining(["business_connected", "publishable"])
        return _result(account.key, checks)

    try:
        r = client.get(
            f"{base}/{target_id}",
            params={"fields": "id,name", "access_token": token},
            timeout=30,
        )
        status_code = getattr(r, "status_code", 200)
        body = r.json() if callable(getattr(r, "json", None)) else {}
        if status_code < 400 and (body or {}).get("id") == str(target_id):
            _check("target_reachable", "pass")
        elif status_code < 400 and (body or {}).get("id"):
            # id present but did not match exactly
            _check("target_reachable", "fail",
                   f"id mismatch: expected {target_id}, got {body.get('id')}")
        else:
            error = ((body or {}).get("error") or {}).get("message",
                                                           f"HTTP {status_code}")
            _check("target_reachable", "fail", error)
    except Exception as exc:
        _check("target_reachable", "fail", str(exc))
        _skip_remaining(["business_connected", "publishable"])
        return _result(account.key, checks)

    # e. business_connected
    try:
        r = client.get(
            f"{base}/{target_id}",
            params={"fields": "business", "access_token": token},
            timeout=30,
        )
        body = r.json() if callable(getattr(r, "json", None)) else {}
        if (body or {}).get("business"):
            _check("business_connected", "pass")
        else:
            _check("business_connected", "warn",
                   "business field absent (not required but expected)")
    except Exception as exc:
        _check("business_connected", "fail", str(exc))

    # f. publishable
    try:
        if platform == Platform.INSTAGRAM:
            r = client.get(
                f"{base}/{target_id}",
                params={"fields": "username,is_business_account",
                        "access_token": token},
                timeout=30,
            )
            body = r.json() if callable(getattr(r, "json", None)) else {}
            is_biz = (body or {}).get("is_business_account")
            if is_biz is True:
                _check("publishable", "pass")
            elif is_biz is False:
                _check("publishable", "fail",
                       "is_business_account=false; account cannot publish via API")
            else:
                _check("publishable", "warn",
                       "is_business_account field absent in response")
        else:
            # Facebook Page
            r = client.get(
                f"{base}/{target_id}",
                params={"fields": "can_post", "access_token": token},
                timeout=30,
            )
            body = r.json() if callable(getattr(r, "json", None)) else {}
            can_post = (body or {}).get("can_post")
            if can_post is True:
                _check("publishable", "pass")
            elif can_post is False:
                _check("publishable", "fail", "can_post=false for this Page")
            else:
                _check("publishable", "warn",
                       "can_post field absent in response")
    except Exception as exc:
        _check("publishable", "fail", str(exc))

    return _result(account.key, checks)


def _result(key, checks):
    """Build the final result dict from the accumulated checks list."""
    missing = [c["name"] for c in checks if c["status"] == "fail"]
    ready = not missing
    return {"account": key, "ready": ready, "checks": checks, "missing": missing}


def check_all(http=None, accounts=None):
    """
    Run check_account for all active accounts (or the subset in accounts list).

    Returns list of result dicts (one per account).
    """
    targets = accounts if accounts is not None else active_accounts()
    return [check_account(a, http=http) for a in targets]
