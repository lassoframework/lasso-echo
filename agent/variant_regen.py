"""
variant_regen.py — "Regenerate this photo" produces a v2 CANDIDATE, never an
in-place overwrite (migration 0318 / ECHO_VARIANT_PAIRING).

THE GAP THIS CLOSES: media_swap.py already makes "swap in a different photo
FROM THE LIBRARY" free and instant, and caption_swap.py already makes "rewrite
just the words" scoped. Neither covers "the photo is fine as a CONCEPT but I
want Astra to take another pass at rendering it" -- today that is only
possible via a full deny/recreate, which OVERWRITES the row: the original
creative is gone the moment the new one lands, so a human can never compare
before/after or change their mind. This module generates the new image WITHOUT
touching the existing row at all; the caller (portal_social.handle_regen_variant)
inserts it as a linked 'candidate' via SupabaseCalendarStore.create_variant_candidate,
and the human picks between the two later via swap_variant.

THE GENERATION ITSELF reuses creative_studio.generate — the SAME Astra-first,
grade-gated pipeline every normal build uses (Astra -> retry -> Gemini ->
"needs human"; house-style grade retries; no fabrication). For LASSO's reviewed
infographic path, the scheduled caption itself is the complete source. Other
accounts use the row's pillar as a short headline and its caption as the fact
set.

Flag: config.variant_pairing_enabled() (ECHO_VARIANT_PAIRING, default OFF).
"""

import hashlib

from . import config, media_host

REASON_DISABLED = "disabled"
REASON_NO_CAPTION = "no_caption"
REASON_GENERATE_FAILED = "generate_failed"
REASON_HOSTING = "hosting_unavailable"

_CLIENT_MESSAGES = {
    REASON_NO_CAPTION: "This post has no caption yet, so there is nothing to "
                       "regenerate a photo from.",
    REASON_GENERATE_FAILED: "Astra could not produce a clean v2 image for this "
                            "post. Nothing changed; try again shortly.",
    REASON_HOSTING: "The new image was generated but could not be hosted. "
                    "Nothing changed; try again shortly.",
}


def enabled():
    return config.variant_pairing_enabled()


def client_message(reason):
    return _CLIENT_MESSAGES.get(reason, "Could not regenerate this photo right now.")


def _facts_for(row):
    """The ONLY facts fed to the brief: the row's own already-approved caption,
    split into short lines. Empty caption -> no facts -> creative_studio.generate's
    own no-fabrication gate refuses (returns None) with no API call, but we check
    it here too so the caller gets a specific, honest reason instead of a bare
    500."""
    caption = str(row.get("caption") or "").strip()
    if not caption:
        return []
    # creative_studio.build_prompt renders facts as concept context, not literal
    # on-image text: split on sentence-ish breaks so a long caption doesn't
    # arrive as one enormous fact line.
    parts = [p.strip() for p in caption.replace("\n", ". ").split(". ") if p.strip()]
    return parts[:6] or [caption]


def _lasso_caption_brief(row):
    """Keep the scheduled caption intact as approved concept context.

    A short, verbatim first sentence serves as the on-image headline. No
    copy-bank block, rewritten claim, or unrelated CTA can replace the row.
    """
    caption = str(row.get("caption") or "").strip()
    first_line = next((line.strip() for line in caption.splitlines() if line.strip()), "")
    headline = first_line.split(". ", 1)[0].strip() or caption
    return headline, [caption]


def _caption_approved_cta(caption):
    """Render a Brain CTA only when this scheduled caption actually contains it."""
    try:
        from .content_planner import load_source_doc
        doc = load_source_doc()
        matches = [cta for cta in (doc.ctas if doc else ())
                   if cta and cta.casefold() in caption.casefold()]
        return max(matches, key=len) if matches else ""
    except Exception:
        return ""


def generate_variant_image(row, account_key, client=None, generate_fn=None,
                           host_fn=None):
    """Generate ONE new image for the logical post `row` already represents,
    WITHOUT touching `row` itself. Returns {"ok": True, "image_url": ...,
    "prompt": ...} or {"ok": False, "reason": REASON_*}.

    `generate_fn`/`host_fn` are injectable (test seam), defaulting to
    creative_studio.generate and media_host.host_media -- the exact functions
    every normal build path uses, so a v2 candidate is held to the same bar
    (Astra-first, house-style grade gate) as the original creative."""
    if not enabled():
        return {"ok": False, "reason": REASON_DISABLED}
    facts = _facts_for(row)
    if not facts:
        return {"ok": False, "reason": REASON_NO_CAPTION}

    lasso_quality = config.lasso_infographic_quality_enabled(account_key)
    headline = str(row.get("pillar") or "").strip() or "A new take on this post"
    if lasso_quality:
        if not row.get("id") or str(row.get("gym_id") or "").strip().lower() not in {
                "lasso", "lasso-framework-llc"}:
            return {"ok": False, "reason": "caption_source_unavailable"}
        headline, facts = _lasso_caption_brief(row)
    is_story = "story" in str(row.get("format") or "").lower()
    aspect = "9:16" if is_story else None
    pixels = "1080x1920" if is_story else None
    surface = "story" if is_story else "feed post"

    gen = generate_fn or _default_generate
    # The caption remains the source of claim text. A CTA must already occur in
    # that caption; None lets creative_studio render its approved URL footer.
    extra = ({"cta": _caption_approved_cta(str(row["caption"])),
              "footer": None, "draft_id": str(row["id"])}
             if lasso_quality else {})
    result = gen(headline, facts, client=client, aspect=aspect, pixels=pixels,
                surface=surface, account_key=account_key, **extra)
    if not result or not result.get("path"):
        return {"ok": False, "reason": REASON_GENERATE_FAILED}

    host = host_fn or media_host.host_media
    url = host(result["path"], account_key)
    if not url:
        return {"ok": False, "reason": REASON_HOSTING}

    if lasso_quality:
        from .infographic_artifacts import ArtifactStore
        caption = str(row["caption"])
        try:
            ArtifactStore().save(account_key, url, result["path"],
                {"source_id": f"content_calendar:{row['id']}:caption",
                 "source_hash": hashlib.sha256(caption.encode("utf-8")).hexdigest()})
        except Exception:
            return {"ok": False, "reason": "review_evidence_not_saved"}

    return {"ok": True, "image_url": url, "prompt": result.get("prompt", ""),
           "model": result.get("model", ""), "route": result.get("route", "")}


def _default_generate(headline, facts, client=None, aspect=None, pixels=None,
                      surface=None, account_key=None, cta="", footer=None, draft_id=""):
    from . import creative_studio
    return creative_studio.generate(
        headline, facts, client=client, aspect=aspect, pixels=pixels,
        surface=surface, account_key=account_key, cta=cta, footer=footer,
        draft_id=draft_id)
