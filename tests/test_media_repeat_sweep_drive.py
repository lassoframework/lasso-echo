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
