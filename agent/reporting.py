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

import json
import os
from datetime import datetime, timezone

from . import config


def fetch_insights(account, http=None):
    """
    READ-ONLY insights fetch. Returns None when reporting is disabled (no network,
    no read). The real client (Meta Graph, views-based) is wired only when the flag
    is on; until then this is the safe no-op that keeps the report path inert.

    Returns dict:
      {
        "current": {"followers": N, "views": sum_media_views,
                    "engagements": sum_likes+comments, "posts": count},
        "posts": [{"id": .., "views": N, "like_count": N,
                   "comments_count": N, "saved": N, "reach": N}, ...]
      }
    or None on flag-off / missing credentials / network error.
    """
    if not config.reporting_enabled():
        return None

    # Credentials are read lazily; a missing env var is not an error -- we
    # return None gracefully so the caller never crashes on unconfigured accounts.
    token = account.get_token() if callable(getattr(account, "get_token", None)) else getattr(account, "token", None)
    user_id = account.get_target_id() if callable(getattr(account, "get_target_id", None)) else getattr(account, "target_id", None)

    if not token or not user_id:
        return None

    if http is None:
        import requests as http  # noqa: PLC0415

    base = config.GRAPH_API_BASE

    try:
        # Fetch recent media with views-era fields. media_views is VIEWS.
        media_url = (
            f"{base}/{user_id}/media"
            f"?fields=id,timestamp,like_count,comments_count,saved,reach,media_views"
            f"&limit=100"
            f"&access_token={token}"
        )
        media_resp = http.get(media_url, timeout=20)
        media_resp.raise_for_status()
        media_data = media_resp.json().get("data", [])

        # Fetch follower count.
        profile_url = (
            f"{base}/{user_id}"
            f"?fields=followers_count"
            f"&access_token={token}"
        )
        profile_resp = http.get(profile_url, timeout=20)
        profile_resp.raise_for_status()
        followers = profile_resp.json().get("followers_count")

    except Exception:
        return None

    posts_out = []
    total_views = 0
    total_engagements = 0
    for item in media_data:
        views = item.get("media_views")
        likes = item.get("like_count", 0) or 0
        comments = item.get("comments_count", 0) or 0
        saved = item.get("saved", 0) or 0
        reach = item.get("reach", 0) or 0
        posts_out.append({
            "id": item.get("id", ""),
            "views": views,
            "like_count": likes,
            "comments_count": comments,
            "saved": saved,
            "reach": reach,
        })
        if views is not None:
            total_views += views
        total_engagements += likes + comments

    return {
        "current": {
            "followers": followers,
            "views": total_views,
            "engagements": total_engagements,
            "posts": len(posts_out),
        },
        "posts": posts_out,
    }


def take_daily_snapshot(account_key, now=None, http=None):
    """
    Fetch live insights for account_key and persist a row in the snapshots table.
    Returns the current metrics dict or None when fetch returns nothing (flag off,
    credentials missing, network error). Idempotent: INSERT OR REPLACE on
    (account_key, date).
    """
    from . import accounts as _accounts, db as _db  # noqa: PLC0415

    account = _accounts.get_account(account_key)
    if account is None:
        return None

    result = fetch_insights(account, http=http)
    if result is None:
        return None

    today = (now or datetime.now(timezone.utc)).date().isoformat()
    current = result["current"]

    with _db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO snapshots (account_key, date, metrics) "
            "VALUES (?, ?, ?)",
            (account_key, today, json.dumps(current)),
        )
        conn.commit()

    return current


def render_report(report):
    """
    Render a build_report() dict to a human-readable multi-line string.

    Rules:
      - No em dashes, en dashes, or hyphens in any output line.
      - Negative numbers are written as "down N" not "-N".
      - Missing metrics emit a "Data gap: <name>" line instead of raising.
    """

    def _v(val, *, fmt=None):
        """Format a value or return 'no data'."""
        if val is None:
            return "no data"
        if fmt == "pct" and isinstance(val, (int, float)):
            return f"{round(val * 100, 2)}%"
        if fmt == "signed" and isinstance(val, (int, float)):
            if val > 0:
                return f"up {val:,}"
            if val < 0:
                return f"down {abs(val):,}"
            return "flat"
        if isinstance(val, float):
            return f"{val:g}"
        if isinstance(val, int):
            return f"{val:,}"
        return str(val)

    lines = []
    key = report.get("account_key", "")
    health = report.get("health", "unknown")
    lines.append(f"ECHO REPORT {key} 30d health: {health}")

    er = report.get("engagement_rate")
    if er is not None:
        lines.append(f"Engagement rate: {_v(er, fmt='pct')} (on views)")
    else:
        lines.append("Data gap: views")

    followers_net = report.get("followers_net")
    followers_growth_rate = report.get("followers_growth_rate")
    if followers_net is not None:
        growth_str = _v(followers_net, fmt="signed")
        if followers_growth_rate is not None:
            growth_pct = _v(followers_growth_rate, fmt="pct")
            lines.append(f"Follower change: {growth_str} ({growth_pct})")
        else:
            lines.append(f"Follower change: {growth_str}")
    else:
        lines.append("Data gap: followers")

    freq = report.get("posting_freq_current")
    freq_base = report.get("posting_freq_baseline")
    if freq is not None:
        lines.append(f"Posts this period: {_v(freq)}")
    if freq_base is not None:
        lines.append(f"Posts baseline: {_v(freq_base)}")

    top = report.get("top_posts") or []
    if top:
        ids = " ".join(p.get("id", "") for p in top if p.get("id"))
        lines.append(f"Top posts: {ids}")

    bottom = report.get("bottom_posts") or []
    if bottom:
        ids = " ".join(p.get("id", "") for p in bottom if p.get("id"))
        lines.append(f"Bottom posts: {ids}")

    gaps = report.get("gaps") or []
    for g in gaps:
        clean = g.replace("-", " ").replace("–", " ").replace("—", " ")
        lines.append(f"Data gap: {clean}")

    text = "\n".join(lines)
    # Hard assertion: the standing law forbids dashes in marketing copy.
    assert "—" not in text and "–" not in text, \
        "render_report produced a dash character"
    return text


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


# =============================================================================
# Social Grade v1 (flag AGENT_GRADE_ENABLED, default OFF). Rubric documented in
# docs/SOCIAL_GRADE.md. HONEST GRADES: a missing input never fakes a score; the
# subscore is None, listed in grade["gaps"], and simply does not vote.
# =============================================================================

def _letter(score):
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def load_posting_baseline(account_key, month, base_dir=None):
    """avg_posts_per_week from /data/baseline_<month>.json when present, else None.
    The baseline file is written by `capture-baseline` (agent/baseline.py)."""
    import json
    base_dir = base_dir or os.environ.get("AGENT_BASELINE_DIR", "/data")
    try:
        with open(os.path.join(base_dir, f"baseline_{month}.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return (data.get("accounts", {}).get(account_key, {}) or {}).get("avg_posts_per_week")
    except Exception:
        return None


def compute_grade(report, planned_posts=None, pillar_counts=None, proof_posts=None,
                  baseline_month="2026-07", base_dir=None):
    """
    The per-account Social Grade: letter + subscores (each 0 to 100 or None).
    Returns None while AGENT_GRADE_ENABLED is OFF. Subscores:
      consistency: published vs planned posts
      mix:         balance across content pillars (evenness of pillar_counts)
      engagement:  engagement rate trend vs its baseline window
      growth:      follower growth rate
      proof:       verified social proof used in the window
    """
    if not config.grade_enabled():
        return None

    subs, gaps = {}, []

    published = _num(report.get("posting_freq_current"))
    if planned_posts and published is not None:
        subs["consistency"] = round(min(1.0, published / planned_posts) * 100)
    else:
        subs["consistency"] = None
        gaps.append("consistency (planned or published count missing)")

    counts = [c for c in (pillar_counts or {}).values() if _num(c) is not None]
    if counts and sum(counts) > 0:
        if len(counts) == 1:
            subs["mix"] = 40  # one pillar only: on-message but not a balanced mix
        else:
            subs["mix"] = round((min(counts) / max(counts)) * 100) if max(counts) else None
    else:
        subs["mix"] = None
        gaps.append("mix (no pillar counts)")

    er, erb = report.get("engagement_rate"), report.get("engagement_rate_baseline")
    if er is not None and erb is not None:
        if erb <= 0:
            subs["engagement"] = 70 if er <= 0 else 90
        else:
            change = (er - erb) / erb
            subs["engagement"] = 90 if change > 0.05 else 70 if change >= -0.05 else 40
    else:
        subs["engagement"] = None
        gaps.append("engagement (rate or baseline missing)")

    g = report.get("followers_growth_rate")
    if g is not None:
        subs["growth"] = 90 if g > 0.02 else 70 if g >= 0 else 40
    else:
        subs["growth"] = None
        gaps.append("growth (follower data missing)")

    if proof_posts is None:
        subs["proof"] = None
        gaps.append("proof (usage not tracked in window)")
    else:
        subs["proof"] = 100 if proof_posts >= 1 else 40

    votes = [v for v in subs.values() if v is not None]
    if not votes:
        return {"letter": None, "score": None, "subscores": subs, "gaps": gaps,
                "posting_freq_before": None, "posting_freq_after": published}

    score = round(sum(votes) / len(votes))
    before = load_posting_baseline(report.get("account_key", ""), baseline_month,
                                   base_dir=base_dir)
    if before is None:
        gaps.append(f"baseline_{baseline_month}.json not present")
    return {
        "letter": _letter(score),
        "score": score,
        "subscores": subs,
        "gaps": gaps,
        # before/after posting frequency (before = pre-Echo baseline capture)
        "posting_freq_before": before,
        "posting_freq_after": published,
    }


def grade_summary_line(account_key, grade):
    """The one-line Slack summary. Honest: gaps shown, absent subscores marked '-'."""
    if not grade or grade.get("letter") is None:
        return f"GRADE {account_key}: not enough data to grade honestly"
    s = grade["subscores"]

    def fmt(k):
        return str(s[k]) if s.get(k) is not None else "-"

    line = (f"GRADE {account_key}: {grade['letter']} ({grade['score']}) "
            f"consistency {fmt('consistency')}, mix {fmt('mix')}, "
            f"engagement {fmt('engagement')}, growth {fmt('growth')}, proof {fmt('proof')}")
    if grade.get("posting_freq_before") is not None and grade.get("posting_freq_after") is not None:
        line += (f" | posts/wk {grade['posting_freq_before']} before -> "
                 f"{grade['posting_freq_after']} now")
    return line
