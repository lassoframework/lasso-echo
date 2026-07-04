"""
Platform doctrine resolver (readiness Part B): where daily caption angles
resolve their citations from.

THE CITATION HIERARCHY, stated plainly:
  1. book campaign content cites the book files (book_campaign.py, UNTOUCHED)
  2. every other LASSO draft resolves its angle against
     brand_voice/knowledge/08_platform_2026.md FIRST (positioning lines, six
     engines, receipts), citation attached
  3. lasso_now.md is the FALLBACK and the home of time sensitive angles

DORMANT BEHIND THE EXISTING KNOWLEDGE FLAG: the platform doc loads only while
AGENT_KNOWLEDGE_ENABLED is armed, so with it OFF this module resolves nothing
and drafting is byte for byte yesterday's (lasso_now only). No new flag, no
approval flow change, no cadence change, no fabrication gate change: the gate
already clears platform USE lines; this only changes where angles come from.

An angle whose citation cannot resolve (the anchor tag is missing or the copy
is not a USE line) is DROPPED with an audited reason, never shipped uncited.
"""

import re

from . import db, knowledge

PLATFORM_FILE = "08_platform_2026.md"
_ANGLE_RE = re.compile(r'^USE:\s*"(?P<copy>.+?)"\s*\((?P<anchor>platform_2026_[a-z_0-9]+)\)\s*$')

# pillar vocabulary -> the doctrine anchors that carry that pillar's angles
_PILLAR_ANCHORS = (
    (("ad", "ads", "marketing", "paid", "campaign", "spend"),
     ("platform_2026_receipts", "platform_2026_positioning")),
    (("follow up", "speed", "lead", "nurture", "respond"),
     ("platform_2026_positioning", "platform_2026_receipts")),
    (("sales", "sell", "close", "coaching", "consultation"),
     ("platform_2026_positioning", "platform_2026_engines")),
    (("number", "churn", "profit", "cost per lead", "know your"),
     ("platform_2026_receipts", "platform_2026_positioning")),
    (("messaging", "message", "website", "clear", "hero"),
     ("platform_2026_positioning", "platform_2026_receipts")),
)


def platform_angles():
    """[(copy, anchor)] from the platform doc's USE lines, file order. Empty
    while the knowledge flag is OFF or the doc is absent (drafting then falls
    back to lasso_now everywhere, zero behavior change)."""
    corpus = knowledge.load_corpus().get(PLATFORM_FILE)
    if not corpus:
        return []
    out = []
    for item in knowledge.join_items(corpus):
        text = item.lstrip("-* ").strip()
        m = _ANGLE_RE.match(text)
        if m:
            out.append((m.group("copy"), m.group("anchor")))
    return out


def _anchors_for_pillar(pillar):
    low = (pillar or "").lower()
    for words, anchors in _PILLAR_ANCHORS:
        if any(w in low for w in words):
            return anchors
    return ("platform_2026_positioning",)


def angle_for_pillar(pillar, day_key):
    """
    The doctrine angle for one pillar and day: {copy, anchor} picked
    deterministically from the pillar's anchor sections, or None (no doc, flag
    off, or nothing resolvable) so the caller falls back to lasso_now. An
    angle candidate that LOOKS like doctrine but carries no resolvable anchor
    is dropped loudly (audited), never returned uncited.
    """
    angles = platform_angles()
    if not angles:
        return None
    wanted = _anchors_for_pillar(pillar)
    pool = [(c, a) for c, a in angles
            if any(a.startswith(w) for w in wanted)]
    if not pool:
        db.audit("doctrine_drop", pillar or "?",
                 "no platform_2026 angle resolves for this pillar; "
                 "falling back to lasso_now")
        return None
    from .content_planner import _day_seq
    copy, anchor = pool[_day_seq(day_key) % len(pool)]
    return {"copy": copy, "anchor": anchor}


def verify_citation(copy, anchor):
    """True when (copy, anchor) is a real platform doc USE pairing. The drop
    gate for anything claiming to be a doctrine angle."""
    return (copy, anchor) in set(platform_angles())
