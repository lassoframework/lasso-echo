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

from agent import config, creative_studio, db, media_host, ops_alerts, podcast_feed  # noqa: E402
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


# ---- the same builder; the LOCKED template rides as the scoped palette -------------------
def test_release_renders_through_house_builder(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: FEED)
    nano = FakeNano()
    d = podcast_release.build_podcast_slot_draft(_acct(), "2026-07-06",
                                                 nano_client=nano, s3_client=FakeS3())
    assert d is not None
    assert any("EPISODE 7: The follow up problem" in p for p in nano.prompts)
    # the locked navy poster template replaces the cream palette for release
    # cards ONLY (a scoped exception, exactly like the book cover)
    assert any("LOCKED TEMPLATE podcast_release_" in p for p in nano.prompts)
    assert any("#1A2340" in p for p in nano.prompts)
    assert all("BLACK canvas" not in p for p in nano.prompts)   # no book exception here


# ---- locked templates: deterministic rotation, 3 digit slot, word boundary title ---------
def test_template_rotation_deterministic_mod4():
    assert podcast_release.template_for_episode(131) == "e"
    assert podcast_release.template_for_episode(132) == "a"
    assert podcast_release.template_for_episode(133) == "b"
    assert podcast_release.template_for_episode(134) == "c"
    # stable across re-drafts (a pure function of the episode number)
    for n in (7, 131, 132, 999):
        assert (podcast_release.template_for_episode(n)
                == podcast_release.template_for_episode(n))
    # every episode number lands in the set A, B, C, E; never random
    assert {podcast_release.template_for_episode(n)
            for n in range(100, 108)} == {"a", "b", "c", "e"}


def test_title_truncates_at_word_boundary_onto_two_lines():
    long_title = ("Episode 7: The follow up problem every single gym owner "
                  "keeps ignoring until the calendar goes completely empty")
    lines = podcast_release.title_lines(long_title)
    assert len(lines) == 2
    assert all(len(line) <= 40 for line in lines)
    stripped = podcast_release._title_slot(long_title)
    assert stripped.startswith(" ".join(lines))     # a clean word boundary prefix
    source_words = stripped.split()
    for line in lines:
        for word in line.split():
            assert word in source_words             # never cut mid word
    # short titles stay one line, untouched
    assert podcast_release.title_lines("Episode 9: Short and sweet") == [
        "Short and sweet"]


def test_template_slots_filled_and_audit_names_template(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: FEED)
    nano = FakeNano()
    d = podcast_release.build_podcast_slot_draft(_acct(), "2026-07-06",
                                                 nano_client=nano, s3_client=FakeS3())
    assert d is not None
    prompt = nano.prompts[0]
    assert "EPISODE 007" in prompt                  # 3 digit episode slot (ep 7)
    assert "podcast_release_e" in prompt            # 7 % 4 = 3 -> template E
    assert "GYM MARKETING MADE SIMPLE" in prompt and "BY LASSO" in prompt
    assert "NOW PLAYING" in prompt                  # the player card, faithfully
    assert "HOSTED BY SHERMAN MERRICKS AND BLAKE RUFF" in prompt
    # every rendered text element lives in the template spec: it is dash free
    # (the model-facing prompt scaffolding around it may carry hyphens)
    ep = podcast_feed.get_episode(7)
    spec = podcast_release.release_concept(ep)
    assert not podcast_release._DASH_RE.search(spec["palette"])
    rows = [r for r in db.audit_rows() if r["kind"] == "podcast_release"]
    assert rows and "podcast_release_e" in rows[0]["reason"]  # the pick is logged


# ---- dark studio: state not advanced, episode stays eligible --------------------------------
def test_dark_studio_leaves_episode_eligible(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: FEED)
    monkeypatch.setattr(creative_studio, "generate", lambda *a, **k: None)
    d = podcast_release._next_release_draft(_acct(), "2026-07-06", FakeNano(), FakeS3())
    assert d is None
    # the carding watermark must NOT have been stamped: the episode stays eligible
    assert db.kv_get("podcast_release_carded_ep7-guid_lasso_ig") == ""
    # after the studio recovers, the same episode cards normally on the next poll
    art = tmp_path / "release.png"
    art.write_bytes(b"PNG")
    monkeypatch.setattr(creative_studio, "generate",
                        lambda *a, **k: {"path": str(art), "prompt": "p"})
    monkeypatch.setattr(media_host, "host_media",
                        lambda *a, **k: "https://cdn.echo.test/r.png")
    d = podcast_release._next_release_draft(_acct(), "2026-07-07", FakeNano(), FakeS3())
    assert d is not None and "EPISODE 7" in d.caption


# ---- dark studio: ops alert fires on None, names the episode --------------------------------
def test_dark_studio_fires_ops_alert(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: FEED)
    monkeypatch.setattr(creative_studio, "generate", lambda *a, **k: None)
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: fired.append(msg))
    podcast_release._next_release_draft(_acct(), "2026-07-06", FakeNano(), FakeS3())
    assert len(fired) == 1
    assert "7" in fired[0]         # episode number present
    assert "studio" in fired[0].lower() or "unavailable" in fired[0].lower()
    assert "eligible" in fired[0]  # operator knows the episode will retry


# ---- manual podcast-draft command: held for approval, recovers missed episode ---------------
def test_podcast_draft_cli_recovery(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: FEED)
    art = tmp_path / "release.png"
    art.write_bytes(b"PNG")
    monkeypatch.setattr(creative_studio, "generate",
                        lambda *a, **k: {"path": str(art), "prompt": "p"})
    monkeypatch.setattr(media_host, "host_media",
                        lambda *a, **k: "https://cdn.echo.test/r.png")
    d = podcast_release.release_draft_for_episode(
        _acct(), 7, "2026-07-06", FakeNano(), FakeS3())
    assert d is not None and d.status == DraftStatus.PENDING
    assert "EPISODE 7" in d.caption
    assert d.draft_type == "podcast"
    assert "cite:podcast_ep7" in d.source_fragments
    assert db.kv_get("podcast_release_carded_ep7-guid_lasso_ig") == "2026-07-06"
    # flag off: returns None
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    assert podcast_release.release_draft_for_episode(
        _acct(), 7, "2026-07-07", FakeNano(), FakeS3()) is None
    # unknown episode: returns None
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "true")
    assert podcast_release.release_draft_for_episode(
        _acct(), 999, "2026-07-07", FakeNano(), FakeS3()) is None


# ---- manual redraft (podcast-draft CLI) with dark studio: state not advanced, episode recoverable ---
def test_manual_redraft_dark_studio_leaves_episode_recoverable(monkeypatch, tmp_path):
    """release_draft_for_episode with a dark studio must: return None, NOT stamp the
    carded key (so the episode stays eligible for a second attempt once the studio
    recovers), and fire an ops alert that names the episode number. This is the
    redraft path end-to-end lock for the podcast silent-miss sweep."""
    _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: FEED)
    monkeypatch.setattr(creative_studio, "generate", lambda *a, **k: None)
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: fired.append(msg))

    d = podcast_release.release_draft_for_episode(
        _acct(), 7, "2026-07-06", FakeNano(), FakeS3())

    assert d is None
    assert db.kv_get("podcast_release_carded_ep7-guid_lasso_ig") == "", (
        "state must NOT advance when the studio is dark; "
        "the episode must stay eligible for the next attempt"
    )
    assert len(fired) == 1, f"expected exactly 1 ops alert, got {len(fired)}: {fired}"
    assert "7" in fired[0]         # episode number named in the alert
    assert "studio" in fired[0].lower() or "unavailable" in fired[0].lower()

    # After the studio recovers, the SAME episode can be manually redrafted:
    art = tmp_path / "release.png"
    art.write_bytes(b"PNG")
    monkeypatch.setattr(creative_studio, "generate",
                        lambda *a, **k: {"path": str(art), "prompt": "p"})
    monkeypatch.setattr(media_host, "host_media",
                        lambda *a, **k: "https://cdn.echo.test/r.png")
    d2 = podcast_release.release_draft_for_episode(
        _acct(), 7, "2026-07-07", FakeNano(), FakeS3())
    assert d2 is not None and d2.status == DraftStatus.PENDING
    assert "EPISODE 7" in d2.caption
    assert db.kv_get("podcast_release_carded_ep7-guid_lasso_ig") == "2026-07-07"


# ---- flag off = inert ---------------------------------------------------------------------
def test_flag_off_slot_inert(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: FEED)                       # episodes stored armed
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    assert _slot("2026-07-06") is None                          # zero behavior change
    assert db.kv_get("podcast_served_lasso_ig_2026-07-06") == ""
