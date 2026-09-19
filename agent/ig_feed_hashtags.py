"""
ig_feed_hashtags.py: fold a gym's APPROVED Instagram hashtags into the STORED
feed caption, so the client portal preview and the publish wire share the same
hashtag-bearing base caption. The existing optional mention lane may append
allowlisted @handles at publish time.

WHY THIS EXISTS. The drafter keeps hashtags in a SEPARATE field (Draft.hashtags,
selected by drafter._select_hashtags from the gym's approved VoiceDoc). The Meta
publishers append that field at publish time (meta_publisher.py /
socialapi_publisher.py: caption + "\n\n" + " ".join(hashtags)), but the Zernio
wire — the path every client gym publishes through — sends the stored caption
as its base body (agent/zernio_publisher.py) and the portal serves the stored
caption verbatim. An IG feed caption with no tags folded in therefore shows and
ships with zero hashtags. This module is the one pure rule that closes that gap
at the calendar boundary: the tags land IN the stored base caption once, so the
portal and publisher use the same hashtag copy before any optional mentions.

THE CONTRACT (ensure_feed_tag_line):
  * Tags come ONLY from the gym's approved VoiceDoc (agent/voice.py —
    VoiceDoc.hashtags, hex colors already filtered out by the extractor).
    Nothing is invented here; a voice doc with no usable tags leaves the
    caption byte-identical.
  * The selection is the drafter's OWN (drafter._select_hashtags): brand tier
    first, deterministic rotation keyed on the creative's filename stem, capped
    at TemplateGenerator.HASHTAG_LIMIT (5). Same stem -> same tags -> a re-run
    reproduces the exact line it wrote before.
  * ONE final line: a caption without a trailing hashtag line gets one, joined the
    way the existing composers join body and tags — one blank line, then the
    space-joined tags. Everything above that line is preserved byte-for-byte.
  * Idempotent and manual-copy safe: any existing trailing hashtag-only line is
    returned untouched because its provenance is unknown. A tag already present ANYWHERE in the caption body
    is never duplicated onto the final line (full-token, case-insensitive).
  * FALLBACK (fewer than 3): in 2026 the strategy is 3-5 tags, but an approved
    doc that yields only one or two usable tags contributes exactly those —
    never padded, never invented. Any existing trailing hashtag line is treated
    as owned copy and remains byte-identical.

Facebook is never touched here (the caller gates on the platform); stories
carry no caption at all by design. This module is PURE: string in, string out,
no I/O, no network.
"""

import re

from . import drafter as _drafter

# The same token shape voice._extract_hashtags accepts, so a tag the voice doc
# approves is always a tag this module can recognize on a caption line.
_TAG_TOKEN = re.compile(r"^#[A-Za-z0-9_]+$")
_TAG_IN_TEXT = re.compile(r"#[A-Za-z0-9_]+")


def _is_tag_line(line):
    """True when every whitespace-separated token on the line is a hashtag —
    the shape of a caption's final tag line."""
    tokens = line.split()
    return bool(tokens) and all(_TAG_TOKEN.match(t) for t in tokens)


def _unique_tags(tags):
    """Valid hashtag tokens, preserving the first spelling and order."""
    out, seen = [], set()
    for tag in tags or ():
        if not isinstance(tag, str) or not _TAG_TOKEN.match(tag):
            continue
        low = tag.lower()
        if low not in seen:
            seen.add(low)
            out.append(tag)
    return out


def final_tag_line_kind(caption, approved_tags):
    """Classify the final hashtag line for backfill reporting.

    ``manual`` means it contains an unapproved tag or more than five tags and
    must never be rewritten. ``compliant`` means the existing all-approved line
    is already owned copy and remains byte-identical. The helper never expands
    an existing line because stored rows do not carry reliable provenance.
    """
    lines = [line for line in (caption or "").split("\n") if line.strip()]
    if not lines or not _is_tag_line(lines[-1]):
        return "none"
    tokens = lines[-1].split()
    approved = {tag.lower() for tag in _unique_tags(approved_tags)}
    if (any(tag.lower() not in approved for tag in tokens)
            or len(tokens) > _drafter.TemplateGenerator.HASHTAG_LIMIT):
        return "manual"
    if len(tokens) <= _drafter.TemplateGenerator.HASHTAG_LIMIT:
        return "compliant"
    return "manual"


def ensure_feed_tag_line(caption, voice=None, creative=None, *,
                         approved_tags=None, selected_tags=None):
    """Return `caption` ending with exactly ONE final hashtag line of 3-5 tags
    drawn ONLY from the voice doc's approved set.

    voice: the gym's approved VoiceDoc. It is used by the backfill, which reads
    the current approved voice file. At the build boundary, callers pass the
    draft's already-approved ``hashtags`` list as both ``approved_tags`` and
    ``selected_tags``. That keeps the calendar row faithful to the draft that
    was reviewed and avoids a second voice-file read while staging.
    creative: anything drafter._stem can read (a .stem or .path attribute); the
    stem keys the deterministic rotation so a re-run selects the same tags.
    None rotates from index 0.

    Never raises on odd input: an empty caption stays empty (the caption
    standard owns empty-body blocking; this rule never manufactures a body)."""
    original = caption or ""
    approved = _unique_tags(
        approved_tags if approved_tags is not None
        else getattr(voice, "hashtags", None) or [])
    if not approved:
        return original
    approved_lower = {t.lower() for t in approved}

    lines = original.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    tail_kind = final_tag_line_kind(original, approved)

    # A final line containing ANY manual/unapproved tag is owned copy. The
    # calendar must never erase or rewrite it just to satisfy a generated tag
    # rule. It is reported as skipped by the backfill for a human to decide.
    if tail_kind in ("manual", "compliant"):
        return original

    # No trailing hashtag line exists, so the entire original caption is the
    # owned body. Keep every byte and add only the separator plus generated line.
    body = original
    if not body.strip():
        return original  # a caption that is ONLY tags (or empty) is left as-is

    if selected_tags is None:
        selected = _drafter._select_hashtags(voice, creative) if voice is not None else approved
    else:
        selected = selected_tags
    selected = [tag for tag in _unique_tags(selected)
                if tag.lower() in approved_lower]

    # Preserve the draft's selected order first, then replenish from the
    # remaining approved pool. This reaches 3--5 tags whenever the approved
    # source has enough unused tags, without inventing or duplicating one.
    candidates = _unique_tags([*selected, *approved])
    present = {m.lower() for m in _TAG_IN_TEXT.findall(body)}
    chosen = [t for t in candidates if t.lower() not in present]
    chosen = chosen[:_drafter.TemplateGenerator.HASHTAG_LIMIT]
    if not chosen:
        return original  # every approved tag already appears; never duplicate
    separator = "" if body.endswith("\n\n") else ("\n" if body.endswith("\n") else "\n\n")
    rendered = body + separator + " ".join(chosen)
    return original if rendered == original else rendered
