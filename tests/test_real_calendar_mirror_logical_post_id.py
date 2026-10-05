"""Durable logical_post_id identity (agent/real_calendar_mirror.py + agent/store.py),
offline.

config.logical_post_id_enabled() lands on the integration branch, not this one, so
every test monkeypatches it onto agent.config with raising=False (flag OFF = lambda
False, flag ON = lambda True).

Rules under test:
  * Flag OFF (the default on this branch): rows omit logical_post_id entirely, the
    prior row shape is byte-for-byte preserved, and the mirror performs NO draft-store
    writes (no stamping, no holds).
  * Flag ON: _real_row / collect_real_drafts / mirror_plan stay PURE — they never mint
    or stash an id; a draft without a durable id yields a row without the key.
  * The mirror stamps durably via PendingStore.ensure_logical_post_id BEFORE any
    calendar delete, re-reads the store, and only then builds rows. Any preflight
    failure aborts with ZERO calendar deletes and ZERO inserts.
  * No feed/story sibling inference from date/photo/caption: independent rows each
    own a UNIQUE singleton UUID.
  * A valid existing id is preserved verbatim across a FRESH PendingStore object and
    repeated mirror runs.
  * Fail closed (flag ON): an invalid existing id, an unstampable store, or a row
    missing its durable id after preflight raises ValueError BEFORE any store write.
"""

import os
import sys
import uuid as _uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config
from agent import real_calendar_mirror as rcm
from agent.drafter import Draft, DraftStatus
from agent.store import PendingStore


def _draft(draft_id="d1", account_key="northside_ig", day_key="2026-08-06",
           is_story=False, **kw):
    return Draft(
        draft_id=draft_id, account_key=account_key, platform="instagram",
        caption="hi", hashtags=[], creative_path="x.png",
        creative_public_url="https://cdn/x.jpg", scheduled_for="",
        status=DraftStatus.PENDING, is_story=is_story,
        day_key=day_key, draft_type="story" if is_story else "feed",
        category="proof", **kw)


def _flag(monkeypatch, on):
    # The helper does not exist on this branch; integration branch adds it.
    monkeypatch.setattr(config, "logical_post_id_enabled", lambda: on, raising=False)


class _FakeStore:
    def __init__(self, drafts):
        self._drafts = list(drafts)

    def list_for_account(self, account_key):
        return [d for d in self._drafts if d.account_key == account_key]

    def ensure_logical_post_id(self, account_key, draft_id, **kw):
        for d in self._drafts:
            if d.draft_id == draft_id:
                if d.account_key != account_key:
                    raise ValueError("tenant mismatch")
                existing = getattr(d, "logical_post_id", "") or ""
                if existing:
                    return str(_uuid.UUID(existing))  # raises if invalid
                d.logical_post_id = str(_uuid.uuid4())
                return d.logical_post_id
        raise KeyError(draft_id)


class _FakeSB:
    """Rejects any insert carrying an `id` (models 22P02); records inserted rows."""

    def __init__(self):
        self.inserted = []
        self.delete_calls = 0

    def insert_rows(self, account_key, rows, **kw):
        for r in rows:
            assert not r.get("id"), "insert must not send id; DB generates the uuid"
            self.inserted.append(dict(r))
        return rows

    def delete_month(self, account_key, month):
        self.delete_calls += 1
        return 0


# ---- flag OFF ----------------------------------------------------------------

def test_flag_off_omits_key_and_preserves_shape(monkeypatch):
    _flag(monkeypatch, False)
    draft = _draft()
    row = rcm._real_row("northside_ig", draft)
    assert "logical_post_id" not in row
    assert set(row) == {"gym_id", "account", "post_date", "pillar", "format",
                        "caption", "image_url", "status"}


def test_flag_off_still_absent_without_monkeypatch():
    # No monkeypatch at all: the missing helper reads as flag OFF, never raises.
    draft = _draft()
    row = rcm._real_row("northside_ig", draft)
    assert "logical_post_id" not in row


def test_mirror_flag_off_rows_omit_key_and_no_store_writes(monkeypatch, tmp_path):
    _flag(monkeypatch, False)
    store = PendingStore(str(tmp_path / "p.db"))
    store.put(_draft("off1"))
    sb = _FakeSB()
    res = rcm.mirror_to_supabase("northside_ig", store, sb)
    assert res["ok"] and res["upserted"] == 1
    assert "logical_post_id" not in sb.inserted[0]
    # OFF = no new draft-store writes: the row was never stamped.
    fresh = PendingStore(str(tmp_path / "p.db"))
    assert fresh.get("off1").logical_post_id == ""


# ---- purity (flag ON) ----------------------------------------------------------

def test_flag_on_row_pure_no_mint_no_stash(monkeypatch):
    _flag(monkeypatch, True)
    draft = _draft()
    row = rcm._real_row("northside_ig", draft)
    assert "logical_post_id" not in row  # stamping is the store's job, pre-delete
    assert draft.logical_post_id == ""  # _real_row never mutates the draft


def test_collect_and_plan_pure_no_stash(monkeypatch):
    _flag(monkeypatch, True)
    feed = _draft("df")
    story = _draft("ds", is_story=True)
    store = _FakeStore([feed, story])
    rows = rcm.collect_real_drafts("northside_ig", store)
    assert all("logical_post_id" not in r for r in rows)
    assert feed.logical_post_id == "" and story.logical_post_id == ""
    plan = rcm.mirror_plan("northside_ig", store, [])
    assert plan["upsert"] and feed.logical_post_id == ""


def test_valid_existing_id_preserved(monkeypatch):
    _flag(monkeypatch, True)
    existing = str(_uuid.uuid4())
    draft = _draft()
    draft.logical_post_id = existing
    assert rcm._real_row("northside_ig", draft)["logical_post_id"] == existing


def test_invalid_existing_id_fails_closed(monkeypatch):
    _flag(monkeypatch, True)
    draft = _draft()
    draft.logical_post_id = "not-a-uuid"
    with pytest.raises(ValueError):
        rcm._real_row("northside_ig", draft)


# ---- mirror with durable stamping ----------------------------------------------

def test_mirror_insert_carries_uuid(monkeypatch):
    _flag(monkeypatch, True)
    sb = _FakeSB()
    res = rcm.mirror_to_supabase("northside_ig", _FakeStore([_draft("good")]), sb)
    assert res["ok"] and res["upserted"] == 1
    for row in sb.inserted:
        _uuid.UUID(row["logical_post_id"])


def test_no_sibling_inference_feed_and_story_unique(monkeypatch):
    _flag(monkeypatch, True)
    feed = _draft("df")
    story = _draft("ds", is_story=True)  # same day, same photo lineage
    sb = _FakeSB()
    res = rcm.mirror_to_supabase("northside_ig", _FakeStore([feed, story]), sb)
    assert res["ok"] and res["upserted"] == 2
    ids = [r["logical_post_id"] for r in sb.inserted]
    assert len(set(ids)) == 2, "feed/story siblings must NOT share a logical id"


def test_same_day_feeds_unique(monkeypatch):
    _flag(monkeypatch, True)
    sb = _FakeSB()
    rcm.mirror_to_supabase("northside_ig",
                           _FakeStore([_draft("a"), _draft("b")]), sb)
    ids = [r["logical_post_id"] for r in sb.inserted]
    assert ids[0] != ids[1]


def test_mirror_aborts_on_invalid_draft_id_zero_writes(monkeypatch):
    _flag(monkeypatch, True)
    good = _draft("good")
    bad = _draft("bad")
    bad.logical_post_id = "garbage"
    sb = _FakeSB()
    with pytest.raises(ValueError):
        rcm.mirror_to_supabase("northside_ig", _FakeStore([good, bad]), sb)
    assert sb.inserted == []  # no insert
    assert sb.delete_calls == 0  # no delete


def test_mirror_aborts_when_stamp_fails_zero_deletes(monkeypatch):
    _flag(monkeypatch, True)

    class _BoomStore(_FakeStore):
        def ensure_logical_post_id(self, account_key, draft_id, **kw):
            raise RuntimeError("db gone")

    sb = _FakeSB()
    with pytest.raises(RuntimeError):
        rcm.mirror_to_supabase("northside_ig", _BoomStore([_draft("x")]), sb)
    assert sb.inserted == [] and sb.delete_calls == 0


def test_mirror_aborts_when_store_cannot_stamp(monkeypatch):
    _flag(monkeypatch, True)

    class _NoEnsure:
        def list_for_account(self, account_key):
            return [_draft("x")]

    sb = _FakeSB()
    with pytest.raises(ValueError):
        rcm.mirror_to_supabase("northside_ig", _NoEnsure(), sb)
    assert sb.inserted == [] and sb.delete_calls == 0


def test_cross_run_stability_fresh_store_objects(monkeypatch, tmp_path):
    """IDs stay stable across a FRESH PendingStore object and repeated mirror runs."""
    _flag(monkeypatch, True)
    path = str(tmp_path / "p.db")
    store1 = PendingStore(path)
    store1.put(_draft("f1"))
    store1.put(_draft("s1", is_story=True))
    sb1 = _FakeSB()
    res1 = rcm.mirror_to_supabase("northside_ig", store1, sb1)
    assert res1["ok"] and res1["upserted"] == 2
    first_ids = {r["format"]: r["logical_post_id"] for r in sb1.inserted}

    # A brand-new store object over the same db: no in-memory stash can leak.
    store2 = PendingStore(path)
    sb2 = _FakeSB()
    res2 = rcm.mirror_to_supabase("northside_ig", store2, sb2)
    assert res2["ok"] and res2["upserted"] == 2
    second_ids = {r["format"]: r["logical_post_id"] for r in sb2.inserted}
    assert second_ids == first_ids

    # And a third run on the first object: still identical.
    sb3 = _FakeSB()
    rcm.mirror_to_supabase("northside_ig", store1, sb3)
    assert {r["format"]: r["logical_post_id"] for r in sb3.inserted} == first_ids
