"""
The LASSO knowledge brain: approved source material under brand_voice/knowledge/.

Dormant behind AGENT_KNOWLEDGE_ENABLED (default OFF). When ON, the drafter may draw
facts, hooks, pillars, and podcast angles from the knowledge folder, but the HARD
GATES live in the files themselves and are enforced here:

  - a file named *_pending.md is NEVER a drafting source (whole file excluded);
    03_social_proof_pending.md is excluded twice over (by that rule and by name):
    social proof flows ONLY through brand_voice/social_proof.md, which enforces
    Permission: yes.
  - a heading whose line carries LOCKED, PENDING, or NOT FOUND excludes its whole
    section (until the next heading of the same or higher level).
  - any single line carrying LOCKED, PENDING, or NOT FOUND is excluded.
  - STATS: only lines marked USE (a line beginning "USE:") may appear in copy, and
    the wording after the marker is preserved EXACTLY. Numbers on unmarked lines
    are context, never copy.

A missing or empty knowledge folder means the brain is silently absent; normal
drafting is untouched. Nothing here publishes.
"""

import os
import re

from . import config

_BLOCK_MARKERS = ("LOCKED", "PENDING", "NOT FOUND")
_HEADING_RE = re.compile(r"^(#{1,6})\s")
_USE_RE = re.compile(r"^\s*(?:[-*]\s*)?USE:\s*(.+?)\s*$")


def _has_marker(line):
    upper = line.upper()
    return any(m in upper for m in _BLOCK_MARKERS)


def _usable_lines(text):
    """The lines of one file that MAY be drafting sources, exclusions applied."""
    usable = []
    skip_until_level = None  # inside a LOCKED/PENDING/NOT FOUND section
    for line in (text or "").splitlines():
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            if skip_until_level is not None and level <= skip_until_level:
                skip_until_level = None  # the excluded section ended
            if _has_marker(line):
                skip_until_level = len(m.group(1))
                continue
            if skip_until_level is not None:
                continue
            usable.append(line)
            continue
        if skip_until_level is not None:
            continue
        if _has_marker(line):
            continue  # a single marked line is excluded
        usable.append(line)
    return usable


def _is_source_file(name):
    lower = name.lower()
    if not lower.endswith(".md"):
        return False
    if lower.endswith("_pending.md"):
        return False
    if lower == "03_social_proof_pending.md":
        return False  # belt and suspenders; social proof has its own gated path
    return True


def load_corpus(knowledge_dir=None):
    """
    {filename: [usable lines]} across the knowledge folder, all gates applied.
    Returns {} while the flag is OFF or when the folder is missing/empty.
    """
    if not config.knowledge_enabled():
        return {}
    knowledge_dir = knowledge_dir or config.KNOWLEDGE_DIR
    if not os.path.isdir(knowledge_dir):
        return {}
    corpus = {}
    for name in sorted(os.listdir(knowledge_dir)):
        if not _is_source_file(name):
            continue
        path = os.path.join(knowledge_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                lines = _usable_lines(fh.read())
        except OSError:
            continue
        if any(ln.strip() for ln in lines):
            corpus[name] = lines
    return corpus


def usable_stats(knowledge_dir=None):
    """
    The ONLY stats allowed in copy: lines marked USE, wording preserved exactly
    (the text after "USE:" verbatim). Empty while the flag is OFF.
    """
    stats = []
    for lines in load_corpus(knowledge_dir).values():
        for line in lines:
            m = _USE_RE.match(line)
            if m:
                stats.append(m.group(1))
    return stats


def approved_text(knowledge_dir=None):
    """Every usable line joined per file, for hook/pillar/angle parsing downstream.
    The copy-bank parser only lifts explicitly structured lines from this text, so
    anything unstructured is context, never copy."""
    return {name: "\n".join(lines) for name, lines in load_corpus(knowledge_dir).items()}
