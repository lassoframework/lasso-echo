"""The owned LASSO visual standard (brand_voice/lasso_visual_standard.md).

Single source for the user-approved grounded-editorial direction that replaced
the "futuristic graphics are welcome" era. The generation brief
(astra_prompt.build_content_brief, LASSO brand only) embeds the guide text;
the independent reviewer (infographic_review.evaluate) grades style
conformance against it; infographic_evidence.reviewed_asset refuses any
artifact not reviewed under this VERSION.

FAIL CLOSED: load_guidance() raises on a missing, undecodable, or empty
guide. A visual standard that cannot be read must degrade to no generation,
never to silently dropping the standard from the brief.
"""
import hashlib
from pathlib import Path

VERSION = "lasso-grounded-editorial-2026-10-09-v1"

_ROOT = Path(__file__).resolve().parent.parent
GUIDE_PATH = _ROOT / "brand_voice" / "lasso_visual_standard.md"
GUIDE_KEY = "brand_voice/lasso_visual_standard.md"


def load_guidance() -> str:
    """The full guide text, strict UTF-8, non-empty. Raises on any failure:
    missing file, malformed encoding, or empty content. Never returns a
    fallback and never silently omits the standard."""
    text = Path(GUIDE_PATH).read_text(encoding="utf-8", errors="strict")
    if not text.strip():
        raise ValueError(f"visual standard guide is empty: {GUIDE_PATH}")
    return text


def guide_sha256() -> str:
    """SHA-256 of the loaded guide text (UTF-8). Raises exactly like
    load_guidance when the guide is missing or malformed."""
    return hashlib.sha256(load_guidance().encode("utf-8")).hexdigest()
