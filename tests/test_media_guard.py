"""
CROSS-DAY MEDIA GUARD (Blake, 2026-08-31: a client saw the SAME photo across
different weeks of their calendar). One photo never sits on multiple different
days of a gym's forward book; same-DATE siblings (FB mirror, paired story) are
one post and always share it. Fully offline: injected stores + tmp libraries.

Covers:
  * blocked_keys: cross-day pending/approved block; same-date exempt; published
    blocks only inside the trailing repeat window; GBP rows are out of scope
  * surviving_keys: span-month wipeable rows are FREE (the rebuild replaces
    them); coach_review + out-of-span pendings + trailing published still block
  * the DENY BACKFILL never reuses a photo pending on another day, and falls
    back to maximum spacing (one digest) when the library is too small
  * the MONTH BUILD never re-picks a photo published in the trailing window
  * EXPIRED AUTO-REDATE moves same-date siblings TOGETHER to one new date, and
    retires an unapproved expired row whose photo already sits on a future day
  * flag OFF restores the old behavior
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_sources as cs, media_guard  # noqa: E402
from agent import client_month_run as cmr  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv("AGENT_CLIENT_MONTH", "true")
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_MEDIA_CROSS_DAY_GUARD", raising=False)
    yield


def _voice():
    return VoiceDoc(raw="We help members win.\n#GetFit",
                    hashtags=["#GetFit"], ctas=["Save this post."])


def _account():
    return Account(key="gritx_ig", display_name="GritX", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="TID")


def _lib(tmp_path, n=6):
    import json
    lib = tmp_path / "gritx_lib"
    lib.mkdir(exist_ok=True)
    for i in range(n):
        (lib / f"photo_{i:02d}.jpg").write_bytes(b"\xff\xd8\xffFAKEJPEG")
        (lib / f"photo_{i:02d}.json").write_text(
            json.dumps({"public_url": f"https://gritx.media/photo_{i:02d}.jpg"}))
    return str(lib)


def _stock(account_key="gritx_ig"):
    cs.add_source(account_key, "offer", "21 day kickstart for busy parents",
                  "client social intake")
    cs.add_source(account_key, "service", "Small group training",
                  "client social intake")
    cs.add_source(account_key, "about", "Who we help: parents in their 40s",
                  "client social intake")


class _Store:
    """list_month over seeded rows + write capture (the month/backfill surface)."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.deleted, self.inserted = [], []

    def list_month(self, base, month):
        return [dict(r) for r in self.rows
                if str(r.get("post_date", "")).startswith(month)]

    def delete_month(self, base, month, **kw):
        self.deleted.append((base, month))
        return 0

    def insert_rows(self, base, rows):
        self.inserted.extend(rows)
        return rows

    def has_owner_visible_rows(self, base):
        return True


def _row(post_date, photo, status="pending", account="instagram", fmt="feed"):
    return {"gym_id": "gritx", "account": account, "format": fmt,
            "post_date": post_date, "status": status, "caption": "c",
            "image_url": f"https://gritx.media/{photo}"}


# ---- blocked_keys ------------------------------------------------------------------
def test_blocked_keys_cross_day_blocks_same_date_exempt():
    store = _Store([
        _row("2026-08-20", "a.jpg", "pending"),                       # other day
        _row("2026-08-25", "b.jpg", "pending"),                       # target day
        _row("2026-08-25", "b.jpg", "pending", account="facebook"),   # sibling
        _row("2026-08-25", "b.jpg", "pending", fmt="story"),          # sibling
        _row("2026-08-22", "c.jpg", "approved"),
    ])
    state = media_guard.book_state("gritx", store, "2026-08-15", 30)
    blocked = media_guard.blocked_keys(state, "2026-08-25")
    assert "a.jpg" in blocked and "c.jpg" in blocked
    assert "b.jpg" not in blocked, "same-date siblings are ONE post; never blocked"


def test_blocked_keys_published_only_inside_window(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_WINDOW_DAYS", "30")
    store = _Store([
        _row("2026-08-10", "recent.jpg", "published"),
        _row("2026-06-01", "old.jpg", "published"),
    ])
    state = media_guard.book_state("gritx", store, "2026-08-15", 30)
    blocked = media_guard.blocked_keys(state, "2026-09-01")
    assert "recent.jpg" in blocked
    assert "old.jpg" not in blocked, "published beyond the window may rotate back in"


def test_gbp_rows_out_of_scope():
    store = _Store([_row("2026-08-20", "g.jpg", "pending", account="googlebusiness")])
    state = media_guard.book_state("gritx", store, "2026-08-15", 30)
    assert state == {}, "GBP keeps its own deliberate reuse windows (rotation §3)"


# ---- surviving_keys (the month-rebuild view) ----------------------------------------
def test_surviving_keys_frees_span_wipeables_keeps_the_rest():
    from datetime import date
    store = _Store([
        _row("2026-08-20", "wipe.jpg", "pending"),          # in-span pending: replaced
        _row("2026-08-21", "coach.jpg", "coach_review"),    # NOT wipeable: survives
        _row("2026-08-22", "appr.jpg", "approved"),         # survives
        _row("2026-08-05", "pub.jpg", "published"),         # trailing window: survives
    ])
    keys = media_guard.surviving_keys("gritx", store, date(2026, 8, 15), 30)
    assert "wipe.jpg" not in keys, "the rebuild replaces span pendings; photo is free"
    assert {"coach.jpg", "appr.jpg", "pub.jpg"} <= keys


def test_surviving_keys_flag_off_is_empty(monkeypatch):
    from datetime import date
    monkeypatch.setenv("AGENT_MEDIA_CROSS_DAY_GUARD", "false")
    store = _Store([_row("2026-08-22", "appr.jpg", "approved")])
    assert media_guard.surviving_keys("gritx", store, date(2026, 8, 15), 30) == set()


# ---- month build never re-picks a trailing published photo -------------------------
def test_month_build_excludes_recently_published_photo(tmp_path):
    _stock()
    lib = _lib(tmp_path, n=6)
    # photo_02 published five days before the span starts (previous month's build).
    store = _Store([_row("2026-08-05", "photo_02.jpg", "published")])
    out = cmr.build_client_month(_account(), "gritx", "2026-08-10", days=10,
                                 voice=_voice(), library_path=lib, store=store,
                                 banned_words=())
    assert out["ok"] is True and store.inserted
    assert all("photo_02" not in (r.get("image_url") or "") for r in store.inserted), \
        "a photo published inside the repeat window must not come back on a new day"


# ---- deny backfill -----------------------------------------------------------------
def _denied(post_date, photo="photo_00.jpg"):
    return _row(post_date, photo, status="denied")


def test_deny_backfill_never_reuses_a_pending_photo(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DENY_BACKFILL", "true")
    _stock()
    lib = _lib(tmp_path, n=3)
    # photo_01 is PENDING on another day — the old code would happily reuse it.
    store = _Store([_denied("2026-08-19"),
                    _row("2026-08-25", "photo_01.jpg", "pending")])
    out = cmr.backfill_denied_slots(_account(), "gritx", "2026-08-19", days=30,
                                    voice=_voice(), library_path=lib, store=store,
                                    banned_words=())
    assert out["ok"] is True and out["backfilled"] == 1
    feeds = [r for r in store.inserted if r["format"] == "feed"]
    assert feeds and all("photo_02" in r["image_url"] for r in feeds), \
        "only photo_02 is free: photo_00 is the denied one, photo_01 pends elsewhere"


def test_deny_backfill_small_library_spaced_fallback_with_one_digest(
        monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DENY_BACKFILL", "true")
    _stock()
    lib = _lib(tmp_path, n=3)
    alerts = []
    from agent import ops_alerts
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: alerts.append(m))
    # Every photo already sits somewhere: 00 on the denied row, 01 pending a day
    # after the slot, 02 pending three weeks out. MAXIMUM SPACING must choose 02.
    store = _Store([_denied("2026-08-19", "photo_00.jpg"),
                    _row("2026-08-20", "photo_01.jpg", "pending"),
                    _row("2026-09-09", "photo_02.jpg", "pending")])
    out = cmr.backfill_denied_slots(_account(), "gritx", "2026-08-19", days=30,
                                    voice=_voice(), library_path=lib, store=store,
                                    banned_words=())
    assert out["ok"] is True and out["backfilled"] == 1
    feeds = [r for r in store.inserted if r["format"] == "feed"
             and r["account"] == "instagram"]
    assert feeds and "photo_02" in feeds[0]["image_url"], \
        "the spaced fallback must pick the photo FARTHEST from the slot"
    small = [m for m in alerts if "smaller than the forward book" in m]
    assert len(small) == 1, "exactly ONE small-library digest"


def test_deny_backfill_guard_flag_off_keeps_old_behavior(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DENY_BACKFILL", "true")
    monkeypatch.setenv("AGENT_MEDIA_CROSS_DAY_GUARD", "false")
    _stock()
    lib = _lib(tmp_path, n=2)
    # With the guard OFF, a pending photo elsewhere is again fair game (old code).
    store = _Store([_denied("2026-08-19", "photo_00.jpg"),
                    _row("2026-08-25", "photo_01.jpg", "pending")])
    out = cmr.backfill_denied_slots(_account(), "gritx", "2026-08-19", days=30,
                                    voice=_voice(), library_path=lib, store=store,
                                    banned_words=())
    assert out["ok"] is True and out["backfilled"] == 1
    feeds = [r for r in store.inserted if r["format"] == "feed"
             and r["account"] == "instagram"]
    assert feeds and "photo_01" in feeds[0]["image_url"]


# ---- spaced_choice ------------------------------------------------------------------
def test_spaced_choice_prefers_never_used_then_farthest(tmp_path):
    lib = _lib(tmp_path, n=3)
    state = {"photo_00.jpg": {("2026-08-20", "pending")},
             "photo_01.jpg": {("2026-09-05", "pending")}}
    # photo_02 never used -> wins outright
    assert media_guard.spaced_choice(lib, state, "2026-08-19") == "photo_02.jpg"
    state["photo_02.jpg"] = {("2026-08-19", "pending")}   # same-date still counts dist 0
    # now: 00 is 1 day away, 01 is 17 days away, 02 is 0 -> 01 wins
    assert media_guard.spaced_choice(lib, state, "2026-08-19") == "photo_01.jpg"
    # hard exclusions are never chosen
    assert media_guard.spaced_choice(
        lib, state, "2026-08-19",
        hard_exclude={"photo_01.jpg"}) == "photo_00.jpg"


# ---- expired auto-redate: siblings move together ------------------------------------
class _RedateStore:
    def __init__(self, occupied=()):
        self._occupied = list(occupied)
        self.redates, self.status_sets = [], []

    def list_month(self, gym, month):
        return [dict(r) for r in self._occupied
                if str(r.get("post_date", "")).startswith(month)]

    def patch_post_date(self, row_id, new_date):
        self.redates.append((row_id, new_date))
        return {"id": row_id, "post_date": new_date}

    def set_status(self, gym, row_id, status):
        self.status_sets.append((row_id, status))
        return {"id": row_id, "status": status}


class _MemKV:
    def __init__(self):
        self.d = {}

    def get(self, k, default=""):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


def test_redate_moves_same_date_siblings_to_one_day():
    from datetime import datetime
    from agent import calendar_autopublish as cap
    rows = [
        {"id": "f_ig", "gym_id": "eng", "account": "instagram", "format": "feed",
         "post_date": "2026-08-07", "status": "approved",
         "image_url": "https://eng.media/p7.jpg"},
        {"id": "f_fb", "gym_id": "eng", "account": "facebook", "format": "feed",
         "post_date": "2026-08-07", "status": "approved",
         "image_url": "https://eng.media/p7.jpg"},
        {"id": "s_ig", "gym_id": "eng", "account": "instagram", "format": "story",
         "post_date": "2026-08-07", "status": "approved",
         "image_url": "https://eng.media/p7_story.jpg"},
    ]
    store, kv = _RedateStore(), _MemKV()
    moved, retired = cap._auto_redate_expired(
        "eng", rows, store, kv, datetime.fromisoformat("2026-08-30T12:00:00"))
    assert retired == []
    dates = {rid: d for rid, d in store.redates}
    # the feed + its FB mirror share ONE photo and must land on ONE date
    assert dates["f_ig"] == dates["f_fb"], \
        "same-photo siblings must be re-dated TOGETHER, never split across days"
    assert len(moved) == 3


def test_redate_retires_unapproved_row_whose_photo_moved_on():
    from datetime import datetime
    from agent import calendar_autopublish as cap
    rows = [{"id": "dup", "gym_id": "eng", "account": "instagram", "format": "feed",
             "post_date": "2026-08-07", "status": "pending",
             "image_url": "https://eng.media/p9.jpg"}]
    occupied = [{"account": "instagram", "format": "feed",
                 "post_date": "2026-09-05", "status": "pending",
                 "image_url": "https://eng.media/p9.jpg"}]
    store, kv = _RedateStore(occupied), _MemKV()
    moved, retired = cap._auto_redate_expired(
        "eng", rows, store, kv, datetime.fromisoformat("2026-08-30T12:00:00"))
    assert moved == []
    assert [rid for rid, _ in store.status_sets] == ["dup"], \
        "an unapproved expired row whose photo already sits ahead is redundant"


def test_redate_still_moves_approved_row_even_when_photo_repeats():
    from datetime import datetime
    from agent import calendar_autopublish as cap
    rows = [{"id": "ap", "gym_id": "eng", "account": "instagram", "format": "feed",
             "post_date": "2026-08-07", "status": "approved",
             "image_url": "https://eng.media/p9.jpg"}]
    occupied = [{"account": "instagram", "format": "feed",
                 "post_date": "2026-09-05", "status": "pending",
                 "image_url": "https://eng.media/p9.jpg"}]
    store, kv = _RedateStore(occupied), _MemKV()
    moved, retired = cap._auto_redate_expired(
        "eng", rows, store, kv, datetime.fromisoformat("2026-08-30T12:00:00"))
    assert [m["id"] for m in moved] == ["ap"], \
        "the gym's approved word is never dropped silently"
    assert retired == []


# ---- sweep grouping (pure) ----------------------------------------------------------
def test_find_cross_day_repeats_groups_and_exempts_same_date():
    rows = [
        _row("2026-09-01", "x.jpg", "pending"),
        _row("2026-09-01", "x.jpg", "pending", account="facebook"),
        _row("2026-09-04", "x.jpg", "pending"),
        _row("2026-09-02", "y.jpg", "pending"),
    ]
    dupes = media_guard.find_cross_day_repeats(rows)
    assert set(dupes) == {"x.jpg"}
    assert set(dupes["x.jpg"]) == {"2026-09-01", "2026-09-04"}


# ---- autofit reframe resolution (zanshin: approved reframe never blocked the raw) ---
def _sha12(path):
    import hashlib
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]


def test_surviving_keys_resolves_reframe_names_to_raw_photos(tmp_path):
    from datetime import date
    lib = _lib(tmp_path, n=2)
    h = _sha12(os.path.join(lib, "photo_00.jpg"))
    # An APPROVED row that shipped through feed autofit carries the reframe name,
    # not the library basename — the guard must still block re-picking photo_00.
    store = _Store([_row("2026-08-22", f"{h}__feed.jpg", "approved")])
    keys = media_guard.surviving_keys("gritx", store, date(2026, 8, 15), 30,
                                      library_path=lib)
    assert "photo_00.jpg" in keys, \
        "an approved autofit reframe must block its RAW library photo"


def test_row_media_key_prefers_story_raw_source():
    row = {"image_url": "https://cdn/abc123_story_card.jpg",
           "source_media_url": "https://gritx.media/photo_05.jpg"}
    assert media_guard.row_media_key(row) == "photo_05.jpg", \
        "a story's identity is its raw source photo, not the burned caption card"


def test_book_state_keys_stories_by_source(tmp_path):
    lib = _lib(tmp_path, n=2)
    story = _row("2026-08-20", "burned_card_zz.jpg", "pending", fmt="story")
    story["source_media_url"] = "https://gritx.media/photo_01.jpg"
    store = _Store([story])
    state = media_guard.book_state("gritx", store, "2026-08-15", 30,
                                   library_path=lib)
    assert "photo_01.jpg" in state
    blocked = media_guard.blocked_keys(state, "2026-08-25")
    assert "photo_01.jpg" in blocked
