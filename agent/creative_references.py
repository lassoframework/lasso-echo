"""
creative_references.py — approved visual references for the Astra brief.

Spec section 3: pass SELECTED reference images into the actual generation API
request as real image inputs (image_engine.AstraImageEngine.generate accepts
opts["reference_images"], each a dict with b64/bytes + mime + id — see that
module for the exact Responses API `input_image` shape), not just a filename
mentioned in text. References influence typography hierarchy, spacing, diagram
clarity, visual detail, and finish — NEVER the new card's copy: a reference's
own headline/claims must never leak onto the new card (the brief still carries
only the caller's approved headline/facts/CTA; nothing here touches text).

WHY IN-REPO ASSETS, NOT THE ORIGINAL SOURCE FOLDERS: this module ships as part
of the Echo pipeline and runs on Railway, which has none of Blake's local
folders (~/Documents/Codex/..., ~/Desktop/...). A hardcoded path to a laptop-only
folder would work in this branch's local run and then silently do nothing in
production. So a small, curated set of the ALREADY-APPROVED reference images is
copied into agent/assets/reference/ and committed with this change:

  full_gym/  — 2 feed-format cards from the approved 20-concept/40-image "Full
               Gym — Free Book Ads" package (Codex approved-campaign, 2026-09-10,
               already quality-reviewed per that package's own quality-review.md)
  summit/    — 2 concept cards from this repo's own
               content_library/summit_sprint_preview/ (LASSO Growth Summit,
               Nov 7-8 2026). No single folder in this repo is labeled "approved"
               for Summit the way Full Gym's package is; these are the existing
               working concept renders. Blake should confirm/replace these with
               his own designated "approved" Summit set if one exists elsewhere
               (see the build report for the exact ask).

OFF BY DEFAULT: config.astra_reference_images_enabled() gates whether
creative_studio.generate() attaches these at all (AGENT_ASTRA_REFERENCE_IMAGES).
"""

import hashlib
import os

_ASSET_ROOT = os.path.join(os.path.dirname(__file__), "assets", "reference")

# kind -> ordered list of filenames under _ASSET_ROOT/<kind>/
REFERENCE_SETS = {
    "lasso_content": ["growth-levers.png", "lead-followup.png", "halo-effect.png", "growth-playbook.png"],
    "full_gym": ["01-growth-controls.png", "02-client-lens.png"],
    "summit": ["02_deliverable_a_story.png", "04_funnel_a_story.png"],
}


def _ref_id(path: str) -> str:
    """A short stable id for a reference file (its content hash), used for
    logging/generation-record purposes — never a filename in a text prompt."""
    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    return digest[:12]


def references_for(kind: str, limit=None) -> list:
    """Load up to `limit` (or config.astra_reference_max()) reference images for
    `kind` ("full_gym" or "summit"). Returns a list of
    {"id", "path", "bytes", "b64", "mime"} dicts, oldest-listed-first. An
    unknown kind or a missing file returns [] (never raises: a reference is a
    quality enhancement, never a hard dependency for a card to render)."""
    from . import config
    names = REFERENCE_SETS.get(str(kind or "").strip().lower(), [])
    if not names:
        return []
    cap = limit if limit is not None else config.astra_reference_max()
    out = []
    for name in names[:cap] if cap else []:
        path = os.path.join(_ASSET_ROOT, str(kind).strip().lower(), name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
            import base64
            out.append({
                "id": _ref_id(path),
                "path": path,
                "bytes": raw,
                "b64": base64.b64encode(raw).decode("ascii"),
                "mime": "image/png",
            })
        except Exception:
            continue
    return out


def kind_for(headline: str = "", account_key: str = None, tags=None) -> str:
    """Best-effort reference kind for one card: explicit tag wins; else sniff
    the headline/account for "summit" or "full gym"/"book"; else "" (no
    reference set applies, e.g. a routine gym-marketing card with no matching
    approved reference package)."""
    for t in (tags or []):
        t = str(t or "").strip().lower()
        if t in REFERENCE_SETS:
            return t
    hay = f"{headline} {account_key or ''}".lower()
    if "summit" in hay:
        return "summit"
    if "full gym" in hay or "full-gym" in hay or "fullgym" in hay:
        return "full_gym"
    return ""
