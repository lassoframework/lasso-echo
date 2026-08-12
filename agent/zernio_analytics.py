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


def _post_metric(p, key):
    """One post's analytics[key] as a real number, else None (bools rejected)."""
    a = (p or {}).get("analytics") or {}
    v = a.get(key)
    if isinstance(v, bool):
        return None
    return v if isinstance(v, (int, float)) else None


def _post_caption(p):
    """The post's caption text. Zernio may put it under 'caption' or 'content'.
    Returns a stripped string, or "" when neither is present (never invented)."""
    for key in ("caption", "content"):
        v = (p or {}).get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _post_topic(p):
    """The topic/pillar of a post from Zernio's OWN fields only (never invented).

    Prefers an explicit category/pillar/topic field; returns None when the post carries
    no topic signal. Caption text is NOT parsed into a topic here because that would
    fabricate a category the client never set; a post with only a caption contributes
    no topic. Returns a stripped string or None.
    """
    for key in ("category", "pillar", "topic"):
        v = (p or {}).get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _months_span(days):
    """A window of `days` expressed in months (30-day months). None when days<=0."""
    if not isinstance(days, (int, float)) or days <= 0:
        return None
    return float(days) / 30.0


def _per_month(total, months):
    """total normalized to a per-month rate. None when total is None or months falsy.
    A real 0 total over a real span stays 0 (null-not-zero: only a MISSING total is
    null; a present 0 is a real rate of 0)."""
    if total is None or not months:
        return None
    return round(total / months, 2)


def _echo_cutoff(baseline_at, echo_start=None):
    """The 'Echo start' cutoff that splits the posts history into before vs after.

    Precedence:
      1. An explicit echo_start (aware datetime or ISO string) if given.
      2. baseline_captured_at (the moment Echo's baseline was captured == the moment
         Echo began managing the account).
      3. None when neither is available -> the caller leaves the 'before' legs null
         (we never guess a cutoff).
    Returns an aware datetime or None.
    """
    if echo_start is not None:
        if isinstance(echo_start, datetime):
            return echo_start
        parsed = _parse_iso(echo_start) if isinstance(echo_start, str) else None
        if parsed is not None:
            return parsed
    parsed = _parse_iso(baseline_at) if isinstance(baseline_at, str) else None
    if parsed is not None:
        return parsed
    return None


def _sum_present_range(posts, key, lo, hi):
    """Sum posts[].analytics[key] over present numeric values whose publishedAt is in
    [lo, hi) (lo/hi may be None = open). Gap (no present value in range) -> None."""
    total = 0
    seen = False
    for p in posts:
        ts = _parse_iso((p or {}).get("publishedAt"))
        if ts is None:
            continue
        if lo is not None and ts < lo:
            continue
        if hi is not None and ts >= hi:
            continue
        v = _post_metric(p, key)
        if v is not None:
            total += v
            seen = True
    return total if seen else None


def _months_between(lo, hi):
    """Months (30-day) between lo and hi (both aware). None when either is None or the
    span is non-positive."""
    if lo is None or hi is None:
        return None
    secs = (hi - lo).total_seconds()
    if secs <= 0:
        return None
    return secs / (30.0 * 24 * 3600)


def _before_after(all_posts, in_window, days, cutoff, now):
    """The proof-of-growth block: per-month reach/saves/likes/comments/shares/followers
    before vs after Echo.

    after  = the current per-month rate from the recent IN-WINDOW posts, normalized
             from the window length to a 30-day month.
    before = the average per-month rate over the posts published BEFORE the cutoff
             (the pre-Echo era), computed from the real posts history.

    null-not-zero: any leg with no real data is None (never a fabricated 0). A leg with
    a genuine present-0 total over a real span stays 0. When cutoff is None the 'before'
    era can't be bounded, so every 'before' leg is None.
    """
    months_window = _months_span(days)
    # AFTER-ECHO leg is bounded at the Echo cutoff, NOT the raw window start. For a gym
    # onboarded INSIDE the analysis window (exactly the recent-signup case), the window
    # reaches back before Echo took over; counting those pre-Echo posts in the "after
    # Echo" rate would blend the gym's own old performance into Echo's claimed results.
    # We take the LATER of the window start and the cutoff, and normalize by that real
    # span. When the cutoff is None (no known Echo start) the after leg is the full
    # window (today's behavior).
    after_lo = cutoff
    if cutoff is not None and now is not None:
        # window start = now - days; after era = [max(window_start, cutoff), now]
        from datetime import timedelta as _td
        window_start = now - _td(days=float(days)) if (isinstance(days, (int, float))
                                                       and days > 0) else None
        after_lo = cutoff if window_start is None else max(window_start, cutoff)
    after_posts = ([p for p in in_window if _in_window(p, after_lo)]
                   if after_lo is not None else in_window)
    after_months = (_months_between(after_lo, now)
                    if (after_lo is not None and now is not None) else months_window)

    reach_after = _per_month(_sum_present(after_posts, "reach"), after_months)
    follows_after = _per_month(_sum_present(after_posts, "follows"), after_months)
    saves_after = _per_month(_sum_present(after_posts, "saves"), after_months)
    likes_after = _per_month(_sum_present(after_posts, "likes"), after_months)
    comments_after = _per_month(_sum_present(after_posts, "comments"), after_months)
    shares_after = _per_month(_sum_present(after_posts, "shares"), after_months)

    reach_before = None
    follows_before = None
    saves_before = None
    likes_before = None
    comments_before = None
    shares_before = None
    if cutoff is not None:
        # bound the 'before' era from the earliest pre-cutoff post up to the cutoff.
        stamps = []
        for p in all_posts:
            ts = _parse_iso((p or {}).get("publishedAt"))
            if ts is not None and ts < cutoff:
                stamps.append(ts)
        earliest = min(stamps) if stamps else None
        before_months = _months_between(earliest, cutoff)
        reach_before = _per_month(
            _sum_present_range(all_posts, "reach", None, cutoff), before_months)
        follows_before = _per_month(
            _sum_present_range(all_posts, "follows", None, cutoff), before_months)
        saves_before = _per_month(
            _sum_present_range(all_posts, "saves", None, cutoff), before_months)
        likes_before = _per_month(
            _sum_present_range(all_posts, "likes", None, cutoff), before_months)
        comments_before = _per_month(
            _sum_present_range(all_posts, "comments", None, cutoff), before_months)
        shares_before = _per_month(
            _sum_present_range(all_posts, "shares", None, cutoff), before_months)

    return {
        # followers per month: from per-post analytics.follows (new followers attributed
        # to each post, verified live 2026-08-12) — a REAL windowed series, not a delta
        # derived from the follower total. null-not-zero holds: a leg with no reported
        # follows stays None.
        "followers_per_month": {"before": follows_before, "after": follows_after},
        "reach_per_month": {"before": reach_before, "after": reach_after},
        "saves_per_month": {"before": saves_before, "after": saves_after},
        "likes_per_month": {"before": likes_before, "after": likes_after},
        "comments_per_month": {"before": comments_before, "after": comments_after},
        "shares_per_month": {"before": shares_before, "after": shares_after},
    }


def _learnings(published):
    """What performed best, from the IN-WINDOW published posts' real analytics.

    Returns None (not a shell of zeros) when no published post carries a usable metric,
    so the portal renders "coming soon" until a real month of data lands. best_post =
    max reach; most_saved = max saves; most_shared = max shares. top_topics are the real
    categories/pillars of the top posts; next_month_focus restates them as actions.
    Nothing is invented: a post with no caption contributes an empty caption, a post
    with no topic contributes no topic.
    """
    def _best(key):
        best = None
        best_val = None
        for p in published:
            v = _post_metric(p, key)
            if v is None:
                continue
            if best_val is None or v > best_val:
                best_val = v
                best = p
        return best, best_val

    best_reach_post, best_reach = _best("reach")
    most_saved_post, most_saved = _best("saves")
    most_shared_post, most_shared = _best("shares")

    # No published post carried ANY of reach/saves/shares -> no learnings at all.
    if best_reach_post is None and most_saved_post is None and most_shared_post is None:
        return None

    best_post = None
    if best_reach_post is not None:
        best_post = {
            "caption": _post_caption(best_reach_post),
            "metric_label": "reach",
            "metric_value": best_reach,
        }
    most_saved_out = None
    if most_saved_post is not None:
        most_saved_out = {
            "caption": _post_caption(most_saved_post),
            "metric_value": most_saved,
        }
    most_shared_out = None
    if most_shared_post is not None:
        most_shared_out = {
            "caption": _post_caption(most_shared_post),
            "metric_value": most_shared,
        }

    # top_topics: the real topics of the top posts, de-duplicated in priority order
    # (best reach, then most saved, then most shared). Only real topics; a top post
    # with no topic contributes nothing.
    top_topics = []
    for p in (best_reach_post, most_saved_post, most_shared_post):
        if p is None:
            continue
        topic = _post_topic(p)
        if topic and topic not in top_topics:
            top_topics.append(topic)

    next_month_focus = [f"more {t}" for t in top_topics]

    return {
        "top_topics": top_topics,
        "best_post": best_post,
        "most_saved": most_saved_out,
        "most_shared": most_shared_out,
        "next_month_focus": next_month_focus,
    }


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
                account_key=None, now=None, include_posts=True, echo_start=None):
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
    - before_after: per-month reach/saves/followers before vs after the Echo-start cutoff
      (echo_start else baseline_captured_at); a leg with no real data stays null.
    - learnings: best_post/most_saved/most_shared + top_topics from the in-window
      published posts; None when no published post carries a usable metric.
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
    # NEW FOLLOWERS: Zernio posts carry analytics.follows (per-post new followers,
    # verified live). Sum the window; null when absent everywhere (never 0).
    new_follows = _sum_present(in_window, "follows")

    likes = _sum_present(in_window, "likes")
    comments = _sum_present(in_window, "comments")
    saves = _sum_present(in_window, "saves")
    shares = _sum_present(in_window, "shares")

    # posts_published: a real 0 is honest here (an in-window count, not a summed metric).
    posts_published = len(published)

    # current_posts_per_week: an HONEST whole-number weekly cadence. Count the published
    # posts IN THE ANALYSIS WINDOW, divide by the window length in weeks, and round to the
    # nearest whole integer (an organic gym cadence is a small integer, not 58.33). The old
    # round(...,2) here emitted implausible fractional rates (e.g. 58.33) when the window
    # was too small or an all-time count landed over a short window; the int + plausibility
    # guard below keeps it honest. null-not-zero: too small a window / too little data ->
    # null, and an implausible rate (> 21 posts/week for an organic gym) -> null, never a
    # fabricated number.
    current_ppw = None
    if isinstance(days, (int, float)) and days > 0:
        weeks = float(days) / 7.0
        if weeks > 0:
            rate = int(round(len(published) / weeks))
            current_ppw = rate if rate <= 21 else None

    engagement_rate = _mean_engagement(in_window)

    posts_list = []
    if include_posts:
        posts_list = [_compact_post(p) for p in in_window]

    # before_after (proof of growth) + learnings (what performed best). The Echo-start
    # cutoff comes from echo_start else baseline_captured_at; when neither exists the
    # 'before' legs stay null. learnings is None until a published month of data exists.
    echo_cut = _echo_cutoff(baseline_at, echo_start=echo_start)
    before_after = _before_after(all_posts, in_window, days, echo_cut, now)
    learnings = _learnings(published)

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
            # delta. follower_delta is the sum of in-window posts[].analytics.follows:
            # Zernio reports per-post FOLLOWS (new followers attributed to each post,
            # verified live 2026-08-12), which IS a genuine windowed new-follower count.
            # NULL-not-zero: when no post reports follows it stays null (the portal
            # shows "coming soon"). We still NEVER derive a delta from the total.
            "followers": followers,
            "follower_delta": new_follows,
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
        # proof of growth: per-month before vs after Echo (null legs -> "coming soon")
        "before_after": before_after,
        # what performed best this window; None until a published month of data lands
        "learnings": learnings,
        "gaps": [],
    }
