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


def test_resolve_order_store_then_kv_then_default(monkeypatch):
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    assert cadence.resolve_posts_per_day("gritx", _FakeStore(ppd=None)) == 1
    assert cadence.resolve_posts_per_day("gritx", _FakeStore(ppd=2)) == 2
    # The SHARED PLANE wins over the local kv. The portal writes the owner's toggle
    # there and each service has its own SQLite, so a stale local '1' must never
    # override the owner's saved choice (it used to, permanently and unclearably).
    db.set_posts_per_day("gritx", 1)
    assert cadence.resolve_posts_per_day("gritx", _FakeStore(ppd=2)) == 2
    # kv is still the fallback when the shared plane has nothing to say.
    assert cadence.resolve_posts_per_day("gritx", _FakeStore(ppd=None)) == 1
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

# Builds start at TODAY, exactly like the production scan (client_media_sync passes
# date.today()). A fixed future literal here would collide with the HARD planning
# horizon (a build may not start beyond today+31; plan_horizon.py, Blake 2026-08-28).
from datetime import date as _date  # noqa: E402

_BUILD_START = _date.today().isoformat()


def _build(tmp_path, store, days=4, n_media=8):
    account = _account()
    _stock_clean(account.key)
    return cmr.build_client_month(
        account, "gritx", _BUILD_START, days, voice=_voice(),
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
            return [{"post_date": _BUILD_START, "format": "feed",
                     "account": "instagram", "status": "approved",
                     "caption": "human owned", "image_url":
                     "https://gritx.media/photo_00.jpg"}]

    store = _StoreWithApproved(ppd=2)
    out = _build(tmp_path, store, days=2, n_media=8)
    assert out["ok"]
    planned_days = {r["post_date"] for r in store.inserted}
    assert _BUILD_START not in planned_days            # locked day never re-planned


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


def test_cadence_noop_build_never_stamps_applied(monkeypatch, tmp_path):
    """Audit 2026-08-27 MAJOR regression: a rebuild the never-shrink/empty guard
    no-ops must NOT stamp cadence_applied — the toggle stays pending and the next
    scan retries (with allow_reshape=True threaded so the reshape can land)."""
    from agent import client_media_sync as cms
    monkeypatch.setenv("AGENT_CLIENT_MEDIA_SYNC", "true")
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")

    calls = {"built": 0, "reshape_flags": []}

    class _Store(_FakeStore):
        def list_month(self, base_key, month):
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

    results = [{"ok": True, "upserted": 0, "noop_shrink": True},   # guard no-op'd
               {"ok": True, "upserted": 8}]                        # real apply

    def _fake_build(account, base, start, days, **kw):
        calls["reshape_flags"].append(kw.get("allow_reshape"))
        out = results[min(calls["built"], len(results) - 1)]
        calls["built"] += 1
        return out
    import agent.client_month_run as _cmr
    monkeypatch.setattr(_cmr, "build_client_month", _fake_build)

    store = _Store(ppd=2)
    cms.scan_and_generate(clients=["gritx"], store=store)
    assert calls["built"] == 1
    assert db.kv_get("cadence_applied_gritx") in ("", None)   # NOT stamped on a no-op

    cms.scan_and_generate(clients=["gritx"], store=store)     # retries the toggle
    assert calls["built"] == 2
    assert calls["reshape_flags"] == [True, True]             # reshape threaded through
    assert db.kv_get("cadence_applied_gritx") == "2"          # stamped on the real apply


def test_apply_allow_reshape_skips_never_shrink_once(tmp_path, monkeypatch):
    """1x->2x mid-band (media between days and 2x days): fewer covered DATES with
    MORE feeds is a legitimate reshape — _apply must write it when allow_reshape."""
    rows = []
    for d in (1, 2):
        for slot in (0, 1):
            rows.append({"gym_id": "gritx", "post_date": f"2026-10-0{d}",
                         "account": "instagram", "format": "feed",
                         "caption": f"c{d}{slot}", "image_url": f"u{d}{slot}",
                         "status": "pending", "slot_index": slot})

    class _Existing(_FakeStore):
        def list_month(self, base_key, month):
            # 3 existing 1x feed DATES -> the date-unit guard would read 2 < 3 as shrink
            return [{"post_date": f"2026-10-0{d}", "format": "feed",
                     "account": "instagram", "status": "pending",
                     "caption": "x", "image_url": f"e{d}"} for d in (1, 2, 3)]

    from datetime import date
    store = _Existing()
    blocked = cmr._apply("gritx", list(rows), date(2026, 10, 1), 30, store,
                         lambda m: None)
    assert blocked.get("noop_shrink") is True and store.inserted == []

    store2 = _Existing()
    applied = cmr._apply("gritx", list(rows), date(2026, 10, 1), 30, store2,
                         lambda m: None, allow_reshape=True)
    assert applied["ok"] and not applied.get("noop_shrink")
    assert len(store2.inserted) == 4                          # the reshape landed


# ---- mix-counter bug fix (D9 / A6: tally drawn concepts, correct at 1x AND 2x) ----

def _insert_published_post(account_key, draft_id, category, creative, published_at):
    """One published post + (optionally) the draft that produced it, in the
    per-test sqlite. category=None -> legacy post with NO surviving draft."""
    import json as _json
    with db.connect() as conn:
        if category is not None:
            conn.execute(
                "INSERT OR REPLACE INTO drafts (draft_id, account_key, status, "
                "day_key, draft_type, data) VALUES (?,?,?,?,?,?)",
                (draft_id, account_key, "published", published_at[:10], "feed",
                 _json.dumps({"category": category})))
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, "
            "media_id, mode, creative_key, published_at) VALUES (?,?,?,?,?,?,?,?)",
            (draft_id, account_key, "instagram", "cap", "M", "published",
             creative, published_at))
        conn.commit()


def test_mix_tally_counts_drawn_concepts_1x():
    from agent import monthly_report
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    day = (now - timedelta(days=2)).isoformat()
    _insert_published_post("mixgym", "d1", "offer", "lasso_p1_x.jpg", day)
    _insert_published_post("mixgym", "d2", "testimonial", "lasso_p1_y.jpg", day)
    _snaps, posts = monthly_report.gather("mixgym", now=now)
    tally = {}
    for p_row in posts:
        k = monthly_report.pillar_for_post(p_row)
        tally[k] = tally.get(k, 0) + 1
    # DRAWN concepts counted — NOT the filename family (both files are lasso_p1_*,
    # which the old filename inference would have bucketed together as 'p1').
    assert tally == {"offer": 1, "testimonial": 1}


def test_mix_tally_counts_both_posts_on_a_2x_day():
    from agent import monthly_report
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    day = (now - timedelta(days=1)).isoformat()      # SAME day, two drawn concepts
    _insert_published_post("mixgym", "am", "offer", "a.jpg", day)
    _insert_published_post("mixgym", "pm", "service", "b.jpg", day)
    _snaps, posts = monthly_report.gather("mixgym", now=now)
    tally = {}
    for p_row in posts:
        k = monthly_report.pillar_for_post(p_row)
        tally[k] = tally.get(k, 0) + 1
    assert tally == {"offer": 1, "service": 1}       # two posts, two tallies


def test_mix_tally_legacy_post_falls_back_to_filename():
    from agent import monthly_report
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    day = (now - timedelta(days=3)).isoformat()
    _insert_published_post("mixgym", "old", None, "lasso_p2_hook.jpg", day)
    _snaps, posts = monthly_report.gather("mixgym", now=now)
    assert monthly_report.pillar_for_post(posts[0]) == "p2"   # rotation.pillar_of


def test_grade_card_pillar_counts_use_drawn_concepts(monkeypatch):
    from agent import grade_card
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    day = (now - timedelta(days=1)).isoformat()
    _insert_published_post("mixgym", "g1", "offer", "same_family_1.jpg", day)
    _insert_published_post("mixgym", "g2", "about", "same_family_2.jpg", day)
    _report, _planned, pillar_counts, _r = grade_card._grade_inputs("mixgym", now=now)
    assert pillar_counts == {"offer": 1, "about": 1}


# ---- deny-volume line in the monthly rollup (D8 addition 1) ------------------------

def test_retro_digest_carries_deny_line_when_counted():
    from agent.jobs.monthly_retro import build_digest
    findings = {"keep_doing": [], "stop_doing": [],
                "deny_volume": "Denies this month: 4 of 15 recreate budget used"}
    text = build_digest("gritx", "2026-08", findings, tainted=False)
    assert "Denies this month: 4 of 15" in text
    text2 = build_digest("gritx", "2026-08",
                         {"keep_doing": [], "stop_doing": []}, tainted=False)
    assert "Denies this month" not in text2               # no count -> no line, never guessed


# ---- 2x single-distinct-concept day emits ONE pair (D5 fallback) -------------------

def test_client_2x_single_concept_day_emits_one_pair(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    real = client_content.build_client_draft

    def _same_caption(account, day_key, voice, library_path, **kw):
        d = real(account, day_key, voice, library_path, **kw)
        if d is not None:
            d.caption = ("The same single concept caption for every draft today. "
                         "Save this post.")
        return d

    monkeypatch.setattr(cmr.client_content, "build_client_draft", _same_caption)
    store = _FakeStore(ppd=2)
    out = _build(tmp_path, store, days=2, n_media=8)
    assert out["ok"]
    ig_feeds = [r for r in store.inserted
                if r["format"] == "feed" and r["account"] == "instagram"]
    by_day = {}
    for r in ig_feeds:
        by_day.setdefault(r["post_date"], []).append(r)
    for day, feeds in by_day.items():
        assert len(feeds) == 1, f"{day}: dup concept must not fill slot 2"


# ---- publish-boundary caption floor + avatar rail (Defect B) -----------------------
# Full publish_due boundary tests live in tests/test_calendar_autopublish.py
# (thin feed reverts, story exempt, avatar term reverts, flag-off no-op).

def test_avatar_breach_detector(monkeypatch):
    # The avatar rail is OFF by default since Blake's 2026-09-01 ruling (CrossFit,
    # hyrox and competitive athletics are allowed). This test describes the rail's
    # behavior WHEN ARMED, so it arms it explicitly.
    monkeypatch.setenv("AGENT_AVATAR_ATHLETE_RAIL", "true")
    from agent import post_quality as pq
    assert pq.avatar_breach("Join our HYROX prep class") == "HYROX"
    assert pq.avatar_breach("built for the competitive  CrossFit athlete")
    assert pq.avatar_breach("strength athletes welcome")
    assert pq.avatar_breach("CrossFit ENG welcomes beginners") == ""   # gym names ok
    assert pq.avatar_breach("") == ""


def test_post_issues_blocks_avatar_terms(monkeypatch):
    # The avatar rail is OFF by default since Blake's 2026-09-01 ruling (CrossFit,
    # hyrox and competitive athletics are allowed). This test describes the rail's
    # behavior WHEN ARMED, so it arms it explicitly.
    monkeypatch.setenv("AGENT_AVATAR_ATHLETE_RAIL", "true")
    from agent import post_quality as pq

    class _D:
        caption = ("HYROX season is coming and we are ready for it. "
                   "Train with our coaches five days a week and see real results. "
                   "Save your spot today.")
        creative_public_url = "https://x/y.jpg"
        grounding = None
    issues = pq.post_issues(_D(), ())
    assert any("banned-audience" in i for i in issues)
