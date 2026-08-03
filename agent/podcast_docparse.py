"""
GMMS show-notes doc parser (podcast clip selection from the DOC, not guesswork).

The Gym Marketing Made Simple VA hands off a show-notes document per episode.
This module turns that document into clip CANDIDATES so the clipper cuts what a
human already picked, rather than scoring the raw transcript blind. Two sections
are read:

  - "Episode Chapters"  - timestamped structural markers (H:MM:SS, MM:SS,
    [MM:SS], or "MM:SS Title"). Secondary, structural candidates.
  - "Memorable Quotes"  - the VA's hand-picked money lines, verbatim, some with
    a leading or trailing timestamp, some without. PRIMARY candidates.

Design note: this parser only REPORTS what the doc contains. It never scores a
transcript and never invents a candidate. When the doc lacks quotes (or lacks a
usable section entirely), has_usable_doc / clip_candidates report that emptiness
clearly so the CALLER can fall back to transcript scoring (clipper.select_moments).
A doc that carries a section HEADER but zero parseable lines is loud by being
empty here, not by guessing a line: the caller sees no candidates and falls back.

Every candidate traces to a doc line (the "raw" field is kept), so a card built
from a candidate can always be checked back against what the VA actually wrote.
Offline, pure Python, no network.
"""

import re

# Header variations we accept for each section. Compared case-insensitively
# against a line stripped of markdown header hashes, bullets, and bold markers.
_QUOTE_HEADERS = (
    "memorable quotes", "notable quotes", "quotes", "key quotes",
    "best quotes", "money quotes", "quotable moments",
)
_CHAPTER_HEADERS = (
    "episode chapters", "chapters", "timestamps", "chapter markers",
    "episode timestamps",
)

# A timestamp anywhere: H:MM:SS or MM:SS or M:SS, optionally in [brackets].
_TS_RE = re.compile(r"\[?\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\]?")
# A timestamp anchored at the START of the text (chapter / leading-ts quote).
_LEADING_TS_RE = re.compile(r"^\[?\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\]?\s*")
# A timestamp anchored at the END of the text (trailing-ts quote).
_TRAILING_TS_RE = re.compile(r"\s*\[?\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\]?\s*$")


def parse_timestamp(s):
    """Convert '1:02:03', '12:34', or '[12:34]' to whole seconds. None when the
    string carries no well-formed timestamp. Robust to surrounding whitespace and
    a single pair of square brackets."""
    if not s:
        return None
    m = _TS_RE.search(str(s))
    if not m:
        return None
    parts = [int(p) for p in m.group(1).split(":")]
    if len(parts) == 3:
        h, mm, ss = parts
    elif len(parts) == 2:
        h, mm, ss = 0, parts[0], parts[1]
    else:
        return None
    if mm >= 60 or ss >= 60:
        return None
    return h * 3600 + mm * 60 + ss


def _strip_markup(line):
    """A single doc line reduced to its content: leading markdown header hashes,
    list bullets (-, *, +, and the bullet glyph), and **bold** markers removed;
    whitespace collapsed. Returns the cleaned string (may be empty)."""
    s = (line or "").strip()
    s = re.sub(r"^#{1,6}\s*", "", s)          # markdown header hashes
    s = re.sub(r"^[\-\*\+•]\s+", "", s)  # list bullet (dash/star/plus/bullet)
    s = s.replace("**", "").replace("__", "")  # bold markers
    s = s.strip().strip('"').strip("'").strip()  # surrounding quote marks
    return re.sub(r"\s+", " ", s).strip()


def _header_kind(line):
    """'quote', 'chapter', or None for a line that reads as a section header.
    A header is a short line (no sentence-length prose) whose cleaned, lowercased
    text matches one of the known section names, tolerant of a trailing colon."""
    cleaned = _strip_markup(line).rstrip(":").strip().lower()
    if not cleaned:
        return None
    if cleaned in _QUOTE_HEADERS:
        return "quote"
    if cleaned in _CHAPTER_HEADERS:
        return "chapter"
    return None


def _sectionize(doc_text):
    """Split the doc into {'quote': [lines], 'chapter': [lines]} by walking the
    lines and switching the active section each time a known header appears. Lines
    before any header belong to no section and are ignored."""
    sections = {"quote": [], "chapter": []}
    active = None
    for raw in (doc_text or "").splitlines():
        kind = _header_kind(raw)
        if kind is not None:
            active = kind
            continue
        if active is not None and raw.strip():
            sections[active].append(raw)
    return sections


def parse_chapters(doc_text):
    """Chapters from the 'Episode Chapters' section. Each is
    {'ts_seconds': int, 'title': str, 'raw': str}. Only lines that carry a
    LEADING timestamp count as a chapter (the timestamp anchors the marker);
    a line without one is not a chapter and is dropped. Order preserved."""
    out = []
    for raw in _sectionize(doc_text)["chapter"]:
        cleaned = _strip_markup(raw)
        m = _LEADING_TS_RE.match(cleaned)
        if not m:
            continue
        ts = parse_timestamp(m.group(1))
        if ts is None:
            continue
        # title is whatever follows the leading timestamp, with a separator
        # dash/colon trimmed ("12:34 - Topic" and "12:34: Topic" both work).
        title = cleaned[m.end():].strip()
        title = re.sub(r"^[\-–—:]\s*", "", title).strip()
        out.append({"ts_seconds": ts, "title": title, "raw": raw.strip()})
    return out


def parse_quotes(doc_text):
    """Quotes from the 'Memorable Quotes' section. Each is
    {'text': str, 'ts_seconds': int|None, 'raw': str}. A leading OR trailing
    timestamp is stripped off and reported in ts_seconds; a quote with no
    timestamp keeps ts_seconds None. The verbatim quote TEXT is preserved (only
    the timestamp token and surrounding markup are removed). Order preserved."""
    out = []
    for raw in _sectionize(doc_text)["quote"]:
        cleaned = _strip_markup(raw)
        if not cleaned:
            continue
        ts = None
        # A leading timestamp: pull it, then take the remainder as the quote.
        lm = _LEADING_TS_RE.match(cleaned)
        if lm:
            ts = parse_timestamp(lm.group(1))
            cleaned = cleaned[lm.end():].strip()
            cleaned = re.sub(r"^[\-–—:]\s*", "", cleaned).strip()
        else:
            # Else a trailing timestamp: pull it, keep the leading quote text.
            tm = _TRAILING_TS_RE.search(cleaned)
            if tm and tm.start() > 0:
                ts = parse_timestamp(tm.group(1))
                cleaned = cleaned[:tm.start()].strip()
        cleaned = cleaned.strip().strip('"').strip("'").strip()
        if not cleaned:
            continue
        out.append({"text": cleaned, "ts_seconds": ts, "raw": raw.strip()})
    return out


def clip_candidates(doc_text):
    """
    The clip candidate list for one episode, PRIMARY quotes first (in doc order),
    then chapters as secondary structural candidates (in doc order). Each is
    {'source': 'quote'|'chapter', 'text': str, 'ts_seconds': int|None,
     'title': str|None}. Empty when the doc has no parseable lines in either
    section - the signal to the caller to fall back to transcript scoring.
    """
    cands = []
    for q in parse_quotes(doc_text):
        cands.append({"source": "quote", "text": q["text"],
                      "ts_seconds": q["ts_seconds"], "title": None})
    for c in parse_chapters(doc_text):
        cands.append({"source": "chapter", "text": c["title"],
                      "ts_seconds": c["ts_seconds"], "title": c["title"]})
    return cands


def has_usable_doc(doc_text):
    """True when the doc yields at least one Memorable Quote OR at least one
    Episode Chapter. False for a doc that is blank, has no known section, or has a
    section header but zero parseable lines under it: the caller then knows to
    fall back to transcript scoring rather than trust a hollow doc."""
    return bool(parse_quotes(doc_text)) or bool(parse_chapters(doc_text))
