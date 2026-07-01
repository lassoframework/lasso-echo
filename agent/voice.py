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
    Extract the approved CTA rotation from the '### CTA rotation' section of the
    voice doc. Reads ONLY that section, up to the next heading.

    Two doc styles are supported so the extractor stays robust as the bible
    evolves:
      - quoted CTAs   -> "Save this post." "Tag a gym owner."
      - list items    -> 1. Save this post.   or   - Tag a gym owner.
    If any quoted strings exist in the section they win; otherwise numbered /
    bulleted list items are used.

    In every case: whitespace is normalized, any candidate containing '[' or ']'
    is SKIPPED (those are templated placeholders, not approved copy), and the
    result is deduped preserving order. Only CTAs that literally appear in the
    approved doc are ever usable — nothing is invented here.
    """
    section_match = re.search(
        r"###\s+CTA rotation.*?\n(.*?)(?=\n###|\n##|\Z)",
        raw,
        re.DOTALL | re.IGNORECASE,
    )
    if not section_match:
        return []

    section_text = section_match.group(1)
    out, seen = [], set()

    def _add(candidate):
        cta = re.sub(r"\s+", " ", candidate).strip()
        if not cta:
            return
        if "[" in cta or "]" in cta:  # skip templated placeholders
            return
        key = cta.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(cta)

    quoted = re.findall(r'"([^"]+)"', section_text)
    if quoted:
        for q in quoted:
            _add(q)
        return out

    for line in section_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(?:\d+[.)]|[-*])\s+(.+)$", line)
        if not m:
            continue
        _add(m.group(1))
    return out
