"""
Fabrication gate on RENDERED creative text (the pixels), not just captions.

The caption fabrication gate was always watching the words in a post. It never
watched the words baked INTO the image: a headline, a stat, an overlay. A card
that rendered "80% more conversions" as a stat slab shipped to the approval
queue because that number lives only in the pixels, in NO caption and in NO card
metadata, and nothing read the pixels.

This module closes that gap with the SAME rule the caption gate uses (rotation
.is_gate_clean): any number, percentage, or claim ("N%", "$N", "N times/x")
rendered into a creative must resolve VERBATIM to an approved receipt in the
sources (knowledge USE-lines + approved social proof entries). An unresolved
figure BLOCKS the card and NAMES the offending number; it never softens, never
falls back, never publishes. Approval + publish gates are untouched: this only
decides whether a card is approvable material at all.

Two layers, per Blake's call (OCR at ingest, gate daily):
  1. DETERMINISTIC (always on, free): a creative carries its rendered text in its
     sidecar (`rendered_text`, written once at generation / regen / first scan).
     Every draw re-checks that recorded text against approved receipts for free.
  2. OCR BELT (when the studio is armed): the pixels are read once at ingest and
     that read is recorded, so a generator that silently drifts a number is
     caught the first time the card is seen, then gated for free forever after.

Nothing here imports the drafter or the studio at module load (both import paths
that would import us back); the few cross-module calls are lazy inside functions.
"""

import os
import re

# Same claim detector the caption gate uses, word for word (rotation._CLAIM_RE):
# a sentence carrying a percent, a dollar figure, or an "N times / N x" claim.
_CLAIM_RE = re.compile(r"%|\bpercent\b|\$\s?\d|\b\d+(?:\.\d+)?\s*(?:x|times)\b",
                       re.IGNORECASE)
# The specific numeric token to NAME in the block reason (so Blake sees the number).
_NUM_TOKEN_RE = re.compile(r"\$?\d[\d,\.]*\s*(?:%|percent|x|times)?", re.IGNORECASE)


def _approved_claims():
    """Every cleared claim: USE-marked knowledge stats + approved social proof
    lines. Lazy import (rotation pulls the drafter) so we never cycle."""
    from . import rotation
    return rotation._approved_claims()


def _sentences(text):
    return re.split(r"(?<=[.!?])\s+", (text or "").strip())


def uncleared_sentences(text, approved_claims=None):
    """
    The claim-bearing sentences in `text` NOT cleared by an approved receipt.
    Empty list = clean (no claims, or every claim resolves). Same containment
    rule as rotation.is_gate_clean: a rendered line must appear verbatim inside
    (or exactly equal) an approved claim line. "80% more conversions" is NOT a
    substring of "lift conversions up to 80 percent", so it fails; the approved
    wording passes.
    """
    text = (text or "").strip()
    if not text:
        return []
    claims = approved_claims if approved_claims is not None else _approved_claims()
    out = []
    for s in _sentences(text):
        s = s.strip()
        if not s or not _CLAIM_RE.search(s):
            continue
        if not any(s in c or c in s for c in claims):
            out.append(s)
    return out


def offending_numbers(text, approved_claims=None):
    """The numeric tokens inside the uncleared sentences, deduped in order, so a
    block reason can name exactly which number has no approved source."""
    seen, out = set(), []
    for s in uncleared_sentences(text, approved_claims):
        for tok in _NUM_TOKEN_RE.findall(s):
            tok = tok.strip()
            if tok and tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


def is_clean(text, approved_claims=None):
    """True when every claim in the rendered text resolves to an approved receipt."""
    return not uncleared_sentences(text, approved_claims)


# ---- reading a creative's rendered text ---------------------------------------
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
# The sentinel recorded when a successful read found NO text on the image (a pure
# photo / no overlay): it is EXEMPT from the gate henceforth, and distinct from a
# card that was never scanned at all.
_NO_TEXT = "__no_rendered_text__"


def _sidecar_path(creative_path):
    if creative_path and os.path.isdir(creative_path):
        return os.path.join(creative_path, "note.json")  # carousel sidecar
    return os.path.splitext(creative_path or "")[0] + ".json"


def _renderable_images(creative_path):
    """The still images whose pixels we can read for this creative: the file
    itself (an image), or a carousel folder's slides. A video or a missing path
    yields [] (nothing this gate can OCR)."""
    if not creative_path:
        return []
    if os.path.isdir(creative_path):
        return sorted(
            os.path.join(creative_path, n) for n in os.listdir(creative_path)
            if os.path.splitext(n)[1].lower() in _IMAGE_EXTS)
    ext = os.path.splitext(creative_path)[1].lower()
    if ext in _IMAGE_EXTS and os.path.isfile(creative_path):
        return [creative_path]
    return []


def has_renderable_creative(creative_path):
    """True when this creative is a still image (or a carousel of images) whose
    text we are expected to verify. A video or a missing/empty path is False
    (nothing to OCR here; videos are a documented gap, never silently 'clean')."""
    return bool(_renderable_images(creative_path))


def recorded_rendered_text(creative_path):
    """
    The rendered text recorded in a card's sidecar, or None when NOTHING was ever
    recorded. Distinguishes three states the gate needs to tell apart:
      - None                -> never scanned (no `rendered_text` key present)
      - "" (empty string)   -> scanned, the image carries no text (pure visual)
      - "<text>"            -> the recorded rendered text to gate
    """
    import json
    try:
        with open(_sidecar_path(creative_path), encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except Exception:
        return None
    if "rendered_text" not in data and "rendered_headline" not in data:
        return None
    val = data.get("rendered_text")
    if val is None:
        val = data.get("rendered_headline")
    if isinstance(val, list):
        val = "\n".join(str(v) for v in val)
    val = str(val or "").strip()
    return "" if val == _NO_TEXT else val


def write_rendered_text(creative_path, rendered_text):
    """Record the rendered text into the card's sidecar (merge, never clobber
    other fields). An empty read records the NO_TEXT sentinel so the card reads as
    'scanned, no text' rather than 'never scanned'. Best effort: never raises."""
    import json
    path = _sidecar_path(creative_path)
    data = {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except Exception:
        data = {}
    data["rendered_text"] = rendered_text if rendered_text else _NO_TEXT
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        return True
    except Exception as e:
        print(f"[pixel-gate] could not record rendered text for "
              f"{os.path.basename(creative_path)}: {type(e).__name__}: {e}")
        return False


def _ocr_attempt(creative_path, reader=None):
    """
    Try to read the pixels. Returns (ran, text):
      - ran False           -> the read could NOT run (no reader, or every image
                               errored): the caller must treat this as unverifiable.
      - ran True, text ""   -> the read ran and found no text (pure visual).
      - ran True, text "..."-> the read ran and returned this text.
    Never raises.
    """
    reader = reader or _default_reader()
    if reader is None:
        return False, ""
    images = _renderable_images(creative_path)
    if not images:
        return False, ""
    reads, any_ok = [], False
    for img in images:
        try:
            with open(img, "rb") as fh:
                reads.append((reader(fh.read()) or "").strip())
            any_ok = True
        except Exception as e:
            print(f"[pixel-gate] OCR read failed for {os.path.basename(img)}: "
                  f"{type(e).__name__}: {e}")
    if not any_ok:
        return False, ""
    return True, "\n".join(t for t in reads if t)


def _default_reader():
    """The Gemini vision reader (studio armed + key present), else None. Reuses
    ocr_check's reader so there is one transcription path."""
    from .ocr_check import _default_reader as _r
    return _r()


def resolve_rendered_text(creative_path, reader=None, allow_ocr=True, record=True):
    """
    The best available rendered text for a creative. Returns (text, source):
      - ('<text>', 'recorded') -> the sidecar had recorded text.
      - ('',       'recorded') -> the sidecar recorded 'scanned, no text' (exempt).
      - ('<text>', 'ocr')      -> a fresh OCR read (recorded back for next time).
      - ('',       'ocr_empty')-> a fresh read found no text (recorded; exempt).
      - ('',       'no_creative') -> nothing renderable to read (video / no image).
      - ('',       'unreadable')  -> HAS renderable pixels but they could not be
                                     read (no reader / read failed): fail closed.
    """
    recorded = recorded_rendered_text(creative_path)
    if recorded is not None:
        return recorded, "recorded"
    if not has_renderable_creative(creative_path):
        return "", "no_creative"
    if allow_ocr:
        ran, text = _ocr_attempt(creative_path, reader=reader)
        if ran:
            if record:
                write_rendered_text(creative_path, text)
            return (text, "ocr") if text else ("", "ocr_empty")
    return "", "unreadable"


def gate_creative(creative, approved_claims=None, reader=None, allow_ocr=True,
                  require_verification=None):
    """
    The draw-time pixel gate for a creative. Returns (ok, reason):
      - (True, "")   -> clean: rendered text resolves to approved receipts, OR the
                        creative carries no rendered text (pure visual / video).
      - (False, "...")-> BLOCKED. Two block classes, both named plainly:
          * a rendered stat with no approved receipt (the number is named), or
          * FAIL CLOSED: the creative HAS rendered pixels the gate could not read
            or verify ("could not verify rendered text against approved claims").

    Fail-closed applies when verification is EXPECTED. By default that is whenever
    the creative studio is armed (config.creative_studio_enabled()): in production
    the OCR reader is available, so an unreadable image means an outage, not an
    excuse to pass. With the studio fully disarmed there is no vision path to fail
    closed on, so an un-scanned image falls back to the deterministic note check
    (this keeps dev / non-OCR deployments working); pass require_verification=True
    to force fail-closed regardless.
    """
    from . import config
    path = getattr(creative, "path", "") or ""
    note = getattr(creative, "client_note", "") or ""
    claims = approved_claims if approved_claims is not None else _approved_claims()
    if require_verification is None:
        require_verification = config.creative_studio_enabled()

    rendered, src = resolve_rendered_text(path, reader=reader, allow_ocr=allow_ocr)

    # FAIL CLOSED: rendered pixels we could not read/verify, and verification is
    # expected -> block. A card with no renderable creative is exempt (nothing to
    # fabricate); the note is still checked below.
    if src == "unreadable" and require_verification:
        return False, ("could not verify rendered text against approved claims "
                       "(the creative has rendered pixels but the OCR read is "
                       "unavailable or failed; blocked fail-closed).")

    combined = "\n".join(t for t in (rendered, note) if t)
    nums = offending_numbers(combined, claims)
    if nums:
        return False, ("rendered creative carries a stat with no approved receipt: "
                       + ", ".join(nums))
    return True, ""
