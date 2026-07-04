"""
Monthly report generator (Stage 3): the per-account 30 day cycle report from
snapshots + posts, plus the creative REFRESH proposal (THE PRODUCT).

    /opt/venv/bin/python -m agent monthly-report [--account key]

Assembles from the /data store: engagement rate and raw (on VIEWS, never
impressions), saves, likes, comments, reach, views, follower growth net and
rate, posting frequency before vs after Echo (the capture-baseline file), top
and bottom three posts by engagement, and the health read. Output: one clean
V3-branded HTML file per account under /data/reports/ (no dash characters in
any copy) and a Slack summary line with the top numbers.

The REFRESH section reads real performance by pillar / archetype / set and
proposes three new creative angles drafted ONLY from approved source docs (each
proposal cites its source); plus a plain ask list of raw material to request
from the client. Proposals are TEXT in the report, never auto-created concepts.
Gated by AGENT_REPORTING_ENABLED (OFF = the command reports and does nothing).
"""

import json
import os
from datetime import datetime, timedelta, timezone

from . import config, content_planner, db, reporting, rotation
from .accounts import active_accounts


def _reports_dir():
    return os.environ.get("AGENT_REPORTS_DIR", "/data/reports")


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def gather(account_key, now=None):
    """(snapshots list oldest..newest, posts list) for the trailing 30 days."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=30)).date().isoformat()
    with db.connect() as conn:
        snaps = [
            {"date": r["date"], **json.loads(r["metrics"] or "{}")}
            for r in conn.execute(
                "SELECT date, metrics FROM snapshots WHERE account_key=? "
                "AND date >= ? ORDER BY date", (account_key, since)).fetchall()]
        posts = [dict(r) for r in conn.execute(
            "SELECT * FROM posts WHERE account_key=? AND published_at >= ? "
            "ORDER BY published_at", (account_key, since)).fetchall()]
    return snaps, posts


def assemble(account_key, snaps, posts, baseline_month=None, base_dir=None):
    """The report dict. Missing inputs stay None or land in gaps, never guessed."""
    def total(key):
        vals = [_num(s.get(key)) for s in snaps]
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None

    views, reach = total("views"), total("reach")
    likes, comments = total("likes"), total("comments")
    saves, shares = total("saves"), total("shares")
    engagements = None
    parts = [x for x in (likes, comments, saves, shares) if x is not None]
    if parts:
        engagements = sum(parts)

    followers_first = next((_num(s.get("followers")) for s in snaps
                            if _num(s.get("followers")) is not None), None)
    followers_last = next((_num(s.get("followers")) for s in reversed(snaps)
                           if _num(s.get("followers")) is not None), None)
    follower_net = (followers_last - followers_first
                    if followers_first is not None and followers_last is not None else None)
    follower_rate = (round(follower_net / followers_first, 4)
                     if follower_net is not None and followers_first else None)

    published = [p for p in posts if p.get("mode") == "published"]
    per_post = []
    for p in published:
        eng_parts = [_num(p.get(k)) for k in ("likes", "comments", "saves", "shares")]
        eng_parts = [v for v in eng_parts if v is not None]
        if eng_parts:
            per_post.append({"caption": (p.get("caption") or "")[:70],
                             "permalink": p.get("permalink") or "",
                             "creative_key": p.get("creative_key") or "",
                             "engagement": sum(eng_parts)})
    ranked = sorted(per_post, key=lambda x: x["engagement"], reverse=True)

    month = baseline_month or datetime.now(timezone.utc).strftime("%Y-%m")
    before = reporting.load_posting_baseline(account_key, month, base_dir=base_dir)
    freq_after = round(len(published) / (30 / 7), 2) if published else 0.0

    report = {
        "account_key": account_key,
        "views": views, "reach": reach, "likes": likes, "comments": comments,
        "saves": saves, "shares": shares, "engagements": engagements,
        "engagement_rate": (round(engagements / views, 4)
                            if engagements is not None and views else None),
        "followers": followers_last, "follower_net": follower_net,
        "follower_rate": follower_rate,
        "posts_published": len(published),
        "posting_freq_before": before,           # avg posts per week pre Echo
        "posting_freq_after": freq_after,        # posts per week this cycle
        "top_posts": ranked[:3],
        "bottom_posts": list(reversed(ranked[-3:])) if ranked else [],
        "gaps": [],
    }
    if views is None:
        report["gaps"].append("views (no snapshots landed)")
    if before is None:
        report["gaps"].append("pre Echo baseline file missing")
    # the shared health read: growing / flat / declining
    report["health"] = reporting.health_read({
        "followers_growth_rate": follower_rate,
        "engagement_rate": report["engagement_rate"],
        "engagement_rate_baseline": None,
    })
    return report


def refresh_section(account_key, posts):
    """
    THE PRODUCT: what to refresh next cycle, from real data + approved sources only.
    Returns {performance, proposals, asks}; every proposal cites its source doc.
    """
    perf = {}
    for p in posts:
        if p.get("mode") != "published":
            continue
        eng_parts = [_num(p.get(k)) for k in ("likes", "comments", "saves", "shares")]
        eng_parts = [v for v in eng_parts if v is not None]
        if not eng_parts:
            continue
        eng = sum(eng_parts)
        pillar = rotation.pillar_of(p.get("creative_key") or "")
        for dim, val in (("pillar", pillar),
                         ("archetype", p.get("archetype") or "unknown"),
                         ("set", p.get("set_name") or "unknown")):
            bucket = perf.setdefault(dim, {}).setdefault(val, {"n": 0, "eng": 0})
            bucket["n"] += 1
            bucket["eng"] += eng

    def _rank(dim):
        rows = [(k, v["eng"] / v["n"]) for k, v in perf.get(dim, {}).items() if v["n"]]
        rows.sort(key=lambda x: x[1], reverse=True)
        return rows

    # three proposed angles, ONLY from approved sources, each LABELED by its
    # source: the platform doctrine leads (citation hierarchy: book, then
    # 08_platform_2026.md, then lasso_now), lasso_now still contributes (it
    # stays the home of time sensitive angles).
    proposals = []
    from . import doctrine
    for copy, anchor in doctrine.platform_angles()[:2]:
        proposals.append(f"Angle from {doctrine.PLATFORM_FILE} ({anchor}): {copy}")
    doc = content_planner.load_source_doc()
    if doc is not None:
        for pillar in doc.pillars_with_copy():
            hooks = doc.copy_bank.get(pillar, {}).get("hooks", [])
            if hooks:
                proposals.append(f"Angle from {config.SOURCE_DOC_PATH} pillar "
                                 f"'{pillar}': {hooks[0]}")
            if len(proposals) >= 3:
                break
    from . import knowledge
    if len(proposals) < 3:
        for stat in knowledge.usable_stats()[: 3 - len(proposals)]:
            proposals.append(f"Angle from knowledge USE stat: {stat}")

    asks = [
        "Three recent member photos or short clips with permission to post.",
        "One member win in the member's own words, with permission on record.",
        "A short walkthrough of anything new at the gym this month.",
    ]
    return {"performance": {d: _rank(d) for d in ("pillar", "archetype", "set")},
            "proposals": proposals, "asks": asks}


def _fmt(v):
    return "no data yet" if v is None else f"{v:,}" if isinstance(v, int) else str(v)


def render_html(report, refresh):
    """One clean V3 branded HTML file. No dash characters in any copy."""
    r = report
    rows = "".join(
        f"<tr><td>{label}</td><td>{_fmt(val)}</td></tr>" for label, val in [
            ("Views", r["views"]), ("Reach", r["reach"]), ("Likes", r["likes"]),
            ("Comments", r["comments"]), ("Saves", r["saves"]), ("Shares", r["shares"]),
            ("Engagements", r["engagements"]),
            ("Engagement rate on views", r["engagement_rate"]),
            ("Followers", r["followers"]), ("Follower growth net", r["follower_net"]),
            ("Follower growth rate", r["follower_rate"]),
            ("Posts published", r["posts_published"]),
            ("Posts per week before Echo", r["posting_freq_before"]),
            ("Posts per week this cycle", r["posting_freq_after"]),
            ("Health read", r["health"])])

    def post_list(posts):
        if not posts:
            return "<li>no scored posts yet</li>"
        return "".join(f"<li>{p['engagement']} engagements: {p['caption']}</li>"
                       for p in posts)

    proposals = "".join(f"<li>{p}</li>" for p in refresh["proposals"]) or "<li>none</li>"
    asks = "".join(f"<li>{a}</li>" for a in refresh["asks"])
    perf_bits = []
    for dim, rows_ in refresh["performance"].items():
        if rows_:
            best, worst = rows_[0], rows_[-1]
            perf_bits.append(f"<li>{dim}: strongest {best[0]}, weakest {worst[0]}</li>")
    perf_html = "".join(perf_bits) or "<li>not enough scored posts yet</li>"

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>LASSO 30 day report: {r['account_key']}</title></head>
<body style="font-family:Helvetica,Arial,sans-serif;background:#FAF6F0;color:#121E3C;padding:32px">
<h1 style="color:#121E3C">LASSO 30 day report</h1>
<h2 style="color:#5EB9E6">{r['account_key']}</h2>
<table cellpadding="8" style="background:#FFFFFF;border-radius:8px">{rows}</table>
<h2 style="color:#121E3C">Top posts</h2><ol>{post_list(r['top_posts'])}</ol>
<h2 style="color:#121E3C">Bottom posts</h2><ol>{post_list(r['bottom_posts'])}</ol>
<h2 style="color:#FF0000">Refresh for next cycle</h2>
<ul>{perf_html}</ul>
<h3>Proposed angles (approved sources only)</h3><ul>{proposals}</ul>
<h3>Raw material to request</h3><ul>{asks}</ul>
<p>Gaps: {", ".join(r['gaps']) or "none"}</p>
</body></html>"""


def render_report_pdf(report, refresh, out_path, brand=None):
    """The same 30 day report as a white label PDF (reportlab rebuild)."""
    from .pdf_report import build_pdf
    r = report
    rows = [("Views", _fmt(r["views"])), ("Reach", _fmt(r["reach"])),
            ("Likes", _fmt(r["likes"])), ("Comments", _fmt(r["comments"])),
            ("Saves", _fmt(r["saves"])), ("Shares", _fmt(r["shares"])),
            ("Engagements", _fmt(r["engagements"])),
            ("Engagement rate on views", _fmt(r["engagement_rate"])),
            ("Followers", _fmt(r["followers"])),
            ("Follower growth net", _fmt(r["follower_net"])),
            ("Posts published", _fmt(r["posts_published"])),
            ("Posts per week before Echo", _fmt(r["posting_freq_before"])),
            ("Posts per week this cycle", _fmt(r["posting_freq_after"])),
            ("Health read", _fmt(r["health"]))]
    top = [f"{p['engagement']} engagements: {p['caption']}" for p in r["top_posts"]] or ["no scored posts yet"]
    bottom = [f"{p['engagement']} engagements: {p['caption']}" for p in r["bottom_posts"]] or ["no scored posts yet"]
    perf = []
    for dim, ranked in refresh["performance"].items():
        if ranked:
            perf.append(f"{dim}: strongest {ranked[0][0]}, weakest {ranked[-1][0]}")
    sections = [
        ("heading", "The numbers"), ("table", rows),
        ("heading", "Top posts"), ("list", top),
        ("heading", "Bottom posts"), ("list", bottom),
        ("heading", "Refresh for next cycle"),
        ("list", perf or ["not enough scored posts yet"]),
        ("heading", "Proposed angles (approved sources only)"),
        ("list", refresh["proposals"] or ["none"]),
        ("heading", "Raw material to request"), ("list", refresh["asks"]),
        ("para", "Gaps: " + (", ".join(r["gaps"]) or "none")),
    ]
    return build_pdf(out_path, "30 day report", r["account_key"], sections,
                     brand=brand)


def run(account=None, poster=None, now=None, base_dir=None, pdf=False):
    """Build the monthly report per account. Returns {account: html_path} or None
    while AGENT_REPORTING_ENABLED is OFF."""
    if not config.reporting_enabled():
        print("monthly-report: AGENT_REPORTING_ENABLED is OFF. Nothing built.")
        return None
    out = {}
    accounts = [a for a in active_accounts() if account in (None, a.key)]
    os.makedirs(_reports_dir(), exist_ok=True)
    month = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
    for acct in accounts:
        snaps, posts = gather(acct.key, now=now)
        report = assemble(acct.key, snaps, posts, baseline_month=month,
                          base_dir=base_dir)
        refresh = refresh_section(acct.key, posts)
        html = render_html(report, refresh)
        path = os.path.join(_reports_dir(), f"{acct.key}_{month}.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        out[acct.key] = path
        if pdf:
            from .pdf_report import brand_for
            pdf_path = os.path.join(_reports_dir(), f"{acct.key}_{month}.pdf")
            render_report_pdf(report, refresh, pdf_path, brand=brand_for(acct))
            out[acct.key + ":pdf"] = pdf_path
            print(f"PDF saved: {pdf_path}")
        summary = (f"LASSO 30 day report for {acct.key}: views {_fmt(report['views'])}, "
                   f"engagement rate {_fmt(report['engagement_rate'])}, followers "
                   f"{_fmt(report['followers'])} (net {_fmt(report['follower_net'])}), "
                   f"posts {report['posts_published']}, health {report['health']}. "
                   f"Full report saved: {path}")
        if poster is not None:
            poster.post_notice(summary)
        print(summary)
    return out
