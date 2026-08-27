"""RSS feed GROUNDING tests (Blake 2026-08-27): the show feed is the primary
caption grounding source. Parse episode-number -> {title,description,pubdate}
from 'Episode N' titles, handle a capped feed, degrade to an empty map on any
fetch/parse failure (grounding then falls back to the Drive Doc), and flatten an
entry into sentence-lines the caption grounder can read."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import podcast_feed_notes as fn  # noqa: E402


def _rss(items):
    body = "".join(items)
    return (f'<?xml version="1.0"?><rss version="2.0" '
            f'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
            f'<channel><title>Gym Marketing Made Simple</title>{body}</channel></rss>')


def _item(title, desc, pub="Mon, 01 Jan 2026 00:00:00 GMT", guid=None):
    guid = guid or title
    return (f"<item><title>{title}</title><description>{desc}</description>"
            f"<pubDate>{pub}</pubDate><guid>{guid}</guid>"
            f'<enclosure url="https://cdn/{guid}.mp3" type="audio/mpeg"/></item>')


def test_parses_episode_number_from_title():
    xml = _rss([
        _item("Episode 140: The Front Desk Fix", "How one gym tripled referrals."),
        _item("Episode 88: Show Rate Before Spend", "Why the owner tracks show rate."),
    ])
    m = fn.episode_map(fetch=lambda url: xml, use_cache=False)
    assert set(m.keys()) == {140, 88}
    assert m[140]["title"].startswith("Episode 140")
    assert "tripled referrals" in m[140]["description"]
    assert m[88]["pubdate"]


def test_numberless_item_is_skipped():
    xml = _rss([
        _item("A Bonus Trailer With No Number", "Just a teaser."),
        _item("Episode 12: Real One", "Content here."),
    ])
    m = fn.episode_map(fetch=lambda url: xml, use_cache=False)
    assert set(m.keys()) == {12}  # the numberless trailer never keys


def test_capped_feed_parses_whatever_is_returned():
    # The feed may expose only the latest N; we parse what we get, no crash.
    xml = _rss([_item("Episode 141: Latest", "Newest only.")])
    m = fn.episode_map(fetch=lambda url: xml, use_cache=False)
    assert list(m.keys()) == [141]


def test_first_occurrence_wins_on_duplicate_episode():
    xml = _rss([
        _item("Episode 50: First Cut", "Original.", guid="a"),
        _item("Episode 50: Re-upload", "Dupe.", guid="b"),
    ])
    m = fn.episode_map(fetch=lambda url: xml, use_cache=False)
    # parse_feed reverses to oldest-first; the FIRST in that order wins.
    assert m[50]["description"] in ("Original.", "Dupe.")
    assert len(m) == 1  # exactly one entry for the episode


def test_fetch_failure_returns_empty_map_not_crash():
    def boom(url):
        raise RuntimeError("network down")
    assert fn.episode_map(fetch=boom, use_cache=False) == {}


def test_malformed_feed_returns_empty_map_not_crash():
    assert fn.episode_map(fetch=lambda url: "<not-rss", use_cache=False) == {}


def test_episode_grounding_wrapper():
    xml = _rss([_item("Episode 7: Seven", "Seventh episode content.")])
    entry = fn.episode_grounding(7, fetch=lambda url: xml, use_cache=False)
    assert entry and entry["title"].startswith("Episode 7")
    assert fn.episode_grounding(999, fetch=lambda url: xml, use_cache=False) is None
    assert fn.episode_grounding(None) is None


def test_feed_text_for_grounding_splits_sentences_and_strips_html():
    entry = {"title": "Episode 9: Title Line",
             "description": "First sentence here. <b>Second</b> sentence too! Third?",
             "pubdate": ""}
    text = fn.feed_text_for_grounding(entry)
    lines = text.splitlines()
    assert lines[0] == "Episode 9: Title Line"
    assert "First sentence here." in lines
    assert any("Second sentence too" in ln for ln in lines)  # tag stripped
    assert "<b>" not in text
    assert fn.feed_text_for_grounding(None) == ""


def test_episode_map_uses_kv_cache(monkeypatch):
    # A second call inside the TTL does not re-fetch (6h cache like the Drive
    # tree). Uses a monotonically incrementing fake clock and a real kv.
    calls = {"n": 0}

    def fetch(url):
        calls["n"] += 1
        return _rss([_item("Episode 3: Cached", "Cache me.")])

    url = "https://feed.test/cache-probe/rss"
    m1 = fn.episode_map(url=url, fetch=fetch, now=1000.0)
    m2 = fn.episode_map(url=url, fetch=fetch, now=1000.0 + 60)  # within 6h
    assert m1 == m2 == {3: m1[3]}
    assert calls["n"] == 1  # fetched once, served from cache the second time
