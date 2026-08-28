"""
story_grounding.py — where overlay + caption copy is GROUNDED (spec §0).

Copy control, client first:
  1. A client BRIEF (the optional "What's this about?" one-liner on the upload / the
     Story request) grounds the overlay + caption. The brief is COMPRESSED, never
     contradicted, never added to. When a brief is present it wins.
  2. No brief -> the VISION sidecar grounds it (the analysis stored on the asset:
     subjects, setting, mood, verified details). Only what vision actually saw is used.
  3. LOW CONFIDENCE (no brief AND the vision sidecar is weak/absent) -> the caller
     ships a GENERIC-SAFE overlay + a flag ("no brief, edit before approving"). Echo
     NEVER fabricates a claim, a number, or a name it did not see.

This module decides WHICH source grounds the copy and returns a Grounding verdict
the overlay builder consumes. It never invents facts: with a brief it returns the
brief text; with only weak vision it returns low_confidence=True so the overlay goes
generic-safe.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SOURCE_BRIEF = "brief"
SOURCE_VISION = "vision"
SOURCE_GENERIC = "generic_safe"

# Below this vision confidence (or with no usable subjects), grounding is low.
_VISION_CONF_FLOOR = 0.55


@dataclass
class Grounding:
    source: str                       # brief | vision | generic_safe
    text: str = ""                    # the grounded copy seed (empty for generic_safe)
    low_confidence: bool = False
    flags: list = field(default_factory=list)


def _brief_ok(brief):
    b = str(brief or "").strip()
    return len(b) >= 3


def _vision_seed(analysis):
    """A short, factual copy seed from the vision sidecar, or ('', conf) when the
    analysis is too weak to ground anything. Uses ONLY what vision recorded."""
    if not analysis or analysis.get("analysis_failed"):
        return "", 0.0
    conf = float(analysis.get("confidence") or 0.0)
    subjects = analysis.get("subjects") or []
    setting = analysis.get("setting") or ""
    mood = analysis.get("mood") or ""
    parts = []
    if subjects:
        parts.append(", ".join(str(s) for s in subjects[:3]))
    if setting:
        parts.append(str(setting))
    if mood:
        parts.append(str(mood))
    return (" ".join(parts).strip(), conf)


def ground_copy(brief=None, analysis=None):
    """Decide the grounding source for one overlay/caption.

    Returns a Grounding. Brief present -> source=brief, text=brief (compressed at the
    overlay/caption layer, never contradicted). No brief but a confident vision
    sidecar -> source=vision, text=the vision seed. Otherwise -> source=generic_safe,
    low_confidence=True, with the 'no brief, edit before approving' flag (the overlay
    builder then ships a generic-safe overlay)."""
    if _brief_ok(brief):
        return Grounding(source=SOURCE_BRIEF, text=str(brief).strip())

    seed, conf = _vision_seed(analysis)
    if seed and conf >= _VISION_CONF_FLOOR:
        return Grounding(source=SOURCE_VISION, text=seed)

    return Grounding(
        source=SOURCE_GENERIC, text="", low_confidence=True,
        flags=["no brief and low vision confidence: overlay is generic-safe, "
               "edit before approving"])
