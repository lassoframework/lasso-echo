"""
Reporting live path (Stage 3 core), gated by AGENT_REPORTING_ENABLED (OFF).

Daily snapshot job (listener, after the daily draft): per active account, ONE
READ-ONLY Meta Graph insights pull on VIEWS, never impressions (Meta migrated
April 2025; the word impressions appears in no request this module makes), plus
reach, likes, comments, saves, shares, and follower count. Snapshots land in the
snapshots table; recent published posts get their per-post metrics refreshed on
the posts table. A failure posts ONE ops alert line and never crashes the
listener. Tokens are read at call time and never logged.
"""

import json
from datetime import datetime, timezone

from . import config, db, ops_alerts
from .accounts import Platform, active_accounts

# The account-level metric set. VIEWS, not impressions, by design.
IG_ACCOUNT_METRICS = "views,reach,likes,comments,saves,shares"
IG_POST_METRICS = "views,reach,likes,comments,saves,shares"


def graph_error_detail(resp):
    """The HONEST error line for a failed Graph read: code, subcode, type, and
    message from the response body, token scrubbed. A bare "HTTP 400" with no
    reason is banned; this is what every skip line and audit row carries."""
    from . import ops_alerts
    status = getattr(resp, "status_code", "?")
    try:
        err = (resp.json() or {}).get("error", {}) or {}
    except Exception:
        err = {}
    code = err.get("code", "?")
    sub = err.get("error_subcode", "")
    msg = err.get("message", "") or getattr(resp, "text", "")[:200]
    detail = (f"HTTP {status}, code {code}"
              + (f", subcode {sub}" if sub else "")
              + f": {msg}")
    return ops_alerts.scrub(detail)


def permission_hint(detail, platform):
    """When the Graph error smells like a permissions problem, NAME the missing
    permission so the fix is obvious from the terminal."""
    low = (detail or "").lower()
    if ("permission" in low or "oauth" in low or "code 10:" in low
            or "code 200" in low or "code 190" in low or "(#10)" in low):
        from .accounts import Platform
        if platform == Platform.INSTAGRAM or platform == "instagram":
            return " Likely missing permission: instagram_manage_insights."
        return (" Likely missing permission: pages_read_engagement or "
                "read_insights.")
    return ""


def _requests():
    import requests  # lazy
    return requests


def fetch_account_snapshot(account, token, http=None):
    """One account's daily metrics dict from the Graph API (READ ONLY)."""
    client = http or _requests()
    base = config.GRAPH_API_BASE
    target = account.get_target_id()
    out = {}

    if account.platform == Platform.INSTAGRAM:
        r = client.get(f"{base}/{target}/insights",
                       params={"metric": IG_ACCOUNT_METRICS,
                               "metric_type": "total_value", "period": "day",
                               "access_token": token},
                       timeout=30)
        for item in (r.json() or {}).get("data", []):
            name = item.get("name")
            value = ((item.get("total_value") or {}).get("value")
                     if isinstance(item.get("total_value"), dict)
                     else (item.get("values") or [{}])[-1].get("value"))
            if name:
                out[name] = value
        r2 = client.get(f"{base}/{target}",
                        params={"fields": "followers_count", "access_token": token},
                        timeout=30)
        out["followers"] = (r2.json() or {}).get("followers_count")
    else:
        # Facebook Page: the views-era page metrics plus fan count. No impressions.
        r = client.get(f"{base}/{target}/insights",
                       params={"metric": "page_views_total,page_post_engagements",
                               "period": "day", "access_token": token},
                       timeout=30)
        for item in (r.json() or {}).get("data", []):
            name = item.get("name")
            value = (item.get("values") or [{}])[-1].get("value")
            if name:
                out[name] = value
        r2 = client.get(f"{base}/{target}",
                        params={"fields": "followers_count,fan_count",
                                "access_token": token},
                        timeout=30)
        body = r2.json() or {}
        out["followers"] = body.get("followers_count") or body.get("fan_count")
    return out


def fetch_post_metrics(media_id, token, http=None):
    """Per-post insight read (VIEWS, never impressions)."""
    client = http or _requests()
    r = client.get(f"{config.GRAPH_API_BASE}/{media_id}/insights",
                   params={"metric": IG_POST_METRICS, "access_token": token},
                   timeout=30)
    out = {}
    for item in (r.json() or {}).get("data", []):
        name = item.get("name")
        value = (item.get("values") or [{}])[-1].get("value")
        if name:
            out[name] = value
    return out


def snapshot_all(http=None, poster=None, now=None):
    """
    The daily snapshot pass. Returns {account: ok_bool} or None while
    AGENT_REPORTING_ENABLED is OFF. One ops alert per failed account, never a crash.
    """
    if not config.reporting_enabled():
        return None
    today = (now or datetime.now(timezone.utc)).date().isoformat()
    results = {}
    for account in active_accounts():
        token = account.get_token()
        if not token:
            results[account.key] = False
            continue
        try:
            metrics = fetch_account_snapshot(account, token, http=http)
            with db.connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO snapshots (account_key, date, metrics) "
                    "VALUES (?,?,?)",
                    (account.key, today, json.dumps(metrics)))
                # refresh per-post metrics for this account's recent published posts
                rows = conn.execute(
                    "SELECT id, media_id FROM posts WHERE account_key=? "
                    "AND mode='published' AND media_id != '' "
                    "AND published_at >= date(?, '-35 day')",
                    (account.key, today)).fetchall()
                for row in rows:
                    try:
                        pm = fetch_post_metrics(row["media_id"], token, http=http)
                        conn.execute(
                            "UPDATE posts SET likes=?, comments=?, saves=?, "
                            "shares=?, views=?, reach=? WHERE id=?",
                            (pm.get("likes"), pm.get("comments"), pm.get("saves"),
                             pm.get("shares"), pm.get("views"), pm.get("reach"),
                             row["id"]))
                    except Exception as e:
                        # one bad post read never sinks the snapshot, but it is
                        # never silent either: the WHY prints and lands in audit.
                        reason = ops_alerts.scrub(str(e))
                        print(f"[reporting] post read skipped "
                              f"{row['media_id']}: {reason}")
                        db.audit("insights_skip", row["media_id"], reason,
                                 account.key, today)
                conn.commit()
            results[account.key] = True
        except Exception as e:
            results[account.key] = False
            ops_alerts.alert(f"reporting snapshot failed for {account.key}: "
                             f"{type(e).__name__}: {e}")
    return results
