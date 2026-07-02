"""
Nightly brain: the READ-ONLY proposer (the hook this file stubbed, now real).
Flag: AGENT_BRAIN_PROPOSALS_ENABLED (default OFF).

Once nightly (the hour after the digest; env AGENT_BRAIN_HOUR_UTC overrides),
read the store's recent performance and the APPROVED source docs and post ONE
short Slack note: which pillar / archetype / set is winning, one proposed angle
for tomorrow drafted ONLY from approved sources, and one question for Blake
when data is thin. TEXT ONLY.

Hard lines, unchanged from the original stub and enforced by construction:
  - Human owns voice: this PROPOSES angles; it never edits the voice doc,
    never creates concepts, never schedules, never alters the plan.
  - No fabrication: every proposed angle is quoted from an approved source
    (the lasso_now copy bank or a knowledge USE stat) with its citation.
    LOCKED / PENDING knowledge can never appear (the knowledge gate applies).
"""

import os
from datetime import datetime, timedelta, timezone

from . import config, content_planner, db, rotation

WINDOW_DAYS = 14
THIN_DATA_POSTS = 3


def _recent_posts(now):
    since = (now - timedelta(days=WINDOW_DAYS)).date().isoformat()
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM posts WHERE mode='published' AND published_at >= ? "
            "ORDER BY published_at", (since,)).fetchall()]


def _winning(posts):
    """{dimension: value} winners from real engagement; {} when unscored."""
    perf = {}
    for p in posts:
        parts = [p.get(k) for k in ("likes", "comments", "saves", "shares")]
        parts = [v for v in parts if isinstance(v, (int, float))]
        if not parts:
            continue
        eng = sum(parts)
        for dim, val in (("pillar", rotation.pillar_of(p.get("creative_key") or "")),
                         ("archetype", p.get("archetype") or ""),
                         ("set", p.get("set_name") or "")):
            if val and val != "unknown":
                b = perf.setdefault(dim, {}).setdefault(val, {"n": 0, "eng": 0})
                b["n"] += 1
                b["eng"] += eng
    winners = {}
    for dim, buckets in perf.items():
        ranked = sorted(((k, v["eng"] / v["n"]) for k, v in buckets.items()),
                        key=lambda x: x[1], reverse=True)
        if ranked:
            winners[dim] = ranked[0][0]
    return winners


def _proposed_angle():
    """ONE angle, ONLY from approved sources, with its citation. None when the
    sources offer nothing; nothing is ever invented."""
    doc = content_planner.load_source_doc()
    if doc is not None:
        for pillar in doc.pillars_with_copy():
            hooks = doc.copy_bank.get(pillar, {}).get("hooks", [])
            if hooks:
                return (f"Proposed angle (from {config.SOURCE_DOC_PATH}, pillar "
                        f"'{pillar}'): {hooks[0]}")
    from . import knowledge
    stats = knowledge.usable_stats()  # the USE gate: LOCKED/PENDING never pass
    if stats:
        return f"Proposed angle (from knowledge USE stat): {stats[0]}"
    return None


def build_note(now=None):
    """The one nightly note. Text only, approved sources only, honest when thin."""
    now = now or datetime.now(timezone.utc)
    posts = _recent_posts(now)
    scored = [p for p in posts
              if any(isinstance(p.get(k), (int, float))
                     for k in ("likes", "comments", "saves", "shares"))]
    lines = ["ECHO BRAIN (read only, proposes never posts):"]
    winners = _winning(posts)
    if winners:
        lines.append("Winning this window: "
                     + ", ".join(f"{dim} {val}" for dim, val in winners.items()) + ".")
    angle = _proposed_angle()
    if angle:
        lines.append(angle)
    if len(scored) < THIN_DATA_POSTS:
        lines.append(f"Data is thin ({len(scored)} scored post(s) in {WINDOW_DAYS} "
                     "days). Question for Blake: anything you want emphasized this "
                     "week while the numbers fill in?")
    return " ".join(lines)


def maybe_send(poster, now=None):
    """Fire ONCE per night at the brain hour (digest hour + 1 by default), with a
    persisted sent mark. Fully inert while AGENT_BRAIN_PROPOSALS_ENABLED is OFF."""
    if not config.brain_proposals_enabled():
        return None
    now = now or datetime.now(timezone.utc)
    default_hour = (int(os.environ.get("AGENT_DIGEST_HOUR_UTC", "23")) + 1) % 24
    hour = int(os.environ.get("AGENT_BRAIN_HOUR_UTC", str(default_hour)))
    if now.hour != hour:
        return None
    today = now.date().isoformat()
    if db.kv_get("brain_sent_date") == today:
        return None
    note = build_note(now=now)
    if poster is not None:
        poster.post_notice(note)
    db.kv_set("brain_sent_date", today)
    return note
