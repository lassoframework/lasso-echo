"""
Creative studio: Nano Banana infographic generation (Gemini image API).

OFF BY DEFAULT (`config.creative_studio_enabled()`). Two guarantees mirror the
rest of Echo:

  - NO FABRICATION: the prompt is built ONLY from approved input — the headline and
    the facts passed in. Empty facts -> generate NOTHING (return None), the same
    block-on-missing-input contract the drafter uses.
  - NO SECRETS: the API key is read lazily from env, never stored on a returned
    object, never logged. The real client is built only when the flag is on AND a
    key is present.

Publishing is unaffected: this module never posts and never touches the publish
flag. It only draws an image a human then reviews through the normal approval gate.
"""

import os
import re

from . import config


# LOCKED LASSO V3 brand palette for infographic styling. The directive tells the model
# HOW to use each color (role + usage), not just lists hexes, so images come out on
# brand (navy/red/sky/cream) instead of navy/gray/white.
BRAND_PALETTE = (
    "LOCKED LASSO V3 BRAND PALETTE (apply these exact colors, do not substitute or "
    "invent others):\n"
    "- Navy #121E3C: the PRIMARY, dominant dark. Use for the main background and the "
    "structure of the layout.\n"
    "- Red #FF0000: the accent for emphasis and key highlights. Use SPARINGLY, for the "
    "single most important element only (one arrow, or one key word), never everywhere.\n"
    "- Sky Blue #5EB9E6: the secondary accent. Use for supporting icons, lines, and "
    "highlights.\n"
    "- Cream #FAF6F0: the light background, negative space, and card fill. Use cream as "
    "the light background instead of pure white.\n"
    "Style: clean, flat, modern infographic; high contrast; brand-consistent. Navy is "
    "the dominant dark, cream is the light background (not white), sky blue carries the "
    "supporting icons and lines, and red is reserved for one single focal accent."
)

# Composition: a LOCKED house style (consistent every card so the run reads as one brand
# system) while the illustrated SUBJECT varies by pillar. No forced monitor/dashboard.
COMPOSITION_STYLE = (
    "House style (keep this CONSISTENT on every card so the whole run reads as one brand "
    "system): a clean, minimal, modern FLAT infographic with generous negative space, "
    "uncluttered and premium. Use an icon-driven left to right (or otherwise simple) flow "
    "with a few clear, labeled icons in a simple line-and-icon illustration style, a "
    "consistent stroke weight, and the brand palette throughout.\n"
    "Subject varies by pillar: choose simple icons that FIT this card's topic and message. "
    "Do NOT default to a computer, monitor, or dashboard every time; pick the everyday "
    "objects relevant to the subject, rendered in the SAME clean house style and palette. "
    "Avoid a dense collage of many icons and boxes.\n"
    "Text: render ONLY the one short headline as text on the image; do NOT put body "
    "sentences, paragraphs, or the caption on the image. Labels on icons are one or two "
    "words at most. Overall feel: minimal, modern, high end, brand-consistent, easy to "
    "read at a glance. Think one clean diagram, not a busy poster."
)

# Copy mechanics from the brand bible: rendered copy carries no dashes.
NO_DASH_RULE = (
    "Copy mechanics: no em dashes, no en dashes, avoid hyphens in the rendered "
    "text. Use the word 'to' for ranges."
)


def _scrub_dashes(text):
    """Strip em/en dashes from approved copy (the brand no-dash rule). Hyphens and
    newlines are left intact; only the doubled spaces a removed dash leaves are
    collapsed."""
    if not text:
        return ""
    cleaned = str(text).replace("—", " ").replace("–", " ")
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def build_prompt(headline, facts):
    """
    Build the image prompt from APPROVED input only. The single on-image headline is
    the approved pillar hook (kept short); the approved body lines are passed as CONCEPT
    CONTEXT for the focal graphic and are NOT rendered as text on the image (the caption
    carries the words). Plus the brand palette, composition style, and no-dash rule.
    Dashes in the approved text are scrubbed. Nothing is invented.
    """
    fact_lines = "\n".join(f"- {_scrub_dashes(f)}" for f in facts if str(f).strip())
    prompt = (
        "Design a clean, minimal, premium LASSO-branded infographic.\n"
        f"Headline (the ONLY text to render on the image, keep it short): "
        f"{_scrub_dashes(headline)}\n"
        "Concept context for the single focal graphic (do NOT render this text on the "
        "image; the caption carries the words):\n"
        f"{fact_lines}\n"
        f"{COMPOSITION_STYLE}\n"
        f"{BRAND_PALETTE}\n"
        f"{NO_DASH_RULE}"
    )
    return _scrub_dashes(prompt)


class _GeminiImageClient:
    """
    Thin wrapper over the Gemini image API. Constructed only with a present key;
    the key is held for the call and never logged, printed, or returned.
    """

    def __init__(self, api_key, genai_client=None):
        self._api_key = api_key          # never logged or exposed
        self._genai_client = genai_client  # injection seam for tests; None in production

    def generate_image(self, prompt, model):
        # The Gemini image models (gemini-3-pro-image, gemini-2.5-flash-image, ...)
        # support generateContent, NOT predict/generate_images. Lazy import so
        # flag-off / draft-only never needs the SDK installed.
        client = self._genai_client
        if client is None:
            from google import genai  # type: ignore
            from google.genai import types  # noqa: F401

            client = genai.Client(api_key=self._api_key)
        resp = client.models.generate_content(model=model, contents=prompt)
        # The image comes back as inline data on a response part. Return the first one.
        for part in resp.candidates[0].content.parts:
            if getattr(part, "inline_data", None):
                return part.inline_data.data  # raw image bytes
        raise ValueError("no image returned from Gemini")


def _default_client():
    """
    Build the real Nano Banana (Gemini image) client, but ONLY when the flag is on
    AND a key is present. Returns None otherwise so generate() no-ops safely. The
    key is read here lazily and never logged or returned.
    """
    if not config.creative_studio_enabled():
        return None
    key = os.environ.get(config.NANO_API_KEY_ENV)
    if not key:
        return None
    return _GeminiImageClient(key)


def generate(headline, facts, client=None, out_path=None):
    """
    Generate a LASSO infographic from APPROVED input. Returns {"path", "prompt"} on
    success, or None when it must not run:
      - the flag is OFF (creative_studio_enabled() is False) -> None, no API call
      - facts is empty (the no-fabrication gate)             -> None, no API call
      - no client and no key available                       -> None, no API call
    """
    if not config.creative_studio_enabled():
        return None
    if not facts:
        return None

    prompt = build_prompt(headline, facts)

    client = client or _default_client()
    if client is None:
        return None

    image_bytes = client.generate_image(prompt=prompt, model=config.NANO_MODEL)

    if out_path is None:
        slug = re.sub(r"[^a-z0-9]+", "_", (headline or "infographic").lower()).strip("_") or "infographic"
        out_path = os.path.join(config.LIBRARY_PATH, f"nano_{slug}.png")
    with open(out_path, "wb") as fh:
        fh.write(image_bytes)

    return {"path": out_path, "prompt": prompt}
