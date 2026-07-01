"""
Brand voice loader.

The voice doc is the ONLY source of voice and the ONLY source of approved
phrasing the drafter may lean on. If it is missing or empty, the agent drafts
NOTHING and says so. No voice doc, no posts. Hard rule.
"""

import os
import re
from dataclasses import dataclass, field


class VoiceDocMissing(Exception):
    pass


@dataclass
class VoiceDoc:
    raw: str
    hashtags: list  # approved hashtags pulled from the doc, never invented
    ctas: list = field(default_factory=list)  # approved CTA rotation from the doc

    @property
    def text(self):
        return self.raw


def load_voice(path):
    """
    Load the voice doc. Returns a VoiceDoc, or None if the file is missing/empty.
    Callers MUST treat None as 'do not draft'.
    """
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return None
    return VoiceDoc(
        raw=raw,
        hashtags=_extract_hashtags(raw),
        ctas=_extract_ctas(raw),
    )


def _extract_hashtags(raw):
    """
    Only hashtags that literally appear in the approved doc are usable. Hex color
    codes (e.g. #121E3C, #FF0000) live in the visual-identity section and must NOT
    be treated as hashtags, so they are filtered out.
    """
    found = re.findall(r"#[A-Za-z0-9_]+", raw)
    seen, out = set(), []
    for h in found:
        body = h[1:]
        # drop hex color codes like #FFFFFF or #1B3 (3 or 6 hex chars)
        if re.fullmatch(r"[0-9A-Fa-f]{3}", body) or re.fullmatch(r"[0-9A-Fa-f]{6}", body):
            continue
        if h.lower() in seen:
            continue
        seen.add(h.lower())
        out.append(h)
    return out


def _extract_ctas(raw):
    """
    Extract the numbered CTA rotation list from the '### CTA rotation' section
    of the voice doc. Returns a list of CTA strings in order.
    Only CTAs that literally appear in the approved doc are usable.
    """
    # Find the CTA rotation section
    section_match = re.search(
        r"###\s+CTA rotation.*?\n(.*?)(?=\n###|\n##|\Z)",
        raw,
        re.DOTALL | re.IGNORECASE,
    )
    if not section_match:
        return []

    section_text = section_match.group(1)
    # Extract numbered items: "1. Some CTA text."
    items = re.findall(r"^\d+\.\s+(.+)$", section_text, re.MULTILINE)
    return [item.strip() for item in items if item.strip()]
