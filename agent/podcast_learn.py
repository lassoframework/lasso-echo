"""
Episode learnings memory (Part E of the podcast pipeline).

    python -m agent podcast-learn --episode N

Rides AGENT_PODCAST_ENABLED, no new flag (OFF = zero behavior change: the CLI
refuses, podcast-cards writes no learning file, nothing under knowledge/ moves).
Armed, 3 to 7 learnings extract from the episode's STORED transcript (Part C)
into brand_voice/knowledge/podcast/ep<N>_learnings.md plus the rolling
brand_voice/knowledge/podcast/INDEX.md. Every learning entry carries: a one
line takeaway and its VERBATIM supporting quote (both string match the stored
transcript exactly; a paraphrase is refused), the citation id podcast_ep<N>,
the episode title and date, and topic tags from the existing pillar taxonomy
(the seven pillars in 06_content_pillars.md).

SCOPE, stated plainly: the knowledge loader reads only TOP LEVEL files, so
nothing in knowledge/podcast/ ever enters the GLOBAL fabrication gate. These
files are readable approved sources for EPISODE TAGGED drafts (the
podcast_ep<N> citation resolves against the stored transcript, exactly as in
Parts C and D) for Echo and any other agent with repo access.

ADDITIVE ONLY: an existing ep<N> learnings file is never edited or deleted (a
re-run refuses and says so); the index only gains lines. Written copy is dash
free. Logs carry counts, never transcript text. Nothing here drafts, posts, or
touches any standing knowledge file (promotion is Part F, tap gated).
"""

import os
import re

from . import config, db, podcast_feed, podcast_transcripts
from .podcast_release import _DASH_RE, _dash_free

_MIN_TAKEAWAY, _MAX_TAKEAWAY = 30, 160
_MAX_QUOTE = 320
MIN_LEARNINGS, MAX_LEARNINGS = 3, 7

# Topic tags come from the EXISTING pillar taxonomy (06_content_pillars.md,
# "The seven pillars"), matched by the pillar's own vocabulary. Never invented.
_PILLAR_TAGS = (
    ("paid marketing is non negotiable", ("ad", "ads", "marketing", "campaign",
                                          "spend", "organic")),
    ("follow up and speed to lead", ("follow up", "lead", "respond", "response",
                                     "touchpoint", "minutes", "speed")),
    ("sales as coaching", ("sell", "selling", "sales", "close", "consultation",
                           "free trial")),
    ("know your numbers", ("number", "churn", "cost per lead", "sign up",
                           "signup", "profit", "rate")),
    ("clear messaging", ("message", "messaging", "hero", "guide", "one liner",
                         "website")),
    ("retention and operations", ("retention", "onboarding", "member", "coach",
                                  "hire", "no show")),
    ("stage based growth", ("stage", "startup", "scale", "grow", "growth")),
)


def learn_dir():
    return os.path.join(config.KNOWLEDGE_DIR, "podcast")


def _learnings_path(episode):
    return os.path.join(learn_dir(), f"ep{int(episode)}_learnings.md")


def _index_path():
    return os.path.join(learn_dir(), "INDEX.md")


def tags_for(text):
    low = (text or "").lower()
    tags = [name for name, words in _PILLAR_TAGS
            if any(w in low for w in words)]
    return tags or ["general"]


# ---- extraction: verbatim from what was said -----------------------------------------
def extract_learnings(episode, count=None):
    """
    3 to 7 learnings from the episode's stored transcript, spread across the
    show, deterministic. The takeaway is a VERBATIM transcript sentence; the
    supporting quote is that sentence plus its neighbor, also verbatim (never
    rewritten, never paraphrased); dashed sentences are skipped, not cleaned.
    LOUD when no transcript is stored or fewer than 3 clean learnings exist.
    """
    want = MAX_LEARNINGS if count is None else int(count)
    if not (MIN_LEARNINGS <= want <= MAX_LEARNINGS):
        raise ValueError(f"count must be {MIN_LEARNINGS} to {MAX_LEARNINGS}")
    sentences = podcast_transcripts.transcript_sentences(episode)
    if not sentences:
        raise ValueError(f"no transcript stored for episode {episode}; ingest "
                         "it first (podcast-transcript). Never guessed.")
    candidates = []
    for i, s in enumerate(sentences):
        if not (_MIN_TAKEAWAY <= len(s) <= _MAX_TAKEAWAY) or _DASH_RE.search(s):
            continue
        neighbor = sentences[i + 1] if i + 1 < len(sentences) else ""
        quote = f"{s} {neighbor}".strip() if neighbor else s
        if len(quote) > _MAX_QUOTE or _DASH_RE.search(quote):
            quote = s
        candidates.append({"takeaway": s, "quote": quote,
                           "tags": tags_for(quote)})
    if len(candidates) < MIN_LEARNINGS:
        raise ValueError(f"episode {episode}: only {len(candidates)} clean "
                         f"learning(s) in the transcript (need {MIN_LEARNINGS}). "
                         "Nothing written.")
    if len(candidates) <= want:
        return candidates
    span = len(candidates) - 1
    picked, seen = [], set()
    for k in range(want):
        j = round(k * span / (want - 1))
        if j not in seen:
            seen.add(j)
            picked.append(candidates[j])
    return picked


def verify_learning(episode, learning):
    """The verbatim gate: takeaway AND quote must string match the stored
    transcript. A paraphrase, however close, is refused LOUD."""
    for field in ("takeaway", "quote"):
        if not podcast_transcripts.contains_verbatim(episode, learning[field]):
            raise ValueError(
                f"learning refused: {field} is not verbatim in the "
                f"{podcast_transcripts.citation_id(episode)} transcript: "
                f"{learning[field][:80]!r}")


# ---- writing: additive only, knowledge file conventions -------------------------------
def write_learnings(episode, count=None):
    """
    Extract, verify, and write ep<N>_learnings.md plus the index line. Flag
    OFF -> None (zero behavior change). An existing episode file is NEVER
    edited or deleted: the re-run says so and leaves every byte alone.
    Returns {"path", "learnings", "citation", "existed"}.
    """
    if not config.podcast_enabled():
        return None
    episode = int(episode)
    record = podcast_feed.get_episode(episode)
    if record is None:
        raise ValueError(f"episode {episode} is not in the episode store; the "
                         "feed watcher has not seen it. Never guessed.")
    path = _learnings_path(episode)
    cite = podcast_transcripts.citation_id(episode)
    if os.path.exists(path):
        print(f"[podcast] learnings for episode {episode} already exist "
              f"({os.path.basename(path)}); additive only, nothing rewritten.")
        return {"path": path, "learnings": 0, "citation": cite, "existed": True}
    learnings = extract_learnings(episode, count)
    for learning in learnings:
        verify_learning(episode, learning)

    title = _dash_free(record.get("title") or "")
    published = record.get("published") or "date not in feed"
    lines = [
        f"# PODCAST EPISODE {episode} LEARNINGS (approved source, episode scoped)",
        f"Episode: {title} ({published})",
        f"Citation: {cite}. Scope: episode tagged drafts only; the global",
        "fabrication gate does not read this folder. Every quote below is",
        "VERBATIM from the stored transcript; wording must match exactly.",
        "",
        "## Learnings",
    ]
    for learning in learnings:
        lines.append(f"- TAKEAWAY: {learning['takeaway']}")
        lines.append(f"  QUOTE: \"{learning['quote']}\" ({cite})")
        lines.append(f"  TAGS: {', '.join(learning['tags'])}")
    os.makedirs(learn_dir(), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    _index_add(episode, title, published, len(learnings))
    db.audit("podcast_learnings", cite,
             f"{len(learnings)} learning(s) written to "
             f"{os.path.basename(path)}")  # counts only, never transcript text
    print(f"[podcast] episode {episode}: {len(learnings)} learning(s) written "
          f"(citation {cite})")
    return {"path": path, "learnings": len(learnings), "citation": cite,
            "existed": False}


_INDEX_LINE_RE = re.compile(
    r"^\s*[*-]\s*ep(?P<n>\d+): (?P<title>.*?) \((?P<published>.*?)\), "
    r"(?P<count>\d+) learning\(s\), citation podcast_ep(?P=n)\s*$")


def _index_add(episode, title, published, count):
    """The rolling index gains one line per episode, additive and idempotent;
    prior lines are never edited or dropped."""
    entries = read_index()
    if any(e["episode"] == int(episode) for e in entries):
        return
    header = ("# PODCAST LEARNINGS INDEX (approved source, episode scoped)\n"
              "One line per episode; each file below carries verbatim quotes\n"
              "with podcast_ep<N> citations for episode tagged drafts.\n")
    line = (f"- ep{int(episode)}: {title} ({published}), {count} learning(s), "
            f"citation podcast_ep{int(episode)}")
    existing = ""
    if os.path.exists(_index_path()):
        with open(_index_path(), encoding="utf-8") as fh:
            existing = fh.read().rstrip("\n")
    body = existing if existing else header.rstrip("\n")
    with open(_index_path(), "w", encoding="utf-8") as fh:
        fh.write(body + "\n" + line + "\n")


def read_index():
    """[{episode, title, published, count}] parsed back from INDEX.md (the
    round trip the tests hold)."""
    try:
        with open(_index_path(), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    entries = []
    for raw in text.splitlines():
        m = _INDEX_LINE_RE.match(raw)
        if m:
            entries.append({"episode": int(m.group("n")),
                            "title": m.group("title"),
                            "published": m.group("published"),
                            "count": int(m.group("count"))})
    return entries


# ---- CLI ------------------------------------------------------------------------------
def learn_cli(episode, count=None):
    """python -m agent podcast-learn --episode N [--count 3..7]."""
    if not config.podcast_enabled():
        print("podcast pipeline is OFF (set AGENT_PODCAST_ENABLED=true to arm "
              "it). Nothing written.")
        return
    if episode is None:
        print("usage: python -m agent podcast-learn --episode N [--count 3..7]")
        return
    try:
        out = write_learnings(episode, count)
    except ValueError as e:
        print(f"podcast-learn: {e}")
        return
    if out["existed"]:
        print(f"podcast-learn: episode {episode} learnings already on file; "
              "additive only.")
    else:
        print(f"podcast-learn: {out['learnings']} learning(s) at {out['path']} "
              f"(citation {out['citation']}); index updated.")
