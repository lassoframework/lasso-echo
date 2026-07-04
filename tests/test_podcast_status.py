"""
podcast-status probe + first poll verification (operator hygiene Part C).
Offline; the fixture is shaped like the real Anchor feed (139 items, newest
first, every item exposing a transcript url). Asserts: the probe is READ ONLY
(the store dumps byte identical before and after, kv included); the forecast
matches the mod 4 rotation math and flips to "would skip" once carded; the
first poll after arming stores the whole backlog but drafts AT MOST the single
newest episode and never a back episode; the transcript backlog guard fetches
exactly one transcript on a backfill poll.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, db, podcast_feed, podcast_release  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402

EPISODES = 139


def _anchor_feed(count=EPISODES):
    """A real world shaped RSS document: newest first, itunes episode numbers,
    enclosures, and a podcast:transcript url per item."""
    items = []
    for n in range(count, 0, -1):                    # newest first, like Anchor
        items.append(f"""
    <item>
      <title>Episode {n}: Gym marketing made simple show {n}</title>
      <guid>anchor-guid-{n}</guid>
      <description>Show {n} in one sentence. Second sentence never used.</description>
      <itunes:episode>{n}</itunes:episode>
      <enclosure url="https://cdn.anchor.test/ep{n}.mp3" type="audio/mpeg"/>
      <pubDate>Mon, 01 Jun 2026 10:{n % 60:02d}:00 +0000</pubDate>
      <podcast:transcript url="https://cdn.anchor.test/ep{n}.txt" type="text/plain"/>
    </item>""")
    return ("""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel><title>Gym Marketing Made Simple</title>""" + "".join(items)
            + "</channel></rss>")


def _acct():
    return Account(key="lasso_ig", display_name="LASSO IG",
                   platform=Platform.INSTAGRAM, token_env="X", target_id_env="Y")


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(lib))


class FakeNano:
    def generate_image(self, prompt, model):
        return b"\x89PNG\r\n\x1a\nFAKE"


class FakeS3:
    def exists(self, key):
        return False

    def put(self, key, local_path):
        pass


def _dump():
    with db.connect() as conn:
        return "\n".join(conn.iterdump())


# ---- the first poll: backlog stores, at most the newest drafts ------------------------
def test_first_poll_backlog_never_drafts_back_episodes(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    fetches = []

    def transcript_fetch(url):
        fetches.append(url)
        return ("A clean spoken sentence about follow up for gym owners. "
                "Another clean supporting sentence about booked calls.")

    new = podcast_feed.poll(fetch=lambda: _anchor_feed(),
                            transcript_fetch=transcript_fetch)
    assert len(new) == EPISODES                     # the backlog stores once
    assert len(fetches) == 1                        # transcript guard: newest only
    assert fetches[0].endswith("ep139.txt")

    d = podcast_release.build_podcast_slot_draft(
        _acct(), "2026-07-06", nano_client=FakeNano(), s3_client=FakeS3())
    assert d is not None and d.status == DraftStatus.PENDING
    assert d.caption.startswith("EPISODE 139:")     # ONLY the newest episode
    # the rest of the week: no back episode ever cards
    for day in ("2026-07-07", "2026-07-08", "2026-07-09"):
        assert podcast_release.build_podcast_slot_draft(
            _acct(), day, nano_client=FakeNano(), s3_client=FakeS3()) is None
    # a re poll of the unchanged feed detects nothing and fetches nothing more
    assert podcast_feed.poll(fetch=lambda: _anchor_feed(),
                             transcript_fetch=transcript_fetch) == []
    assert len(fetches) == 1


# ---- the probe: read only, honest forecast ---------------------------------------------
def test_probe_is_side_effect_free(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: _anchor_feed(3),
                      transcript_fetch=lambda u: "One clean spoken sentence here.")
    before = _dump()
    out = podcast_feed.status_cli(fetch=lambda: _anchor_feed(3))
    assert out["reachable"] is True
    assert _dump() == before                        # store byte identical
    # and on a VIRGIN store the probe adds no schema either
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "virgin.db"))
    with db.connect() as conn:
        pass                                        # core schema only
    before = _dump()
    podcast_feed.status_cli(fetch=lambda: _anchor_feed(3))
    assert _dump() == before


def test_forecast_matches_rotation_math(monkeypatch, tmp_path, capsys):
    _arm(monkeypatch, tmp_path)
    out = podcast_feed.status_cli(fetch=lambda: _anchor_feed())
    printed = capsys.readouterr().out
    assert out["items"] == EPISODES and out["new"] == EPISODES
    assert out["latest"] == 139
    # 139 mod 4 = 3 -> template E, exactly the slot's own rotation
    assert podcast_release.template_for_episode(139) == "e"
    assert "ONLY episode 139" in out["forecast"]
    assert "podcast_release_e" in out["forecast"]
    assert "139 item(s) in the feed" in printed
    assert "0 already stored" in printed            # the armed watermark, pre poll

    # after the poll + the card, the forecast flips to the honest skip
    podcast_feed.poll(fetch=lambda: _anchor_feed(),
                      transcript_fetch=lambda u: "One clean spoken sentence here.")
    podcast_release.build_podcast_slot_draft(
        _acct(), "2026-07-06", nano_client=FakeNano(), s3_client=FakeS3())
    out = podcast_feed.status_cli(fetch=lambda: _anchor_feed())
    assert "would skip" in out["forecast"]
    assert "already carded" in out["forecast"]


def test_probe_honest_when_unreachable_or_dark(monkeypatch, tmp_path, capsys):
    _arm(monkeypatch, tmp_path)

    def boom():
        raise RuntimeError("connection refused")

    out = podcast_feed.status_cli(fetch=boom)
    assert out == {"reachable": False}
    assert "feed reachable: NO" in capsys.readouterr().out
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    out = podcast_feed.status_cli(fetch=lambda: _anchor_feed(1))
    assert out == {"reachable": None}               # dark = says so, does nothing
    assert "OFF" in capsys.readouterr().out
