"""
Episode infographics (Part D of the podcast pipeline).

    python -m agent podcast-cards --episode N [--count 2|3]

Dormant behind AGENT_PODCAST_ENABLED (default OFF = zero behavior change: the
CLI refuses, the queue never serves). Armed, the CLI reads the episode's STORED
transcript (Part C) and extracts 2 or 3 card concepts: a short HOOK plus one
SUPPORT line, both sentences TAKEN VERBATIM from what was actually said, every
card carrying a citation to podcast_ep<N> that must resolve against the stored
transcript. An uncited or unresolvable card CANNOT enter the queue (loud
ValueError), and the queue re-verifies at serve time, belt and suspenders.

RENDERING: the SAME house style daily studio builder (creative_studio.generate,
default palette, default archetype rotation) - no style overrides, and the 18
existing concepts are never touched. COPY RULES enforced at extraction and at
build: dash free, StoryBrand order (the hook leads with the problem or insight,
the support line resolves it), first person we for LASSO in the fixed
attribution line.

QUEUE: cards enter the daily draft queue tagged to the episode and spread at
most ONE per day (the same card serves every LASSO account on its day, exactly
like the book queue), BEHIND book priority (the runner's podcast slot sits
after the book campaign), and every one is PENDING, held for the tap. Nothing
here publishes.
"""

import re

from . import config, creative_studio, db, media_host, ops_alerts, schedule
from . import podcast_transcripts
from .drafter import Draft, DraftStatus, _make_id
from .podcast_release import RELEASE_HASHTAGS, _DASH_RE

_MIN_HOOK, _MAX_HOOK = 25, 110
_MIN_SUPPORT, _MAX_SUPPORT = 15, 180

_SCHEMA = """
CREATE TABLE IF NOT EXISTS podcast_card_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  episode INTEGER,
  hook TEXT,
  support TEXT,
  status TEXT DEFAULT 'queued',
  carded_day TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now')));
"""


def _conn():
    conn = db.connect()
    conn.executescript(_SCHEMA)
    return conn


# ---- extraction: verbatim from what was said ------------------------------------------
def extract_concepts(episode, count=2):
    """
    2 or 3 (hook, support) pairs from the episode's stored transcript, spread
    across the show, deterministic. Every line is a VERBATIM transcript
    sentence; sentences carrying any dash family character are skipped (the
    copy law; the words are never rewritten). LOUD when no transcript is
    stored or no clean pairs exist. Returns fewer than <count> pairs (with a
    printed notice, never silently) when the transcript is thin.
    """
    if count not in (2, 3):
        raise ValueError("count must be 2 or 3")
    sentences = podcast_transcripts.transcript_sentences(episode)
    if not sentences:
        raise ValueError(f"no transcript stored for episode {episode}; ingest it "
                         "first (podcast-transcript). Never guessed.")
    candidates = []  # (index, hook, support)
    for i in range(len(sentences) - 1):
        hook, support = sentences[i], sentences[i + 1]
        if not (_MIN_HOOK <= len(hook) <= _MAX_HOOK):
            continue
        if not (_MIN_SUPPORT <= len(support) <= _MAX_SUPPORT):
            continue
        if _DASH_RE.search(hook) or _DASH_RE.search(support):
            continue  # verbatim only: a dashed sentence is skipped, not rewritten
        candidates.append((i, hook, support))
    if not candidates:
        raise ValueError(f"episode {episode}: no clean hook/support pairs in the "
                         "transcript (length and dash rules). Nothing queued.")
    # spread across the show: evenly spaced picks, no shared sentences
    picks, used = [], set()
    span = max(len(candidates) - 1, 1)
    for k in range(count):
        idx = round(k * span / max(count - 1, 1))
        for j in list(range(idx, len(candidates))) + list(range(idx - 1, -1, -1)):
            i, hook, support = candidates[j]
            if i in used or i + 1 in used:
                continue
            picks.append((hook, support))
            used.update((i, i + 1))
            break
        if len(picks) == k:  # nothing left for this slot
            break
    if len(picks) < count:
        print(f"[podcast] episode {episode}: transcript is thin; extracted "
              f"{len(picks)} card(s), not {count}")
    return picks


# ---- the queue gate: an uncited card cannot enter --------------------------------------
def enqueue(episode, hook, support):
    """
    Queue one card. HARD GATES, all loud: the transcript must be stored, hook
    and support must appear VERBATIM in it (the citation must resolve), both
    must be dash free, and the composed copy must clear the episode-scoped
    fabrication gate. A duplicate (same episode + hook) is skipped, not
    re-queued. Returns the queue row id, or None for a skipped duplicate.
    """
    episode = int(episode)
    if not podcast_transcripts.contains_verbatim(episode, hook):
        raise ValueError(f"uncited card refused: hook is not in the "
                         f"{podcast_transcripts.citation_id(episode)} transcript "
                         f"verbatim: {hook[:80]!r}")
    if not podcast_transcripts.contains_verbatim(episode, support):
        raise ValueError(f"uncited card refused: support line is not in the "
                         f"{podcast_transcripts.citation_id(episode)} transcript "
                         f"verbatim: {support[:80]!r}")
    if _DASH_RE.search(hook) or _DASH_RE.search(support):
        raise ValueError("card copy carries a dash character; refused")
    if not podcast_transcripts.gate_clean_for_episode(f"{hook} {support}", episode):
        raise ValueError("card copy failed the episode-scoped fabrication gate")
    with db._lock, _conn() as conn:
        dupe = conn.execute("SELECT id FROM podcast_card_queue WHERE episode=? "
                            "AND hook=?", (episode, hook)).fetchone()
        if dupe:
            return None
        cur = conn.execute("INSERT INTO podcast_card_queue (episode, hook, support) "
                           "VALUES (?,?,?)", (episode, hook, support))
        conn.commit()
        row_id = cur.lastrowid
    db.audit("podcast_card_queued", podcast_transcripts.citation_id(episode),
             f"card {row_id} queued: {hook[:80]}")
    return row_id


def queue_item_for(day_key):
    """Today's card: the one already assigned to this day (the same card serves
    every account on its day), else the oldest still-queued card, UNMARKED (it
    is only marked carded after a successful draft, so an unavailable studio
    never consumes content)."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM podcast_card_queue WHERE carded_day=? "
                           "ORDER BY id LIMIT 1", (day_key,)).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM podcast_card_queue WHERE "
                               "status='queued' ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else None


def _mark_carded(card_id, day_key):
    with db._lock, _conn() as conn:
        conn.execute("UPDATE podcast_card_queue SET status='carded', carded_day=? "
                     "WHERE id=?", (day_key, card_id))
        conn.commit()


def list_queue():
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM podcast_card_queue ORDER BY id").fetchall()
    return [dict(r) for r in rows]


# ---- serving (called by the runner's podcast slot, behind book priority) -----------------
def build_card_draft(account, day_key, nano_client=None, s3_client=None):
    """
    The day's episode card as a PENDING draft, or None (flag off, queue empty,
    or studio/hosting unavailable - the card then stays queued). Re-verifies
    the citation at serve time: a card that no longer resolves fails LOUD.
    """
    if not config.podcast_enabled():
        return None
    item = queue_item_for(day_key)
    if item is None:
        return None
    n, hook, support = item["episode"], item["hook"], item["support"]
    if not (podcast_transcripts.contains_verbatim(n, hook)
            and podcast_transcripts.contains_verbatim(n, support)):
        ops_alerts.alert(f"queued podcast card {item['id']} no longer resolves "
                         f"against {podcast_transcripts.citation_id(n)}; refusing "
                         "to draft it.")
        raise ValueError(f"queued podcast card {item['id']} lost its citation")
    # SAME house style daily studio builder, no style overrides: default
    # palette, default archetype rotation, the hook as the one on-image line.
    art = creative_studio.generate(hook, [support], client=nano_client,
                                   archetype=creative_studio.archetype_for_day(day_key))
    if art is None:
        ops_alerts.alert(
            f"podcast card: studio returned nothing for {account.key} episode {n} "
            "(studio dark or Gemini unavailable); card stays queued."
        )
        return None
    hosted = media_host.host_media(art["path"], account.key, client=s3_client)
    if not hosted:
        return None
    # StoryBrand order: the hook leads with the problem/insight, the support
    # resolves it; the fixed attribution line is first person we for LASSO.
    caption = (f"{hook}\n\n{support}\n\n"
               f"We break it down in episode {n} of our podcast. Listen now.")
    assert not _DASH_RE.search(caption), "podcast card caption carries a dash"
    draft = Draft(
        draft_id=_make_id(account.key, f"podcast_card_{n}_{item['id']}", day_key),
        account_key=account.key, platform=account.platform,
        caption=caption, hashtags=list(RELEASE_HASHTAGS),
        creative_path=art["path"], creative_public_url=hosted,
        scheduled_for=schedule.scheduled_for(day_key), status=DraftStatus.PENDING,
        source_fragments=[f"cite:{podcast_transcripts.citation_id(n)}", hook, support],
        day_key=day_key, draft_type="podcast",
    )
    _mark_carded(item["id"], day_key)
    db.audit("podcast_card", draft.draft_id,
             f"episode {n} card {item['id']} drafted (held for approval)",
             account.key, day_key)
    return draft


def resolve_citation(draft):
    """True when the draft carries exactly one cite:podcast_ep<N> fragment and
    every quoted fragment appears verbatim in that episode's stored transcript."""
    cites = [f[5:] for f in (draft.source_fragments or []) if f.startswith("cite:")]
    if len(cites) != 1:
        return False
    n = podcast_transcripts.parse_citation(cites[0])
    if n is None:
        return False
    quoted = [f for f in draft.source_fragments if not f.startswith("cite:")]
    return bool(quoted) and all(
        podcast_transcripts.contains_verbatim(n, q) for q in quoted)


# ---- CLI ----------------------------------------------------------------------------
def cards_cli(episode, count):
    """python -m agent podcast-cards --episode N [--count 2|3]."""
    if not config.podcast_enabled():
        print("podcast pipeline is OFF (set AGENT_PODCAST_ENABLED=true to arm it). "
              "Nothing queued.")
        return
    if episode is None:
        print("usage: python -m agent podcast-cards --episode N [--count 2|3]")
        return
    try:
        picks = extract_concepts(episode, count)
    except ValueError as e:
        print(f"podcast-cards: {e}")
        return
    queued = 0
    for hook, support in picks:
        if enqueue(episode, hook, support) is not None:
            queued += 1
    cite = podcast_transcripts.citation_id(episode)
    print(f"podcast-cards: {queued} card(s) queued for episode {episode} "
          f"(citation {cite}), {len(picks) - queued} duplicate(s) skipped.")
    for hook, _support in picks:
        print(f"  hook: {hook}")
    print("  cards spread max one per day, behind book priority, every one "
          "held for approval.")
    # Part E rides along: episode learnings memory (verbatim, additive only,
    # episode scoped). A learn failure is LOUD but never blocks the cards.
    try:
        from . import podcast_learn
        podcast_learn.write_learnings(episode)
    except Exception as e:
        print(f"[podcast] learnings not written for episode {episode}: "
              f"{type(e).__name__}: {e}")
