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

# The ACCOUNT-level (user insights edge) metric set. VIEWS, not impressions.
# NOTE the Meta asymmetry: the USER edge metric is "saves" (plural) while the
# MEDIA edge metric is "saved" (singular). Do not "fix" one to match the other.
IG_ACCOUNT_METRICS = "views,reach,likes,comments,saves,shares"

# MEDIA-level metric sets, PER MEDIA TYPE, verified against the current Graph
# docs for the pinned version. Feed/reel/story each have DIFFERENT valid sets;
# requesting the wrong set is exactly the 400 this patch kills ("saves" is not
# a media metric, and FB Page posts use a different namespace entirely).
MEDIA_METRICS = {
    ("instagram", "feed"): "views,reach,likes,comments,saved,shares,total_interactions",
    ("instagram", "reel"): "views,reach,likes,comments,saved,shares,total_interactions",
    ("instagram", "story"): "views,reach,replies,shares,navigation",
}
# kept as the feed set for older callers/tests; the builder is the real API
IG_POST_METRICS = MEDIA_METRICS[("instagram", "feed")]

STORY_INSIGHTS_WINDOW_HOURS = 24


class SkipRead(Exception):
    """A graceful skip (not an error): e.g. story insights expired."""


def media_metrics_for(platform, kind):
    """The ONE metric builder both the backfill and the daily snapshot use.
    Returns the metric CSV for IG kinds, or None for Facebook (FB Page posts
    are read via object fields, never the insights metric namespace)."""
    plat = "instagram" if str(platform).lower().startswith("insta") else "facebook"
    if plat == "facebook":
        return None
    return MEDIA_METRICS.get(("instagram", kind), MEDIA_METRICS[("instagram", "feed")])


def media_kind_for_post(post_row):
    """feed | reel | story from the store's publish records: the draft record's
    is_story wins; a video creative reads as a reel; else feed."""
    draft_id = post_row.get("draft_id") or ""
    if draft_id:
        try:
            with db.connect() as conn:
                row = conn.execute("SELECT data FROM drafts WHERE draft_id=?",
                                   (draft_id,)).fetchone()
            if row:
                rec = json.loads(row["data"] or "{}")
                if rec.get("is_story"):
                    return "story"
        except Exception:
            pass
    key = (post_row.get("creative_key") or "").lower()
    if key.endswith((".mp4", ".mov")):
        return "reel"
    return "feed"


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


def fetch_post_metrics(media_id, token, http=None, platform="instagram",
                       kind="feed", published_at=""):
    """
    Per-post metric read, MEDIA-TYPE AWARE (views, never impressions):
      - IG feed/reel/story request only that type's valid metric set
      - a story past its 24h insights window SKIPS gracefully ("story insights
        expired"), never an error
      - FB Page posts skip the insights namespace entirely: likes/comments/
        shares come from object fields the page token always reads
      - a Graph error raises with the honest detail (code/subcode/message,
        scrubbed) plus the missing permission named when it smells like one
    Returns a dict on our column names (the media "saved" maps to our "saves").
    """
    client = http or _requests()
    metrics = media_metrics_for(platform, kind)

    if kind == "story" and published_at:
        try:
            when = datetime.fromisoformat(published_at.replace("+0000", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - when
            if age.total_seconds() > STORY_INSIGHTS_WINDOW_HOURS * 3600:
                raise SkipRead("story insights expired")
        except ValueError:
            pass  # unparseable timestamp: attempt the read

    if metrics is None:
        # Facebook: NODE-TYPE AWARE object-fields read (no insights namespace,
        # and the field "likes" is never requested on any FB node).
        #   pageid_postid (underscore) = a PagePost node: read reactions/
        #   comments/shares directly.
        #   a bare id = a PHOTO/media node (the /photos publish return): first
        #   resolve the OWNING post via page_story_id, then read the post.
        post_id = media_id
        if "_" not in media_id:
            r0 = client.get(f"{config.GRAPH_API_BASE}/{media_id}",
                            params={"fields": "page_story_id",
                                    "access_token": token},
                            timeout=30)
            if getattr(r0, "status_code", 200) >= 400:
                detail = graph_error_detail(r0)
                raise RuntimeError(f"resolving owner of photo {media_id}: {detail}"
                                   + permission_hint(detail, "facebook"))
            post_id = (r0.json() or {}).get("page_story_id") or ""
            if not post_id:
                raise SkipRead("photo node has no owning post to read metrics from")
        r = client.get(f"{config.GRAPH_API_BASE}/{post_id}",
                       params={"fields": "reactions.summary(true),"
                                         "comments.summary(true),shares",
                               "access_token": token},
                       timeout=30)
        if getattr(r, "status_code", 200) >= 400:
            detail = graph_error_detail(r)
            raise RuntimeError(f"reading {post_id}: {detail}"
                               + permission_hint(detail, "facebook"))
        body = r.json() or {}
        return {
            # reactions total (likes plus love/wow etc) lands on our likes
            # column: the honest closest equivalent the node exposes.
            "likes": ((body.get("reactions") or {}).get("summary") or {}).get("total_count"),
            "comments": ((body.get("comments") or {}).get("summary") or {}).get("total_count"),
            "shares": (body.get("shares") or {}).get("count"),
        }

    r = client.get(f"{config.GRAPH_API_BASE}/{media_id}/insights",
                   params={"metric": metrics, "access_token": token},
                   timeout=30)
    if getattr(r, "status_code", 200) >= 400:
        detail = graph_error_detail(r)
        raise RuntimeError(f"reading {media_id}: {detail}"
                           + permission_hint(detail, platform))
    out = {}
    for item in (r.json() or {}).get("data", []):
        name = item.get("name")
        value = (item.get("values") or [{}])[-1].get("value")
        if name:
            out[name] = value
    if "saved" in out:
        out["saves"] = out.pop("saved")  # media metric name -> our column name
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
                    "SELECT id, media_id, draft_id, creative_key, published_at "
                    "FROM posts WHERE account_key=? "
                    "AND mode='published' AND media_id != '' "
                    "AND published_at >= date(?, '-35 day')",
                    (account.key, today)).fetchall()
                for row in rows:
                    try:
                        pm = fetch_post_metrics(
                            row["media_id"], token, http=http,
                            platform=account.platform,
                            kind=media_kind_for_post(dict(row)),
                            published_at=row["published_at"] or "")
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
