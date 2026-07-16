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
def _sidecar_path(creative_path):
    return os.path.splitext(creative_path or "")[0] + ".json"


def recorded_rendered_text(creative_path):
    """The rendered text recorded in a card's json sidecar: the `rendered_text`
    field (the exact strings we asked the generator to render), else empty. This
    is the deterministic, free gate input written once at generation / regen /
    scan time."""
    import json
    try:
        with open(_sidecar_path(creative_path), encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except Exception:
        return ""
    val = data.get("rendered_text") or data.get("rendered_headline") or ""
    if isinstance(val, list):
        val = "\n".join(str(v) for v in val)
    return str(val).strip()


def write_rendered_text(creative_path, rendered_text):
    """Record the rendered text into the card's sidecar (merge, never clobber
    other fields). Best effort: a missing/unwritable sidecar never raises."""
    import json
    path = _sidecar_path(creative_path)
    data = {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except Exception:
        data = {}
    data["rendered_text"] = rendered_text
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        return True
    except Exception as e:
        print(f"[pixel-gate] could not record rendered text for "
              f"{os.path.basename(creative_path)}: {type(e).__name__}: {e}")
        return False


def ocr_rendered_text(creative_path, reader=None):
    """One-time OCR read of the pixels (the ingest belt). Returns the read text or
    '' when no reader is available (studio dark). Never raises."""
    reader = reader or _default_reader()
    if reader is None or not creative_path or not os.path.isfile(creative_path):
        return ""
    try:
        with open(creative_path, "rb") as fh:
            return (reader(fh.read()) or "").strip()
    except Exception as e:
        print(f"[pixel-gate] OCR read failed for {os.path.basename(creative_path)}: "
              f"{type(e).__name__}: {e}")
        return ""


def _default_reader():
    """The Gemini vision reader (studio armed + key present), else None. Reuses
    ocr_check's reader so there is one transcription path."""
    from .ocr_check import _default_reader as _r
    return _r()


def resolve_rendered_text(creative_path, reader=None, allow_ocr=True, record=True):
    """
    The best available rendered text for a creative, in priority order:
      1. the recorded sidecar `rendered_text` (free, deterministic), else
      2. a one-time OCR read of the pixels (belt), recorded back to the sidecar
         so the next draw is free, else
      3. '' (unverifiable: no record, no reader).
    Returns (text, source) where source is 'recorded' | 'ocr' | 'none'.
    """
    recorded = recorded_rendered_text(creative_path)
    if recorded:
        return recorded, "recorded"
    if allow_ocr:
        read = ocr_rendered_text(creative_path, reader=reader)
        if read:
            if record:
                write_rendered_text(creative_path, read)
            return read, "ocr"
    return "", "none"


def gate_creative(creative, approved_claims=None, reader=None, allow_ocr=True):
    """
    The draw-time pixel gate for a library creative. Returns (ok, reason):
      - ok True, reason ""              -> rendered text is clean (or unverifiable
                                           with no reader: nothing to gate on yet).
      - ok False, reason "..."          -> a rendered number has no approved
                                           receipt; the reason NAMES the number(s).
    A caption/client note is ALSO checked (the old behavior) so nothing regresses.
    """
    path = getattr(creative, "path", "") or ""
    note = getattr(creative, "client_note", "") or ""
    rendered, _src = resolve_rendered_text(path, reader=reader, allow_ocr=allow_ocr)
    claims = approved_claims if approved_claims is not None else _approved_claims()
    combined = "\n".join(t for t in (rendered, note) if t)
    nums = offending_numbers(combined, claims)
    if nums:
        return False, ("rendered creative carries a stat with no approved receipt: "
                       + ", ".join(nums))
    return True, ""
