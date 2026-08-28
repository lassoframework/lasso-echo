"""
testimonial_pillar.py — the LASSO owner-voice proof pillar (report-card build,
2026-08-28). Client-owner quotes and case numbers (e.g. the Fit Mamas Tribe
$19K -> $47K proof) as a recurring slot in the LASSO month plan.

Behind AGENT_LASSO_TESTIMONIAL_PILLAR (config.lasso_testimonial_pillar_enabled(),
default OFF). OFF -> build_testimonial_draft returns None and nothing changes.

HARD RAIL — NO FABRICATION, EVER:
  * The ONLY source is the approved social-proof doc (social_proof.load_entries
    over brand_voice/social_proof.md or the per-account variant): entries with
    an explicit `Permission: yes` AND a `Verified: YYYY-MM-DD` date. The same
    verified-source discipline social_proof.py already enforces.
  * The caption carries ONLY the entry's approved lines (quote/stat, support,
    attribution). No new claims, numbers, or names are ever written.
  * No approved entry, no card render, no hosting -> return None. The month
    planner's existing fallback then fills the slot from a REAL pillar
    (podcast/platform/b2b/doctrine) — the slot is never left to an invented
    quote and never fabricated.

Distinct from social_proof.build_social_proof_draft (the weekly proof-day lane):
this builder serves the MONTH PLANNER, which decides the day, so there is no
weekday gate here — the planner's rotation (alternate Tuesdays) owns cadence.
Everything runs through copy_gate.scrub (no dashes). Nothing here publishes.
"""

from datetime import date

from . import config, copy_gate, social_proof

CATEGORY = "testimonial"


def pick_entry(approved, day_key):
    """Deterministic rotation through the approved entries: ISO week + day
    ordinal indexes the list so consecutive testimonial days rotate instead of
    repeating (the caption dedup belts would block a verbatim repeat anyway)."""
    if not approved:
        return None
    d = date.fromisoformat(day_key)
    idx = (d.isocalendar()[1] + d.toordinal()) % len(approved)
    return approved[idx]


def approved_entries(account, path=None):
    """The approved (Permission: yes + Verified date) entries for this account,
    from the same source-resolution order social_proof uses. [] when the doc is
    missing/empty or nothing is approved — the caller then falls back."""
    key = getattr(account, "key", None) or (account if isinstance(account, str) else "")
    src = (path
           or getattr(account, "social_proof_doc", "")
           or social_proof.source_path(key))
    approved, _skipped = social_proof.load_entries(src)
    return approved


def build_testimonial_draft(account, day_key, *, path=None, nano_client=None,
                            s3_client=None):
    """The owner-voice testimonial Draft for this slot, or None whenever an
    honest draft cannot be produced (flag off, no approved entry, card
    generation or hosting dark). None -> the planner fills the day from a REAL
    fallback pillar; a quote or number is NEVER invented."""
    if not config.lasso_testimonial_pillar_enabled():
        return None
    approved = approved_entries(account, path=path)
    if not approved:
        return None  # hard rail: nothing approved -> fall back, never fabricate

    entry = pick_entry(approved, day_key)

    # Card image from the verified entry ONLY (the same renderer the proof lane
    # uses). Generation or hosting dark -> None (fall back).
    from . import creative_studio, media_host
    art = creative_studio.generate_social_proof(
        entry.kind, entry.main, entry.support, entry.attribution,
        client=nano_client)
    if not art:
        return None
    key = getattr(account, "key", None) or (account if isinstance(account, str) else "")
    hosted = media_host.host_media(art["path"], key, client=s3_client)
    if not hosted:
        return None

    # Caption: the entry's approved lines, scrubbed by the house-style gate.
    # NO ask is appended here: testimonial posts are the plan's genuine
    # no-ask room (ask_coverage leaves them askless while the floor is met).
    caption = copy_gate.scrub("\n\n".join(entry.approved_lines()))

    from .drafter import Draft, DraftStatus, _make_id
    from .library import Creative  # noqa: F401  (import parity with social_proof)
    from . import schedule
    return Draft(
        draft_id=_make_id(key, f"testimonial_{entry.kind}", day_key),
        account_key=key,
        platform=getattr(account, "platform", "") or "",
        caption=caption, hashtags=[],
        creative_path=art["path"], creative_public_url=hosted,
        scheduled_for=schedule.scheduled_for(day_key), status=DraftStatus.PENDING,
        day_key=day_key, draft_type=CATEGORY, category=CATEGORY,
        source_fragments=entry.approved_lines(),  # audit: verified entry text only
    )
