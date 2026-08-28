"""
tests/test_caption_dedup.py — the HARD never-verbatim-twice guarantee
(report-card build, 2026-08-28; behind AGENT_CAPTION_COOLDOWN, default OFF).

Covers every lane LASSO publishes through:
  * ledger: verbatim_hash normalization + the 180-day different-date rule
  * stage lane: portal_calendar_store.insert_rows drops a verbatim-dup FEED row
  * plan lane: real_month_planner re-drafts a verbatim-dup builder output
  * publish lane: publish_guard.check emits duplicate_caption
  * chat lane: chat_publish.caption_belts blocks a verbatim dup
  * flag OFF: byte-for-byte today (nothing blocked anywhere)
All offline: fake kv db + fake http, no Supabase, no Slack, no Meta.
"""
from __future__ import annotations

import json

import pytest

from agent import caption_ledger as cl


class _FakeDB:
    def __init__(self, initial=None):
        self._store = dict(initial or {})

    def kv_get(self, key, default=""):
        return self._store.get(key, default)

    def kv_set(self, key, value):
        self._store[key] = str(value)


# ---------------------------------------------------------------------------
# Ledger: verbatim hash + window
# ---------------------------------------------------------------------------

def test_verbatim_hash_normalizes_only_trim_case_whitespace():
    assert cl.verbatim_hash("  Book a CALL   today. ") == \
        cl.verbatim_hash("book a call today.")
    # punctuation and tags COUNT for the verbatim rule (a reword is not a dup)
    assert cl.verbatim_hash("book a call today") != \
        cl.verbatim_hash("book a call today!")
    assert cl.verbatim_hash("#gym hello") != cl.verbatim_hash("hello")


def test_verbatim_hash_empty_is_never_matchable():
    assert cl.verbatim_hash("") == ""
    assert cl.verbatim_hash("   \n\t ") == ""


def test_verbatim_blocked_within_180_days_different_date():
    db = _FakeDB()
    cl.record_staged("lasso", "Exact same caption.", "2026-01-01", db=db)
    # 59 days later: the OLD fuzzy cooldown would block; the verbatim rule too
    assert cl.is_verbatim_blocked("lasso", "Exact same caption.", "2026-03-01", db=db)
    # 179 days later: OUTSIDE the fuzzy 60-day window but INSIDE verbatim 180
    assert cl.is_verbatim_blocked("lasso", "exact same caption.", "2026-06-29", db=db)
    assert not cl.is_on_cooldown("lasso", "Exact same caption.", "2026-06-29", db=db)
    # 181 days later: clear
    assert not cl.is_verbatim_blocked("lasso", "Exact same caption.", "2026-07-01", db=db)


def test_same_date_is_the_same_post_never_blocked():
    # The IG/FB cross-post + paired story share a caption on ONE date by design,
    # and a staged row's own ledger stamp carries its own post_date.
    db = _FakeDB()
    cl.record_staged("lasso", "One post, three rows.", "2026-05-05", db=db)
    assert not cl.is_verbatim_blocked("lasso", "One post, three rows.", "2026-05-05", db=db)
    assert not cl.is_on_cooldown("lasso", "One post, three rows.", "2026-05-05", db=db)
    assert not cl.is_blocked("lasso", "One post, three rows.", "2026-05-05", db=db)


def test_own_stamp_never_masks_an_earlier_true_dup():
    # Row A staged Jan 1; row B (same caption) staged Mar 1. At B's publish the
    # ledger's last_used is B's own date — the DATES LIST still exposes A.
    db = _FakeDB()
    cl.record_staged("lasso", "The cycling hook.", "2026-01-01", db=db)
    cl.record_staged("lasso", "The cycling hook.", "2026-03-01", db=db)
    assert cl.is_verbatim_blocked("lasso", "The cycling hook.", "2026-03-01", db=db)


def test_verbatim_ledger_is_gym_scoped_and_error_safe():
    db = _FakeDB()
    cl.record_staged("gym_a", "Shared industry caption.", "2026-01-01", db=db)
    assert not cl.is_verbatim_blocked("gym_b", "Shared industry caption.",
                                      "2026-02-01", db=db)

    class _Broken:
        def kv_get(self, key, default=""):
            raise RuntimeError("disk full")

        def kv_set(self, key, value):
            raise RuntimeError("disk full")

    # errors never block content and never raise
    assert not cl.is_verbatim_blocked("g", "cap", "2026-01-01", db=_Broken())
    cl.record_staged("g", "cap", "2026-01-01", db=_Broken())


def test_is_blocked_combines_cooldown_and_verbatim():
    db = _FakeDB()
    cl.record_staged("lasso", "Combined check caption.", "2026-01-01", db=db)
    assert cl.is_blocked("lasso", "Combined check caption.", "2026-01-15", db=db)
    assert cl.is_blocked("lasso", "combined check caption.", "2026-06-20", db=db)
    assert not cl.is_blocked("lasso", "Combined check caption.", "2026-08-01", db=db)


# ---------------------------------------------------------------------------
# Stage lane: insert_rows drops a verbatim-dup FEED row (flag armed)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self.status_code = 201
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class _FakeHTTP:
    """Echoes the POSTed rows back as the inserted representation."""

    def __init__(self):
        self.posted = []

    def post(self, url, headers=None, json=None, params=None, timeout=None):
        self.posted.append(json)
        return _FakeResp(list(json or []))


def _store(http):
    from agent.portal_calendar_store import SupabaseCalendarStore
    return SupabaseCalendarStore(url="https://sb.test", service_key="k", http=http)


def _feed_row(caption, post_date, account="instagram", fmt="feed"):
    return {"post_date": post_date, "account": account, "format": fmt,
            "caption": caption, "image_url": "https://cdn/x.jpg",
            "status": "pending", "pillar": "doctrine"}


@pytest.fixture
def ledger_db(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(cl, "_default_db", lambda: db)
    return db


def test_insert_rows_drops_verbatim_dup_when_armed(monkeypatch, ledger_db):
    monkeypatch.setenv("AGENT_CAPTION_COOLDOWN", "true")
    cl.record_staged("lasso", "Already shipped hook.", "2026-06-01", db=ledger_db)
    http = _FakeHTTP()
    inserted = _store(http).insert_rows("lasso", [
        _feed_row("Already shipped hook.", "2026-09-10"),   # verbatim dup -> dropped
        _feed_row("A genuinely fresh caption.", "2026-09-11"),
    ])
    captions = [r["caption"] for r in inserted]
    assert "A genuinely fresh caption." in captions
    assert "Already shipped hook." not in captions


def test_insert_rows_blocks_intra_batch_dup_but_allows_same_date_pack(
        monkeypatch, ledger_db):
    monkeypatch.setenv("AGENT_CAPTION_COOLDOWN", "true")
    http = _FakeHTTP()
    inserted = _store(http).insert_rows("lasso", [
        # ONE post = IG feed + FB mirror + paired story, all same date: allowed
        _feed_row("Same post three rows.", "2026-09-10", account="instagram"),
        _feed_row("Same post three rows.", "2026-09-10", account="facebook"),
        _feed_row("Same post three rows.", "2026-09-10", fmt="story"),
        # the SAME caption on another date in the same batch: a true dup
        _feed_row("Same post three rows.", "2026-09-20"),
    ])
    dates = sorted(r["post_date"] for r in inserted)
    assert dates == ["2026-09-10", "2026-09-10", "2026-09-10"]


def test_insert_rows_flag_off_is_byte_for_byte(monkeypatch, ledger_db):
    monkeypatch.delenv("AGENT_CAPTION_COOLDOWN", raising=False)
    monkeypatch.delenv("AGENT_EMPTY_CAPTION_GUARD", raising=False)
    cl.record_staged("lasso", "Already shipped hook.", "2026-06-01", db=ledger_db)
    http = _FakeHTTP()
    inserted = _store(http).insert_rows("lasso", [
        _feed_row("Already shipped hook.", "2026-09-10"),
        _feed_row("A genuinely fresh caption.", "2026-09-11"),
    ])
    assert len(inserted) == 2  # nothing blocked with the flag off


# ---------------------------------------------------------------------------
# Plan lane: the month planner re-drafts a dup, never ships it
# ---------------------------------------------------------------------------

def test_planner_redrafts_verbatim_dup_slot(monkeypatch, ledger_db):
    monkeypatch.setenv("AGENT_CAPTION_COOLDOWN", "true")
    from agent import real_month_planner as rmp

    cl.record_staged("lasso", "The verbatim repeat hook.", "2026-06-20", db=ledger_db)

    class _Draft:
        def __init__(self, caption):
            self.caption = caption
            self.category = ""
            self.day_key = ""
            self.draft_type = ""
            self.is_story = False

    calls = {"n": 0}

    def _builder(_target, _day):
        calls["n"] += 1
        # first concept is the 179-day-old verbatim dup; the retry is fresh
        if calls["n"] == 1:
            return _Draft("The verbatim repeat hook.")
        return _Draft("A fresh second concept.")

    slot = rmp.PlanSlot(post_date="2026-09-15", category="doctrine", fmt=rmp.FEED)
    draft, cat = rmp._build_feed_with_fallback(
        slot, {"doctrine": _builder}, "lasso", lambda m: None)
    # CADENCE: the slot re-drafted (never skipped, never shipped the dup)
    assert draft is not None and draft.caption == "A fresh second concept."
    assert cat == "doctrine" and calls["n"] == 2


# ---------------------------------------------------------------------------
# Publish lane: publish_guard emits duplicate_caption
# ---------------------------------------------------------------------------

def test_publish_guard_blocks_verbatim_dup(monkeypatch, ledger_db):
    monkeypatch.setenv("AGENT_CAPTION_COOLDOWN", "true")
    from agent import publish_guard as pg

    cl.record_staged("lasso", "This exact caption already ran and it was long enough to pass the thin caption floor for sure, one hundred and forty plus characters of real words. Book a call today.", "2026-05-01", db=ledger_db)
    cap = "This exact caption already ran and it was long enough to pass the thin caption floor for sure, one hundred and forty plus characters of real words. Book a call today."
    payload = pg.PublishPayload(
        row_id="r1", gym_id="lasso", platform="instagram", caption=cap,
        category="doctrine", media_ready=True, post_date="2026-09-01")
    assert pg.DUPLICATE_CAPTION in pg.check(payload)
    # the row's OWN date (the staging stamp) never blocks it
    own = pg.PublishPayload(
        row_id="r1", gym_id="lasso", platform="instagram", caption=cap,
        category="doctrine", media_ready=True, post_date="2026-05-01")
    assert pg.DUPLICATE_CAPTION not in pg.check(own)


def test_publish_guard_dedup_flag_off_and_story_exempt(monkeypatch, ledger_db):
    from agent import publish_guard as pg
    cl.record_staged("lasso", "Dup while off.", "2026-05-01", db=ledger_db)
    monkeypatch.delenv("AGENT_CAPTION_COOLDOWN", raising=False)
    payload = pg.PublishPayload(
        row_id="r1", gym_id="lasso", platform="instagram",
        caption="Dup while off.", media_ready=True, post_date="2026-06-01")
    assert pg.DUPLICATE_CAPTION not in pg.check(payload)
    monkeypatch.setenv("AGENT_CAPTION_COOLDOWN", "true")
    story = pg.PublishPayload(
        row_id="r2", gym_id="lasso", platform="instagram",
        caption="Dup while off.", media_ready=True, is_story=True,
        post_date="2026-06-01")
    assert pg.DUPLICATE_CAPTION not in pg.check(story)


# ---------------------------------------------------------------------------
# Chat lane: caption_belts blocks the dup with an honest reason
# ---------------------------------------------------------------------------

class _ChatDraft:
    def __init__(self, caption, account_key="lasso_ig", day_key="2026-09-01",
                 is_story=False):
        self.caption = caption
        self.account_key = account_key
        self.day_key = day_key
        self.is_story = is_story


def test_chat_lane_blocks_verbatim_dup(monkeypatch, ledger_db):
    monkeypatch.setenv("AGENT_CAPTION_COOLDOWN", "true")
    from agent import chat_publish
    cl.record_staged("lasso", "Chat repeat caption.", "2026-06-01", db=ledger_db)
    reason = chat_publish.caption_belts(_ChatDraft("Chat repeat caption."))
    assert reason and "duplicate caption" in reason
    # a fresh caption clears the belt
    assert chat_publish.caption_belts(_ChatDraft("A never-used caption.")) is None
    # flag off: the dup passes (byte-for-byte today's chat gate)
    monkeypatch.delenv("AGENT_CAPTION_COOLDOWN", raising=False)
    assert chat_publish.caption_belts(_ChatDraft("Chat repeat caption.")) is None


def test_chat_gate_fn_blocks_before_fabrication_scan(monkeypatch, ledger_db):
    # the belt runs FIRST, so a blocked caption needs no store at all
    monkeypatch.setenv("AGENT_CAPTION_COOLDOWN", "true")
    from agent import chat_publish
    cl.record_staged("lasso", "Gate order caption.", "2026-06-01", db=ledger_db)
    gate = chat_publish._real_gate_fn(store=None)
    out = gate(_ChatDraft("Gate order caption."))
    assert out["ok"] is False and "duplicate caption" in out["reason"]


# ---------------------------------------------------------------------------
# Publisher wires: the belt holds no matter which lane reached a publisher
# ---------------------------------------------------------------------------

class _Acct:
    key = "lasso_ig"
    platform = "instagram"

    def get_token(self):
        return "tok"


class _PubDraft:
    hashtags = []
    is_story = False
    creative_public_url = "https://cdn/x.jpg"

    def __init__(self, caption, day_key="2026-09-01"):
        self.caption = caption
        self.day_key = day_key


def test_meta_publisher_wire_refuses_verbatim_dup(monkeypatch, ledger_db):
    monkeypatch.setenv("AGENT_CAPTION_COOLDOWN", "true")
    from agent import meta_publisher
    cl.record_staged("lasso", "Wire level duplicate caption.", "2026-06-01",
                     db=ledger_db)
    with pytest.raises(ValueError, match="verbatim duplicate"):
        meta_publisher._publish_gated(
            _PubDraft("Wire level duplicate caption."), _Acct())
    # its OWN date (the staging stamp) never blocks; flag off never blocks
    cl.record_staged("lasso", "Own date caption.", "2026-09-01", db=ledger_db)
    monkeypatch.delenv("AGENT_CAPTION_COOLDOWN", raising=False)
    monkeypatch.setenv("AGENT_CAPTION_COOLDOWN", "false")
    # (no raise expected past the belt; stop before any network by clearing token)

    class _NoTok(_Acct):
        def get_token(self):
            return ""

    with pytest.raises(Exception, match="No token"):
        meta_publisher._publish_gated(
            _PubDraft("Wire level duplicate caption."), _NoTok())


def test_zernio_publisher_wire_refuses_verbatim_dup(monkeypatch, ledger_db):
    monkeypatch.setenv("AGENT_CAPTION_COOLDOWN", "true")
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_ZERNIO_PUBLISH", "true")
    from agent import zernio_publisher
    from agent.accounts import Platform

    cl.record_staged("eng", "Client wire duplicate caption.", "2026-06-01",
                     db=ledger_db)

    class _ClientAcct:
        key = "eng_ig"
        platform = Platform.INSTAGRAM

    class _Client:
        def list_accounts(self, profile_id):
            return {"accounts": [{"_id": "a1", "platform": "instagram",
                                  "account_state": "connected"}]}

    with pytest.raises(ValueError, match="verbatim duplicate"):
        zernio_publisher.publish(
            _PubDraft("Client wire duplicate caption."), _ClientAcct(),
            client=_Client(), profile_resolver=lambda k: "p1",
            page_resolver=lambda k: None)
