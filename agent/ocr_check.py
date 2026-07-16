"""
Headline OCR check (quality guard), gated by AGENT_OCR_CHECK_ENABLED (OFF).

After a card renders, read the headline text off the image and fuzzy-match it
against the intended headline. A mismatch FLAGS the Slack card with a warning
line; it never blocks (Blake decides at the tap).

Implementation note, stated plainly: the container has no pure python OCR path
(tesseract is not installed and pillow has no OCR), so the read uses the
EXISTING Gemini vision call at lowest cost (one short transcription request per
generated card, same lazy client as creative_studio). The reader is injectable
so tests never spend anything.
"""

import difflib
import re

from . import config

MATCH_THRESHOLD = 0.75
_TRANSCRIBE_PROMPT = ("Transcribe ONLY the single largest text on this image, "
                      "exactly as rendered. Reply with that text and nothing else.")


def _normalize(text):
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()


def _default_reader():
    """Gemini vision transcription (lazy; None when the studio is unarmed)."""
    if not config.creative_studio_enabled():
        return None
    import os
    key = os.environ.get(config.NANO_API_KEY_ENV)
    if not key:
        return None
    from google import genai  # lazy
    from google.genai import types as gtypes
    client = genai.Client(api_key=key)

    def _read(image_bytes):
        # OCR_MODEL, NOT NANO_MODEL: the generation model returns image parts, not
        # text, so it can never transcribe. This is the vision-capable text model.
        resp = client.models.generate_content(
            model=config.OCR_MODEL,
            contents=[gtypes.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                      _TRANSCRIBE_PROMPT])
        return getattr(resp, "text", "") or ""

    return _read


def headline_warning(image_path, intended_headline, reader=None):
    """
    None when the rendered headline matches (or the check cannot run); a warning
    LINE for the Slack card when it does not. NEVER blocks, never raises.
    """
    if not config.ocr_check_enabled():
        return None
    reader = reader or _default_reader()
    if reader is None:
        return None
    try:
        with open(image_path, "rb") as fh:
            rendered = reader(fh.read())
    except Exception:
        return None  # an unreadable image or a reader error never blocks a card
    ratio = difflib.SequenceMatcher(
        None, _normalize(rendered), _normalize(intended_headline)).ratio()
    if ratio >= MATCH_THRESHOLD:
        return None
    return (f"HEADLINE CHECK: the rendered text reads {rendered.strip()[:60]!r} "
            f"but the intended headline is {intended_headline[:60]!r}. "
            "Eyeball before approving.")


_NUM_RE = re.compile(r"\$?\d[\d,\.]*\s*(?:%|percent|x|times)?", re.IGNORECASE)


def _numbers(text):
    """The numeric/claim tokens in a string, normalized ('80 percent' -> '80%')."""
    out = []
    for tok in _NUM_RE.findall(text or ""):
        t = re.sub(r"\s+", "", tok.lower()).replace("percent", "%")
        if t and any(ch.isdigit() for ch in t):
            out.append(t)
    return out


def rendered_read(image_path, reader=None):
    """One raw OCR read of the largest text on an image, or '' when the read
    cannot run. Shared by the block check and pixel_gate's ingest belt."""
    reader = reader or _default_reader()
    if reader is None or not image_path:
        return ""
    try:
        with open(image_path, "rb") as fh:
            return (reader(fh.read()) or "").strip()
    except Exception:
        return ""


def headline_block(image_path, intended_headline, approved_claims=None, reader=None):
    """
    A BLOCK reason (string) when the PIXELS carry a number they must not, else
    None. This is the pixel fabrication belt: it reads the image and blocks when

      - a number rendered on the image does NOT appear in the intended headline
        (a silent generator drift, e.g. the "80% more conversions" slab), or
      - a rendered number resolves to no approved receipt at all.

    Never raises. Returns None (no block) when the reader is unavailable; the
    deterministic recorded-text gate still holds the line in that case.
    """
    reader = reader or _default_reader()
    if reader is None:
        return None
    rendered = rendered_read(image_path, reader=reader)
    if not rendered:
        return None
    rendered_nums = _numbers(rendered)
    if not rendered_nums:
        return None  # no numbers on the image: nothing for this belt to gate
    intended_nums = set(_numbers(intended_headline))
    drifted = [n for n in rendered_nums if n not in intended_nums]
    if not drifted:
        return None  # every rendered number is in the approved headline
    # A number on the image that the headline never asked for. Confirm it is also
    # not cleared by any approved receipt before blocking (belt, not a hair trigger).
    from . import pixel_gate
    unresolved = pixel_gate.offending_numbers(rendered, approved_claims)
    bad = [n for n in drifted if any(n.replace("%", "") in u.replace(" ", "").lower()
                                     or n in u for u in unresolved)] or (
        drifted if unresolved else [])
    if not bad:
        return None
    return ("rendered image carries a number with no approved receipt: "
            + ", ".join(sorted(set(bad))))
