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
        resp = client.models.generate_content(
            model=config.NANO_MODEL,
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
