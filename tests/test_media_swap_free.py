"""
B6 — swapping a wrong photo is FREE; recreating a caption still costs one of 15.

THE BUDGET DESIGN BUG (Pete, zanshin): the portal's only levers are approve / edit /
deny / kill, so the "Use a different photo" chip and the "Caption needs work" chip
are BOTH a deny, and both burn one of the 15 monthly recreates
(portal_social.MONTHLY_RECREATE_BUDGET). Pete ran out of recreates swapping PHOTOS
and then could not fix a caption. The counter was never wrong -- the two actions
were never separated.

Everything offline: a fake store stands in for PostgREST, the photo picker is
injected, no hosting, no network, no library on disk.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import media_swap as msw          # noqa: E402
from agent import portal_social as ps        # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-secret")
    monkeypatch.setenv("AGENT_SOCIAL_BILLING_DELEGATED", "true")
    yield


def _row(row_id="p1", gym_id="zanshin", status="pending", fmt="feed"):
    return {"id": row_id, "gym_id": gym_id, "post_date": "2026-09-20",
            "account": "instagram", "status": status, "format": fmt,
            "caption": "a caption the gym is happy with",
            "image_url": "https://cdn/old.jpg", "source_media_url": "old.jpg",
            "pillar": "community", "scheduled_at": None}


class _Store:
    """Enforces the same gym isolation and the same pending/coach_review write guard
    the real SupabaseCalendarStore.swap_media applies server-side."""

    def __init__(self, rows=None):
        self._rows = {r["id"]: dict(r) for r in (rows or [])}
        self.swaps = []
        self.patches = []

    def get_row(self, account_key, row_id):
        r = self._rows.get(row_id)
        return dict(r) if r and str(r.get("gym_id")) == str(account_key) else None

    def set_status(self, account_key, row_id, status):
        self.patches.append((row_id, status))
        r = self._rows.get(row_id)
        if not r or str(r.get("gym_id")) != str(account_key):
            return None
        r["status"] = status
        return dict(r)

    def swap_media(self, account_key, row_id, image_url, source_media_url=None,
                   extra_fields=None):
        self.swaps.append((row_id, image_url, source_media_url))
        self.extras = dict(extra_fields or {})
        r = self._rows.get(row_id)
        if not r or str(r.get("gym_id")) != str(account_key):
            return None
        if r.get("status") not in ("pending", "coach_review"):
            return None                      # the server-side status guard
        r["image_url"] = image_url
        if source_media_url is not None:
            r["source_media_url"] = source_media_url
        for col in ("thumbnail_url", "source_media_asset_id"):
            if col in (extra_fields or {}):
                r[col] = extra_fields[col]
        return dict(r)

    def list_month(self, account_key, month):
        return [dict(r) for r in self._rows.values()]


def _picker(_gym, _row, siblings=(), **_kw):
    var = {"ok": True, "image_url": "https://cdn/new.jpg",
           "source_media_url": None, "key": "new.jpg", "kind": "photo",
           "source": "local", "thumbnail_url": "", "source_media_asset_id": "",
           "path": ""}
    # the real picker shapes one variant per sibling BEFORE any write (all or nothing)
    return dict(var, siblings={str(s["id"]): dict(var) for s in siblings})


def _wire(monkeypatch, store):
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)
    # after_swap settles the Drive/served ledgers post-write; the handler tests are
    # about the HTTP contract, so keep them off the real ledgers.
    monkeypatch.setattr(msw, "after_swap", lambda *a, **k: None)


def _cand(key, kind="photo", source="local", last_used="", used_count=0, path=None):
    c = {"source": source, "kind": kind, "key": key, "last_used": last_used,
         "used_count": used_count, "name": key}
    if source == "local":
        c["path"] = path or f"/lib/{key}"
    else:
        c["asset"] = {"id": key, "kind": kind, "title": key}
    return c


def _mat(cand):
    """An offline materializer: local files are 'on disk', Drive assets 'download'."""
    return {"path": cand.get("path") or f"/tmp/{cand['key']}", "hosted": None}


# ---- the split itself -------------------------------------------------------

def test_media_swap_is_free_and_repeatable_while_a_caption_recreate_costs_one(
        monkeypatch):
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    store = _Store([_row("p1"), _row("p2")])
    _wire(monkeypatch, store)

    before = ps.recreate_remaining("zanshin")
    for _ in range(5):                       # far more swaps than a month's budget
        status, body = ps.handle_swap_media("zanshin", "p1", "u1", sb_store=store,
                                            picker=_picker)
        assert status == 200 and body["free"] is True
    assert ps.recreate_remaining("zanshin") == before, "a photo swap must cost nothing"

    status, _ = ps.handle_deny("zanshin", "p2", "u1", sb_store=store)
    assert status == 200
    assert ps.recreate_remaining("zanshin") == before - 1, "a caption recreate costs 1"


def test_the_different_photo_chip_routes_to_the_free_swap(monkeypatch):
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    store = _Store([_row("p1")])
    _wire(monkeypatch, store)
    monkeypatch.setattr(msw, "pick_replacement", _picker)

    before = ps.recreate_remaining("zanshin")
    status, body = ps.handle_deny("zanshin", "p1", "u1", intent="media",
                                  sb_store=store)
    assert status == 200 and body["action"] == "swap-media"
    assert ps.recreate_remaining("zanshin") == before
    assert store.patches == [], "a photo swap must never flip the row to denied"
    assert store._rows["p1"]["image_url"] == "https://cdn/new.jpg"


def test_caption_intent_still_charges_exactly_as_before(monkeypatch):
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    store = _Store([_row("p1")])
    _wire(monkeypatch, store)
    before = ps.recreate_remaining("zanshin")
    status, _ = ps.handle_deny("zanshin", "p1", "u1", intent="caption",
                               sb_store=store)
    assert status == 200
    assert ps.recreate_remaining("zanshin") == before - 1


# ---- the flag ---------------------------------------------------------------

def test_flag_off_the_swap_endpoint_403s_and_never_reads_the_store(monkeypatch):
    store = _Store([_row("p1")])
    _wire(monkeypatch, store)
    status, body = ps.handle_swap_media("zanshin", "p1", "u1", sb_store=store,
                                        picker=_picker)
    assert status == 403 and store.swaps == []
    assert body["ok"] is False


def test_flag_off_the_media_intent_is_ignored_and_the_deny_charges(monkeypatch):
    store = _Store([_row("p1")])
    _wire(monkeypatch, store)
    before = ps.recreate_remaining("zanshin")
    status, _ = ps.handle_deny("zanshin", "p1", "u1", intent="media", sb_store=store)
    assert status == 200
    assert ps.recreate_remaining("zanshin") == before - 1, "flag off = today's behavior"


# ---- what a swap may never do ----------------------------------------------

def test_an_approved_post_keeps_the_pixels_the_gym_approved(monkeypatch):
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    store = _Store([_row("p1", status="approved")])
    _wire(monkeypatch, store)
    status, body = ps.handle_swap_media("zanshin", "p1", "u1", sb_store=store,
                                        picker=_picker)
    assert status == 409
    assert store._rows["p1"]["image_url"] == "https://cdn/old.jpg"


def test_a_cross_gym_post_id_is_a_404_and_no_swap_is_attempted(monkeypatch):
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    store = _Store([_row("p1", gym_id="pierce")])
    _wire(monkeypatch, store)
    status, _ = ps.handle_swap_media("zanshin", "p1", "u1", sb_store=store,
                                     picker=_picker)
    assert status == 404 and store.swaps == []


def test_no_fresh_photo_is_a_409_that_costs_nothing_and_says_what_to_do(monkeypatch):
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    store = _Store([_row("p1")])
    _wire(monkeypatch, store)
    before = ps.recreate_remaining("zanshin")
    status, body = ps.handle_swap_media(
        "zanshin", "p1", "u1", sb_store=store,
        picker=lambda *a, **k: {"ok": False, "reason": msw.REASON_NO_FRESH_PHOTO})
    assert status == 409
    assert ps.recreate_remaining("zanshin") == before
    assert "recreates were not touched" in body["error"]
    assert store._rows["p1"]["image_url"] == "https://cdn/old.jpg"


# ---- the picker itself ------------------------------------------------------

def test_pick_replacement_never_returns_the_photo_the_row_already_has(tmp_path):
    """The row's current photo AND every photo on the book are excluded from the
    local candidate list (media_guard state keys), so a swap can never hand back the
    same picture or trade one repeat for another."""
    from PIL import Image
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ("old.jpg", "booked.jpg", "new.jpg"):
        Image.new("RGB", (64, 64), "red").save(lib / name, quality=95)
        # PIL writes a small JPEG; pad past the 2 KB real-file floor
        with open(lib / name, "ab") as fh:
            fh.write(b"\0" * 4096)
    cands = msw.candidates_for(
        "zanshin", _row("p1"), store=_Store(), lib=str(lib),
        book_state={"booked.jpg": {("2026-09-22", "pending")}}, asset_state={},
        media_store=type("S", (), {"available": lambda self: False})())
    assert [c["key"] for c in cands] == ["new.jpg"]
    out = msw.pick_replacement(
        "zanshin", _row("p1"), store=_Store(), library_path=str(lib),
        candidates_fn=lambda g, r: cands, materialize_fn=_mat,
        host_fn=lambda p: "https://cdn/new.jpg",
        feed_fn=lambda p: "https://cdn/new__feed.jpg")
    assert out["ok"] is True and out["image_url"] == "https://cdn/new__feed.jpg"
    assert out["kind"] == "photo" and out["source_media_asset_id"] == ""


def test_pick_replacement_reports_no_library_instead_of_borrowing_another_gyms():
    out = msw.pick_replacement(
        "zanshin", _row("p1"), store=_Store(), library_path="",
        media_store=type("S", (), {"available": lambda self: False})())
    assert out == {"ok": False, "reason": msw.REASON_NO_LIBRARY}


def test_pick_replacement_refuses_when_hosting_is_down_rather_than_half_swapping():
    out = msw.pick_replacement(
        "zanshin", _row("p1"), store=_Store(), library_path="/lib",
        candidates_fn=lambda g, r: [_cand("new.jpg")], materialize_fn=_mat,
        host_fn=lambda p: "")
    assert out == {"ok": False, "reason": msw.REASON_HOSTING}


def test_a_story_swap_reburns_its_caption_and_never_ships_a_bare_photo(monkeypatch):
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "true")
    out = msw.pick_replacement(
        "zanshin", _row("p1", fmt="story"), store=_Store(), library_path="/lib",
        candidates_fn=lambda g, r: [_cand("new.jpg")], materialize_fn=_mat,
        host_fn=lambda p: "https://cdn/new.jpg",
        reburn_fn=lambda *a: "https://cdn/new__story.jpg")
    assert out["image_url"] == "https://cdn/new__story.jpg"
    assert out["source_media_url"] == "https://cdn/new.jpg"


def test_a_failed_story_reburn_changes_nothing(monkeypatch):
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    out = msw.pick_replacement(
        "zanshin", _row("p1", fmt="story"), store=_Store(), library_path="/lib",
        candidates_fn=lambda g, r: [_cand("new.jpg")], materialize_fn=_mat,
        host_fn=lambda p: "https://cdn/new.jpg", reburn_fn=lambda *a: None)
    assert out == {"ok": False, "reason": msw.REASON_STORY_REBURN}


# ---- the ordering the swap actually uses (Tough Temple, 2026-09-10) ---------------
# The old picker returned the FIRST ALPHABETICAL unused local image. "Edit image"
# handed John Weeks another equally unengaging still, in filename order, every time.

def test_candidates_are_least_recently_used_first_not_alphabetical():
    cands = [_cand("a_used_yesterday.jpg", last_used="2026-09-19"),
             _cand("m_never_used.jpg"),
             _cand("z_used_last_month.jpg", last_used="2026-08-10")]
    ordered = msw.order_candidates(cands, current_is_video=True)   # no video pref
    assert [c["key"] for c in ordered] == [
        "m_never_used.jpg", "z_used_last_month.jpg", "a_used_yesterday.jpg"]


def test_a_still_swaps_to_ready_footage_first_then_photos_then_unrenditioned_video():
    """Audit R-D1 #4: the swap runs inside a portal request, so the order is
    never-used video WITH a rendition (ready to serve) > never-used photo >
    never-used video WITHOUT a rendition (would need a transcode) > anything used
    (LRU). A gym with ready footage is handed footage, never a tenth still; a gym
    whose footage is all unrenditioned gets a fresh photo before it waits on ffmpeg."""
    ready = _cand("clipA.mp4", kind="video", source="drive")
    ready["asset"]["rendition_url"] = "https://cdn/clipA.mp4"
    raw = _cand("clipR.mov", kind="video", source="drive")            # no rendition yet
    used = _cand("clipB.mp4", kind="video", source="drive", last_used="2026-06-01")
    used["asset"]["rendition_url"] = "https://cdn/clipB.mp4"
    local_vid = _cand("gym.mp4", kind="video", source="local")         # served as-is
    cands = [_cand("a_photo.jpg"), used, raw, ready, local_vid]
    ordered = msw.order_candidates(cands, current_is_video=False)
    assert [c["key"] for c in ordered] == [
        "clipA.mp4", "gym.mp4", "a_photo.jpg", "clipR.mov", "clipB.mp4"]
    assert msw.has_rendition(ready) and msw.has_rendition(local_vid)
    assert not msw.has_rendition(raw) and not msw.has_rendition(_cand("a_photo.jpg"))


def test_no_videos_means_photos_still_swap():
    cands = [_cand("b.jpg", last_used="2026-09-01"), _cand("a.jpg")]
    assert [c["key"] for c in msw.order_candidates(cands, current_is_video=False)] \
        == ["a.jpg", "b.jpg"]


def test_local_candidates_skip_a_photo_served_inside_the_repeat_window(tmp_path,
                                                                        monkeypatch):
    """The served ledger is honored: a still served 10 days ago (inside the 30-day
    repeat window) is not a swap candidate even though it is not on the book."""
    from PIL import Image
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ("recent.jpg", "old.jpg"):
        Image.new("RGB", (64, 64), "blue").save(lib / name, quality=95)
        with open(lib / name, "ab") as fh:
            fh.write(b"\0" * 4096)
    monkeypatch.setattr(msw, "_last_served_local",
                        lambda base: {"recent.jpg": "2026-09-10", "old.jpg": "2026-07-01"})
    cands = msw.local_candidates("zanshin", str(lib), "2026-09-20", set())
    assert [c["key"] for c in cands] == ["old.jpg"]
    assert cands[0]["last_used"] == "2026-07-01"


def test_drive_candidates_come_from_the_pickable_pool_minus_the_book():
    from tests.gym_media_fakes import FakeMediaStore, make_asset
    store = FakeMediaStore(assets=[
        make_asset("v1", gym_id="zanshin", kind="video", title="squat.mov"),
        make_asset("v2", gym_id="zanshin", kind="video", title="row.mp4"),
        make_asset("p_hidden", gym_id="zanshin", kind="photo", excluded_by_coach=True),
        make_asset("p_unprobed", gym_id="zanshin", kind="photo", eligible=None)])
    cands = msw.drive_candidates("zanshin", {"v2"}, media_store=store)
    assert [c["key"] for c in cands] == ["v1"]
    assert cands[0]["kind"] == "video" and cands[0]["source"] == "drive"


def test_a_drive_video_swap_carries_the_video_row_shape(monkeypatch):
    """A swapped-in Drive video ships exactly like a Drive-built video row: the
    hosted video as image_url (publisher types it 'video' by extension), a poster as
    thumbnail_url, and the asset id so the hide/removed sweeps track it."""
    monkeypatch.delenv("AGENT_FEED_AUTOFIT", raising=False)
    cand = _cand("v1", kind="video", source="drive")
    out = msw.pick_replacement(
        "zanshin", _row("p1"), store=_Store(), library_path="",
        candidates_fn=lambda g, r: [cand],
        materialize_fn=lambda c: {"path": "/tmp/squat.mp4", "hosted": None},
        host_fn=lambda p: "https://cdn/zanshin/abc/squat.mp4",
        poster_fn=lambda path, work, tenant: "https://cdn/zanshin/abc/squat__poster.jpg")
    assert out["ok"] is True
    assert out["image_url"].endswith(".mp4")
    assert out["thumbnail_url"].endswith("__poster.jpg")
    assert out["source_media_asset_id"] == "v1" and out["kind"] == "video"
    fields = msw.swap_fields(out)
    assert fields == {"thumbnail_url": "https://cdn/zanshin/abc/squat__poster.jpg",
                      "source_media_asset_id": "v1"}


def test_a_video_story_swap_burns_the_caption_onto_a_story_video(monkeypatch):
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "true")
    monkeypatch.setattr(msw, "_reburn_story_video",
                        lambda base, row, path, lib: "https://cdn/squat__story.mp4")
    out = msw.pick_replacement(
        "zanshin", _row("p1", fmt="story"), store=_Store(), library_path="",
        candidates_fn=lambda g, r: [_cand("v1", kind="video", source="drive")],
        materialize_fn=lambda c: {"path": "/tmp/squat.mp4", "hosted": None},
        host_fn=lambda p: "https://cdn/squat.mp4",
        poster_fn=lambda *a: "https://cdn/squat__poster.jpg",
        reburn_fn=lambda *a: pytest.fail("the STILL re-burn must not run for a video"))
    assert out["image_url"] == "https://cdn/squat__story.mp4"
    assert out["source_media_url"] == "https://cdn/squat.mp4"
    assert out["thumbnail_url"] == ""      # the captioned story video IS the media


def test_swapping_back_to_a_still_clears_the_stale_poster_and_asset_id():
    pick = {"ok": True, "image_url": "https://cdn/new.jpg", "kind": "photo",
            "source": "local", "thumbnail_url": "", "source_media_asset_id": ""}
    assert msw.swap_fields(pick) == {"thumbnail_url": None, "source_media_asset_id": None}


def test_after_swap_stamps_the_new_drive_asset_and_returns_the_old_one(monkeypatch,
                                                                       tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    from datetime import datetime, timezone
    from agent import gym_media_selector as sel
    from tests.gym_media_fakes import FakeMediaStore, make_asset
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    store = FakeMediaStore(assets=[make_asset("old_a", gym_id="zanshin"),
                                   make_asset("new_v", gym_id="zanshin", kind="video")])
    sel.stamp_use(store.get_asset("old_a"), "zanshin", "2026-09-20", store=store, now=now)
    assert store.assets["old_a"]["used_count"] == 1
    row = dict(_row("p1"), source_media_asset_id="old_a")
    pick = {"ok": True, "source": "drive", "source_media_asset_id": "new_v", "kind": "video"}
    # book UNKNOWN (the re-read failed -> None): the old asset is left stamped, the
    # swapped-in asset still cools down
    msw.after_swap("zanshin", row, pick, media_store=store, now=now, book_rows=None)
    assert store.assets["old_a"]["used_count"] == 1, "unknown book must never roll back"
    assert store.assets["new_v"]["used_count"] == 1, "the swapped-in asset cools down"
    # book KNOWN and empty: nothing else carries it -> back to the pool
    local_pick = {"ok": True, "source": "local", "source_media_asset_id": "", "path": ""}
    msw.after_swap("zanshin", row, local_pick, media_store=store, now=now, book_rows=[])
    assert store.assets["old_a"]["used_count"] == 0, "the replaced asset returns to the pool"


# ---- audit 3c: the FB mirror + paired story move WITH the clicked row -------------

def _sib_rows():
    base = dict(_row("p1"), source_media_asset_id="a1")
    fb = dict(base, id="p2", account="facebook")
    story = dict(base, id="p3", format="story", status="coach_review",
                 image_url="https://cdn/old__story.jpg", source_media_url="old.jpg",
                 caption="the story's own edited caption")
    other = dict(base, id="p4", image_url="https://cdn/other.jpg",
                 source_media_url="other.jpg", source_media_asset_id="a9")
    tomorrow = dict(base, id="p5", post_date="2026-09-21")
    return [base, fb, story, other, tomorrow]


def test_sibling_rows_match_the_same_post_by_asset_id_and_never_the_days_other_post():
    rows = _sib_rows()
    sibs = msw.sibling_rows(rows[0], rows)
    assert sorted(s["id"] for s in sibs) == ["p2", "p3"]
    # clicking the STORY finds both feed rows
    assert sorted(s["id"] for s in msw.sibling_rows(rows[2], rows)) == ["p1", "p2"]


def test_sibling_rows_match_local_library_rows_by_media_key():
    rows = [dict(r, source_media_asset_id="") for r in _sib_rows()]
    assert sorted(s["id"] for s in msw.sibling_rows(rows[0], rows)) == ["p2", "p3"]


def test_pick_replacement_shapes_a_variant_for_every_sibling(monkeypatch):
    """One materialized file, three shapes: the clicked IG feed and its FB mirror get
    the video + poster; the story sibling is re-burned with ITS OWN caption (a client
    edit) and carries no poster (audit D4)."""
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "true")
    rows = _sib_rows()
    burned = []
    monkeypatch.setattr(msw, "_reburn_story_video",
                        lambda base, row, path, lib: burned.append(row["caption"])
                        or "https://cdn/squat__story.mp4")
    posters = []
    out = msw.pick_replacement(
        "zanshin", rows[0], store=_Store(), library_path="",
        candidates_fn=lambda g, r: [_cand("v1", kind="video", source="drive")],
        materialize_fn=lambda c: {"path": "/tmp/squat.mp4", "hosted": None},
        host_fn=lambda p: "https://cdn/squat.mp4",
        poster_fn=lambda *a: posters.append(1) or "https://cdn/squat__poster.jpg",
        siblings=[rows[1], rows[2]])
    assert out["ok"] and out["image_url"] == "https://cdn/squat.mp4"
    assert set(out["siblings"]) == {"p2", "p3"}
    fb, story = out["siblings"]["p2"], out["siblings"]["p3"]
    assert fb["image_url"] == "https://cdn/squat.mp4"
    assert fb["thumbnail_url"] == "https://cdn/squat__poster.jpg"
    assert story["image_url"] == "https://cdn/squat__story.mp4"
    assert story["source_media_url"] == "https://cdn/squat.mp4"
    assert story["thumbnail_url"] == ""
    assert burned == ["the story's own edited caption"]
    assert posters == [1], "ONE poster per swap, not one per variant"


def test_the_handler_swaps_pending_siblings_and_leaves_approved_ones(monkeypatch):
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    rows = _sib_rows()
    rows[1]["status"] = "pending"
    approved_fb = dict(rows[1], id="p6", status="approved")   # same post, gym approved it
    store = _Store(rows + [approved_fb])
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)
    settled = {}
    monkeypatch.setattr(msw, "after_swap",
                        lambda base, row, pick, **kw: settled.update(kw))

    def _picker(_gym, _row, siblings=(), **_kw):
        var = {"ok": True, "image_url": "https://cdn/squat.mp4", "source_media_url": None,
               "key": "v1", "kind": "video", "source": "drive",
               "thumbnail_url": "https://cdn/squat__poster.jpg",
               "source_media_asset_id": "v1", "path": "/tmp/squat.mp4"}
        return dict(var, siblings={str(s["id"]): dict(var) for s in siblings})

    status, body = ps.handle_swap_media("zanshin", "p1", "u1", sb_store=store,
                                        picker=_picker)
    assert status == 200
    assert sorted(body["siblings_swapped"]) == ["p2", "p3"]
    assert body["siblings_left"] == ["p6"]
    swapped_ids = [s[0] for s in store.swaps]
    assert sorted(swapped_ids) == ["p1", "p2", "p3"], "p4/p5/p6 must never be touched"
    for rid in ("p1", "p2", "p3"):
        assert store._rows[rid]["image_url"] == "https://cdn/squat.mp4"
        assert store._rows[rid]["source_media_asset_id"] == "v1"
    assert store._rows["p6"]["image_url"] == "https://cdn/old.jpg"
    # the ledger settle saw the post-swap book and the ids that moved
    assert sorted(settled["swapped_ids"]) == ["p1", "p2", "p3"]
    assert any(r["id"] == "p6" for r in settled["book_rows"])


def test_a_failed_sibling_variant_writes_nothing_and_answers_409(monkeypatch):
    """Audit 3c residual: all or nothing. The story sibling's re-burn fails -> the
    picker fails the WHOLE swap before any write; the clicked row keeps its media."""
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    rows = _sib_rows()
    rows[1]["status"] = "pending"
    out = msw.pick_replacement(
        "zanshin", rows[0], store=_Store(), library_path="",
        candidates_fn=lambda g, r: [_cand("new.jpg")], materialize_fn=_mat,
        host_fn=lambda p: "https://cdn/new.jpg",
        feed_fn=lambda p: "https://cdn/new__feed.jpg",
        reburn_fn=lambda *a: None,                  # the story re-burn fails
        siblings=[rows[1], rows[2]])
    assert out == {"ok": False, "reason": msw.REASON_STORY_REBURN, "failed_sibling": "p3"}

    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    store = _Store(rows)
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)
    monkeypatch.setattr(msw, "after_swap",
                        lambda *a, **k: pytest.fail("nothing to settle on a failed swap"))
    status, body = ps.handle_swap_media(
        "zanshin", "p1", "u1", sb_store=store,
        picker=lambda *a, **k: {"ok": False, "reason": msw.REASON_STORY_REBURN,
                                "failed_sibling": "p3"})
    assert status == 409 and body["reason"] == msw.REASON_STORY_REBURN
    assert body["failed_sibling"] == "p3" and "siblings_left" not in body
    assert store.swaps == [], "no row may move when one sibling cannot"


def test_a_picker_that_forgets_a_sibling_variant_is_refused_before_any_write(monkeypatch):
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    rows = _sib_rows()
    rows[1]["status"] = "pending"
    store = _Store(rows)
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)
    forgetful = lambda _g, _r, siblings=(), **_k: {   # noqa: E731
        "ok": True, "image_url": "https://cdn/new.jpg", "source_media_url": None,
        "key": "new.jpg", "kind": "photo", "source": "local", "thumbnail_url": "",
        "source_media_asset_id": "", "path": "", "siblings": {}}
    status, body = ps.handle_swap_media("zanshin", "p1", "u1", sb_store=store,
                                        picker=forgetful)
    assert status == 409 and body["failed_sibling"] in ("p2", "p3")
    assert store.swaps == []


def test_a_failed_book_reread_leaves_the_old_asset_stamped(monkeypatch):
    """Audit 3c residual: None (the re-read failed) is UNKNOWN, never 'nothing carries
    it'. The handler passes None through; after_swap must not roll the asset back."""
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    rows = _sib_rows()[:1]
    store = _Store(rows)
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)
    calls = {"n": 0}
    real_list = store.list_month

    def _flaky(account_key, month):
        calls["n"] += 1
        if calls["n"] >= 2:                 # the post-swap re-read fails
            raise RuntimeError("supabase hiccup")
        return real_list(account_key, month)
    store.list_month = _flaky
    seen = {}
    monkeypatch.setattr(msw, "after_swap", lambda base, row, pick, **kw: seen.update(kw))
    status, _ = ps.handle_swap_media("zanshin", "p1", "u1", sb_store=store, picker=_picker)
    assert status == 200
    assert seen["book_rows"] is None, "a failed re-read must reach after_swap as None"
    assert ps._month_rows_for(store, "zanshin", rows[0]) is None


def test_book_carries_asset_and_after_swap_keeps_the_old_asset_stamped(monkeypatch,
                                                                       tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    from datetime import datetime, timezone
    from agent import gym_media_selector as sel
    from tests.gym_media_fakes import FakeMediaStore, make_asset
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    store = FakeMediaStore(assets=[make_asset("a1", gym_id="zanshin"),
                                   make_asset("v1", gym_id="zanshin", kind="video")])
    sel.stamp_use(store.get_asset("a1"), "zanshin", "2026-09-20", store=store, now=now)
    rows = _sib_rows()[:4]                    # p5 (tomorrow) also carries a1 by design
    approved_fb = dict(rows[1], id="p6", status="approved")
    book = rows + [approved_fb]
    assert msw.book_carries_asset(book, "a1", except_ids=["p1", "p2", "p3"]) is True
    assert msw.book_carries_asset(rows, "a1", except_ids=["p1", "p2", "p3"]) is False
    # a DIFFERENT day's pending row carrying the same asset also keeps it stamped
    assert msw.book_carries_asset(_sib_rows(), "a1", except_ids=["p1", "p2", "p3"]) is True
    pick = {"ok": True, "source": "drive", "source_media_asset_id": "v1", "kind": "video"}
    # an approved sibling still carries a1: it stays stamped
    msw.after_swap("zanshin", rows[0], pick, media_store=store, now=now,
                   book_rows=book, swapped_ids=["p1", "p2", "p3"])
    assert store.assets["a1"]["used_count"] == 1
    assert store.assets["v1"]["used_count"] == 1
    # nothing left carries a1: it returns to the pool
    msw.after_swap("zanshin", rows[0], pick, media_store=store, now=now,
                   book_rows=rows, swapped_ids=["p1", "p2", "p3"])
    assert store.assets["a1"]["used_count"] == 0


def test_the_handler_writes_the_media_identity_columns_with_the_pixels(monkeypatch):
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    store = _Store([_row("p1")])
    _wire(monkeypatch, store)

    def _video_picker(_gym, _row, **_kw):
        return {"ok": True, "image_url": "https://cdn/squat.mp4", "source_media_url": None,
                "key": "v1", "kind": "video", "source": "drive",
                "thumbnail_url": "https://cdn/squat__poster.jpg",
                "source_media_asset_id": "v1", "path": "/tmp/squat.mp4"}

    status, body = ps.handle_swap_media("zanshin", "p1", "u1", sb_store=store,
                                        picker=_video_picker)
    assert status == 200 and body["free"] is True
    assert store.extras == {"thumbnail_url": "https://cdn/squat__poster.jpg",
                            "source_media_asset_id": "v1"}
    assert body["media_kind"] == "video"
    assert body["video_url"] == "https://cdn/squat.mp4"
    assert body["image_public_url"] == "https://cdn/squat__poster.jpg"


def test_client_messages_carry_no_dashes_and_never_say_vendor():
    for reason in (msw.REASON_NO_LIBRARY, msw.REASON_NO_FRESH_PHOTO,
                   msw.REASON_HOSTING, msw.REASON_STORY_REBURN):
        msg = msw.client_message(reason)
        assert "—" not in msg and "–" not in msg and "-" not in msg
        assert "vendor" not in msg.lower()


# ---- P-11: the exact contract the PORTAL relays against ---------------------
# The portal builds
#   `${base}/portal/${token}/posts/${postId}/${action}`
# in src/lib/echo/portal-content.ts postPostAction, and its ACTIONS set is
# {approve, edit, deny, kill, requeue}. "Photo swap is free" could not be built
# there because Echo had no swap action to relay to. This pins the path shape so
# the portal can add "swap-media" to that set and have it land.

def test_the_portal_relay_path_for_swap_media_is_routable():
    import re

    from agent import intake_web

    actions = set(intake_web.PORTAL_POST_ACTIONS)
    # Everything the portal can already relay, plus the new one.
    assert {"approve", "edit", "deny", "kill"} <= actions
    assert "swap-media" in actions, \
        "the portal relays /posts/<id>/swap-media; Echo must route it"

    # And the path the portal actually builds must match the live route regex.
    pattern = (r"^/portal/([A-Za-z0-9_.-]{8,})/posts/([A-Za-z0-9_-]+)/"
               r"(" + "|".join(intake_web.PORTAL_POST_ACTIONS) + r")$")
    m = re.match(pattern, "/portal/eyJhIjoiZW5nIn0.sig/posts/abc-123/swap-media")
    assert m and m.group(3) == "swap-media"
