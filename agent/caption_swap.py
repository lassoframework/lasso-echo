"""
caption_swap.py — regenerate ONLY the caption on a post, keeping its EXACT SAME photo.

media_swap.py (B6, Pete/zanshin) split "the photo is wrong" into a FREE, unlimited
swap that keeps the caption untouched. This module is the missing other half: "the
caption is wrong" rewrites ONLY the copy on the gym's current photo, instead of
today's deny -> full recreate, which regenerates BOTH and can (often does) hand the
coach a DIFFERENT photo even though only the words were the problem. Blake, 2026-09-
07: "it seems that ppl like one or another also improve the visual reader to make
sure copy is about what the picture is". This module is the "one or the other" half
for copy; agent/media_swap.py is the other half for the photo.

ONE RECIPE, NOT A NEW ONE: the caption is regenerated through the EXACT SAME path
every normal build uses -- client_content.category_for_day / _source_for_day (or
client_sources.educational_source_for) for the day's approved fact, then
client_content.make_caption (SB7), then post_quality.is_a_plus as the gate -- so a
caption-only recreate can never ship a caption a normal build would have rejected.

VISION HARD RULE: when the gym is vision-enabled, the SAME photo is re-verified via
vision.crop_verify before the new caption is graded (verify-then-draft, never draft-
then-verify, exactly like client_content.build_client_draft). Portal calendar rows
carry no persisted `grounding` field (it lives only on the in-memory Draft during a
build), so there is nothing to "reuse" here even in principle -- recomputing it fresh
from the untouched photo is not a nice-to-have, it is the only option, and it is cheap
(the photo did not change, so this is one crop_verify call, not a re-plan). The fresh
grounding dict is threaded into the A+ gate (post_quality.post_issues) exactly like a
normal build, so a caption that contradicts the photo is rejected before it ships.

BUDGET: unlike media swap, a caption recreate still costs one of the monthly 15
(config.caption_recreate_scoped_enabled's docstring / media_swap_free_enabled's own
docstring: "regenerating COPY is still the expensive act"). This module only changes
WHAT gets regenerated (caption only, not caption+photo); the caller owns the budget
charge exactly as today's full deny does.

Flag: config.caption_recreate_scoped_enabled() (ECHO_CAPTION_RECREATE_SCOPED, default
OFF). Ships dark; the caller falls back to today's full recreate while the flag is off.
"""

import os

from . import config, dam, media_guard

# Why a reason code and not an exception: the portal answers a client, and every
# outcome here is a normal thing that can happen to a real gym (media_swap.py's
# convention, mirrored here so the two swap surfaces read the same way).
REASON_NO_LIBRARY = "no_library"
REASON_NO_PHOTO = "no_photo"
REASON_NO_SOURCE = "no_source"
REASON_GATE_EXHAUSTED = "gate_exhausted"

# Bounded retry: how many times we re-ask SB7 for a caption on this SAME source before
# giving up and leaving the post untouched. Mirrors the shape of the month builder's
# neighbour-day walk (client_month_run._clean_draft_for_day), but there is no "next
# day" to walk here -- the day's approved source is fixed by design (the caption must
# still be about the day it is scheduled for), so what we vary across attempts is
# STYLE only: the opening phrase to avoid and the SB7 entry angle.
MAX_ATTEMPTS = 3


def enabled():
    return config.caption_recreate_scoped_enabled()


def _log(msg):
    print(f"[caption-swap] {msg}")


def _load_creative_for_photo(lib, row):
    """The library Creative object for the row's CURRENT photo, so the regenerated
    caption is grounded in the same sidecar note / filename hint a normal build would
    see. Resolves a feed-autofit reframe name back to the raw library file first
    (media_guard.reframe_map) -- a row that shipped through autofit carries the
    reframe's name, not the original photo's. Returns (creative_or_None, raw_key) so
    the caller can still proceed caption_key-only when the file cannot be located."""
    key = media_guard.row_media_key(row)
    if not key:
        return None, ""
    rmap = media_guard.reframe_map(lib, {key})
    raw_key = rmap.get(key, key)
    path = os.path.join(lib, raw_key)
    if not os.path.isfile(path):
        return None, raw_key
    from .library import list_creatives
    for c in list_creatives(lib):
        if os.path.basename(c.path) == raw_key:
            return c, raw_key
    return None, raw_key


def _ground_photo(account_key, creative):
    """(verified, grounding) for the SAME photo, exactly like
    client_content.build_client_draft's own §3.5 crop-verify step. None, None for a
    non-vision gym or when the photo cannot be read (best-effort; a verify failure
    degrades to no grounding, never blocks the day -- same posture as the build path)."""
    if not config.vision_enabled_for(account_key) or creative is None:
        return None, None
    from . import vision, client_sources
    analysis = vision.stored_analysis(creative.path)
    try:
        with open(creative.path, "rb") as fh:
            img_bytes = fh.read()
    except OSError:
        img_bytes = b""
    verified = vision.crop_verify(img_bytes, analysis)
    side = dam.read_sidecar(creative.path)
    ctx = side.get("client_context", "") or ""
    consent = str(side.get("consent", "")).lower() == "granted"
    ctx_ok, _reasons = vision.context_usable(ctx)
    if not ctx_ok:
        ctx = ""
    claims = client_sources.approved_claims(account_key)
    grounding = {"analysis": analysis, "verified": verified, "claims": claims,
                "consent": consent, "client_context": ctx}
    return verified, grounding


def _resolve_source(account_key, day_key):
    """(source, angle) for this day, via the SAME deterministic rotation
    client_content.build_client_draft uses -- category_for_day / _source_for_day, or
    the educational lane. None, "" when nothing approved is eligible (the caller then
    refuses the recreate rather than fabricate a fact)."""
    from . import client_content, client_sources, rotation
    present = client_sources.categories_present(account_key)
    if not present:
        return None, ""
    category = client_content.category_for_day(account_key, day_key, present)
    angle = ""
    if category == "educational":
        source = client_sources.educational_source_for(account_key, day_key)
        angle = "educational"
    else:
        pillars = client_content._pillars_for(account_key, present)  # noqa: SLF001
        source = client_content._source_for_day(  # noqa: SLF001
            account_key, day_key, category, pillars)
    if source is None:
        return None, ""
    claims = client_sources.approved_claims(account_key)
    if not rotation.is_gate_clean(source.text, approved_claims=claims):
        return None, ""
    return source, angle


def recreate_caption(base_key, row, *, account, voice, library_path=None,
                     banned_words=(), make_caption_fn=None, log=None):
    """Rewrite ONLY the caption for `row`, on its EXACT SAME photo.

    Returns {"ok": True, "caption":..., "hashtags":[...]} or
    {"ok": False, "reason": <REASON_*>}. Never writes, never publishes, never touches
    the budget -- the caller (portal_social.handle_deny / handle_recreate_caption)
    owns the write (SupabaseCalendarStore.patch_caption, the same call a human
    caption edit already uses) and the budget charge.

    account: the Account for `base_key`'s generation account (drives voice/library
    resolution the same way every other build-time caller does).
    voice: the gym's loaded VoiceDoc (caller resolves it once; see
    portal_social._voice_for, added alongside this module).
    """
    say = log or _log
    from . import client_content, post_quality, drafter

    day_key = str((row or {}).get("post_date") or "")[:10]
    if not day_key:
        return {"ok": False, "reason": REASON_NO_SOURCE}

    from . import media_swap as _ms
    lib = library_path if library_path is not None else _ms.library_path_for(base_key)
    if not lib:
        return {"ok": False, "reason": REASON_NO_LIBRARY}

    creative, raw_key = _load_creative_for_photo(lib, row)
    if creative is None:
        # Always required, vision or not: "same photo" needs the actual local file
        # both to ground the caption in its sidecar note (client_content.
        # photo_grounding) and, for a vision gym, to crop-verify it below -- there is
        # nothing defensible to check a caption against otherwise.
        return {"ok": False, "reason": REASON_NO_PHOTO}
    creative_key = raw_key or os.path.basename(creative.path)

    source, base_angle = _resolve_source(account.key, day_key)
    if source is None:
        return {"ok": False, "reason": REASON_NO_SOURCE}

    verified, grounding = _ground_photo(account.key, creative)

    make_caption_fn = make_caption_fn or client_content.make_caption
    original_caption = (row or {}).get("caption") or ""
    avoid_openings = [drafter.opening_signature(original_caption)]
    tried_captions = {" ".join(original_caption.split()).strip().lower()}

    for attempt in range(MAX_ATTEMPTS):
        # STYLE-only variation across attempts: rotate the SB7 entry angle so a
        # repeat gate-fail doesn't just re-ask for the identical caption, and keep
        # growing the avoid-openings list so a later attempt can't re-collide with
        # an earlier REJECTED attempt either.
        angle = base_angle or drafter.angle_for_index(
            hash((day_key, base_key, attempt)) % len(drafter.CAPTION_ANGLES))
        caption, hashtags = make_caption_fn(
            account, source, voice, creative_key, creative=creative,
            avoid_openings=avoid_openings, verified=verified, angle=angle)
        norm = " ".join((caption or "").split()).strip().lower()
        if norm and norm in tried_captions:
            continue
        tried_captions.add(norm)
        avoid_openings.append(drafter.opening_signature(caption))

        candidate = drafter.Draft(
            draft_id=f"{base_key}-caption-swap",
            account_key=account.key,
            platform=getattr(account, "platform", ""),
            caption=caption,
            hashtags=hashtags,
            creative_path=creative.path if creative else "",
            creative_public_url=(row or {}).get("image_url") or "",
            scheduled_for=(row or {}).get("post_date") or day_key,
        )
        if grounding is not None:
            candidate.grounding = grounding
        if post_quality.is_a_plus(candidate, banned_words):
            say(f"{base_key}: caption-only recreate ok for {day_key} "
                f"(attempt {attempt + 1}/{MAX_ATTEMPTS})")
            return {"ok": True, "caption": caption, "hashtags": hashtags}

    say(f"{base_key}: caption-only recreate exhausted {MAX_ATTEMPTS} attempts for "
        f"{day_key}; leaving the post's caption unchanged")
    return {"ok": False, "reason": REASON_GATE_EXHAUSTED}


def client_message(reason):
    """What the gym owner reads when a caption-only recreate could not happen.
    Plain, actionable, mirrors media_swap.client_message's tone."""
    if reason == REASON_NO_SOURCE:
        return ("Echo has no approved source left to write a caption from for this "
                "day. Add or approve more source material and try again. Your post "
                "is unchanged and your recreates were not touched.")
    if reason == REASON_NO_PHOTO:
        return ("Echo could not find this post's photo to check the new caption "
                "against it, so nothing was changed. Try again shortly. Your "
                "recreates were not touched.")
    if reason == REASON_NO_LIBRARY:
        return ("Your photo library is not connected yet, so Echo cannot ground a "
                "caption in your photo. Upload photos in the portal and try again. "
                "Your recreates were not touched.")
    if reason == REASON_GATE_EXHAUSTED:
        return ("Echo tried a few fresh captions for this photo and none cleared "
                "its own quality gate, so your post is unchanged. Deny it for a full "
                "recreate, or try again shortly. Your recreates were not touched.")
    return ("Echo could not recreate the caption right now, so nothing was changed. "
            "Try again shortly. Your recreates were not touched.")


__all__ = ["enabled", "recreate_caption", "client_message",
           "REASON_NO_LIBRARY", "REASON_NO_PHOTO", "REASON_NO_SOURCE",
           "REASON_GATE_EXHAUSTED", "MAX_ATTEMPTS"]
