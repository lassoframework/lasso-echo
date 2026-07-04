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
    if not url:
        raise ValueError("AGENT_PODCAST_ENABLED is armed but AGENT_PODCAST_FEED_URL "
                         "is not set. Stopping loud, not guessing.")
    import requests
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def poll(fetch=None):
    """
    One poll pass. Flag OFF -> None: nothing fetched, nothing written (the
    zero-behavior-change guarantee). Armed: parse the feed, store episodes not
    yet in the table, and return ONLY the newly detected ones. A re-poll of an
    unchanged feed returns []. Malformed feeds raise (loud, never silent); the
    listener's poll block catches, logs, and alerts.
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
