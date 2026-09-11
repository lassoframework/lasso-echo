"""
Brand voice loader.

The voice doc is the ONLY source of voice and the ONLY source of approved
phrasing the drafter may lean on. If it is missing or empty, the agent drafts
NOTHING and says so. No voice doc, no posts. Hard rule.

PROVENANCE (2026-09-06, the scraped-bible finding). A bible can be written by a
human (an intake a person filled in, a draft a person reviewed and activated) or
AUTO-DRAFTED by a machine off a gym's public website. Both land in the same file
slot, so the file itself has to say which it is: an auto-drafted bible carries
AUTO_DRAFTED_MARKER on its first line and loads with `auto_drafted=True`.

That flag is what keeps a scrape out of the anti-fabrication rail. A human bible
is APPROVED source material and drafter._output_claims_cleared may clear a
caption's figures against it; an auto-drafted one is UNAPPROVED and may not,
because nobody has yet said the numbers on that website are true of this gym.
"""

import os
import re
from dataclasses import dataclass, field

# The first-line stamp an auto-drafted (machine-written, human-unapproved) brand
# bible carries. An HTML comment so it is invisible in rendered markdown, and a
# literal substring so detection needs no parser and cannot drift.
AUTO_DRAFTED_MARKER = "<!-- echo:auto-drafted -->"


class VoiceDocMissing(Exception):
    pass


@dataclass
class VoiceDoc:
    raw: str
    hashtags: list  # approved hashtags pulled from the doc, never invented
    ctas: list = field(default_factory=list)  # approved CTA rotation from the doc
    # True when this bible was written by a machine off a scrape and NO human has
    # approved it. Such a doc is never an approved source of figures.
    auto_drafted: bool = False

    @property
    def text(self):
        return self.raw


def is_auto_drafted(raw):
    """True when this bible text carries the auto-drafted stamp — machine-written
    from a scrape, approved by nobody."""
    return AUTO_DRAFTED_MARKER in str(raw or "")


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
        auto_drafted=is_auto_drafted(raw),
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
