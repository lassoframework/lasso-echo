"""
Podcast release card tests (pipeline Part B). Offline (fake nano/S3, fixture
RSS). Asserts: the release card drafts on detection and NEVER on a re-poll
(carding state is separate from detection state); the book campaign queue
still outranks it in the daily chain; the about line comes only from the feed
description, one sentence, dash free; only the NEWEST episode ever cards (no
stale backlog blast on first arm); max one podcast draft per account per day;
every draft is PENDING and nothing publishes without the tap; flag OFF = the
slot is inert and the chain is unchanged.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, creative_studio, db, media_host, podcast_feed  # noqa: E402
from agent import podcast_release  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402
from agent.store import PendingStore  # noqa: E402

from test_podcast_feed import FEED, FEED_PLUS_ONE  # noqa: E402


class FakeNano:
    def __init__(self):
        self.prompts = []

    def generate_image(self, prompt, model):
        self.prompts.append(prompt)
        return b"\x89PNG\r\n\x1a\nFAKE"


class FakeS3:
    def exists(self, key):
        return False

    def put(self, key, local_path):
        pass


class FakePoster:
    def __init__(self):
        self.cards, self.notices = [], []

    def post_approval_card(self, draft):
        self.cards.append(draft)
        return {"ok": True}

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}

    def mark_superseded(self, draft):
        pass

    def mark_expired(self, draft):
        pass


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


def _slot(day):
    return podcast_release.build_podcast_slot_draft(
        _acct(), day, nano_client=FakeNano(), s3_client=FakeS3())


# ---- about line: description only, one sentence, dash free ---------------------------
def test_about_line_one_sentence_dash_free():
    raw = ("<p>Most gyms don&#8217;t have a lead problem — they have a "
           "follow-up problem – truly. Second sentence never appears.</p>")
    about = podcast_release.about_line(raw)
    assert "Second sentence" not in about                       # first sentence only
    assert not podcast_release._DASH_RE.search(about)           # every dash family gone
    assert about.startswith("Most gyms don’t have a lead problem")  # curly apostrophe kept (a quote, not a dash)
    assert podcast_release.about_line("") == ""                 # nothing invented


# ---- card drafts on detection, never on re-poll ---------------------------------------
def test_card_on_detection_never_on_repoll(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: FEED)                       # detection
    d = _slot("2026-07-06")
    assert d is not None and d.status == DraftStatus.PENDING
    assert d.caption.startswith("EPISODE 7: The follow up problem")
    assert "cite:podcast_ep7" in d.source_fragments
    assert d.draft_type == "podcast"
    assert not podcast_release._DASH_RE.search(d.caption)       # client copy dash free
    # re-poll of the unchanged feed detects nothing and the slot stays quiet
    assert podcast_feed.poll(fetch=lambda: FEED) == []
    assert _slot("2026-07-07") is None                          # never re-cards
    assert _slot("2026-07-08") is None


def test_only_newest_episode_cards_no_backlog_blast(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: FEED)                       # eps 6 and 7 land
    d = _slot("2026-07-06")
    assert "EPISODE 7" in d.caption                             # newest wins
    assert _slot("2026-07-07") is None                          # ep 6 never cards late


def test_max_one_podcast_draft_per_day(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: FEED)
    assert _slot("2026-07-06") is not None                      # ep 7 cards
    podcast_feed.poll(fetch=lambda: FEED_PLUS_ONE)              # ep 8 arrives same day
    assert _slot("2026-07-06") is None                          # spacing: one per day
    d = _slot("2026-07-07")                                     # next day it cards
    assert d is not None and "EPISODE 8" in d.caption


# ---- book queue still outranks the release card ----------------------------------------
def test_book_queue_outranks_release_card(monkeypatch, tmp_path):
    from agent.runner import run_daily
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_BOOK_CAMPAIGN_ENABLED", "true")
    monkeypatch.setattr(media_host, "host_media",
                        lambda *a, **k: "https://cdn.echo.test/c.png")
    podcast_feed.poll(fetch=lambda: FEED)
    out = run_daily(poster=FakePoster(), accounts=[_acct()],
                    store=PendingStore(), scheduled_for="2026-07-06T12:00:00+00:00")
    feed_drafts = [d for d in out["drafts"] if d.status == DraftStatus.PENDING]
    assert feed_drafts, "book campaign should have drafted"
    assert feed_drafts[0].draft_type == "book"                  # book leads the day
    assert all(d.draft_type != "podcast" for d in out["drafts"])
    # the episode was NOT consumed: it still cards once the book slot frees up
    assert db.kv_get("podcast_release_carded_ep7-guid_lasso_ig") == ""


def test_release_card_takes_slot_when_book_dark(monkeypatch, tmp_path):
    from agent.runner import run_daily
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.delenv("AGENT_BOOK_CAMPAIGN_ENABLED", raising=False)
    monkeypatch.setattr(media_host, "host_media",
                        lambda *a, **k: "https://cdn.echo.test/c.png")
    art = tmp_path / "release.png"
    art.write_bytes(b"PNG")
    monkeypatch.setattr(creative_studio, "generate",
                        lambda *a, **k: {"path": str(art), "prompt": "p"})
    podcast_feed.poll(fetch=lambda: FEED)
    poster = FakePoster()
    out = run_daily(poster=poster, accounts=[_acct()],
                    store=PendingStore(), scheduled_for="2026-07-06T12:00:00+00:00")
    pending = [d for d in out["drafts"] if d.status == DraftStatus.PENDING]
    assert pending and pending[0].draft_type == "podcast"       # slot before rotation
    assert any(getattr(c, "draft_type", "") == "podcast" for c in poster.cards)
    # NEVER publishes without the tap: the card is pending, the posts log empty
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0


# ---- house style through the same builder ----------------------------------------------
def test_release_renders_through_house_builder(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: FEED)
    nano = FakeNano()
    d = podcast_release.build_podcast_slot_draft(_acct(), "2026-07-06",
                                                 nano_client=nano, s3_client=FakeS3())
    assert d is not None
    assert any("Cream #FAF6F0: THE canvas" in p for p in nano.prompts)  # house palette
    assert any("EPISODE 7: The follow up problem" in p for p in nano.prompts)
    assert all("BLACK canvas" not in p for p in nano.prompts)   # no book exception here


# ---- flag off = inert ---------------------------------------------------------------------
def test_flag_off_slot_inert(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: FEED)                       # episodes stored armed
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    assert _slot("2026-07-06") is None                          # zero behavior change
    assert db.kv_get("podcast_served_lasso_ig_2026-07-06") == ""
