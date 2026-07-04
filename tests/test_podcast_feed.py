"""
Podcast feed watcher tests (pipeline Part A). Offline: a fake fetch returns
fixture RSS. Asserts: a new episode is detected EXACTLY once and stored with
number, title, description, audio link, publish date, and the
podcast:transcript url; a re-poll of the unchanged feed is silent (no new
detections, no duplicate rows); a malformed feed fails LOUD (ValueError),
never a silent empty result; a missing feed url with the flag armed is loud
too; and with the flag OFF the watcher is fully inert (nothing fetched,
nothing stored).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, podcast_feed  # noqa: E402

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>LASSO Now</title>
    <item>
      <title>Episode 7: The follow up problem</title>
      <guid>ep7-guid</guid>
      <description>Most gyms do not have a lead problem. They have a follow up problem. We walk the fix.</description>
      <pubDate>Mon, 29 Jun 2026 10:00:00 GMT</pubDate>
      <enclosure url="https://cdn.example.com/ep7.mp3" type="audio/mpeg"/>
      <itunes:episode>7</itunes:episode>
      <podcast:transcript url="https://cdn.example.com/ep7.vtt" type="text/vtt"/>
      <podcast:transcript url="https://cdn.example.com/ep7.txt" type="text/plain"/>
    </item>
    <item>
      <title>Episode 6: Show rate math</title>
      <guid>ep6-guid</guid>
      <description>Show rate is the quiet killer. We walk the math.</description>
      <pubDate>Mon, 22 Jun 2026 10:00:00 GMT</pubDate>
      <enclosure url="https://cdn.example.com/ep6.mp3" type="audio/mpeg"/>
      <itunes:episode>6</itunes:episode>
    </item>
  </channel>
</rss>
"""

FEED_PLUS_ONE = FEED.replace("<title>LASSO Now</title>", """<title>LASSO Now</title>
    <item>
      <title>Ep 8 speed to lead</title>
      <guid>ep8-guid</guid>
      <description>Answer fast. Close more.</description>
      <pubDate>Mon, 06 Jul 2026 10:00:00 GMT</pubDate>
      <enclosure url="https://cdn.example.com/ep8.mp3" type="audio/mpeg"/>
    </item>""")


class CountingFetch:
    def __init__(self, text):
        self.text, self.calls = text, 0

    def __call__(self):
        self.calls += 1
        return self.text


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "true")


# ---- detect once, fields stored ------------------------------------------------------
def test_new_episodes_detected_once_with_full_record(monkeypatch):
    _arm(monkeypatch)
    new = podcast_feed.poll(fetch=lambda: FEED)
    assert [e["episode"] for e in new] == [6, 7]        # oldest first
    ep7 = podcast_feed.get_episode(7)
    assert ep7["title"] == "Episode 7: The follow up problem"
    assert ep7["description"].startswith("Most gyms do not have a lead problem.")
    assert ep7["audio_url"] == "https://cdn.example.com/ep7.mp3"
    assert ep7["published"] == "Mon, 29 Jun 2026 10:00:00 GMT"
    # podcast:transcript namespace supported; plain text wins over vtt
    assert ep7["transcript_url"] == "https://cdn.example.com/ep7.txt"
    assert podcast_feed.get_episode(6)["transcript_url"] == ""


def test_episode_number_falls_back_to_title(monkeypatch):
    _arm(monkeypatch)
    podcast_feed.poll(fetch=lambda: FEED_PLUS_ONE)
    assert podcast_feed.get_episode(8)["guid"] == "ep8-guid"  # "Ep 8" in the title


# ---- re-poll silent -------------------------------------------------------------------
def test_repoll_never_duplicates(monkeypatch):
    _arm(monkeypatch)
    assert len(podcast_feed.poll(fetch=lambda: FEED)) == 2
    assert podcast_feed.poll(fetch=lambda: FEED) == []            # silent re-poll
    assert len(podcast_feed.list_episodes()) == 2                 # no duplicate rows
    # a feed that GREW detects only the one new episode
    new = podcast_feed.poll(fetch=lambda: FEED_PLUS_ONE)
    assert [e["guid"] for e in new] == ["ep8-guid"]
    assert len(podcast_feed.list_episodes()) == 3


# ---- malformed fails loud --------------------------------------------------------------
def test_malformed_feed_fails_loud(monkeypatch):
    _arm(monkeypatch)
    with pytest.raises(ValueError, match="not parseable XML"):
        podcast_feed.poll(fetch=lambda: "definitely <not> xml <<<")
    with pytest.raises(ValueError, match="no <channel>"):
        podcast_feed.poll(fetch=lambda: "<html><body>404</body></html>")
    assert podcast_feed.list_episodes() == []                     # nothing half-stored


def test_missing_feed_url_fails_loud_when_armed(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(config, "PODCAST_FEED_URL", "")
    with pytest.raises(ValueError, match="AGENT_PODCAST_FEED_URL"):
        podcast_feed.poll()


# ---- flag off = fully inert -------------------------------------------------------------
def test_flag_off_zero_behavior(monkeypatch):
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    fetch = CountingFetch(FEED)
    assert podcast_feed.poll(fetch=fetch) is None
    assert fetch.calls == 0                                       # nothing fetched
    assert podcast_feed.list_episodes() == []                     # nothing stored
