"""
Podcast feed watcher (Part A of the podcast pipeline).

Dormant behind AGENT_PODCAST_ENABLED (default OFF = zero behavior change: no
fetch, no episode writes, the listener block never runs). Armed, the listener
polls AGENT_PODCAST_FEED_URL on the existing scheduler cadence and stores one
record per NEW episode: number, title, description, audio link, publish date,
and the transcript url when the feed exposes one (podcast:transcript, the
Podcasting 2.0 namespace; itunes:episode carries the number, with a title
fallback like "Episode 7: ...").

IDEMPOTENT BY KEY: an episode's guid (falling back to its enclosure url, then
its title) is the primary key; a re-poll of an unchanged feed inserts nothing
and returns []. A malformed feed FAILS LOUD (ValueError with the parse detail);
the listener catches it, logs, and posts one ops alert. A broken feed is never
a silent empty result. A missing feed url with the flag armed is the same:
loud, never guessed.

Nothing here drafts or publishes. Detection only.
"""

import re
import xml.etree.ElementTree as ET

from . import config, db

_ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
_PODCAST_NS = "{https://podcastindex.org/namespace/1.0}"
_EP_IN_TITLE = re.compile(r"\b(?:episode|ep\.?)\s*(\d+)\b", re.IGNORECASE)

# A poll detecting more new episodes than this is a backfill: transcripts
# auto ingest for the newest episode only (detection still stores them all).
AUTO_INGEST_BACKLOG_LIMIT = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS podcast_episodes (
  guid TEXT PRIMARY KEY,
  episode INTEGER,
  title TEXT,
  description TEXT,
  audio_url TEXT,
  published TEXT,
  transcript_url TEXT,
  detected_at TEXT DEFAULT (datetime('now')));
"""


def _conn():
    conn = db.connect()
    conn.executescript(_SCHEMA)
    return conn


# ---- feed parsing -------------------------------------------------------------------
def _text(el, tag):
    child = el.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _episode_number(item, title):
    """itunes:episode wins; a title like 'Episode 7: ...' is the fallback. None
    when the feed gives no number (stored anyway; numbered features skip it)."""
    raw = _text(item, f"{_ITUNES}episode")
    if raw.isdigit():
        return int(raw)
    m = _EP_IN_TITLE.search(title or "")
    return int(m.group(1)) if m else None


def _transcript_url(item):
    """The podcast:transcript url when the feed exposes one. A plain text
    transcript wins over caption formats when several are offered."""
    ranks = {"text/plain": 0, "text/html": 1, "application/srt": 2,
             "application/x-subrip": 2, "text/vtt": 3}
    best, best_rank = "", 99
    for t in item.findall(f"{_PODCAST_NS}transcript"):
        url = (t.get("url") or "").strip()
        if not url:
            continue
        rank = ranks.get((t.get("type") or "").strip().lower(), 50)
        if rank < best_rank:
            best, best_rank = url, rank
    return best


def parse_feed(xml_text):
    """
    [episode dict] from one RSS document, oldest first (RSS lists newest first;
    detection reads oldest first so a backfill stores in natural order). LOUD on
    malformed input: a document that does not parse as RSS raises ValueError.
    """
    try:
        root = ET.fromstring(xml_text or "")
    except ET.ParseError as e:
        raise ValueError(f"podcast feed is not parseable XML: {e}") from None
    channel = root.find("channel")
    if channel is None:
        raise ValueError("podcast feed has no <channel>: not an RSS feed")
    episodes = []
    for item in channel.findall("item"):
        title = _text(item, "title")
        enclosure = item.find("enclosure")
        audio_url = (enclosure.get("url") or "").strip() if enclosure is not None else ""
        guid = _text(item, "guid") or audio_url or title
        if not guid:
            raise ValueError("podcast feed item carries no guid, enclosure url, "
                             "or title: cannot key it")
        episodes.append({
            "guid": guid,
            "episode": _episode_number(item, title),
            "title": title,
            "description": _text(item, "description"),
            "audio_url": audio_url,
            "published": _text(item, "pubDate"),
            "transcript_url": _transcript_url(item),
        })
    return list(reversed(episodes))


# ---- polling ------------------------------------------------------------------------
def _default_fetch():
    url = config.PODCAST_FEED_URL
    if not url or url.startswith("<") or "://" not in url:
        raise ValueError("AGENT_PODCAST_ENABLED is armed but AGENT_PODCAST_FEED_URL "
                         f"is not set to a valid URL (got: {url!r}). "
                         "Set AGENT_PODCAST_FEED_URL=https://... in Railway vars.")
    import requests
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def poll(fetch=None, transcript_fetch=None):
    """
    One poll pass. Flag OFF -> None: nothing fetched, nothing written (the
    zero-behavior-change guarantee). Armed: parse the feed, store episodes not
    yet in the table, and return ONLY the newly detected ones. A re-poll of an
    unchanged feed returns []. Malformed feeds raise (loud, never silent); the
    listener's poll block catches, logs, and alerts. A NEW numbered episode
    whose feed entry exposes a transcript url is auto-ingested as that
    episode's approved source (podcast_transcripts); an ingest failure is loud
    but never un-detects the episode.
    """
    if not config.podcast_enabled():
        return None
    episodes = parse_feed((fetch or _default_fetch)())
    new = []
    with db._lock, _conn() as conn:
        for ep in episodes:
            cur = conn.execute(
                "INSERT OR IGNORE INTO podcast_episodes "
                "(guid, episode, title, description, audio_url, published, "
                "transcript_url) VALUES (?,?,?,?,?,?,?)",
                (ep["guid"], ep["episode"], ep["title"], ep["description"],
                 ep["audio_url"], ep["published"], ep["transcript_url"]))
            if cur.rowcount:
                new.append(ep)
        conn.commit()
    # audit outside the lock (db.audit takes it; the lock is not reentrant)
    for ep in new:
        n = ep["episode"] if ep["episode"] is not None else "?"
        db.audit("podcast_detected", ep["guid"],
                 f"episode {n}: {ep['title'][:120]}")
        print(f"[podcast] new episode detected: {n} {ep['title']!r}")
    # Auto ingest a transcript the feed exposes for a NEW numbered episode.
    # BACKLOG GUARD: the first poll against a feed with history detects the
    # whole backlog at once; past AUTO_INGEST_BACKLOG_LIMIT new episodes only
    # the NEWEST one's transcript auto ingests (arming must never fire one
    # fetch per back episode; the rest stay one CLI call away). Loud on
    # failure (log + one ops alert) but the detection stands; the by-hand
    # podcast-transcript CLI can ingest any of them later.
    to_ingest = [ep for ep in new
                 if ep["transcript_url"] and ep["episode"] is not None]
    if len(new) > AUTO_INGEST_BACKLOG_LIMIT and to_ingest:
        newest = to_ingest[-1]  # `new` is oldest first: the last is newest
        print(f"[podcast] backlog: {len(new)} new episodes in one poll; auto "
              f"ingesting ONLY episode {newest['episode']}'s transcript. Back "
              "episodes: podcast-transcript --episode N (--file|--url) by hand.")
        to_ingest = [newest]
    for ep in to_ingest:
        try:
            from . import podcast_transcripts
            podcast_transcripts.ingest_url(ep["episode"], ep["transcript_url"],
                                           fetch=transcript_fetch)
        except Exception as e:
            from . import ops_alerts
            print(f"[podcast] transcript ingest failed for episode "
                  f"{ep['episode']}: {type(e).__name__}: {e}")
            ops_alerts.alert(f"podcast transcript ingest failed for episode "
                             f"{ep['episode']}: {type(e).__name__}: {e}")
    return new


# ---- reads the later pipeline parts share ---------------------------------------------
def get_episode(number):
    """The stored record for episode <number>, or None."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM podcast_episodes WHERE episode=?",
                           (number,)).fetchone()
    return dict(row) if row else None


def list_episodes():
    """Every stored episode record, detection order."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM podcast_episodes ORDER BY detected_at, rowid").fetchall()
    return [dict(r) for r in rows]


# ---- podcast-status: the READ ONLY probe ------------------------------------------------
def _stored_readonly():
    """Stored episodes WITHOUT creating the episodes table (a probe on a virgin
    store must not even add schema)."""
    try:
        with db.connect() as conn:
            rows = conn.execute("SELECT * FROM podcast_episodes "
                                "ORDER BY detected_at, rowid").fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _carded_anywhere(guid):
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT key FROM kv WHERE key LIKE ?",
                (f"podcast_release_carded_{guid}_%",)).fetchone()
        return bool(row)
    except Exception:
        return False


def status_cli(fetch=None):
    """
    podcast-status: READ ONLY, zero side effects (nothing inserted, nothing
    carded, no kv writes, no schema added). Prints: feed reachable yes/no, the
    item count seen, the latest episode number + title as parsed, the armed
    watermark (episodes already in the store), and ONE honest forecast line:
    exactly what the next poll would do.
    """
    if not config.podcast_enabled():
        print("podcast-status: pipeline is OFF (AGENT_PODCAST_ENABLED); the "
              "poll never runs while dark.")
        return {"reachable": None}
    try:
        text = (fetch or _default_fetch)()
    except Exception as e:
        print(f"podcast-status: feed reachable: NO ({type(e).__name__}: {e})")
        return {"reachable": False}
    try:
        episodes = parse_feed(text)
    except ValueError as e:
        print(f"podcast-status: feed reachable: yes, but MALFORMED: {e}")
        return {"reachable": True, "parsed": False}
    stored = _stored_readonly()
    stored_guids = {e["guid"] for e in stored}
    new = [e for e in episodes if e["guid"] not in stored_guids]
    latest = episodes[-1] if episodes else None
    print("podcast-status: feed reachable: yes")
    print(f"podcast-status: {len(episodes)} item(s) in the feed; "
          f"{len(stored)} already stored (the armed watermark); "
          f"{len(new)} new to the store")
    if latest is None:
        print("podcast-status: the feed carries no items; the next poll would "
              "store nothing and draft nothing.")
        return {"reachable": True, "items": 0, "new": 0}
    n = latest["episode"]
    print(f"podcast-status: latest episode: "
          f"{n if n is not None else '(no number in feed)'} {latest['title']!r}")
    # the forecast mirrors the slot's own rules: newest only, once, mod 4
    if n is None:
        forecast = (f"next poll: stores {len(new)} new episode(s); NO release "
                    "card (the latest episode has no number in the feed; "
                    "numbering is never guessed).")
    elif _carded_anywhere(latest["guid"]):
        forecast = (f"next poll: backlog, would skip (episode {n} was already "
                    "carded; a re poll never re cards).")
    else:
        from .podcast_release import template_for_episode
        t = template_for_episode(n)
        note = (f"stores {len(new)} new episode(s) first; " if new else "")
        forecast = (f"next poll: {note}the release card drafts ONLY episode "
                    f"{n} using template podcast_release_{t} (episode mod 4 "
                    "rotation), once per account, held for the tap. Back "
                    "episodes never draft.")
    print(f"podcast-status: {forecast}")
    return {"reachable": True, "items": len(episodes), "stored": len(stored),
            "new": len(new), "latest": n, "forecast": forecast}
