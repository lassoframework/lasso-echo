"""
Podcast transcript ingest (Part C of the podcast pipeline).

Dormant behind AGENT_PODCAST_ENABLED (default OFF = zero behavior change: the
CLI refuses, nothing fetches, the gate is exactly today's). Armed, an episode's
transcript becomes an APPROVED SOURCE **scoped to that one episode**, citation
id podcast_ep<N>:

  - by hand:   python -m agent podcast-transcript --episode N (--file P | --url U)
  - automatic: the feed poll ingests a podcast:transcript url on a NEW episode.

SCOPE, stated plainly: the fabrication gate accepts transcript-backed claims
ONLY through gate_clean_for_episode(text, N) - the wrapper episode tagged
drafts use. rotation._approved_claims() (the global gate) is untouched, so a
non-episode draft can never borrow a podcast stat. A claim NOT present in the
transcript still blocks, episode tag or not.

NO LOG LEAKAGE: transcript content never lands in logs, audits, or alerts
beyond SNIPPET_LEN characters (the CLI preview); the core ingest logs a
character count only. Nothing here drafts or publishes.
"""

import re

from . import config, db

SNIPPET_LEN = 120  # the most transcript text ANY log line may ever carry

_SCHEMA = """
CREATE TABLE IF NOT EXISTS podcast_transcripts (
  episode INTEGER PRIMARY KEY,
  text TEXT,
  source TEXT,
  ingested_at TEXT DEFAULT (datetime('now')));
"""

_CITE_RE = re.compile(r"^podcast_ep(\d+)$")


def _conn():
    conn = db.connect()
    conn.executescript(_SCHEMA)
    return conn


def citation_id(episode):
    return f"podcast_ep{int(episode)}"


def parse_citation(cite):
    """episode number for a 'podcast_ep<N>' citation id, else None."""
    m = _CITE_RE.match((cite or "").strip())
    return int(m.group(1)) if m else None


def _plain_text(raw):
    """Spoken words only: WEBVTT headers, srt counters, timestamp lines, and
    inline cue tags dropped; whitespace normalized."""
    lines = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s or s.upper().startswith(("WEBVTT", "NOTE ")) or "-->" in s or s.isdigit():
            continue
        lines.append(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


# ---- ingest -------------------------------------------------------------------------
def ingest(episode, raw_text, source):
    """
    Store one episode's transcript as its approved source. Replaces on
    re-ingest (the newest transcript wins). LOUD on empty input; the flag OFF
    refuses (None) so nothing changes anywhere while dark.
    """
    if not config.podcast_enabled():
        return None
    episode = int(episode)
    text = _plain_text(raw_text)
    if not text:
        raise ValueError(f"transcript for episode {episode} is empty after "
                         "cleanup: nothing to ingest")
    with db._lock, _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO podcast_transcripts "
                     "(episode, text, source) VALUES (?,?,?)",
                     (episode, text, source))
        conn.commit()
    # character COUNT only: transcript content never lands in a log line
    db.audit("podcast_transcript", citation_id(episode),
             f"transcript ingested ({len(text)} chars) from {source}")
    print(f"[podcast] transcript ingested for episode {episode}: "
          f"{len(text)} chars (citation {citation_id(episode)})")
    return {"episode": episode, "chars": len(text), "citation": citation_id(episode)}


def ingest_file(episode, path):
    with open(path, encoding="utf-8") as fh:
        return ingest(episode, fh.read(), f"file:{path}")


def ingest_url(episode, url, fetch=None):
    if fetch is None:
        import requests

        def fetch(u):
            resp = requests.get(u, timeout=30)
            resp.raise_for_status()
            return resp.text
    return ingest(episode, fetch(url), f"url:{url}")


# ---- reads --------------------------------------------------------------------------
def transcript_text(episode):
    """The stored transcript for episode <N>, '' when absent or flag OFF."""
    if not config.podcast_enabled():
        return ""
    with _conn() as conn:
        row = conn.execute("SELECT text FROM podcast_transcripts WHERE episode=?",
                           (int(episode),)).fetchone()
    return row["text"] if row else ""


def transcript_sentences(episode):
    """The transcript split to sentences: the claim units the gate matches
    against. [] when absent or the flag is OFF (conservative)."""
    text = transcript_text(episode)
    if not text:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def contains_verbatim(episode, quoted):
    """True when <quoted> appears verbatim (whitespace normalized) in the
    stored transcript. The Part D card gate: what was actually said, exactly."""
    text = transcript_text(episode)
    q = re.sub(r"\s+", " ", (quoted or "")).strip()
    return bool(q) and q in text


# ---- the episode-scoped fabrication gate ------------------------------------------------
def approved_claims_for(episode):
    """Every globally approved claim PLUS this ONE episode's transcript
    sentences. Only episode tagged drafts ever pass an episode here; the
    global gate (rotation._approved_claims) is untouched."""
    from . import rotation
    claims = rotation._approved_claims()
    claims.extend(transcript_sentences(episode))
    return claims


def gate_clean_for_episode(text, episode):
    """The fabrication gate for an episode tagged draft: cleared by the global
    approved sources OR this episode's transcript. NEVER weakened: a claim in
    neither still blocks, and with the flag OFF this is exactly the global gate."""
    from . import rotation
    return rotation.is_gate_clean(text, approved_claims_for(episode))


# ---- CLI ----------------------------------------------------------------------------
def ingest_cli(episode, path, url):
    """python -m agent podcast-transcript --episode N (--file PATH | --url URL).
    Prints a SNIPPET_LEN preview at most; never the transcript."""
    if not config.podcast_enabled():
        print("podcast pipeline is OFF (set AGENT_PODCAST_ENABLED=true to arm it). "
              "Nothing ingested.")
        return
    if episode is None or bool(path) == bool(url):
        print("usage: python -m agent podcast-transcript --episode N "
              "(--file PATH | --url URL)")
        return
    out = ingest_file(episode, path) if path else ingest_url(episode, url)
    preview = transcript_text(episode)[:SNIPPET_LEN]
    print(f"podcast-transcript: episode {out['episode']} stored, {out['chars']} chars, "
          f"citation {out['citation']}")
    print(f"  preview (first {SNIPPET_LEN} chars): {preview!r}")
