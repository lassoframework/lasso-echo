"""John Weeks / Tough Temple, 2026-09-11 -- "9/14-9/16 are still repeat images".

PR #98 taught the MONTH BUILD to stage from a gym's connected Drive pool, so newly
built months stopped repeating stills. The nightly sweep, which is the ONLY thing that
can fix a row already sitting on the book, was never taught the same pool: its picker
(_fresh_photo) reads the LOCAL library, images only. Tough Temple's stills were all on
the book and its 57 eligible Drive clips were invisible to it, so every repeat already
on the calendar was reported as a "small library" and LEFT -- which is exactly what
John saw after being told the fix had shipped, and why the client-readable report asked
him to connect a Drive folder he had already connected.

These tests pin, fully offline:
  * the OLD behavior with the flag off (byte for byte: small library, repeat left)
  * a repeat re-pointed to a Drive clip when the flag is on, feed + FB mirror + story
    moving together off ONE materialized file
  * every rail the sweep already had: published / publishing untouched, an APPROVED
    row never swapped, nothing fabricated when BOTH pools are empty
  * one run never hands the same clip to two repeated dates
  * a dry run stays a dry run (no download, no transcode, no write)
  * the report stops telling a Drive-connected gym to add photos
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import media_guard  # noqa: E402
from agent.jobs import media_repeat_sweep as mrs  # noqa: E402

GYM = "toughtemple52040e"
REPEATED = "IMG_6771.jpg"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", raising=False)
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_WINDOW_DAYS", "30")
    lib = tmp_path / "lib"
    lib.mkdir()
    # ONE local still, and it is already the repeated photo: the local pool is
    # genuinely exhausted, which is what made the sweep give up.
    (lib / REPEATED).write_bytes(b"\xff\xd8\xff" + b"x" * 4096)
    monkeypatch.setattr(mrs, "_lib_dir", lambda base: str(lib))
    monkeypatch.setattr(mrs, "_is_real_image", lambda path: True)
    yield


def _row(rid, pd, account, fmt, status="pending", asset=None):
    return {"id": rid, "gym_id": GYM, "post_date": pd, "account": account,
            "format": fmt, "status": status, "caption": f"caption {rid}",
            "image_url": f"https://cdn.tt/{REPEATED}",
            "source_media_asset_id": asset}


def _book(status_0914="pending"):
    """The shape John reported: one still on 09-13 and again on 09-14, the 09-14 day
    carrying the full post (IG feed + FB mirror + paired story)."""
    return [
        _row("r13", "2026-09-13", "instagram", "feed", "approved"),
        _row("r14ig", "2026-09-14", "instagram", "feed", status_0914),
        _row("r14fb", "2026-09-14", "facebook", "feed", status_0914),
        _row("r14st", "2026-09-14", "instagram", "story", status_0914),
    ]


class _Store:
    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]
        self.swaps = []

    def rows_in_range(self, base, start, end):
        return [dict(r) for r in self.rows
                if start <= str(r.get("post_date") or "") <= end]

    def swap_media(self, base, row_id, image_url, source_media_url=None,
                   extra_fields=None):
        for r in self.rows:
            if str(r["id"]) != str(row_id):
                continue
            # The real store filters the PATCH to pending / coach_review.
            if str(r.get("status") or "").lower() not in ("pending", "coach_review"):
                return None
            r["image_url"] = image_url
            if source_media_url is not None:
                r["source_media_url"] = source_media_url
            for k, v in (extra_fields or {}).items():
                r[k] = v
            self.swaps.append((row_id, image_url))
            return dict(r)
        return None


def _clip(n):
    """What media_swap.pick_replacement returns for a Drive video."""
    return {"ok": True, "source": "drive", "kind": "video", "key": f"clip{n}.mp4",
            "image_url": f"https://cdn.tt/clip{n}.mp4",
            "source_media_url": f"https://cdn.tt/clip{n}.mp4",
            "thumbnail_url": f"https://cdn.tt/clip{n}__poster.jpg",
            "source_media_asset_id": f"v{n:03d}", "path": f"/tmp/clip{n}.mp4"}


def _picker(clips=(1,), seen=None):
    """A stand-in for media_swap.pick_replacement: hands out the next clip and shapes
    a variant per sibling off the SAME pick, recording the asset_state it was given."""
    pool = list(clips)

    def pick(base, row, *, store, library_path=None, book_state=None,
             asset_state=None, siblings=(), **kw):
        if seen is not None:
            seen.append(set(asset_state or {}))
        # Honor the block list the sweep hands us, like the real engine does.
        avail = [n for n in pool if f"v{n:03d}" not in set(asset_state or {})]
        if not avail:
            return {"ok": False, "reason": "no_fresh_photo"}
        got = _clip(avail[0])
        got["siblings"] = {str(s.get("id")): dict(got) for s in siblings}
        return got

    return pick


def _sweep(store, *, apply=True, picker=None, drive_n=0, monkeypatch=None):
    if picker is not None:
        monkeypatch.setattr("agent.media_swap.pick_replacement", picker)
    monkeypatch.setattr("agent.media_swap.after_swap",
                        lambda *a, **k: None)
    monkeypatch.setattr(mrs, "_drive_candidate_count",
                        lambda base, asset_state: drive_n)
    return mrs.sweep_gym(GYM, store, apply=apply, today=__import__(
        "datetime").date(2026, 9, 11))


# ---- the regression: the flag OFF is the old behavior, byte for byte ----------------
def test_flag_off_leaves_the_repeat_and_calls_it_a_small_library(monkeypatch):
    store = _Store(_book())
    res = _sweep(store, picker=_picker(), drive_n=57, monkeypatch=monkeypatch)
    assert res["small_library"] is True
    assert res["dates_fixed"] == 0
    assert store.swaps == [], "the flag is off; nothing may be written"
    assert any("no unused photo left" in d for d in res["detail"])


# ---- the fix -----------------------------------------------------------------------
def test_repeat_is_repointed_to_a_drive_clip_when_armed(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    store = _Store(_book())
    res = _sweep(store, picker=_picker(), drive_n=57, monkeypatch=monkeypatch)
    assert res["small_library"] is False, "57 unused clips is not a small library"
    assert res["dates_fixed"] == 1
    # feed + FB mirror + story all move off the SAME clip: one post, one piece of media
    assert res["rows_repointed"] == 3
    moved = {r["id"]: r["image_url"] for r in store.rows if r["id"].startswith("r14")}
    assert set(moved.values()) == {"https://cdn.tt/clip1.mp4"}
    assert all(r.get("source_media_asset_id") == "v001" for r in store.rows
               if r["id"].startswith("r14"))
    assert any("connected Drive pool" in d for d in res["detail"])


def test_the_owner_date_and_the_approved_row_are_never_touched(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    store = _Store(_book())
    _sweep(store, picker=_picker(), drive_n=57, monkeypatch=monkeypatch)
    owner = [r for r in store.rows if r["id"] == "r13"][0]
    assert owner["image_url"] == f"https://cdn.tt/{REPEATED}"
    assert owner["status"] == "approved"


def test_an_approved_duplicate_is_still_reported_never_swapped(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    store = _Store(_book(status_0914="approved"))
    res = _sweep(store, picker=_picker(), drive_n=57, monkeypatch=monkeypatch)
    assert res["approved_left"] == 1
    assert store.swaps == [], "the gym approved that exact card"
    assert any("APPROVED duplicate" in d for d in res["detail"])


def test_a_live_row_is_never_swapped_even_with_a_full_drive_pool(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    store = _Store(_book(status_0914="published"))
    res = _sweep(store, picker=_picker(), drive_n=57, monkeypatch=monkeypatch)
    assert store.swaps == []
    assert any("LIVE row also carries it" in d for d in res["detail"])


def test_both_pools_empty_is_still_a_small_library_never_fabricated(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    store = _Store(_book())
    res = _sweep(store, picker=_picker(clips=()), drive_n=0, monkeypatch=monkeypatch)
    assert res["small_library"] is True
    assert store.swaps == []
    assert res["drive_pool"] == 0


def test_one_run_never_hands_the_same_clip_to_two_dates(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    rows = _book() + [_row("r15ig", "2026-09-15", "instagram", "feed")]
    store = _Store(rows)
    seen = []
    res = _sweep(store, picker=_picker(clips=(1, 2), seen=seen),
                 drive_n=57, monkeypatch=monkeypatch)
    assert res["dates_fixed"] == 2
    got = {r["source_media_asset_id"] for r in store.rows
           if r["id"].startswith(("r14", "r15"))}
    assert got == {"v001", "v002"}, "the second date must not reuse the first clip"
    # the second pick was told the first clip is now on the book
    assert "v001" in seen[1]


def test_an_asset_already_on_the_book_is_blocked_from_the_start(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    rows = _book()
    rows.append(_row("r20", "2026-09-20", "instagram", "feed", asset="v001"))
    store = _Store(rows)
    seen = []
    _sweep(store, picker=_picker(clips=(1, 2), seen=seen), drive_n=57,
           monkeypatch=monkeypatch)
    assert "v001" in seen[0], "a clip already on 09-20 may not be offered for 09-14"
    assert [r for r in store.rows if r["id"] == "r14ig"][0][
        "source_media_asset_id"] == "v002"


def test_a_sibling_the_picker_cannot_shape_leaves_the_date_alone(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    store = _Store(_book())

    def half_shaped(base, row, *, store, siblings=(), **kw):
        got = _clip(1)
        got["siblings"] = {str(s.get("id")): {"ok": False} for s in siblings}
        return got

    res = _sweep(store, picker=half_shaped, drive_n=57, monkeypatch=monkeypatch)
    assert store.swaps == [], "all or nothing: never half a post"
    assert res["small_library"] is True


def test_a_picker_that_raises_never_breaks_the_sweep(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    store = _Store(_book())

    def boom(*a, **k):
        raise RuntimeError("drive down")

    res = _sweep(store, picker=boom, drive_n=57, monkeypatch=monkeypatch)
    assert res["small_library"] is True
    assert store.swaps == []


def test_a_dry_run_never_downloads_hosts_or_writes(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    store = _Store(_book())

    def never(*a, **k):
        raise AssertionError("a dry run must not materialize media")

    res = _sweep(store, apply=False, picker=never, drive_n=57, monkeypatch=monkeypatch)
    assert store.swaps == []
    assert res["dates_fixed"] == 1
    assert any("[dry-run]" in d and "Drive pool" in d for d in res["detail"])


# ---- the report stops asking for photos the gym already gave us ---------------------
def test_report_names_the_unreached_drive_pool_instead_of_asking_for_photos():
    res = {"gym": GYM, "photos_repeated": 3, "approved_left": 0, "small_library": True,
           "drive_pool": 57,
           "detail": [f"{REPEATED} 2026-09-14: no unused photo left "
                      "(small library; left with spacing)"]}
    text = mrs.unfixable_report(res)
    assert "57 unused item(s)" in text
    assert "connect the" not in text, "he already connected it"
    assert "Nothing more is needed from the gym." in text


def test_report_still_asks_for_photos_when_there_is_genuinely_no_pool():
    res = {"gym": "somegym", "photos_repeated": 3, "approved_left": 0,
           "small_library": True, "drive_pool": 0,
           "detail": ["a.jpg 2026-09-14: no unused photo left "
                      "(small library; left with spacing)"]}
    text = mrs.unfixable_report(res)
    assert "connect the gym's Drive folder or upload in the portal" in text


def test_asset_state_maps_every_drive_row_by_id_and_date():
    rows = [_row("a", "2026-09-14", "instagram", "feed", asset="v001"),
            _row("b", "2026-09-14", "facebook", "feed", asset="v001"),
            _row("c", "2026-09-20", "instagram", "feed", asset="v002"),
            _row("d", "2026-09-21", "instagram", "feed")]
    state = mrs._asset_state(rows)
    assert state == {"v001": {("2026-09-14", "x")}, "v002": {("2026-09-20", "x")}}
    assert media_guard.row_asset_key(rows[3]) == ""


def test_feed_first_shapes_the_pick_for_the_feed_row():
    rows = [{"id": "s", "format": "story"}, {"id": "f", "format": "feed"}]
    assert [r["id"] for r in mrs._feed_first(rows)] == ["f", "s"]


# ---- END TO END through the REAL swap engine ---------------------------------------
# The stubs above pin the sweep's own logic. This one proves the WIRING: the sweep
# calls media_swap.pick_replacement with the arguments it actually takes and reads the
# shape it actually returns. Without it, a signature drift would be swallowed by
# _swap_from_drive_pool's except and look exactly like "small library" -- the very
# silence this whole fix exists to end.
def test_end_to_end_a_real_drive_clip_replaces_the_repeat(monkeypatch, tmp_path):
    from tests.gym_media_fakes import FakeDrive, FakeMediaStore, make_asset

    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    for k in ("AGENT_STORY_FORMAT", "AGENT_FEED_AUTOFIT", "AGENT_STORY_SOURCE_MEDIA"):
        monkeypatch.delenv(k, raising=False)

    media_store = FakeMediaStore(assets=[
        make_asset(f"v{i:03d}", gym_id=GYM, kind="video", title=f"clip{i:03d}.mp4")
        for i in range(57)])
    monkeypatch.setattr("agent.gym_media_index.default_store", lambda: media_store)
    monkeypatch.setattr("agent.integrations.drive_client.DriveClient",
                        lambda *a, **k: FakeDrive())
    monkeypatch.setattr("agent.media_swap._probe",
                        lambda path, timeout: {"duration_sec": 20.0, "width": 1080,
                                               "height": 1920, "codec": "h264"})
    monkeypatch.setattr("agent.media_host.host_media",
                        lambda path, tenant: f"https://cdn.tt/{os.path.basename(path)}")
    monkeypatch.setattr("agent.gym_media_builder.video_poster_url",
                        lambda path, work, tenant: "https://cdn.tt/poster.jpg")
    monkeypatch.setattr(mrs, "_drive_candidate_count", lambda base, st: 57)

    import datetime
    store = _Store(_book())
    res = mrs.sweep_gym(GYM, store, apply=True, today=datetime.date(2026, 9, 11))

    assert res["small_library"] is False
    assert res["dates_fixed"] == 1
    assert res["rows_repointed"] == 3
    moved = [r for r in store.rows if r["id"].startswith("r14")]
    assert all(r["image_url"].endswith(".mp4") for r in moved), \
        "every row on the repeated day now carries the Drive clip"
    assert len({r["image_url"] for r in moved}) == 1, "one post, one piece of media"
    aid = moved[0]["source_media_asset_id"]
    assert aid and aid.startswith("v")
    # the FEED rows carry the poster frame; the story's media IS its own card
    assert {r["thumbnail_url"] for r in moved if r["format"] == "feed"} == {
        "https://cdn.tt/poster.jpg"}
    # after_swap settled the ledger: the clip is stamped, so tomorrow's sweep and the
    # next month build both see it as used and cannot hand it out again.
    assert media_store.get_asset(aid).get("last_used_at"), "the clip must cool down"


def test_end_to_end_an_empty_drive_pool_is_still_a_small_library(monkeypatch):
    from tests.gym_media_fakes import FakeDrive, FakeMediaStore

    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr("agent.gym_media_index.default_store",
                        lambda: FakeMediaStore(assets=[]))
    monkeypatch.setattr("agent.integrations.drive_client.DriveClient",
                        lambda *a, **k: FakeDrive())

    import datetime
    store = _Store(_book())
    res = mrs.sweep_gym(GYM, store, apply=True, today=datetime.date(2026, 9, 11))
    assert res["small_library"] is True
    assert store.swaps == []
    assert res["drive_pool"] == 0


# ---- the two counters the ops table reads ------------------------------------------
def test_a_local_video_pick_is_not_reported_as_the_drive_pool(monkeypatch):
    """pick_replacement draws from BOTH pools, and a local VIDEO is a legitimate answer
    here (_fresh_photo only ever looked at images). The detail line must say so."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    store = _Store(_book())

    def local_pick(base, row, *, store, siblings=(), **kw):
        got = {"ok": True, "source": "local", "kind": "video", "key": "reel_02.mp4",
               "image_url": "https://cdn.tt/reel_02.mp4", "source_media_url": None,
               "thumbnail_url": "https://cdn.tt/p.jpg", "source_media_asset_id": "",
               "path": "/tmp/reel_02.mp4"}
        got["siblings"] = {str(s.get("id")): dict(got) for s in siblings}
        return got

    res = _sweep(store, picker=local_pick, drive_n=0, monkeypatch=monkeypatch)
    assert res["dates_fixed"] == 1
    assert any("from the local library" in d for d in res["detail"])
    assert not any("connected Drive pool" in d for d in res["detail"])


def test_a_reburned_story_is_counted_like_the_local_path_counts_it(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    store = _Store(_book())
    res = _sweep(store, picker=_picker(), drive_n=57, monkeypatch=monkeypatch)
    assert res["rows_repointed"] == 3
    assert res["stories_reburned"] == 1, "the one story row was re-burned"


# =====================================================================================
# INDEPENDENT AUDIT, 2026-09-11. Round 1 of this fix graded D. Every CRITICAL and MAJOR
# below is pinned here, plus the four claims whose code could be deleted with the whole
# media suite still green.
# =====================================================================================

# ---- B1 CRITICAL: never swap a repeat onto its own near duplicate -------------------
def test_a_near_dupe_of_a_booked_photo_is_never_offered_as_the_replacement(
        monkeypatch, tmp_path):
    """_fresh_photo refuses a near-dupe (IMG_6771.JPG next to IMG_6771.jpg -- the same
    photo uploaded twice, which _cluster_key collapses). The fallback picker blocks only
    by EXACT basename and the SERVED ledger, so a cluster sibling on a PENDING row was
    blocked by neither and came straight back. Swapping John's repeat for a visually
    identical frame and reporting the date fixed IS his complaint, made silent."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    lib = tmp_path / "dupes"
    lib.mkdir()
    (lib / REPEATED).write_bytes(b"\xff\xd8\xff" + b"x" * 4096)
    (lib / "IMG_6771.JPG").write_bytes(b"\xff\xd8\xff" + b"x" * 4096)
    monkeypatch.setattr(mrs, "_lib_dir", lambda base: str(lib))

    # the local pool really is "exhausted" by _fresh_photo's rules
    assert mrs._fresh_photo(str(lib), {REPEATED: {("2026-09-13", "x")}},
                            exclude={REPEATED}) == (None, None)

    seen = []

    def pick(base, row, *, store, book_state=None, siblings=(), **kw):
        seen.append(set(book_state or {}))
        return {"ok": False, "reason": "no_fresh_photo"}

    store = _Store(_book())
    res = _sweep(store, picker=pick, drive_n=0, monkeypatch=monkeypatch)
    assert seen, "the picker was never consulted"
    assert "IMG_6771.JPG" in seen[0], \
        "the near-dupe of the repeated photo must be blocked before the picker sees it"
    assert REPEATED in seen[0]
    assert store.swaps == []
    assert res["small_library"] is True


def test_the_near_dupe_widening_leaves_unrelated_photos_available(monkeypatch, tmp_path):
    """_blocked_book_state must widen to CLUSTER SIBLINGS only, never to the library."""
    lib = tmp_path / "mixed"
    lib.mkdir()
    for name in (REPEATED, "IMG_6771.JPG", "totally_other.jpg", "another_one.jpg"):
        (lib / name).write_bytes(b"\xff\xd8\xff" + b"z" * 4096)
    monkeypatch.setattr(mrs, "_lib_dir", lambda base: str(lib))
    blocked = mrs._blocked_book_state(GYM, {REPEATED: {("2026-09-13", "x")}}, REPEATED)
    assert "IMG_6771.JPG" in blocked, "the case-variant dupe must be blocked"
    assert "totally_other.jpg" not in blocked
    assert "another_one.jpg" not in blocked


def test_the_near_dupe_widening_never_raises_on_a_missing_library(monkeypatch):
    monkeypatch.setattr(mrs, "_lib_dir", lambda base: "/nope/not/here")
    state = {REPEATED: {("2026-09-13", "x")}}
    assert mrs._blocked_book_state(GYM, state, REPEATED) == state


# ---- B2 CRITICAL: the dry run must predict what apply will really do ----------------
def test_a_dry_run_spends_the_pool_down_instead_of_promising_it_to_every_date(
        monkeypatch):
    """Three repeated dates, ONE asset in the pool. The dry run used to claim all three
    were fixable; apply fixes one. The dry run is the instrument we verify a client's
    gym with, so it has to match."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    rows = _book() + [_row("r15", "2026-09-15", "instagram", "feed"),
                      _row("r16", "2026-09-16", "instagram", "feed")]
    res = _sweep(_Store(rows), apply=False, picker=None, drive_n=1,
                 monkeypatch=monkeypatch)
    assert res["dates_fixed"] == 1, \
        f"a one-asset pool cannot fix {res['dates_fixed']} dates"
    assert res["small_library"] is True, "the dates it cannot reach must still report"


def test_a_dry_run_with_a_deep_pool_reports_every_date(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    rows = _book() + [_row("r15", "2026-09-15", "instagram", "feed"),
                      _row("r16", "2026-09-16", "instagram", "feed")]
    res = _sweep(_Store(rows), apply=False, picker=None, drive_n=57,
                 monkeypatch=monkeypatch)
    assert res["dates_fixed"] == 3
    assert res["small_library"] is False


# ---- C1 MAJOR: all-or-nothing at WRITE time, not just at pick time ------------------
def test_a_failed_sibling_write_rolls_the_whole_post_back(monkeypatch):
    """The picker's all-or-nothing only covers SHAPING. Each swap_media is a separate
    network write; one failure used to leave the IG feed and FB mirror on the new clip
    and the paired story still on the repeat -- a mixed post, counted as fixed."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    store = _Store(_book())
    real_swap = store.swap_media

    def flaky(base, row_id, image_url, source_media_url=None, extra_fields=None):
        if str(row_id) == "r14st":
            raise RuntimeError("supabase 500")
        return real_swap(base, row_id, image_url,
                         source_media_url=source_media_url, extra_fields=extra_fields)

    store.swap_media = flaky
    res = _sweep(store, picker=_picker(), drive_n=57, monkeypatch=monkeypatch)
    carried = {r["id"]: r["image_url"] for r in store.rows if r["id"].startswith("r14")}
    assert len(set(carried.values())) == 1, f"post shipped mixed media: {carried}"
    assert all(u.endswith(REPEATED) for u in carried.values()), \
        "every row must be back on what it carried before"
    assert res["dates_fixed"] == 0
    assert res["rows_repointed"] == 0


def test_a_row_that_went_live_mid_write_rolls_the_post_back(monkeypatch):
    """swap_media returning None (status-guarded server-side: the row was approved or
    published between the read and the write) is the same hazard as a raise."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    store = _Store(_book())
    real_swap = store.swap_media

    def raced(base, row_id, image_url, source_media_url=None, extra_fields=None):
        if str(row_id) == "r14fb":
            return None
        return real_swap(base, row_id, image_url,
                         source_media_url=source_media_url, extra_fields=extra_fields)

    store.swap_media = raced
    res = _sweep(store, picker=_picker(), drive_n=57, monkeypatch=monkeypatch)
    carried = {r["id"]: r["image_url"] for r in store.rows if r["id"].startswith("r14")}
    assert all(u.endswith(REPEATED) for u in carried.values()), carried
    assert res["rows_repointed"] == 0


# ---- C2 MAJOR: flag OFF is the old behavior, byte for byte --------------------------
def test_flag_off_never_reads_the_drive_pool_at_all(monkeypatch):
    """drive_pool was measured on EVERY small-library gym regardless of the flag: a new
    Supabase read per gym per night, and new client-readable copy, on the default path.
    CLAUDE.md: a new capability ships behind a flag that defaults OFF."""
    monkeypatch.delenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", raising=False)
    calls = []
    monkeypatch.setattr("agent.gym_media_selector.pickable",
                        lambda *a, **k: calls.append(a) or [])
    monkeypatch.setattr("agent.media_swap.after_swap", lambda *a, **k: None)
    import datetime
    res = mrs.sweep_gym(GYM, _Store(_book()), apply=True,
                        today=datetime.date(2026, 9, 11))
    assert calls == [], f"the Drive pool was read with the flag off: {calls}"
    assert res["drive_pool"] == 0
    assert res["drive_armed"] is False
    assert res["small_library"] is True


# ---- C3 MAJOR: the report must not say "cannot reach" a pool it just reached --------
def test_report_says_retrying_when_the_lane_is_armed_and_still_failed():
    res = {"gym": GYM, "photos_repeated": 3, "approved_left": 0, "small_library": True,
           "drive_pool": 57, "drive_armed": True,
           "detail": [f"{REPEATED} 2026-09-14: no unused photo left "
                      "(small library; left with spacing)"]}
    text = mrs.unfixable_report(res)
    assert "could not prepare one" in text
    assert "cannot reach yet" not in text, "it reached the pool; it failed to use it"
    assert "Nothing more is needed from the gym." in text


def test_report_says_cannot_reach_when_the_lane_is_unarmed():
    res = {"gym": GYM, "photos_repeated": 3, "approved_left": 0, "small_library": True,
           "drive_pool": 57, "drive_armed": False,
           "detail": [f"{REPEATED} 2026-09-14: no unused photo left "
                      "(small library; left with spacing)"]}
    assert "cannot reach yet" in mrs.unfixable_report(res)


# ---- C4 MAJOR: one gym never takes the night ---------------------------------------
def test_a_picker_returning_a_non_dict_does_not_raise(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    store = _Store(_book())
    res = _sweep(store, picker=lambda *a, **k: None, drive_n=57,
                 monkeypatch=monkeypatch)
    assert res["small_library"] is True
    assert store.swaps == []


def test_one_gym_raising_never_skips_the_rest(monkeypatch):
    seen = []

    def boom(base, store, **kw):
        seen.append(base)
        if base == "gymA":
            raise RuntimeError("read blew up")
        return {"gym": base, "photos_repeated": 0, "dates_fixed": 0,
                "rows_repointed": 0, "stories_reburned": 0, "approved_left": 0,
                "small_library": False, "drive_pool": 0, "detail": []}

    monkeypatch.setattr(mrs, "sweep_gym", boom)
    monkeypatch.setattr(mrs, "SupabaseCalendarStore", lambda *a, **k: object())
    out = mrs.run(["gymA", "gymB", "gymC"], apply=False)
    assert seen == ["gymA", "gymB", "gymC"], "a raising gym stopped the night"
    assert out[0]["error"] == "RuntimeError"
    assert [r["gym"] for r in out] == ["gymA", "gymB", "gymC"]


# ---- C5 MAJOR: the four claims whose code could be deleted with the suite green -----
def test_the_report_pool_number_is_actually_measured(monkeypatch):
    """Mutant: delete result["drive_pool"] = _drive_candidate_count(...). Nothing
    noticed that the client-readable report stopped knowing the pool size."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    monkeypatch.setattr("agent.media_swap.after_swap", lambda *a, **k: None)
    monkeypatch.setattr(mrs, "_drive_candidate_count", lambda base, st: 57)
    import datetime
    # a picker that never succeeds -> small library, but the pool IS there
    monkeypatch.setattr("agent.media_swap.pick_replacement",
                        lambda *a, **k: {"ok": False, "reason": "hosting_unavailable"})
    res = mrs.sweep_gym(GYM, _Store(_book()), apply=True,
                        today=datetime.date(2026, 9, 11))
    assert res["small_library"] is True
    assert res["drive_pool"] == 57, "the report would have no number to tell the truth with"
    assert res["drive_armed"] is True


def test_a_local_pick_is_recorded_so_one_run_never_places_it_twice(monkeypatch):
    """Mutant: delete the local-pick state.setdefault(...). Nothing noticed that one run
    could hand the SAME LOCAL file to two repeated dates (the Drive-asset twin of this
    was covered; the local one was not)."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    rows = _book() + [_row("r15", "2026-09-15", "instagram", "feed")]
    seen = []

    def local_pick(base, row, *, store, book_state=None, siblings=(), **kw):
        seen.append(set(book_state or {}))
        got = {"ok": True, "source": "local", "kind": "video", "key": "reel_02.mp4",
               "image_url": "https://cdn.tt/reel_02.mp4", "source_media_url": None,
               "thumbnail_url": "", "source_media_asset_id": "",
               "path": "/tmp/reel_02.mp4"}
        got["siblings"] = {str(s.get("id")): dict(got) for s in siblings}
        return got

    _sweep(_Store(rows), picker=local_pick, drive_n=0, monkeypatch=monkeypatch)
    assert len(seen) == 2, "expected a pick for each repeated date"
    assert "reel_02.mp4" in seen[1], \
        "the second date must be told the first date already took reel_02.mp4"


def test_after_swap_is_told_which_rows_this_swap_repointed(monkeypatch):
    """Mutant: drop swapped_ids= from after_swap. Nothing noticed. Without it the
    pre-swap book read still shows the OLD asset on the rows we just moved, so
    book_carries_asset reports it still carried and the old clip is never released."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    got = {}
    monkeypatch.setattr("agent.media_swap.after_swap",
                        lambda base, row, pick, **kw: got.update(kw))
    monkeypatch.setattr("agent.media_swap.pick_replacement", _picker())
    monkeypatch.setattr(mrs, "_drive_candidate_count", lambda base, st: 57)
    import datetime
    mrs.sweep_gym(GYM, _Store(_book()), apply=True, today=datetime.date(2026, 9, 11))
    assert set(got.get("swapped_ids") or ()) == {"r14ig", "r14fb", "r14st"}
    assert got.get("book_rows") is not None, "None means UNKNOWN: never roll back"


def test_the_picker_is_told_what_is_already_on_the_book(monkeypatch):
    """Mutant: pass book_state=None. Nothing noticed. The picker would then re-read a
    book it does not have and could hand back a photo already sitting on another day --
    trading one repeat for another."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    seen = []

    def pick(base, row, *, store, book_state=None, siblings=(), **kw):
        seen.append(book_state)
        return {"ok": False, "reason": "no_fresh_photo"}

    _sweep(_Store(_book()), picker=pick, drive_n=0, monkeypatch=monkeypatch)
    assert seen and seen[0] is not None, "the picker was handed no book state"
    assert REPEATED in seen[0], "the repeated photo must be blocked"


# =====================================================================================
# INDEPENDENT AUDIT ROUND 2 (2026-09-11). Round 2 graded the round-1 fix a D: B1 was
# still open by its own worked example, and four new defects were found.
# =====================================================================================

# ---- B1 / C2: the copy-suffix family, SIBLING GATED ------------------------------
_LIB_WITH_ORIGINAL = [REPEATED, "IMG_6771 (1).jpg", "IMG_6771-copy.jpg",
                      "IMG_6771 copy.jpg", "IMG_6771(2).png", "IMG_6771 - Copy.jpg",
                      "img_6771.JPG", "IMG_6771_1.jpg", "IMG_6771-2.jpg",
                      "Copy of IMG_6771.jpg"]


@pytest.mark.parametrize("dupe", [n for n in _LIB_WITH_ORIGINAL if n != REPEATED])
def test_a_copy_collapses_when_the_original_is_in_the_library(dupe):
    """dam.rotation_key only clusters a MARKED library (mark_near_dupes runs once, at
    onboarding; nothing re-marks after a portal upload or a Drive sync), so in production
    _cluster_key falls back to the stem. Case-folding alone missed every one of these --
    including 'IMG_6771 (1).jpg', the exact example the guard was written for. This is
    the DEFAULT lane too: _fresh_photo shares _cluster_key."""
    assert (mrs._cluster_key("/nolib", dupe, _LIB_WITH_ORIGINAL)
            == mrs._cluster_key("/nolib", REPEATED, _LIB_WITH_ORIGINAL))


@pytest.mark.parametrize("lib", [
    [f"IMG ({i}).jpg" for i in range(1, 9)],          # bulk phone / Drive download
    ["team copy.jpg", "team copy 2.jpg", "team copy 3.jpg"],
    [f"photo_{i:02d}.jpg" for i in range(1, 6)],
    ["1.jpg", "2.jpg", "3.jpg"],
])
def test_a_numbered_family_with_no_original_stays_distinct(lib):
    """THE STARVATION CASE (audit round 3, CRITICAL). Round 2 stripped copy markers
    unconditionally, so a library of 'IMG (1).jpg'..'IMG (8).jpg' -- the standard bulk
    download naming, which client_media_sync keeps verbatim -- collapsed to ONE cluster.
    _fresh_photo then returned None for a gym with eight usable stills and the sweep
    reported 'small library'. Over-collapsing is the worse failure: it causes MORE
    repeats, and it was live on the default path."""
    keys = {mrs._cluster_key("/nolib", n, lib) for n in lib}
    assert len(keys) == len(lib), f"collapsed {lib} into {keys}"


def test_no_library_context_never_collapses():
    """The safe direction: with no library to check a sibling against, do not guess."""
    assert (mrs._cluster_key("/nolib", "IMG_6771 (1).jpg")
            != mrs._cluster_key("/nolib", REPEATED))


def test_a_numbered_library_with_no_original_is_not_starved(tmp_path):
    """End to end through _fresh_photo: eight usable stills must stay eight."""
    lib = tmp_path / "bulk"
    lib.mkdir()
    for i in range(1, 9):
        (lib / f"IMG ({i}).jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 4096)
    state = {"IMG (1).jpg": {("2026-09-13", "x")}}
    got, _path = mrs._fresh_photo(str(lib), state, exclude={"IMG (1).jpg"})
    assert got == "IMG (2).jpg", f"library starved: _fresh_photo returned {got!r}"


def test_the_default_lane_never_swaps_a_repeat_for_its_own_copy(monkeypatch, tmp_path):
    """Flag OFF, the production default. _fresh_photo must refuse IMG_6771 (1).jpg."""
    monkeypatch.delenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", raising=False)
    lib = tmp_path / "copies"
    lib.mkdir()
    for n in (REPEATED, "IMG_6771 (1).jpg"):
        (lib / n).write_bytes(b"\xff\xd8\xff" + b"x" * 4096)
    monkeypatch.setattr(mrs, "_lib_dir", lambda base: str(lib))
    monkeypatch.setattr("agent.media_swap.after_swap", lambda *a, **k: None)
    import datetime
    store = _Store(_book())
    res = mrs.sweep_gym(GYM, store, apply=True, today=datetime.date(2026, 9, 11))
    assert store.swaps == [], "swapped a repeat for a byte-identical copy of itself"
    assert res["small_library"] is True


# ---- C2 again: the flag-OFF path must not grow a book read -------------------------
def test_flag_off_makes_no_extra_calendar_read(monkeypatch):
    """Round 1's _asset_state(rows, base, store) called media_guard.book_state
    unconditionally -- an extra list_month per gym per night on the DEFAULT path. (And
    it read backwards from today, so it never did what its docstring claimed.)"""
    monkeypatch.delenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", raising=False)

    class _Counting(_Store):
        def __init__(self, rows):
            super().__init__(rows)
            self.months = []

        def list_month(self, base, month):
            self.months.append(month)
            return [dict(r) for r in self.rows
                    if str(r.get("post_date", "")).startswith(month)]

    monkeypatch.setattr("agent.media_swap.after_swap", lambda *a, **k: None)
    import datetime
    store = _Counting(_book())
    mrs.sweep_gym(GYM, store, apply=True, today=datetime.date(2026, 9, 11))
    assert store.months == [], f"flag OFF made extra calendar read(s): {store.months}"


# ---- _restore_rows: a refused rollback is never silent ------------------------------
def test_a_rollback_the_status_guard_refuses_is_reported(monkeypatch, capsys):
    """swap_media returning None IS the likely case here (the row went live, which is
    what triggered the rollback). Swallowing it let the caller log 'rolling back N rows
    so the post is never half swapped' without keeping that promise."""
    store = _Store(_book())
    store.swap_media = lambda *a, **k: None
    stuck = mrs._restore_rows(GYM, store, [("r14ig", {"image_url": "u"})])
    assert stuck == ["r14ig"]
    assert "ROLLBACK REFUSED" in capsys.readouterr().out


def test_a_rollback_clears_a_source_media_url_the_forward_swap_set(monkeypatch):
    """source_media_url=None does NOT clear the column (portal_calendar_store only
    writes it when not None). A row whose original had none kept the NEW clip's
    source_media_url on top of the OLD image_url -- and media_guard.row_media_key reads
    source_media_url FIRST, so the repeat would go invisible to every future sweep while
    the gym still saw it."""
    store = _Store([_row("r1", "2026-09-14", "instagram", "feed")])
    store.rows[0]["source_media_url"] = "https://cdn.tt/clip1.mp4"   # set by the swap
    mrs._restore_rows(GYM, store, [("r1", {"image_url": f"https://cdn.tt/{REPEATED}",
                                           "source_media_url": None,
                                           "extra_fields": {}})])
    row = store.rows[0]
    assert row["source_media_url"] == "", "the stale source_media_url masks the repeat"
    assert media_guard.row_media_key(row).endswith(REPEATED)


# ---- C3 again: a DRAINED pool must not read as an empty one ------------------------
def test_a_run_that_drains_the_pool_still_reports_the_folder_as_full():
    """drive_pool is what is LEFT; a run that used the last asset left it at 0 and fell
    through to 'Add photos (connect the gym's Drive folder...)' -- the exact sentence
    this branch exists to stop sending a gym whose folder is full."""
    res = {"gym": GYM, "photos_repeated": 3, "approved_left": 0, "small_library": True,
           "drive_pool": 0, "drive_pool_seen": 57, "drive_armed": True,
           "detail": [f"{REPEATED} 2026-09-14: no unused photo left "
                      "(small library; left with spacing)"]}
    text = mrs.unfixable_report(res)
    assert "57 unused item(s)" in text
    assert "Add photos" not in text, "told a gym with a full folder to add photos"


def test_a_genuinely_empty_pool_still_asks_for_photos():
    res = {"gym": "somegym", "photos_repeated": 3, "approved_left": 0,
           "small_library": True, "drive_pool": 0, "drive_pool_seen": 0,
           "drive_armed": True,
           "detail": ["a.jpg 2026-09-14: no unused photo left "
                      "(small library; left with spacing)"]}
    assert "connect the gym's Drive folder or upload in the portal" in \
        mrs.unfixable_report(res)


# ---- the nightly budget -------------------------------------------------------------
def test_the_drive_fallback_is_capped_per_gym_per_night(monkeypatch):
    """pick_replacement is an HTTP-request-sized engine (real downloads, a probe, maybe
    a transcode, 75s deadline) running inside the nightly draft process. This module's
    own notes record a gym with 28 repeats; uncapped that is ~35 minutes on one gym."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    rows = _book()
    for i in range(15, 25):
        rows.append(_row(f"r{i}", f"2026-09-{i:02d}", "instagram", "feed"))
    calls = []

    def counting(base, row, *, store, siblings=(), **kw):
        calls.append(row.get("id"))
        got = _clip(len(calls))
        got["siblings"] = {str(s.get("id")): dict(got) for s in siblings}
        return got

    res = _sweep(_Store(rows), picker=counting, drive_n=99, monkeypatch=monkeypatch)
    assert len(calls) <= mrs.DRIVE_FALLBACK_MAX_PER_GYM, \
        f"{len(calls)} Drive materializations in one gym-night"
    assert res["dates_fixed"] == mrs.DRIVE_FALLBACK_MAX_PER_GYM
    assert any("budget for tonight is spent" in d for d in res["detail"])


# =====================================================================================
# INDEPENDENT AUDIT ROUND 3 (2026-09-11).
# =====================================================================================

def test_the_dry_run_respects_the_same_cap_apply_does(monkeypatch):
    """C4: the cap was applied only in the apply branch, so a dry run against a client's
    gym promised ~2.5x what apply could deliver -- and the dry run is the instrument we
    verify a gym with before telling the client anything."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    rows = _book()
    for i in range(15, 26):
        rows.append(_row(f"r{i}", f"2026-09-{i:02d}", "instagram", "feed"))
    dry = _sweep(_Store(rows), apply=False, picker=None, drive_n=40,
                 monkeypatch=monkeypatch)

    calls = []

    def counting(base, row, *, store, siblings=(), **kw):
        calls.append(row.get("id"))
        got = _clip(len(calls))
        got["siblings"] = {str(s.get("id")): dict(got) for s in siblings}
        return got

    wet = _sweep(_Store(rows), apply=True, picker=counting, drive_n=40,
                 monkeypatch=monkeypatch)
    assert dry["dates_fixed"] == wet["dates_fixed"] == mrs.DRIVE_FALLBACK_MAX_PER_GYM, \
        f"dry run promised {dry['dates_fixed']}, apply delivered {wet['dates_fixed']}"


def test_budget_exhaustion_is_not_reported_as_a_small_library(monkeypatch):
    """M1: the cap branch fell through to small_library, firing the small-library ops
    alert and telling the gym 'tonight's run could not prepare one' on a run that had
    prepared five, with 35 assets still in the pool."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    rows = _book()
    for i in range(15, 26):
        rows.append(_row(f"r{i}", f"2026-09-{i:02d}", "instagram", "feed"))
    alerts = []
    monkeypatch.setattr("agent.media_guard.alert_small_library",
                        lambda base, day, log=None: alerts.append(base))
    res = _sweep(_Store(rows), picker=_picker(clips=range(1, 40)), drive_n=40,
                 monkeypatch=monkeypatch)
    assert res["budget_capped"] > 0
    assert res["small_library"] is False, "the pool was full; nothing was short of media"
    assert alerts == [], "fired the small-library alert with a full Drive folder"
    text = mrs.unfixable_report(res)
    assert "could not prepare one" not in text, "it prepared five"
    assert "queued behind tonight's per-gym limit" in text


def test_a_run_that_drains_the_pool_reports_what_it_first_saw(monkeypatch):
    """C3: drive_pool_seen was written nowhere, so it always equalled the POST-run
    count. A run that used the last asset reported 0 and sent the exact 'Add photos,
    connect your Drive folder' line this branch exists to stop sending."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    rows = _book() + [_row("r15", "2026-09-15", "instagram", "feed"),
                      _row("r16", "2026-09-16", "instagram", "feed")]
    pool = [2]

    def draining(base, asset_state):
        return pool[0]

    monkeypatch.setattr("agent.media_swap.after_swap", lambda *a, **k: None)
    monkeypatch.setattr(mrs, "_drive_candidate_count", draining)

    def pick(base, row, *, store, siblings=(), **kw):
        if pool[0] <= 0:
            return {"ok": False, "reason": "no_fresh_photo"}
        pool[0] -= 1
        got = _clip(pool[0])
        got["siblings"] = {str(s.get("id")): dict(got) for s in siblings}
        return got

    monkeypatch.setattr("agent.media_swap.pick_replacement", pick)
    import datetime
    res = mrs.sweep_gym(GYM, _Store(rows), apply=True,
                        today=datetime.date(2026, 9, 11))
    assert res["drive_pool"] == 0, "the run drained it"
    assert res["drive_pool_seen"] == 2, "but the folder was not empty when we looked"
    text = mrs.unfixable_report(res)
    assert "Add photos" not in text, "told a gym with a connected folder to add photos"


def test_the_forward_swap_clears_a_stale_source_media_url(monkeypatch):
    """M2: _restore_rows got the '' fix in round 2; the FORWARD path did not. A variant
    carrying no source (a story with AGENT_STORY_SOURCE_MEDIA off) stranded the row's
    previous source_media_url on top of the NEW image_url. media_guard.row_media_key
    reads source_media_url FIRST, so the row would key as the OLD photo forever --
    invisible to the client, and blocking that photo from every future pick."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_DRIVE", "true")
    store = _Store(_book())
    for r in store.rows:
        if r["id"] == "r14st":
            r["source_media_url"] = f"https://tt.media/{REPEATED}"

    def no_src(base, row, *, store, siblings=(), **kw):
        got = _clip(1)
        got["source_media_url"] = None          # what _finish returns for this shape
        got["siblings"] = {str(s.get("id")): dict(got) for s in siblings}
        return got

    _sweep(store, picker=no_src, drive_n=57, monkeypatch=monkeypatch)
    story = [r for r in store.rows if r["id"] == "r14st"][0]
    assert story["source_media_url"] == "", \
        f"stranded {story['source_media_url']!r}: the repeat is now invisible to Echo"
    assert media_guard.row_media_key(story).endswith(".mp4")
