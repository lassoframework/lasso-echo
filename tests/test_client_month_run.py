"""
Per-client MONTH builder (client_month_run). Fully OFFLINE: an injected store + fake
feed/story template_fns (no Gemini, no host, no Supabase). Asserts:
  * flag OFF -> ok:False and the store is never touched
  * a stocked no-library client produces PAUSED rows, gym_id = the BASE, IG+FB for
    feeds and IG-only for stories, image_url set from the template url, NO id, status
    'pending'
  * a source whose caption carries a banned word is DROPPED (never in the output),
    and a different clean source still fills the day
  * the four gritx/topfuel accounts exist and are inactive
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_month_run as cmr, client_sources as cs  # noqa: E402
from agent.accounts import Account, Platform, get_account  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv("AGENT_CLIENT_MONTH", "true")
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    yield


class _FakeStore:
    def __init__(self):
        self.deleted = []
        self.inserted = []

    def delete_month(self, base_key, month):
        self.deleted.append((base_key, month))
        return 0

    def insert_rows(self, base_key, rows):
        self.inserted.extend(rows)
        return rows            # echo back so upserted counts


def _voice():
    return VoiceDoc(raw="We help members win.\n#GetFit",
                    hashtags=["#GetFit"], ctas=["Save this post."])


def _account():
    return Account(key="gritx_ig", display_name="GritX", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="TID")


def _feed_fn(account, source, day_key):
    return "https://cdn.example/feed.png"


def _story_fn(account, source, day_key):
    return "https://cdn.example/story.png"


def _stock_clean(account_key):
    cs.add_source(account_key, "offer", "21 day kickstart for busy parents", "client social intake")
    cs.add_source(account_key, "service", "Small group training", "client social intake")
    cs.add_source(account_key, "about", "Who we help: parents in their 40s", "client social intake")


# ---- 1. flag OFF -> nothing touched ----------------------------------------------
def test_flag_off_touches_nothing(monkeypatch):
    monkeypatch.setenv("AGENT_CLIENT_MONTH", "false")
    _stock_clean("gritx_ig")
    store = _FakeStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=5, voice=_voice(),
        library_path=None, feed_template_fn=_feed_fn, story_template_fn=_story_fn,
        store=store, banned_words=())
    assert out["ok"] is False
    assert store.deleted == [] and store.inserted == []


# ---- 2. stocked no-library client -> PAUSED rows, IG+FB feed, IG story, no id -----
def test_builds_paused_rows_with_fb_mirror():
    _stock_clean("gritx_ig")
    store = _FakeStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=10, voice=_voice(),
        library_path=None, feed_template_fn=_feed_fn, story_template_fn=_story_fn,
        store=store, banned_words=())
    assert out["ok"] is True
    rows = store.inserted
    assert rows, "no rows inserted"
    # every row: gym_id = BASE, PAUSED, no id, image_url from a template
    for r in rows:
        assert r["gym_id"] == "gritx"
        assert r["status"] == "pending"          # PAUSED, never approved/published
        assert "id" not in r
        assert r["image_url"] in ("https://cdn.example/feed.png",
                                  "https://cdn.example/story.png")
    # feeds appear on BOTH instagram and facebook; stories instagram-only
    feed_ig = [r for r in rows if r["format"] == "feed" and r["account"] == "instagram"]
    feed_fb = [r for r in rows if r["format"] == "feed" and r["account"] == "facebook"]
    story_rows = [r for r in rows if r["format"] == "story"]
    assert len(feed_ig) == len(feed_fb) and len(feed_ig) > 0
    assert all(r["account"] == "instagram" for r in story_rows)
    assert story_rows, "no story rows"
    # delete-then-insert swept the month
    assert ("gritx", "2026-08") in store.deleted


# ---- 3. a banned-word caption is DROPPED; a clean source still fills the day ------
def test_banned_word_dropped_never_emitted():
    # one banned source + clean sources. The guard must never emit the banned word.
    cs.add_source("gritx_ig", "service", "High Intensity CrossFit style Cardio", "client social intake")
    _stock_clean("gritx_ig")
    store = _FakeStore()
    banned = ["crossfit", "bootcamp", "cardio", "hyrox", "intensity", "compete"]
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=14, voice=_voice(),
        library_path=None, feed_template_fn=_feed_fn, story_template_fn=_story_fn,
        store=store, banned_words=banned)
    assert out["ok"] is True
    # NO row caption contains any banned word
    for r in store.inserted:
        cap = r["caption"].lower()
        for w in banned:
            assert w not in cap, f"banned word {w!r} leaked: {r['caption']!r}"
    # clean sources still produced a real month
    assert out["upserted"] > 0


def test_all_sources_banned_drops_every_day():
    # ONLY banned sources: every day drops, nothing emitted, no leak.
    cs.add_source("gritx_ig", "service", "CrossFit Cardio Intensity", "client social intake")
    cs.add_source("gritx_ig", "offer", "Bootcamp Hyrox Compete", "client social intake")
    store = _FakeStore()
    banned = ["crossfit", "cardio", "intensity", "bootcamp", "hyrox", "compete"]
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=5, voice=_voice(),
        library_path=None, feed_template_fn=_feed_fn, story_template_fn=_story_fn,
        store=store, banned_words=banned)
    assert out["ok"] is True
    assert out["skipped_banned"] == 5      # the guard fired every day
    assert out["upserted"] == 0
    assert store.inserted == []


# ---- 4. works with library_path=None (infographic path) --------------------------
def test_no_library_uses_infographic_template():
    _stock_clean("gritx_ig")
    seen = {"feed": 0, "story": 0}

    def feed_fn(account, source, day_key):
        seen["feed"] += 1
        return "https://cdn.example/feed.png"

    def story_fn(account, source, day_key):
        seen["story"] += 1
        return "https://cdn.example/story.png"

    store = _FakeStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=6, voice=_voice(),
        library_path=None, feed_template_fn=feed_fn, story_template_fn=story_fn,
        store=store, banned_words=())
    assert out["ok"] is True
    assert seen["feed"] > 0 and seen["story"] > 0   # the infographic template_fn ran


# ---- 5. the four client accounts exist and are inactive --------------------------
def test_accounts_exist_inactive():
    for key in ("gritx_ig", "gritx_fb", "topfuel_ig", "topfuel_fb"):
        a = get_account(key)
        assert a is not None, f"{key} missing"
        assert a.active is False, f"{key} must be inactive"


# ---- 6. banned-word matcher is word-boundary (no false positives) ----------------
def test_banned_word_boundary():
    assert cmr._has_banned_word("we compete weekly", ["compete"])
    assert not cmr._has_banned_word("we are competent coaches", ["compete"])
    assert not cmr._has_banned_word("clean caption", [])
