"""
Daily content brain: plan one post per day from the APPROVED source doc only.

NO FABRICATION. Every caption line is lifted verbatim from `brand_voice/lasso_now.md`
(the "LASSO Now" source doc) — a hook, its body lines, and one approved CTA. This
module never writes new sentences. A missing doc, or a pillar with no approved copy,
BLOCKS the draft rather than inventing anything.

Expected source-doc structure (markdown; section names matched case-insensitively):

    ## Story
    <the story, free text>

    ## Pillars
    - Pillar One
    - Pillar Two

    ## Pillar copy bank
    ### Pillar: Pillar One
    Hook: <a hook line>
    Body: <a body line>
    Body: <another body line>

    ### Pillar: Pillar Two
    Hook: ...
    Body: ...

    ## CTAs
    - Save this post.
    - Tag a gym owner who needs this.

    ## Hashtags
    #LASSOFramework #GymMarketingMadeSimple
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date

from . import config

# Growth-hint CTAs sort first: they drive the save/send/reach signals that grow reach.
GROWTH_CTA_HINTS = ("save", "send", "tag", "share", "dm")


@dataclass
class SourceDoc:
    story: str = ""
    pillars: list = field(default_factory=list)            # pillar names, in order
    copy_bank: dict = field(default_factory=dict)          # name -> {"hooks":[], "bodies":[]}
    ctas: list = field(default_factory=list)
    hashtags: list = field(default_factory=list)

    def pillars_with_copy(self):
        """Pillar names (in copy-bank order) that actually carry approved copy."""
        return [name for name, blk in self.copy_bank.items()
                if blk.get("hooks") or blk.get("bodies")]

    def approved_lines(self):
        """Every line the brain is allowed to ship: hooks + bodies + CTAs, verbatim."""
        lines = set()
        for blk in self.copy_bank.values():
            lines.update(blk.get("hooks", []))
            lines.update(blk.get("bodies", []))
        lines.update(self.ctas)
        return lines


def _split_h2(text):
    """Map each '## Heading' (level 2) to its body text, until the next '## '."""
    sections, current, buf = {}, None, []
    for line in text.splitlines():
        if re.match(r"^##\s+", line) and not line.startswith("###"):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = re.sub(r"^##\s+", "", line).strip().lower()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def _first_section(sections, needle):
    for name, body in sections.items():
        if needle in name:
            return body
    return ""


def _parse_bullets(text):
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if m:
            out.append(m.group(1).strip().strip('"'))
    return out


def _parse_copy_bank(text):
    """Parse '### Pillar: NAME' blocks with their Hook:/Body: lines."""
    bank, current = {}, None
    for line in text.splitlines():
        m = re.match(r"^###\s+Pillar:\s*(.+?)\s*$", line, re.IGNORECASE)
        if m:
            current = m.group(1).strip()
            bank.setdefault(current, {"hooks": [], "bodies": []})
            continue
        if current is None:
            continue
        hook = re.match(r"^\s*Hook:\s*(.+?)\s*$", line, re.IGNORECASE)
        body = re.match(r"^\s*Body:\s*(.+?)\s*$", line, re.IGNORECASE)
        if hook:
            bank[current]["hooks"].append(hook.group(1).strip())
        elif body:
            bank[current]["bodies"].append(body.group(1).strip())
    return bank


def _extract_hashtags(text):
    """#tokens from the hashtags section; hex color codes are NOT hashtags."""
    out, seen = [], set()
    for h in re.findall(r"#[A-Za-z0-9_]+", text):
        body = h[1:]
        if re.fullmatch(r"[0-9A-Fa-f]{3}", body) or re.fullmatch(r"[0-9A-Fa-f]{6}", body):
            continue
        if h.lower() not in seen:
            seen.add(h.lower())
            out.append(h)
    return out


def load_source_doc(path=None):
    """
    Parse the approved source doc. Returns a SourceDoc, or None when the file is
    missing or empty (the brain then blocks — it never fabricates a fallback).
    """
    p = path or config.SOURCE_DOC_PATH
    try:
        with open(p, encoding="utf-8") as fh:
            raw = fh.read()
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None
    if not raw.strip():
        return None

    sections = _split_h2(raw)
    copy_bank = _parse_copy_bank(_first_section(sections, "pillar copy bank"))
    pillars = _parse_bullets(sections.get("pillars", "")) or list(copy_bank.keys())
    ctas = _parse_bullets(_first_section(sections, "cta"))
    hashtags = _extract_hashtags(_first_section(sections, "hashtag"))
    story = sections.get("story", "")
    return SourceDoc(story=story, pillars=pillars, copy_bank=copy_bank,
                     ctas=ctas, hashtags=hashtags)


def _day_seq(day_key):
    """A stable integer for a day. For a YYYY-MM-DD key it is the date ordinal (so
    consecutive days rotate sequentially); otherwise a stable hash."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(day_key))
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).toordinal()
        except ValueError:
            pass
    return int(hashlib.sha1(str(day_key).encode()).hexdigest(), 16)


def pick_pillar(doc, day_key):
    """Deterministic rotation across pillars that have approved copy. None if none do."""
    names = doc.pillars_with_copy()
    if not names:
        return None
    return names[_day_seq(day_key) % len(names)]


def pick_cta(doc, seed):
    """Deterministic, growth-biased CTA pick. Growth-hint CTAs are the pool when any
    exist, so a save/send/tag/share/dm CTA always wins over a plain one."""
    if not doc.ctas:
        return ""
    growth = [c for c in doc.ctas if any(h in c.lower() for h in GROWTH_CTA_HINTS)]
    pool = growth if growth else list(doc.ctas)
    return pool[int(hashlib.sha1(str(seed).encode()).hexdigest(), 16) % len(pool)]


def plan_for(day_key, path=None):
    """
    Plan one day's post from the source doc. Returns
      {pillar, caption, cta, hashtags, fragments}  on success, or
      {blocked: True, reason: ...}                 when it must not draft.

    The caption is assembled ONLY from source-doc lines: one hook + the pillar's body
    lines + one approved CTA. Nothing is generated.
    """
    doc = load_source_doc(path)
    if doc is None:
        return {"blocked": True, "reason": "Source doc missing or empty. Not drafting."}

    pillar = pick_pillar(doc, day_key)
    if pillar is None:
        return {"blocked": True, "reason": "No pillar has approved copy in the source doc."}

    block = doc.copy_bank.get(pillar, {})
    hooks, bodies = block.get("hooks", []), block.get("bodies", [])
    if not hooks and not bodies:
        return {"blocked": True, "reason": f"Pillar '{pillar}' has no approved copy."}

    cta = pick_cta(doc, seed=f"{day_key}|{pillar}")

    hook_line = hooks[_day_seq(day_key) % len(hooks)] if hooks else None
    body_lines = list(bodies)

    # IG/FB caption: hook + body + verbatim CTA text (hashtags carried separately).
    caption_lines = ([hook_line] if hook_line else []) + body_lines + ([cta] if cta else [])
    # GBP summary: hook + body ONLY — no inline CTA text (it becomes a button), no hashtags.
    summary_lines = ([hook_line] if hook_line else []) + body_lines

    return {
        "pillar": pillar,
        "caption": "\n\n".join(caption_lines).strip(),
        "summary": "\n\n".join(summary_lines).strip(),
        "cta": cta,
        "hashtags": list(doc.hashtags[:5]),
        "fragments": list(caption_lines),
        "summary_fragments": list(summary_lines),
    }
