"""
no_media_astra_seed.py — PERMANENT standing fallback: a gym with ZERO real media
AND no approved brand-voice sources gets Echo-generated Astra infographics unique
to it, sourced from its own scraped website + Instagram (gym_deep_brain.py),
instead of sitting on the generic fact-free onboarding SAMPLE month forever.

Blake (2026-09-11): "For Echo, it needs to scrape their website, their Instagram,
and everything, and then create infographics that are unique to the gym." A
permanent, standing capability — not a one-off script — for as long as a gym
remains genuinely media-less. CrossFit Chateau (zero uploaded media, zero
approved sources, verified 2026-09-11) is the real gym this was built to cover.

WHAT THIS IS NOT
  - NOT client_infographic_fill.py. That lane fills GAPS for a gym that already
    has an approved voice + approved sources but ran dry on PHOTOS. This module
    is for the gym BEFORE that: no approved sources at all (the "no_sources"
    branch in client_media_sync.scan_and_generate), which today only fires a
    stall alert telling a human to run `approve-sources` by hand and seeds the
    generic (fact-free) onboarding SAMPLE month. This module runs INSTEAD,
    automatically: scrape -> grounded facts -> Astra infographic DRAFTS,
    gym-specific from day one.
  - NOT an approval bypass. gym_deep_brain.build_deep_brain() lands its facts as
    PENDING client_sources rows exactly like it always has (never auto-approved).
    This module additionally uses those same freshly-scraped, CITED GroundedFacts
    to build infographic candidate rows — but every row still lands in
    content_calendar with status='pending', the SAME human approval gate every
    other post clears before it can ever publish. Nothing here approves a
    source or publishes a post.

SAFETY RAILS
  - Behind AGENT_NO_MEDIA_ASTRA_SEED (default OFF). OFF -> seed_gaps() is a no-op
    and is never called for real from the scan loop.
  - Only fires when the gym has ZERO real media AND no approved client sources
    (thin/no brand voice) — the caller (client_media_sync) already computes both
    of those; this module trusts the caller's gate and does not re-derive media
    counts itself. A gym with either real media OR an approved voice/sources is
    completely untouched: this never regenerates or competes with an existing
    real post (the mistake the 2026-09-10 fleet sweep made on Chateau).
  - One deep-brain scrape attempt per gym (marked in the kv store) so a scan
    every few minutes never re-scrapes the same site; a human can force a
    re-scrape via the existing `python -m agent gym-deep-brain` command.
  - Capped rows per run (default 3): drip, never flood.
  - INSERT-only via store.insert_rows (which itself applies the plan-horizon,
    caption-cooldown, dedupe and cross-day-media belts) — never deletes, never
    swaps, never sets a status other than 'pending'. Nothing here calls publish.
  - Best-effort: any exception is caught and logged; it must never sink the scan
    or block onboarding_demo's own sample seed (both may run the same pass).
"""

import os

from . import config

SEED_MAX_PER_RUN = 3
SEED_DAYS_AHEAD = 7
_SCRAPE_MARK_PREFIX = "deep_brain_scraped_"

# CLIENT-SAFE REVIEW MARK (2026-09-11): every row this module inserts is
# machine-generated from a scrape, ungrounded in any human-approved brand
# voice yet -- it must read as MORE cautious than an ordinary pending draft,
# not identical to one. status is already 'pending' (the same universal
# approval gate every post clears), but a reviewer scanning the queue has no
# way to tell "a human wrote/approved this brief" apart from "Echo scraped
# this and took its best shot" without opening each row. This pillar value is
# the real, visible distinction: distinct from every taxonomy pillar
# (content_categories.GYM_PILLARS, day_shape.PROOF_PILLARS/
# INVITATION_PILLARS all check FIXED, known lists and simply do not match this
# value -- verified, not assumed), so it changes zero downstream rotation
# logic, and it is returned to the portal UI as-is (portal_social.py serializes
# `pillar` verbatim). calendar_autopublish.py additionally hard-blocks
# autopublish on this exact pillar value below (defense in depth beyond the
# approved_only client gate that already exists).
NEEDS_CLIENT_SAFE_REVIEW_PILLAR = "deep_brain_needs_client_safe_review"


def enabled() -> bool:
    """AGENT_NO_MEDIA_ASTRA_SEED, default OFF."""
    return (os.environ.get("AGENT_NO_MEDIA_ASTRA_SEED", "false") or "") \
        .strip().lower() in ("1", "true", "yes", "on")


def _already_scraped(base):
    from . import db
    try:
        return bool(db.kv_get(_SCRAPE_MARK_PREFIX + base))
    except Exception:  # noqa: BLE001 - a kv read failure means "not marked yet"
        return False


def _mark_scraped(base):
    from . import db
    try:
        db.kv_set(_SCRAPE_MARK_PREFIX + base, "1")
    except Exception:  # noqa: BLE001 - a missed mark just costs one extra scrape later
        pass


def _pending_facts_for(base):
    """The gym's own already-landed PENDING sources (from a prior scrape, this
    run or an earlier one) so a repeat scan keeps drawing cards without
    re-scraping. Best-effort; [] on any failure."""
    try:
        from . import client_sources
        return client_sources.pending_sources(base) or []
    except Exception:  # noqa: BLE001
        return []


def _ensure_deep_brain_facts(base, log):
    """Build (once per gym) the deep brain from the gym's own site + Instagram
    and return the facts to draw cards from. Never raises: a block (no domain
    on record, robots disallow, nothing extractable) is logged and treated
    exactly like 'nothing scraped yet' — the caller simply seeds nothing this
    pass and tries again once the block is resolved (e.g. a domain is added)."""
    if not config.gym_deep_brain_enabled():
        log(f"{base}: AGENT_GYM_DEEP_BRAIN is off; no-media Astra seed has no "
            "facts to draw from")
        return []
    if _already_scraped(base):
        return _pending_facts_for(base)
    try:
        from . import gym_deep_brain
        result = gym_deep_brain.build_deep_brain(base)
    except Exception as exc:  # noqa: BLE001 - a scrape failure must not sink the scan
        log(f"{base}: deep-brain scrape raised {type(exc).__name__}: {exc}")
        return []
    _mark_scraped(base)
    if not result.get("ok"):
        log(f"{base}: deep-brain scrape blocked: {result.get('reason')}")
        return []
    return result.get("facts") or _pending_facts_for(base)


def _headline_and_facts(source):
    """A short on-image headline plus its own fact line, both drawn ONLY from the
    grounded source's own text — nothing invented. Empty/unusable source -> ("", [])
    so the caller skips it."""
    text = str(getattr(source, "text", "") or "").strip()
    if not text:
        return "", []
    for stop in (".", "!", "?", ";", ":"):
        cut = text.find(stop)
        if 0 < cut < len(text):
            headline = text[:cut]
            break
    else:
        headline = text
    words = headline.split()
    headline = " ".join(words[:8]).strip()
    return headline, [text]


def seed_gaps(base, account, store, *, log=None, today=None,
             max_rows=SEED_MAX_PER_RUN, days_ahead=SEED_DAYS_AHEAD):
    """Fill this gym's next `days_ahead` empty feed days with Astra infographic
    DRAFTS grounded in its own scraped website/Instagram content. Returns the
    number of rows inserted (0 on any block/failure — never raises). Caller
    (client_media_sync) is responsible for confirming the gym has zero real
    media AND no approved sources before calling this; see module docstring."""
    log = log or (lambda *_: None)
    if not enabled() or store is None or account is None:
        return 0
    facts = _ensure_deep_brain_facts(base, log)
    if not facts:
        return 0

    from .client_infographic_fill import _empty_upcoming_days
    tz_name = getattr(account, "tz", None) or "America/New_York"
    days = _empty_upcoming_days(store, base, tz_name, days_ahead, now=today)
    if not days:
        return 0

    rows = []
    for day, source in zip(days[:max_rows], facts[:max_rows]):
        headline, fact_lines = _headline_and_facts(source)
        if not headline:
            continue
        try:
            from . import creative_studio
            result = creative_studio.generate(headline, fact_lines,
                                              account_key=base, surface="feed post")
        except Exception as exc:  # noqa: BLE001 - one bad card must not sink the pass
            log(f"{base}: no-media Astra seed generate failed for {day}: "
                f"{type(exc).__name__}: {exc}")
            continue
        if not result or not result.get("path"):
            continue
        try:
            from . import media_host
            url = media_host.host_media(result["path"], base)
        except Exception as exc:  # noqa: BLE001
            log(f"{base}: no-media Astra seed hosting failed for {day}: "
                f"{type(exc).__name__}: {exc}")
            continue
        if not url:
            continue
        rows.append({
            "gym_id": base, "account": "instagram", "post_date": day,
            "format": "feed", "pillar": NEEDS_CLIENT_SAFE_REVIEW_PILLAR,
            "caption": headline, "image_url": url, "status": "pending",
        })

    if not rows:
        return 0
    try:
        inserted = store.insert_rows(base, rows) or []
    except Exception as exc:  # noqa: BLE001 - a failed insert must not sink the scan
        log(f"{base}: no-media Astra seed insert failed: {type(exc).__name__}: {exc}")
        return 0
    log(f"{base}: no-media Astra seed inserted {len(inserted)} grounded "
        "infographic draft(s) from its own scraped site/Instagram")
    return len(inserted)
