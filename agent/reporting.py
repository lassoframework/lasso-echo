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

import os

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
