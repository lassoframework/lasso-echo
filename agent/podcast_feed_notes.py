"""
podcast_feed_notes.py — the RSS FEED as a caption-GROUNDING source for the
podcast-clip library builder.

Blake's 2026-08-27 ruling: a clip's caption must be ACCURATE, and the show's
own RSS <item> description is the authoritative "what this episode is about".
This module fetches + parses the public feed into a
{episode_number -> {title, description, pubdate}} map so the caption builder can
ground on the real episode summary, not only the Drive show-notes Doc. It also
widens the groundable pool: a clip whose episode is in the feed can stage even
when its Drive Doc is missing.

Design:
  * URL from AGENT_PODCAST_RSS_URL (config.podcast_rss_grounding_url), defaulting
    to the verified live "Gym Marketing Made Simple" feed. This is INDEPENDENT of
    the Part-A watcher (podcast_feed.py) and NOT gated by AGENT_PODCAST_ENABLED —
    grounding runs even while detection is dark.
  * Parsing reuses podcast_feed.parse_feed (one RSS parser, never duplicated):
    each item's episode number comes from itunes:episode or an "Episode N" title.
  * 6h kv cache, same shape as the Drive tree cache (drive_client._TREE_CACHE_*):
    a network fetch at most once per 6h; a cache read failure is a miss, never a
    crash.
  * Injectable `http`/`fetch` for offline tests. A fetch failure or a malformed
    feed returns an EMPTY map (grounding then falls back to the Drive Doc), it
    never raises out of the builder. The feed may expose all episodes or only the
    latest N; whatever it returns is parsed and the cap is handled gracefully.

Nothing here drafts, stages, or publishes. It is a read that feeds grounding.
"""
from __future__ import annotations

import json
import re
import time

from . import config, db, podcast_feed as _feed

# RSS descriptions are usually one long paragraph, not bulleted lines. The
# caption grounder (podcast_caption.parse_notes) pulls claims line by line, so we
# split the description into sentence-lines first. Split on sentence enders
# followed by whitespace; strips any HTML tags a feed may wrap the summary in.
_TAG_RE = re.compile(r"<[^>]+>")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_CACHE_KEY = "podcast_rss_grounding:{}"
_CACHE_TTL_SEC = 6 * 3600


def _default_fetch(url):
    import requests  # lazy, repo pattern
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def _cache_read(url, now):
    try:
        raw = db.kv_get(_CACHE_KEY.format(url), "")
        if not raw:
            return None
        blob = json.loads(raw)
        if now - float(blob.get("ts", 0)) < _CACHE_TTL_SEC:
            # kv keys are strings; episode numbers are re-cast to int on load.
            return {int(k): v for k, v in (blob.get("episodes") or {}).items()}
    except Exception:
        return None  # unreadable cache is a miss, never a crash
    return None


def _cache_write(url, episodes, now):
    try:
        db.kv_set(_CACHE_KEY.format(url), json.dumps(
            {"ts": now, "episodes": {str(k): v for k, v in episodes.items()}}))
    except Exception:
        pass  # a cache write failure never blocks the result


def episode_map(*, url=None, fetch=None, use_cache=True, now=None):
    """{episode_number -> {'title','description','pubdate'}} from the show feed.

    Only items that carry an episode number (itunes:episode or an "Episode N"
    title) are keyed; a numberless item is skipped (it can never match a clip).
    When the feed lists an episode more than once the FIRST occurrence in
    detection order wins (deterministic). Fetch/parse failure -> {} (the builder
    then falls back to the Drive Doc); NEVER raises."""
    url = url or config.podcast_rss_grounding_url()
    now = now if now is not None else time.time()
    if use_cache:
        cached = _cache_read(url, now)
        if cached is not None:
            return cached
    try:
        xml_text = (fetch or _default_fetch)(url)
        episodes = _feed.parse_feed(xml_text)  # oldest first; raises on malformed
    except Exception as e:  # noqa: BLE001 - grounding must degrade, not crash
        print(f"[podcast-feed-notes] feed fetch/parse failed ({type(e).__name__}: "
              f"{e}); grounding falls back to Drive notes")
        return {}
    out = {}
    for ep in episodes:
        n = ep.get("episode")
        if n is None:
            continue
        n = int(n)
        if n in out:
            continue  # first occurrence wins (deterministic across duplicates)
        out[n] = {
            "title": ep.get("title") or "",
            "description": ep.get("description") or "",
            "pubdate": ep.get("published") or "",
        }
    _cache_write(url, out, now)
    return out


def episode_grounding(episode, *, url=None, fetch=None, use_cache=True, now=None):
    """The feed entry for one episode, or None. Convenience wrapper the caption
    builder uses so it never has to hold the whole map."""
    if episode is None:
        return None
    try:
        ep = int(episode)
    except (TypeError, ValueError):
        return None
    return episode_map(url=url, fetch=fetch, use_cache=use_cache, now=now).get(ep)


def feed_text_for_grounding(entry):
    """Flatten a feed entry into the plain-text block the caption grounder reads:
    the title as the first line (so parse_notes reads it as the hook/title), then
    the description split into one sentence per line (parse_notes pulls claims
    line by line, and RSS summaries are single paragraphs). HTML tags a feed may
    wrap the summary in are stripped. Empty entry -> '' (not groundable)."""
    if not entry:
        return ""
    title = str(entry.get("title") or "").strip()
    desc = _TAG_RE.sub(" ", str(entry.get("description") or ""))
    desc = re.sub(r"\s+", " ", desc).strip()  # collapse the space a tag left
    lines = [title] if title else []
    for sent in _SENTENCE_SPLIT_RE.split(desc):
        sent = sent.strip()
        if sent:
            lines.append(sent)
    return "\n".join(lines)
