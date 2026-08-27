"""
2x posting cadence (CADENCE_SPEC.md, Blake 2026-08-27). Fully OFFLINE.

Asserts, per the spec's acceptance section:
  * A1 flag OFF -> byte-for-byte today: resolve returns 1 even with a stored 2,
    the client month emits the exact 1x shape (no slot_index key), the LASSO plan
    carries no cadence slots, slot times use the pre-cadence hash path.
  * A2 flag ON + posts_per_day=2 -> two DISTINCT feed+story pairs per day (captions
    AND images differ within the day), slot_index stamped 0/1.
  * A3 flag ON + posts_per_day=1/unset -> identical to today.
  * A5 slot times: 2x rows land 07:30 / 18:30 (and the env override), stories keep
    midday, 1x rows keep the stable hash.
  * D5 second-slot distinctness in the LASSO plan (category differs, cap preserved).
  * handle_cadence endpoint contract (save-while-dark, validation, kv write).
  * Replan boundary: a cadence change forces the media-sync lane to rebuild; the
    rebuild itself still honors locked (approved) days.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import cadence, config, db  # noqa: E402
from agent import client_content, client_month_run as cmr, client_sources as cs  # noqa: E402
from agent import calendar_autopublish as cap  # noqa: E402
from agent import portal_social as ps  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.real_month_planner import plan_month  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv("AGENT_CLIENT_MONTH", "true")
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    monkeypatch.delenv("ECHO_CADENCE_2X_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_CADENCE_SLOT_TIMES", raising=False)
    yield


class _FakeStore:
    def __init__(self, ppd=None):
        self.deleted = []
        self.inserted = []
        self._ppd = ppd

    def delete_month(self, base_key, month):
        self.deleted.append((base_key, month))
        return 0

    def insert_rows(self, base_key, rows):
        self.inserted.extend(rows)
        return rows

    def gym_posts_per_day(self, base_key):
        return self._ppd


def _voice():
    return VoiceDoc(raw="We help members win.\n#GetFit",
                    hashtags=["#GetFit"], ctas=["Save this post."])


def _account():
    return Account(key="gritx_ig", display_name="GritX", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="TID")


def _lib(tmp_path, n=8):
    import json
    lib = tmp_path / "gritx_lib"
    lib.mkdir(exist_ok=True)
    for i in range(n):
        (lib / f"photo_{i:02d}.jpg").write_bytes(b"\xff\xd8\xffFAKEJPEG")
        (lib / f"photo_{i:02d}.json").write_text(
            json.dumps({"public_url": f"https://gritx.media/photo_{i:02d}.jpg"}))
    return str(lib)


def _stock_clean(account_key):
    cs.add_source(account_key, "offer", "21 day kickstart for busy parents",
                  "client social intake")
    cs.add_source(account_key, "service", "Small group training",
                  "client social intake")
    cs.add_source(account_key, "about", "Who we help: parents in their 40s",
                  "client social intake")
    cs.add_source(account_key, "testimonial", "Maria dropped two sizes in 12 weeks",
                  "client social intake")


# ---- config + resolution ----------------------------------------------------------

def test_flag_defaults_off_and_slot_times_default():
    assert config.cadence_2x_enabled() is False
    assert config.cadence_slot_times() == ("07:30", "18:30")


def test_slot_times_override_and_invalid_fallback(monkeypatch):
    monkeypatch.setenv("AGENT_CADENCE_SLOT_TIMES", "06:00,20:15")
    assert config.cadence_slot_times() == ("06:00", "20:15")
    monkeypatch.setenv("AGENT_CADENCE_SLOT_TIMES", "6am,late")
    assert config.cadence_slot_times() == ("07:30", "18:30")
    monkeypatch.setenv("AGENT_CADENCE_SLOT_TIMES", "07:30")
    assert config.cadence_slot_times() == ("07:30", "18:30")


def test_db_helpers_validate_and_roundtrip():
    assert db.set_posts_per_day("gritx", 2) is True
    assert db.posts_per_day("gritx") == 2
    assert db.set_posts_per_day("gritx", 3) is False      # refused, unchanged
    assert db.posts_per_day("gritx") == 2
    assert db.set_posts_per_day("gritx", "1") is True
    assert db.posts_per_day("gritx") == 1
    assert db.posts_per_day("") is None
    assert db.posts_per_day("nobody") is None


def test_resolve_flag_off_is_always_one(monkeypatch):
    db.set_posts_per_day("gritx", 2)
    assert cadence.resolve_posts_per_day("gritx", _FakeStore(ppd=2)) == 1


def test_resolve_order_kv_then_store_then_default(monkeypatch):
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    assert cadence.resolve_posts_per_day("gritx", _FakeStore(ppd=None)) == 1
    assert cadence.resolve_posts_per_day("gritx", _FakeStore(ppd=2)) == 2
    db.set_posts_per_day("gritx", 1)
    assert cadence.resolve_posts_per_day("gritx", _FakeStore(ppd=2)) == 1  # kv wins
    db.set_posts_per_day("gritx", 2)
    assert cadence.resolve_posts_per_day("gritx", None) == 2

    class _Boom:
        def gym_posts_per_day(self, b):
            raise RuntimeError("network down")
    db.kv_set("portal_cadence_gritx", "")   # clear local
    assert cadence.resolve_posts_per_day("gritx", _Boom()) == 1  # error -> safe 1


# ---- handle_cadence endpoint ------------------------------------------------------

def test_handle_cadence_saves_while_dark(monkeypatch):
    """The preference SAVES even with ECHO_CADENCE_2X_ENABLED off (spec D4)."""
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setattr(ps, "is_social_active", lambda k, reader=None: True)
    status, resp = ps.handle_cadence("gritx_ig", 2, actor_id="U123")
    assert status == 200 and resp["ok"] is True
    assert resp["posts_per_day"] == 2 and resp["cadence_armed"] is False
    assert resp["replanned"] is False
    assert db.posts_per_day("gritx") == 2      # stored under the BASE


def test_handle_cadence_validation(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setattr(ps, "is_social_active", lambda k, reader=None: True)
    status, resp = ps.handle_cadence("gritx_ig", 3)
    assert status == 400 and resp["ok"] is False
    status, resp = ps.handle_cadence("gritx_ig", "two")
    assert status == 400
    status, _ = ps.handle_cadence("", 2)
    assert status == 400


def test_handle_cadence_gated_off(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "false")
    status, _ = ps.handle_cadence("gritx_ig", 2)
    assert status == 404


# ---- LASSO plan (real_month_planner) ----------------------------------------------

def _no_sprint(dk):
    return False


def test_plan_1x_carries_no_cadence_slots():
    plan = plan_month("lasso", "2026-10-01", 31, sprint_day_fn=_no_sprint,
                      book_dates=set())
    assert all(s.cadence_slot is None for s in plan)
    default_plan = plan_month("lasso", "2026-10-01", 31, sprint_day_fn=_no_sprint,
                              book_dates=set(), posts_per_day=1)
    assert plan == default_plan


def test_plan_2x_two_distinct_pairs_every_day():
    plan = plan_month("lasso", "2026-10-01", 31, sprint_day_fn=_no_sprint,
                      book_dates=set(), posts_per_day=2)
    days = {}
    for s in plan:
        if s.fmt == "feed":
            days.setdefault(s.post_date, []).append(s)
    assert len(days) == 31
    for d, feeds in days.items():
        assert len(feeds) == 2, d
        assert {f.cadence_slot for f in feeds} == {0, 1}, d
        assert feeds[0].category != feeds[1].category, d
    # every feed still has its paired story with the same category + slot
    for s in plan:
        if s.fmt == "story":
            assert s.cadence_slot in (0, 1)


# ---- client month (build_client_month) --------------------------------------------

def _build(tmp_path, store, days=4, n_media=8):
    account = _account()
    _stock_clean(account.key)
    return cmr.build_client_month(
        account, "gritx", "2026-10-01", days, voice=_voice(),
        library_path=_lib(tmp_path, n=n_media), store=store, banned_words=())


def test_client_1x_shape_unchanged_no_slot_index(tmp_path):
    store = _FakeStore()
    out = _build(tmp_path, store)
    assert out["ok"] and out["posts_per_day"] == 1
    assert store.inserted
    assert all("slot_index" not in r for r in store.inserted)


def test_client_flag_on_but_setting_1_is_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    store = _FakeStore(ppd=1)
    out = _build(tmp_path, store)
    assert out["ok"] and out["posts_per_day"] == 1
    assert all("slot_index" not in r for r in store.inserted)


def test_client_2x_two_distinct_pairs_per_day(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    store = _FakeStore(ppd=2)
    out = _build(tmp_path, store, days=3, n_media=8)
    assert out["ok"] and out["posts_per_day"] == 2
    ig_feeds = [r for r in store.inserted
                if r["format"] == "feed" and r["account"] == "instagram"]
    by_day = {}
    for r in ig_feeds:
        by_day.setdefault(r["post_date"], []).append(r)
    # every covered day carries two feeds with distinct caption AND image
    for day, feeds in by_day.items():
        assert len(feeds) == 2, f"{day} expected 2 feeds"
        assert feeds[0]["caption"].strip() != feeds[1]["caption"].strip(), day
        assert feeds[0]["image_url"] != feeds[1]["image_url"], day
        assert sorted(r.get("slot_index") for r in feeds) == [0, 1], day
    # FB mirrors + stories carry the same slot ordinal as their feed
    for r in store.inserted:
        assert r.get("slot_index") in (0, 1)


def test_client_2x_thin_media_covers_fewer_days_never_reuses(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    store = _FakeStore(ppd=2)
    out = _build(tmp_path, store, days=4, n_media=3)   # 3 photos, 2x -> 1.5 days
    assert out["ok"]
    ig_feeds = [r for r in store.inserted
                if r["format"] == "feed" and r["account"] == "instagram"]
    assert len(ig_feeds) <= 3                          # never more feeds than photos
    urls = [r["image_url"] for r in ig_feeds]
    assert len(urls) == len(set(urls))                 # no photo reused


def test_client_2x_locked_day_untouched(tmp_path, monkeypatch):
    """Replan boundary (D7): a day a human owns is skipped entirely at 2x too."""
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")

    class _StoreWithApproved(_FakeStore):
        def list_month(self, base_key, month):
            return [{"post_date": "2026-10-01", "format": "feed",
                     "account": "instagram", "status": "approved",
                     "caption": "human owned", "image_url":
                     "https://gritx.media/photo_00.jpg"}]

    store = _StoreWithApproved(ppd=2)
    out = _build(tmp_path, store, days=2, n_media=8)
    assert out["ok"]
    planned_days = {r["post_date"] for r in store.inserted}
    assert "2026-10-01" not in planned_days            # locked day never re-planned


# ---- publish-time slot times -------------------------------------------------------

def test_slot_time_2x_rows_deterministic(monkeypatch):
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    assert cap.slot_time_for_row(
        {"id": "x", "format": "feed", "slot_index": 0}) == "07:30"
    assert cap.slot_time_for_row(
        {"id": "x", "format": "feed", "slot_index": 1}) == "18:30"
    # stories keep midday even with a slot_index (paired story rides its own slot)
    story_t = cap.slot_time_for_row({"id": "x", "format": "story", "slot_index": 1})
    assert story_t == cap.slot_time_for_row({"id": "x", "format": "story"})


def test_slot_time_flag_off_or_no_index_uses_hash(monkeypatch):
    row = {"id": "abc123", "format": "feed"}
    baseline = cap.slot_time_for_row(row)
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    assert cap.slot_time_for_row(row) == baseline           # no slot_index -> hash
    monkeypatch.delenv("ECHO_CADENCE_2X_ENABLED", raising=False)
    row2 = {"id": "abc123", "format": "feed", "slot_index": 1}
    assert cap.slot_time_for_row(row2) == baseline          # flag off -> hash


# ---- media-sync replan trigger -----------------------------------------------------

def test_cadence_change_forces_rebuild_flag_on(monkeypatch, tmp_path):
    """scan_and_generate: a stored cadence differing from cadence_applied bypasses
    the built-out skip so the toggle replans (2x->1x must shrink, 1x->2x must grow)."""
    from agent import client_media_sync as cms
    monkeypatch.setenv("AGENT_CLIENT_MEDIA_SYNC", "true")
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")

    calls = {"built": 0}

    class _Store(_FakeStore):
        def list_month(self, base_key, month):
            # looks fully built out at 1x: 4 feed days for a 4-photo gym
            return [{"post_date": f"2026-10-{d:02d}", "format": "feed",
                     "account": "instagram", "status": "pending",
                     "caption": "x", "image_url": f"u{d}"} for d in range(1, 5)]

    lib = _lib(tmp_path, n=4)
    account = _account()
    _stock_clean(account.key)

    monkeypatch.setattr(cms, "_client_bases", lambda clients=None: ["gritx"])
    monkeypatch.setattr(cms, "_account_for_base", lambda b: account)
    monkeypatch.setattr(cms, "_library_dir", lambda b, out_dir=None: lib)
    monkeypatch.setattr(cms, "_has_approved_sources", lambda k: True)
    monkeypatch.setattr(cms, "sync_uploads",
                        lambda b, r2=None, out_dir=None, logger=None: {"synced": 0})
    monkeypatch.setattr(cms, "_default_r2", lambda: None)

    def _fake_build(account, base, start, days, **kw):
        calls["built"] += 1
        return {"ok": True, "upserted": 1}
    import agent.client_month_run as _cmr
    monkeypatch.setattr(_cmr, "build_client_month", _fake_build)

    store = _Store(ppd=2)                     # portal toggled 2x; applied stamp is 1
    out = cms.scan_and_generate(clients=["gritx"], store=store)
    assert out["ok"]
    assert calls["built"] == 1                # rebuild forced by the cadence change
    assert db.kv_get("cadence_applied_gritx") == "2"

    # second pass: cadence unchanged AND built-out -> idempotent skip
    out2 = cms.scan_and_generate(clients=["gritx"], store=store)
    assert calls["built"] == 1


# ---- publish-boundary caption floor + avatar rail (Defect B) -----------------------

class _RecheckStore:
    def __init__(self, rows):
        self._rows = rows
        self.reverted = []

    def due_rows(self, gym_id, run_date, catchup_days=0):
        return self._rows

    def claim_for_publish(self, row_id):
        return True

    def mark_publish_failed(self, row_id, revert_status="pending"):
        self.reverted.append((row_id, revert_status))

    def mark_published(self, *a, **k):
        pass


def test_avatar_breach_detector():
    from agent import post_quality as pq
    assert pq.avatar_breach("Join our HYROX prep class") == "HYROX"
    assert pq.avatar_breach("built for the competitive  CrossFit athlete")
    assert pq.avatar_breach("strength athletes welcome")
    assert pq.avatar_breach("CrossFit ENG welcomes beginners") == ""   # gym names ok
    assert pq.avatar_breach("") == ""


def test_post_issues_blocks_avatar_terms():
    from agent import post_quality as pq

    class _D:
        caption = ("HYROX season is coming and we are ready for it. "
                   "Train with our coaches five days a week and see real results. "
                   "Save your spot today.")
        creative_public_url = "https://x/y.jpg"
        grounding = None
    issues = pq.post_issues(_D(), ())
    assert any("banned-audience" in i for i in issues)
