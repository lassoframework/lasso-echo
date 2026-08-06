"""
Pure mapper: Zernio /v1/analytics JSON -> the portal metrics payload SHAPE.

This module is I/O free and unit-testable in isolation. It takes the analytics JSON
(as returned by ZernioClient.analytics / analytics_window), the report window, and the
gym's real baseline (posts per week + captured-at), and folds them into the EXACT dict
shape portal_social._metrics_shape returns, but with REAL values.

NULL-NOT-ZERO IS A HARD RULE. A metric that is missing or None in Zernio stays null in
the output; only values that are actually present are summed. A present 0 is a real 0
and survives. We never coerce a missing metric to 0, and never fabricate a benchmark.
"""

from datetime import datetime, timezone

# Reuse the client's ISO parser so window math matches the pager exactly.
from .zernio import _parse_iso


def _in_window(post, cutoff):
    """True iff the post's publishedAt is at/after cutoff. cutoff None -> all posts in.

    A post with no parseable publishedAt is EXCLUDED from a windowed pull (we cannot
    prove it belongs), which keeps sums honest rather than optimistic.
    """
    if cutoff is None:
        return True
    ts = _parse_iso((post or {}).get("publishedAt"))
    if ts is None:
        return False
    return ts >= cutoff


def _sum_present(posts, key):
    """Sum posts[].analytics[key] over PRESENT numeric values only.

    Returns None when the key is absent/None across every post (a gap, never 0). A
    present 0 counts as a real value, so a genuine all-zero metric returns 0, not None.
    """
    total = 0
    seen = False
    for p in posts:
        a = (p or {}).get("analytics") or {}
        v = a.get(key)
        if isinstance(v, bool):  # guard: bools are ints in Python, never a metric here
            continue
        if isinstance(v, (int, float)):
            total += v
            seen = True
    return total if seen else None


def _mean_engagement(posts):
    """Mean of posts[].analytics.engagementRate over posts that report it, else None."""
    vals = []
    for p in posts:
        a = (p or {}).get("analytics") or {}
        v = a.get("engagementRate")
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            vals.append(v)
    if not vals:
        return None
    return sum(vals) / len(vals)


def _followers(accounts):
    """Sum accounts[].followersCount over NON-null values. All null -> None (never 0).

    FB returns null for followersCount; IG returns an int. If every account is null we
    report null, not a fabricated 0.
    """
    total = 0
    seen = False
    for acct in accounts:
        if not isinstance(acct, dict):
            continue
        v = acct.get("followersCount")
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            total += v
            seen = True
    return total if seen else None


def _post_status(p):
    return (p or {}).get("status") or ""


def _compact_post(p):
    """A small per-post row for the portal list. Only fields Zernio actually returns;
    missing metrics stay null (never 0)."""
    a = (p or {}).get("analytics") or {}

    def _num(key):
        v = a.get(key)
        if isinstance(v, bool):
            return None
        return v if isinstance(v, (int, float)) else None

    ts = _parse_iso(p.get("publishedAt")) if isinstance(p, dict) else None
    day = ts.date().isoformat() if ts is not None else None
    return {
        "day": day,
        "platform": p.get("platform"),
        "likes": _num("likes"),
        "comments": _num("comments"),
        "reach": _num("reach"),
    }


def map_metrics(analytics_json, days, baseline_ppw, baseline_at,
                account_key=None, now=None, include_posts=True):
    """Fold a Zernio analytics JSON into the portal metrics SHAPE with REAL values.

    - analytics_available = hasAnalyticsAccess. data_source = "zernio" when available,
      else None.
    - audience.followers = sum of non-null accounts[].followersCount (all null -> None).
    - audience.reach / impressions = sum of in-window posts[].analytics.{reach,impressions}.
    - totals.{likes,comments,saves,shares} = sum of in-window present values (gap -> None).
    - totals.posts_published = count of in-window posts with status published.
    - frequency: baseline passed through unchanged; current_posts_per_week from the
      in-window published count over days/7 (None when days<=0).
    - engagement_rate (top level) = mean of in-window engagementRate values, else None.
    - benchmark: always None (no trustworthy source; the portal renders "coming soon").
    - gaps: empty when analytics is available (real numbers shown); the honest
      unavailable note is emitted by the caller's null shape when it is not.

    NULL-NOT-ZERO: only present values are summed; a missing metric stays null.
    """
    aj = analytics_json or {}
    available = bool(aj.get("hasAnalyticsAccess"))

    now = now or datetime.now(timezone.utc)
    cutoff = None
    if isinstance(days, (int, float)) and days > 0:
        from datetime import timedelta
        cutoff = now - timedelta(days=float(days))

    all_posts = [p for p in (aj.get("posts") or []) if isinstance(p, dict)]
    in_window = [p for p in all_posts if _in_window(p, cutoff)]
    published = [p for p in in_window if _post_status(p) == "published"]

    followers = _followers(aj.get("accounts") or [])
    reach = _sum_present(in_window, "reach")
    impressions = _sum_present(in_window, "impressions")

    likes = _sum_present(in_window, "likes")
    comments = _sum_present(in_window, "comments")
    saves = _sum_present(in_window, "saves")
    shares = _sum_present(in_window, "shares")

    # posts_published: a real 0 is honest here (an in-window count, not a summed metric).
    posts_published = len(published)

    current_ppw = None
    if isinstance(days, (int, float)) and days > 0:
        weeks = float(days) / 7.0
        current_ppw = round(len(published) / weeks, 2) if weeks > 0 else None

    engagement_rate = _mean_engagement(in_window)

    posts_list = []
    if include_posts:
        posts_list = [_compact_post(p) for p in in_window]

    return {
        "account_key": account_key,
        "window_days": days,
        "analytics_available": available,
        "report_available": None,  # caller overlays the real report flag
        "data_source": "zernio" if available else None,
        # NARRATIVE GATE: Echo emits NO invented prose from metrics. The portal composes
        # any narrative. narrative would only ever carry text when data_source == "zernio"
        # (real numbers); today it stays null because Echo writes no metrics prose at all.
        "narrative": None,
        "posts": posts_list,
        "totals": {
            "posts_published": posts_published,
            "likes": likes,
            "comments": comments,
            "saves": saves,
            "shares": shares,
        },
        "audience": {
            # followers is a TOTAL (sum of accounts[].followersCount), NOT a 30-day
            # delta. follower_delta is a genuine 30-day change; no trustworthy delta
            # exists from a single analytics snapshot, so it stays null (the portal
            # shows "coming soon"). We NEVER derive a delta from the total.
            "followers": followers,
            "follower_delta": None,
            "reach": reach,
            "impressions": impressions,
        },
        "frequency": {
            "baseline_posts_per_week": baseline_ppw,
            "baseline_captured_at": baseline_at,
            "current_posts_per_week": current_ppw,
        },
        "engagement_rate": engagement_rate,
        "benchmark": None,  # no trustworthy source; never fabricated
        "gaps": [],
    }
