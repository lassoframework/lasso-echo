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


_ITEM_START_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s")


def _is_continuation(line):
    """An indented wrap of the previous list item (not a new item, not a heading)."""
    return bool(line[:1].isspace() and line.strip()
                and not _ITEM_START_RE.match(line) and not _HEADING_RE.match(line))


def _usable_lines(text):
    """The lines of one file that MAY be drafting sources, exclusions applied.
    A wrapped continuation line follows its parent's fate: if the marked line was
    dropped, its indented continuations drop with it (no orphaned fragments of
    LOCKED/PENDING content can survive the line gate)."""
    usable = []
    skip_until_level = None   # inside a LOCKED/PENDING/NOT FOUND section
    prev_dropped = False      # the previous CONTENT line was a dropped marked line
    for line in (text or "").splitlines():
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            if skip_until_level is not None and level <= skip_until_level:
                skip_until_level = None  # the excluded section ended
            if _has_marker(line):
                skip_until_level = len(m.group(1))
                prev_dropped = False
                continue
            if skip_until_level is not None:
                continue
            usable.append(line)
            prev_dropped = False
            continue
        if skip_until_level is not None:
            continue
        if _has_marker(line):
            prev_dropped = True
            continue  # a single marked line is excluded
        if prev_dropped and _is_continuation(line):
            continue  # the wrap of a dropped line drops with it
        if line.strip():
            prev_dropped = False
        usable.append(line)
    return usable


def join_items(lines):
    """List items with their wrapped continuations joined into single strings.
    Non-item content lines pass through one per line."""
    items = []
    for line in lines:
        if _is_continuation(line) and items:
            items[-1] = f"{items[-1]} {line.strip()}"
        elif line.strip():
            items.append(line.strip())
    return items


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
    corpus = {}

    def _ingest(path, name):
        if not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                lines = _usable_lines(fh.read())
        except OSError:
            return
        if any(ln.strip() for ln in lines):
            corpus[name] = lines

    if os.path.isdir(knowledge_dir):
        for name in sorted(os.listdir(knowledge_dir)):
            if _is_source_file(name):
                _ingest(os.path.join(knowledge_dir, name), name)
    # The Full Gym book docs are registered approved sources with the same
    # citation mechanics; the book's LOCKED section is excluded by the marker
    # gate exactly like locked stats. The launch QUEUE is campaign ops, not a
    # general source, so it is deliberately not registered here.
    for name in config.BOOK_SOURCE_FILES:
        _ingest(os.path.join(config.BOOK_DIR, name), name)
    return corpus


def usable_stats(knowledge_dir=None):
    """
    The ONLY stats allowed in copy: lines marked USE, wording preserved exactly
    (the text after "USE:" verbatim). Empty while the flag is OFF.
    """
    stats = []
    for lines in load_corpus(knowledge_dir).values():
        for item in join_items(lines):
            m = _USE_RE.match(item)
            if m:
                stats.append(m.group(1))
    return stats


def approved_text(knowledge_dir=None):
    """Every usable line joined per file, for hook/pillar/angle parsing downstream.
    The copy-bank parser only lifts explicitly structured lines from this text, so
    anything unstructured is context, never copy."""
    return {name: "\n".join(lines) for name, lines in load_corpus(knowledge_dir).items()}
