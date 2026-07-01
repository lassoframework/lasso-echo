"""
30-day reporting assembler. READ-ONLY: it reads insights and assembles a report;
it never posts, edits, comments, or sends anything.

Two honesty rules mirror the rest of Echo:
  - VIEWS, not impressions: engagement rate is engagements / VIEWS (Meta migrated to
    views in April 2025; impressions are not comparable). If views are absent we do
    NOT substitute impressions and we do NOT guess — the rate is None and "views" is
    recorded as a gap.
  - Never guess: any missing metric is flagged in `gaps`, never fabricated.

fetch_insights() is the only read path and returns None while the flag is OFF.
"""

from . import config


def fetch_insights(account, http=None):
    """
    READ-ONLY insights fetch. Returns None when reporting is disabled (no network,
    no read). The real client (Meta Graph, views-based) is wired only when the flag
    is on; until then this is the safe no-op that keeps the report path inert.
    """
    if not config.reporting_enabled():
        return None
    # Real read wiring lands here when armed (Meta Graph, on views). It NEVER writes.
    return None


def _num(v):
    """A finite number passes through; anything else (None/str/NaN) becomes None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    return None


def _rate_on_views(engagements, views):
    """Engagement rate on VIEWS. None when either input is missing or views <= 0."""
    e, v = _num(engagements), _num(views)
    if e is None or v is None or v <= 0:
        return None
    return round(e / v, 4)


def _post_engagement(post):
    """A post's engagement: explicit `engagements`, else the sum of its components."""
    if _num(post.get("engagements")) is not None:
        return _num(post.get("engagements"))
    parts = [_num(post.get(k)) for k in ("saves", "sends", "shares", "likes", "comments")]
    present = [p for p in parts if p is not None]
    return sum(present) if present else None


def health_read(report):
    """
    growing / flat / declining from the assembled report. Uses two signals: follower
    growth rate and the engagement-rate trend (current vs baseline). Positive on both
    (or clearly positive net) -> growing; negative -> declining; mixed / flat -> flat.
    Missing signals simply do not vote (never guessed).
    """
    votes = []
    g = report.get("followers_growth_rate")
    if g is not None:
        votes.append(1 if g > 0.005 else -1 if g < -0.005 else 0)
    er, erb = report.get("engagement_rate"), report.get("engagement_rate_baseline")
    if er is not None and erb is not None:
        votes.append(1 if er > erb else -1 if er < erb else 0)
    if not votes:
        return "flat"
    score = sum(votes)
    if score > 0:
        return "growing"
    if score < 0:
        return "declining"
    return "flat"


def build_report(account_key, current, baseline, posts):
    """
    Assemble the 30-day report from the current window, the baseline window, and the
    per-post list. Every value is real or None; missing inputs are recorded in `gaps`.

    current / baseline: dicts that may carry {followers, views, engagements, posts}.
    posts: list of dicts that may carry {id, views, engagements | saves/likes/...}.
    """
    current = current or {}
    baseline = baseline or {}
    posts = list(posts or [])
    gaps = []

    def need(name, source, key):
        v = _num(source.get(key))
        if v is None:
            gaps.append(name)
        return v

    cur_views = need("views", current, "views")
    cur_eng = need("engagements", current, "engagements")
    cur_followers = need("followers", current, "followers")
    base_followers = need("baseline_followers", baseline, "followers")

    engagement_rate = _rate_on_views(cur_eng, cur_views)
    engagement_rate_baseline = _rate_on_views(baseline.get("engagements"), baseline.get("views"))

    followers_net = (cur_followers - base_followers
                     if cur_followers is not None and base_followers is not None else None)
    followers_growth_rate = (round(followers_net / base_followers, 4)
                             if followers_net is not None and base_followers not in (None, 0) else None)

    # posting frequency: count of posts in each window (current derived from `posts`
    # when the window count is absent).
    posting_freq_current = _num(current.get("posts"))
    if posting_freq_current is None:
        posting_freq_current = len(posts) if posts else None
    posting_freq_baseline = _num(baseline.get("posts"))
    if posting_freq_baseline is None:
        gaps.append("baseline_posts")

    # top / bottom 3 by engagement. Posts with no engagement signal are set aside as a gap.
    ranked, unranked = [], 0
    for p in posts:
        e = _post_engagement(p)
        if e is None:
            unranked += 1
            continue
        ranked.append({
            "id": p.get("id", ""),
            "engagement": e,
            "views": _num(p.get("views")),
            "engagement_rate": _rate_on_views(e, p.get("views")),
        })
    if unranked:
        gaps.append(f"{unranked} post(s) missing engagement")
    ranked_desc = sorted(ranked, key=lambda r: r["engagement"], reverse=True)
    top_posts = ranked_desc[:3]
    bottom_posts = sorted(ranked, key=lambda r: r["engagement"])[:3]

    report = {
        "account_key": account_key,
        "window_days": 30,
        "engagement_rate": engagement_rate,               # on VIEWS, never impressions
        "engagement_rate_baseline": engagement_rate_baseline,
        "followers": cur_followers,
        "followers_net": followers_net,
        "followers_growth_rate": followers_growth_rate,
        "posting_freq_current": posting_freq_current,
        "posting_freq_baseline": posting_freq_baseline,
        "top_posts": top_posts,
        "bottom_posts": bottom_posts,
        "gaps": gaps,
    }
    report["health"] = health_read(report)
    return report
