"""
The monthly review loop (THE PRODUCT). Flag: AGENT_MONTHLY_REVIEW_ENABLED (OFF).

    /opt/venv/bin/python -m agent monthly-review [--account X] [--dry]

Per account, per 30 day cycle, from the store's per-post metrics:
  1. top 3 and bottom 3 posts by engagement
  2. health read growing / flat / declining (the same thresholds as reporting)
  3. posting frequency before vs after Echo (the capture-baseline file)
  4. proposed creative angles for next cycle citing ONLY approved sources and
     existing winners; a proposal that would need an unapproved claim is
     DROPPED WITH A REASON, never invented
  5. a client-facing raw material ask listing the specific gaps

Output: one Slack digest card to the approval channel plus a white label PDF
through the existing reportlab path. --dry prints everything and posts nothing.
No dash characters in any client-facing line.
"""

import os
from datetime import datetime, timezone

from . import config, content_planner, monthly_report, reporting, rotation
from .accounts import active_accounts
from .pdf_report import brand_for, build_pdf


def gated_proposals(posts):
    """
    (proposals, dropped): angles for next cycle from approved sources + existing
    winners. THE CITATION GATE: a winner whose caption carries a claim not
    cleared in the approved sources is dropped with the reason named. Nothing
    is ever invented to fill the list.
    """
    proposals, dropped = [], []
    approved_claims = rotation._approved_claims()

    # existing winners first: the top-engagement posts become "more like this"
    scored = []
    for p in posts:
        if p.get("mode") != "published":
            continue
        parts = [p.get(k) for k in ("likes", "comments", "saves", "shares")]
        parts = [v for v in parts if isinstance(v, (int, float))]
        if parts:
            scored.append((sum(parts), p))
    scored.sort(key=lambda x: x[0], reverse=True)
    for eng, p in scored[:3]:
        caption = (p.get("caption") or "")[:80]
        pillar = rotation.pillar_of(p.get("creative_key") or "")
        if not rotation.is_gate_clean(p.get("caption") or "", approved_claims):
            dropped.append(f"winner {caption!r} dropped: its caption carries a "
                           "claim not cleared in the approved sources")
            continue
        proposals.append(f"More of pillar {pillar} (a proven winner, "
                         f"{eng} engagements): {caption}")

    # then fresh angles straight from the approved source doc, cited
    doc = content_planner.load_source_doc()
    if doc is not None:
        for pillar in doc.pillars_with_copy():
            hooks = doc.copy_bank.get(pillar, {}).get("hooks", [])
            if hooks and len(proposals) < 5:
                proposals.append(f"Angle from {config.SOURCE_DOC_PATH} pillar "
                                 f"{pillar}: {hooks[0]}")
    return proposals, dropped


def raw_material_asks(report):
    """The client-facing ask list, tied to the SPECIFIC gaps in the data."""
    asks = ["Three recent member photos or short clips with permission to post.",
            "One member win in the member's own words, with permission on record."]
    for gap in report.get("gaps", []):
        if "baseline" in gap:
            asks.append("Nothing needed from you here; we will capture the "
                        "posting baseline on our side.")
        elif "views" in gap:
            asks.append("No metrics landed this cycle; nothing needed from you, "
                        "we are wiring the reporting reads.")
    return asks


def digest_lines(report, proposals, dropped, asks):
    """The Slack digest card text. Plain sentences, no dash characters."""
    r = report
    fmt = monthly_report._fmt
    lines = [f"MONTHLY REVIEW {r['account_key']}: health {r['health']}, "
             f"engagement rate {fmt(r['engagement_rate'])}, "
             f"posts {r['posts_published']}, posts per week "
             f"{fmt(r['posting_freq_before'])} before Echo vs "
             f"{fmt(r['posting_freq_after'])} now."]
    if r["top_posts"]:
        lines.append("Top post: " + r["top_posts"][0]["caption"])
    if r["bottom_posts"]:
        lines.append("Weakest post: " + r["bottom_posts"][0]["caption"])
    for pr in proposals[:3]:
        lines.append("Next cycle: " + pr)
    for d in dropped:
        lines.append("Dropped: " + d)
    lines.append("Ask the client: " + " ".join(asks[:2]))
    return "\n".join(lines)


def run(account=None, dry=False, poster=None, now=None, base_dir=None):
    """The 30 day loop per account.

    --dry is READ ONLY and runs even while the flag is OFF: it prints the full
    review to stdout and produces ZERO side effects (no Slack, no PDF, no store
    writes). Without --dry the enable check stands exactly as before."""
    if not dry and not config.monthly_review_enabled():
        print("monthly-review: AGENT_MONTHLY_REVIEW_ENABLED is OFF. Nothing built. "
              "(--dry runs read only without the flag.)")
        return None
    now = now or datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    out_dir = os.environ.get("AGENT_REPORTS_DIR", "/data/reports")
    out = {}
    known = [a.key for a in active_accounts()]
    if account and account not in known:
        print(f"monthly-review: no account matches '{account}' "
              f"(known: {', '.join(known)}). Nothing built.")
        return out
    for acct in [a for a in active_accounts() if account in (None, a.key)]:
        snaps, posts = monthly_report.gather(acct.key, now=now)
        report = monthly_report.assemble(acct.key, snaps, posts,
                                         baseline_month=month, base_dir=base_dir)
        proposals, dropped = gated_proposals(posts)
        asks = raw_material_asks(report)
        digest = digest_lines(report, proposals, dropped, asks)
        out[acct.key] = {"report": report, "proposals": proposals,
                         "dropped": dropped, "asks": asks, "digest": digest}
        print(digest)
        if dry:
            continue  # --dry: print only, post nothing, write nothing
        if poster is not None:
            poster.post_notice(digest)
        os.makedirs(out_dir, exist_ok=True)
        pdf_path = os.path.join(out_dir, f"{acct.key}_review_{month}.pdf")
        sections = [
            ("heading", "The cycle in numbers"),
            ("table", [("Health read", report["health"]),
                       ("Engagement rate on views",
                        monthly_report._fmt(report["engagement_rate"])),
                       ("Posts published", report["posts_published"]),
                       ("Posts per week before Echo",
                        monthly_report._fmt(report["posting_freq_before"])),
                       ("Posts per week this cycle",
                        monthly_report._fmt(report["posting_freq_after"]))]),
            ("heading", "Top posts"),
            ("list", [p["caption"] for p in report["top_posts"]] or ["no scored posts yet"]),
            ("heading", "Bottom posts"),
            ("list", [p["caption"] for p in report["bottom_posts"]] or ["no scored posts yet"]),
            ("heading", "Proposed angles for next cycle (approved sources only)"),
            ("list", proposals or ["none yet"]),
        ]
        if dropped:
            sections.append(("heading", "Dropped proposals (and why)"))
            sections.append(("list", dropped))
        sections.append(("heading", "Raw material to request"))
        sections.append(("list", asks))
        build_pdf(pdf_path, "Monthly review", acct.key, sections,
                  brand=brand_for(acct))
        out[acct.key]["pdf"] = pdf_path
        print(f"review PDF saved: {pdf_path}")
    return out
