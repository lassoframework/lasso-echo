"""
Day 30 assembler (readiness Part A): the per account Day 30 narrative, framed
per account so the report can never ship the wrong story.

    python -m agent report --account <key> --dry

FRAMING (declared on the Account record, report_framing):
  frequency  (lasso_fb): the posting cadence story IS the headline: posts per
             week before Echo vs now, with the multiplier (the 28x line).
  engagement (lasso_ig, and the safe default): engagement per post and
             consistency. A frequency comparison NEVER appears in the headline
             metrics or the summary; before vs after posts per week may appear
             ONLY as one internal appendix line flagged "do not publish".

The assembler consumes the BACKFILLED per post insights (the posts table rows
the daily snapshot and backfill-insights fill, VIEWS based, never impressions)
plus the /data snapshots for the follower delta, and produces: engagement
rate, saves, likes, comments, reach, follower delta when available, top 3 and
bottom 3 posts by engagement, and the shared health read. Missing data is a
named gap, never a guess.

READ ONLY: --dry prints the exact text that would card to Slack, watermarked
DRY, and writes NOTHING (no file, no store row, no Slack call). Output copy is
dash free.
"""

import json
from datetime import datetime, timedelta, timezone

from . import db, reporting
from .accounts import get_account

WINDOW_DAYS = 30
DO_NOT_PUBLISH = "INTERNAL APPENDIX, do not publish:"


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def framing_for(account):
    """The account's declared narrative framing; anything unset or unknown is
    the SAFE framing (engagement, no frequency story)."""
    f = (getattr(account, "report_framing", "") or "").strip().lower()
    return f if f in ("frequency", "engagement") else "engagement"


def gather(account_key, now=None):
    """(posts, snapshots) for the trailing window, oldest first. Posts are the
    published rows carrying backfilled insights."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=WINDOW_DAYS)).date().isoformat()
    with db.connect() as conn:
        posts = [dict(r) for r in conn.execute(
            "SELECT * FROM posts WHERE account_key=? AND mode='published' "
            "AND published_at >= ? ORDER BY published_at",
            (account_key, since)).fetchall()]
        snaps = [{"date": r["date"], **json.loads(r["metrics"] or "{}")}
                 for r in conn.execute(
                     "SELECT date, metrics FROM snapshots WHERE account_key=? "
                     "AND date >= ? ORDER BY date",
                     (account_key, since)).fetchall()]
    return posts, snaps


def assemble(account_key, now=None, base_dir=None):
    """The Day 30 report dict from backfilled per post insights + snapshots."""
    posts, snaps = gather(account_key, now=now)
    gaps = []

    def post_engagement(p):
        parts = [_num(p.get(k)) for k in ("likes", "comments", "saves", "shares")]
        parts = [v for v in parts if v is not None]
        return sum(parts) if parts else None

    ranked, unscored = [], 0
    totals = {"likes": 0, "comments": 0, "saves": 0, "shares": 0,
              "views": 0, "reach": 0}
    seen = {k: False for k in totals}
    for p in posts:
        for k in totals:
            v = _num(p.get(k))
            if v is not None:
                totals[k] += v
                seen[k] = True
        e = post_engagement(p)
        if e is None:
            unscored += 1
            continue
        ranked.append({"caption": (p.get("caption") or "")[:70],
                       "media_id": p.get("media_id") or "",
                       "engagement": e, "views": _num(p.get("views"))})
    totals = {k: (v if seen[k] else None) for k, v in totals.items()}
    if unscored:
        gaps.append(f"{unscored} post(s) missing insights")
    for k in ("views", "reach"):
        if totals[k] is None:
            gaps.append(f"{k} (not backfilled)")

    engagements = sum(v for k, v in totals.items()
                      if k in ("likes", "comments", "saves", "shares")
                      and v is not None) if any(
        totals[k] is not None for k in ("likes", "comments", "saves", "shares")) else None
    engagement_rate = (round(engagements / totals["views"], 4)
                       if engagements is not None and totals["views"] else None)

    f_first = next((_num(s.get("followers")) for s in snaps
                    if _num(s.get("followers")) is not None), None)
    f_last = next((_num(s.get("followers")) for s in reversed(snaps)
                   if _num(s.get("followers")) is not None), None)
    follower_delta = (f_last - f_first
                      if f_first is not None and f_last is not None else None)
    if follower_delta is None:
        gaps.append("follower delta (no snapshots)")

    month = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
    freq_before = reporting.load_posting_baseline(account_key, month,
                                                  base_dir=base_dir)
    freq_after = round(len(posts) / (WINDOW_DAYS / 7), 2) if posts else 0.0

    ranked_desc = sorted(ranked, key=lambda r: r["engagement"], reverse=True)
    report = {
        "account_key": account_key,
        "window_days": WINDOW_DAYS,
        "posts_published": len(posts),
        "engagement_rate": engagement_rate,
        "engagements": engagements,
        "likes": totals["likes"], "comments": totals["comments"],
        "saves": totals["saves"], "shares": totals["shares"],
        "views": totals["views"], "reach": totals["reach"],
        "follower_delta": follower_delta,
        "freq_before": freq_before,
        "freq_after": freq_after,
        "top_posts": ranked_desc[:3],
        "bottom_posts": sorted(ranked, key=lambda r: r["engagement"])[:3],
        "gaps": gaps,
    }
    report["health"] = reporting.health_read({
        "followers_growth_rate": (round(follower_delta / f_first, 4)
                                  if follower_delta is not None and f_first
                                  else None),
        "engagement_rate": engagement_rate,
        "engagement_rate_baseline": None,
    })
    return report


def _fmt(v):
    if v is None:
        return "no data"
    if isinstance(v, float):
        return f"{v:g}"
    return f"{v:,}" if isinstance(v, int) else str(v)


def _frequency_headline(r):
    """The FB story: before vs after posts per week with the multiplier."""
    before, after = r["freq_before"], r["freq_after"]
    if before and after:
        mult = round(after / before)
        return (f"HEADLINE: from {_fmt(before)} posts per week to "
                f"{_fmt(after)} per week. {mult}x the publishing cadence "
                "in 30 days.")
    return ("HEADLINE: consistent publishing restored this cycle "
            "(pre Echo baseline file missing; cadence multiplier omitted, "
            "never guessed).")


def render_text(account, report):
    """The exact Day 30 text that would card to Slack, framed per account.
    Dash free by construction and asserted."""
    r = report
    framing = framing_for(account)
    lines = [f"DAY 30 REPORT: {account.display_name} ({r['account_key']})"]
    if framing == "frequency":
        lines.append(_frequency_headline(r))
    else:
        lines.append("HEADLINE: engagement per post and consistency. "
                     f"Engagement rate on views: {_fmt(r['engagement_rate'])}. "
                     f"Health: {r['health']}.")
    lines.append(f"Posts published: {r['posts_published']}. "
                 f"Health read: {r['health']}.")
    lines.append(f"Engagement rate on views: {_fmt(r['engagement_rate'])} "
                 f"({_fmt(r['engagements'])} engagements on {_fmt(r['views'])} views).")
    lines.append(f"Likes {_fmt(r['likes'])}, comments {_fmt(r['comments'])}, "
                 f"saves {_fmt(r['saves'])}, shares {_fmt(r['shares'])}, "
                 f"reach {_fmt(r['reach'])}.")
    lines.append(f"Follower delta: {_fmt(r['follower_delta'])}.")

    def post_lines(label, posts):
        out = [f"{label}:"]
        if not posts:
            out.append("  no scored posts yet")
        for p in posts:
            out.append(f"  {p['engagement']} engagements: {p['caption']}")
        return out

    lines.extend(post_lines("Top 3 posts by engagement", r["top_posts"]))
    lines.extend(post_lines("Bottom 3 posts by engagement", r["bottom_posts"]))
    if r["gaps"]:
        lines.append("Gaps (honest, never guessed): " + "; ".join(r["gaps"]))
    if framing != "frequency":
        # the ONE place frequency may appear for an engagement framed account:
        # an internal appendix line that is never published
        lines.append(f"{DO_NOT_PUBLISH} posts per week before "
                     f"{_fmt(r['freq_before'])} vs after {_fmt(r['freq_after'])}")
    text = "\n".join(lines)
    assert "—" not in text and "–" not in text, "day30 text carries a dash"
    return text


def publishable_text(account, report):
    """The report text with the internal appendix stripped: what may actually
    leave the building for an engagement framed account."""
    text = render_text(account, report)
    return "\n".join(l for l in text.splitlines()
                     if not l.startswith(DO_NOT_PUBLISH))


def report_cli(account_key, dry):
    """python -m agent report --account <key> --dry."""
    acct = get_account(account_key or "")
    if acct is None or not dry:
        print("usage: python -m agent report --account <key> --dry")
        if account_key and acct is None:
            print(f"unknown account: {account_key}")
        return
    report = assemble(acct.key)
    text = render_text(acct, report)
    print("=" * 8 + " DRY: the exact Day 30 text that would card to Slack "
          + "=" * 8)
    print(text)
    print("=" * 8 + " DRY: nothing was written, nothing was posted " + "=" * 8)
