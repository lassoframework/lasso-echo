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
import uuid

from . import config

SEED_MAX_PER_RUN = 3
SEED_DAYS_AHEAD = 2
_SCRAPE_MARK_PREFIX = "deep_brain_scraped_"


def _logical_post_ids_enabled():
    """Read the forward-only identity flag; old deployments default to OFF."""
    try:
        enabled = getattr(config, "logical_post_id_enabled", None)
        return bool(enabled()) if callable(enabled) else False
    except Exception:  # noqa: BLE001 - flag uncertainty never changes legacy behavior
        return False


def _ensure_logical_post_id(row):
    """Stamp one generated standalone row, preserving valid same-object retries."""
    if not _logical_post_ids_enabled():
        return True
    existing = row.get("logical_post_id")
    if existing is not None:
        try:
            uuid.UUID(str(existing))
        except (ValueError, TypeError, AttributeError):
            return False
        return True
    try:
        row["logical_post_id"] = str(uuid.uuid4())
        uuid.UUID(row["logical_post_id"])
        return True
    except Exception:  # noqa: BLE001 - never stage an unkeyed generated post
        return False

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
    # A named account must resolve to this exact tenant before any scrape or
    # render. Test-only callers without an account key may still exercise the
    # earlier read-only gap gate below, but cannot pass the render gate.
    account_key = getattr(account, "key", None)
    if account_key:
        from . import astra_prompt as _ap
        account_base = _ap._account_base(account_key)
        if account_base != base:
            log(f"{base}: account/gym mismatch ({account_base!r} account); "
                "no-media Astra seed held")
            return 0
    from .client_infographic_fill import real_media_depleted
    depleted = real_media_depleted(base, now=today)
    if not depleted:
        return 0
    from .media_bridge import bridge_days, episode, retry_existing_notice
    # Read before bridge_days creates a new episode.  Existing episodes retry
    # their durable outbox entry; new episodes send one notice after a real
    # calendar gap is confirmed below.
    existing_notice = bool(episode(base, now=today, create=False))
    allowed_days = set(bridge_days(base, now=today, days_ahead=days_ahead))
    if existing_notice:
        retry_existing_notice(base, account, store, now=today, logger=log)

    from .client_infographic_fill import _empty_upcoming_days
    tz_name = config.posting_timezone_for(base)
    days = [day for day in _empty_upcoming_days(
        store, base, tz_name, min(days_ahead, 2), now=today) if day in allowed_days]
    if not days:
        return 0
    if depleted and not existing_notice:
        from .media_bridge import notify_bridge
        notify_bridge(base, account, logger=log)
    facts = _ensure_deep_brain_facts(base, log)
    if not facts:
        return 0

    # Blake's global ruling (2026-10-02), same contract as
    # client_infographic_fill.fill_gaps: the generated infographic is the
    # absolute LAST resort behind approved client photos, it MUST render
    # through the Astra path, and it MUST carry this gym's own VERIFIED brand
    # colors in the brief. No verified palette on file -> fail CLOSED: seed
    # nothing and surface the reason, never invent colors from the voice doc's
    # tone and never reach for LASSO's or a generic palette (the old
    # creative_studio.generate lane could fall back to exactly that).
    from . import astra_prompt as _ap
    account_base = _ap._account_base(account_key)
    if account_base != base:
        log(f"{base}: account/gym mismatch ({account_base!r} account); "
            "no-media Astra seed held")
        return 0
    gym_palette = _ap.load_gym_brand_palette(account_key)
    if not gym_palette:
        log(f"{base}: no verified brand colors for no-media Astra seed "
            f"(expected {_ap._gym_brand_colors_path(base)}); held (no generic "
            "palette fallback)")
        return 0
    from .client_infographic_fill import _generate_astra_only

    rows = []
    for day, source in zip(days[:max_rows], facts[:max_rows]):
        headline, fact_lines = _headline_and_facts(source)
        if not headline:
            continue
        # Recheck depletion before EVERY render: an approved photo landing
        # between the caller's gate and this render wins the slot (photos
        # first, infographic last resort). A gym that is no longer media-less
        # is completely untouched from this point on.
        if not real_media_depleted(base, now=today):
            log(f"{base}: usable real media appeared; no-media Astra seed held")
            break
        try:
            astra_brief = _ap.build_infographic_brief(
                headline, fact_lines, surface="feed post",
                account_key=account.key, gym_palette=gym_palette)
        except Exception as exc:  # noqa: BLE001 - a brief we cannot build is a held day
            log(f"{base} {day}: Astra brief could not be built "
                f"({type(exc).__name__}); held (no generic fallback)")
            continue
        # Astra, and ONLY Astra (client_infographic_fill._generate_astra_only):
        # no silent Gemini rung, no LASSO-branded fallback. A total Astra
        # failure marks the slot NEEDS HUMAN and returns None so this day is
        # held instead of filled with off-brand art.
        _res = _generate_astra_only(
            astra_brief,
            {"kind": "infographic", "surface": "feed post",
             "has_text_overlay": bool(str(headline or "").strip()),
             "require_astra": True},
            account_key=account.key,
            subject=f"no-media seed {base} {day}"[:120],
            draft_id=f"no_media_{base}_{day}")
        if _res is None:
            continue
        out = os.path.join(config.LIBRARY_PATH, base, f"no_media_{day}.png")
        try:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as fh:
                fh.write(_res.image_bytes)
        except OSError as exc:
            log(f"{base}: no-media Astra seed could not write card for {day}: "
                f"{type(exc).__name__}: {exc}")
            continue
        try:
            from . import media_host
            url = media_host.host_media(out, base)
        except Exception as exc:  # noqa: BLE001
            log(f"{base}: no-media Astra seed hosting failed for {day}: "
                f"{type(exc).__name__}: {exc}")
            continue
        if not url:
            continue
        # Rendering and hosting are deliberately outside the calendar store;
        # a photo or a competing feed row may have arrived while they ran.
        # Recheck before this candidate can join the pending insert batch.
        if not real_media_depleted(base, now=today):
            log(f"{base}: usable real media appeared before seed insert; held")
            break
        if day not in set(_empty_upcoming_days(
                store, base, tz_name, min(days_ahead, 2), now=today)):
            log(f"{base} {day}: feed slot taken before seed insert; held")
            continue
        row = {
            "gym_id": base, "account": "instagram", "post_date": day,
            "format": "feed", "pillar": NEEDS_CLIENT_SAFE_REVIEW_PILLAR,
            "caption": headline, "image_url": url, "status": "pending",
        }
        if not _ensure_logical_post_id(row):
            log(f"{base} {day}: logical post identity unavailable; holding seed batch")
            return 0
        # The generated PNG is hosted byte-for-byte as rendered; it is not
        # cropped or otherwise transformed after hosting. When the global
        # prepared writer is armed, explicitly identify that same hosted object
        # as its own source so the writer can verify same-object lineage.
        # Keep the legacy payload unchanged while the guard is off.
        try:
            from . import visual_writer_prepare
            if visual_writer_prepare.enabled():
                row["source_media_url"] = url
        except Exception:  # noqa: BLE001 - unknown guard state fails closed
            row["source_media_url"] = url
        rows.append(row)

    if not rows:
        return 0
    # Final batch guard. store.insert_rows has no conditional compare-and-
    # insert API, so a writer can still win after these reads; this module
    # cannot truthfully promise atomicity and holds on every observable race.
    if not real_media_depleted(base, now=today):
        log(f"{base}: usable real media available before seed batch insert; held")
        return 0
    insertable_days = set(_empty_upcoming_days(
        store, base, tz_name, min(days_ahead, 2), now=today))
    rows = [row for row in rows if row["post_date"] in insertable_days]
    if not rows:
        log(f"{base}: seed feed slot taken before batch insert; held")
        return 0
    try:
        inserted = store.insert_rows(base, rows) or []
    except Exception as exc:  # noqa: BLE001 - a failed insert must not sink the scan
        log(f"{base}: no-media Astra seed insert failed: {type(exc).__name__}: {exc}")
        return 0
    log(f"{base}: no-media Astra seed inserted {len(inserted)} grounded "
        "infographic draft(s) from its own scraped site/Instagram")
    return len(inserted)
