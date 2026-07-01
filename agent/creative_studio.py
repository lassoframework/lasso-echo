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


# LASSO brand palette for infographic styling. Anchored on the documented brand
# navy (#121E3C — see the visual-identity note in agent/voice.py). White + slate
# are neutral supports for contrast, not brand claims.
BRAND_PALETTE = (
    "LASSO brand palette: deep navy #121E3C as the primary, clean white #FFFFFF "
    "background, slate gray #6C6C6C for secondary text. Bold, high contrast, modern."
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
    Build the image prompt from APPROVED input only: the headline + the facts, plus
    the brand palette and the no-dash rule. Dashes in the approved text are scrubbed.
    """
    fact_lines = "\n".join(f"- {_scrub_dashes(f)}" for f in facts if str(f).strip())
    prompt = (
        "Design a clean LASSO-branded infographic.\n"
        f"Headline: {_scrub_dashes(headline)}\n"
        "Facts to feature (verbatim, do not invent any others):\n"
        f"{fact_lines}\n"
        f"{BRAND_PALETTE}\n"
        f"{NO_DASH_RULE}"
    )
    return _scrub_dashes(prompt)


class _GeminiImageClient:
    """
    Thin wrapper over the Gemini image API. Constructed only with a present key;
    the key is held for the call and never logged, printed, or returned.
    """

    def __init__(self, api_key):
        self._api_key = api_key  # never logged or exposed

    def generate_image(self, prompt, model):
        # Lazy import so flag-off / draft-only never needs the SDK installed.
        from google import genai  # type: ignore

        client = genai.Client(api_key=self._api_key)
        resp = client.models.generate_images(model=model, prompt=prompt)
        return resp.generated_images[0].image.image_bytes


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
