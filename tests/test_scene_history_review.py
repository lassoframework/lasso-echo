"""Offline tests for agent/jobs/scene_history_review.py (DRAFT/OFF).

No live database, no network: calendar/history readers are in-memory fakes
shaped on the assumed Child A draft-history interface (canonical tenant /
group / date / fingerprint columns); media comes from the shared
tests/gym_media_fakes.FakeMediaStore.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.jobs import scene_history_review as shr  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, make_asset  # noqa: E402

GYM = "pierce"
OTHER = "gritx"
FP_A = "md5:" + "a" * 32
FP_B = "md5:" + "b" * 32
FP_C = "md5:" + "c" * 32


class FakeCalendar:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def list_published_rows(self, gym_id, limit):
        self.calls.append((gym_id, limit))
        return [dict(r) for r in self.rows][:limit]


class FakeHistory:
    """Assumed Child A reader: canonical tenant/group/date columns."""

    def __init__(self, occupied=(), claims=()):
        self.occupied = list(occupied)
        self.claims = list(claims)

    def list_occupied_scenes(self, gym_id, limit):
        return [dict(r) for r in self.occupied][:limit]

    def list_publish_claims(self, gym_id, limit):
        return [dict(r) for r in self.claims][:limit]


class ReadOnlyMediaStore(FakeMediaStore):
    """Any attempted write fails the test outright."""

    def update_asset(self, *a, **k):
        raise AssertionError("review queue must be read-only")

    def insert_assets(self, *a, **k):
        raise AssertionError("review queue must be read-only")

    def update_review_asset(self, *a, **k):
        raise AssertionError("review queue must be read-only")


def published_row(rid, gym_id=GYM, date="2026-09-15", asset_id=None,
                  byte_hash=None, fmt="photo", group="g1"):
    return {"id": rid, "gym_id": gym_id, "post_date": date,
            "status": "published", "format": fmt, "mime_type": "",
            "visual_group_key": group,
            "source_media_asset_id": asset_id, "byte_hash": byte_hash,
            "drive_file_id": None, "image_url": f"https://x/{rid}.jpg"}


def occupied(fp, tenant=GYM, date="2026-09-15", row_id=None, group="g1",
             asset_id=None):
    return {"tenant_id": tenant, "group_key": group, "used_date": date,
            "fingerprint": fp, "calendar_row_id": row_id,
            "asset_id": asset_id, "channel": "feed"}


# ---------------------------------------------------------------- proved lane

def test_published_row_proved_identity_via_byte_hash():
    cal = FakeCalendar([published_row("r1", byte_hash=FP_A)])
    store = ReadOnlyMediaStore()
    hist = FakeHistory(occupied=[occupied(FP_A, row_id="r1")])
    queue = shr.build_review_queue(GYM, cal, store, hist)
    assert len(queue) == 1
    item = queue[0]
    assert item["classification"] == shr.PROVED
    assert item["reasons"] == ["row_bound_on_history"]
    assert item["fingerprint"] == FP_A


def test_published_row_proved_identity_via_bound_approved_asset():
    asset = make_asset("a1", gym_id=GYM, content_hash=FP_A[4:])
    cal = FakeCalendar([published_row("r1", asset_id="a1")])
    store = ReadOnlyMediaStore(assets=[asset])
    hist = FakeHistory(claims=[{"tenant_id": GYM, "group_key": "g1",
                                "claim_date": "2026-09-15", "fingerprint": FP_A,
                                "calendar_row_id": "r1", "asset_id": "a1",
                                "channel": "feed"}])
    queue = shr.build_review_queue(GYM, cal, store, hist)
    row_item = next(i for i in queue if i["lane"] == "published_row")
    asset_item = next(i for i in queue if i["lane"] == "approved_asset")
    assert row_item["classification"] == shr.PROVED
    assert asset_item["classification"] == shr.PROVED


def test_bare_32hex_hash_normalizes_to_md5_fingerprint():
    cal = FakeCalendar([published_row("r1", byte_hash="a" * 32)])
    queue = shr.build_review_queue(GYM, cal, ReadOnlyMediaStore(),
                                   FakeHistory(occupied=[occupied(FP_A, row_id="r1")]))
    assert queue[0]["classification"] == shr.PROVED


# -------------------------------------------------------- unresolved reasons

def test_row_without_any_media_binding_is_unresolved():
    row = published_row("r1")
    row["drive_file_id"] = None
    cal = FakeCalendar([row])
    queue = shr.build_review_queue(GYM, cal, ReadOnlyMediaStore(),
                                   FakeHistory())
    assert queue[0]["classification"] == shr.UNRESOLVED
    assert queue[0]["reasons"] == ["no_media_binding"]


def test_bound_asset_missing_is_unresolved():
    cal = FakeCalendar([published_row("r1", asset_id="ghost")])
    queue = shr.build_review_queue(GYM, cal, ReadOnlyMediaStore(),
                                   FakeHistory())
    assert "asset_missing" in queue[0]["reasons"]


def test_bound_asset_not_approved_is_unresolved():
    asset = make_asset("a1", gym_id=GYM, content_hash="d" * 32)
    asset["review_status"] = "pending_review"
    cal = FakeCalendar([published_row("r1", asset_id="a1")])
    store = ReadOnlyMediaStore(assets=[asset])
    queue = shr.build_review_queue(GYM, cal, store, FakeHistory())
    item = next(i for i in queue if i["lane"] == "published_row")
    assert "asset_not_approved" in item["reasons"]


def test_non_md5_hash_is_unverifiable_not_guessed():
    cal = FakeCalendar([published_row("r1", byte_hash="f" * 64)])  # sha256
    queue = shr.build_review_queue(GYM, cal, ReadOnlyMediaStore(),
                                   FakeHistory())
    assert queue[0]["reasons"] == ["fingerprint_unverifiable"]


def test_fingerprint_absent_from_history_is_unresolved():
    cal = FakeCalendar([published_row("r1", byte_hash=FP_C)])
    queue = shr.build_review_queue(GYM, cal, ReadOnlyMediaStore(),
                                   FakeHistory(occupied=[occupied(FP_A)]))
    assert queue[0]["reasons"] == ["no_history_record"]


def test_history_date_mismatch_is_unresolved():
    cal = FakeCalendar([published_row("r1", date="2026-09-16", byte_hash=FP_A)])  # noqa
    hist = FakeHistory(occupied=[occupied(FP_A, date="2026-09-15",
                                          row_id="r1")])
    queue = shr.build_review_queue(GYM, cal, ReadOnlyMediaStore(), hist)
    assert queue[0]["reasons"] == ["history_date_mismatch"]


def test_approved_asset_never_published_is_unresolved():
    asset = make_asset("a1", gym_id=GYM, content_hash="e" * 32)
    store = ReadOnlyMediaStore(assets=[asset])
    queue = shr.build_review_queue(GYM, FakeCalendar([]), store, FakeHistory())
    assert queue[0]["lane"] == "approved_asset"
    assert queue[0]["reasons"] == ["never_published"]


# ------------------------------------------------------------ tenant boundary

def test_cross_tenant_calendar_rows_are_dropped_before_classification():
    rows = [published_row("mine", byte_hash=FP_A),
            published_row("theirs", gym_id=OTHER, byte_hash=FP_B)]
    hist = FakeHistory(occupied=[occupied(FP_A), occupied(FP_B, tenant=OTHER)])
    queue = shr.build_review_queue(GYM, FakeCalendar(rows),
                                   ReadOnlyMediaStore(), hist)
    assert {i["ref"] for i in queue} == {"mine"}
    assert all(i["gym_id"] == GYM for i in queue)


def test_cross_tenant_history_rows_are_dropped():
    # FP_B is occupied ONLY by another gym: it must not prove OUR row.
    cal = FakeCalendar([published_row("r1", byte_hash=FP_B)])
    hist = FakeHistory(occupied=[occupied(FP_B, tenant=OTHER)])
    queue = shr.build_review_queue(GYM, cal, ReadOnlyMediaStore(), hist)
    assert queue[0]["reasons"] == ["no_history_record"]


def test_row_bound_to_other_gyms_asset_is_tenant_mismatch_without_leak():
    foreign = make_asset("a9", gym_id=OTHER, source_id="src9",
                         content_hash="b" * 32)
    cal = FakeCalendar([published_row("r1", asset_id="a9")])
    store = ReadOnlyMediaStore(assets=[foreign])
    queue = shr.build_review_queue(GYM, cal, store,
                                   FakeHistory(occupied=[occupied(FP_B)]))
    item = next(i for i in queue if i["lane"] == "published_row")
    assert item["classification"] == shr.UNRESOLVED
    assert item["reasons"] == ["tenant_mismatch"]
    assert item["fingerprint"] is None
    # The foreign asset itself must not appear in OUR asset lane either.
    assert all(i["ref"] != "a9" for i in queue)


# ------------------------------------------------------------- bounds + order

def test_photos_rank_before_videos_and_dates_descending():
    rows = [published_row("v1", byte_hash=FP_A, fmt="video", date="2026-09-20"),
            published_row("p1", byte_hash=FP_A, date="2026-09-10"),
            published_row("p2", byte_hash=FP_A, date="2026-09-25")]
    queue = shr.build_review_queue(GYM, FakeCalendar(rows),
                                   ReadOnlyMediaStore(), FakeHistory())
    assert [i["ref"] for i in queue] == ["p2", "p1", "v1"]


def test_per_gym_published_cap_and_queue_cap():
    rows = [published_row(f"r{i:03d}", byte_hash=FP_A,
                          date=f"2026-09-{(i % 28) + 1:02d}")
            for i in range(50)]
    queue = shr.build_review_queue(GYM, FakeCalendar(rows),
                                   ReadOnlyMediaStore(), FakeHistory(),
                                   max_published=10)
    assert len(queue) == 10
    queue = shr.build_review_queue(GYM, FakeCalendar(rows),
                                   ReadOnlyMediaStore(), FakeHistory(),
                                   max_published=50, max_items=7)
    assert len(queue) == 7


def test_asset_cap_and_reviewed_before_filter():
    assets = [make_asset(f"a{i}", gym_id=GYM, content_hash=f"{i:032d}")
              for i in range(5)]
    for i, a in enumerate(assets):
        a["reviewed_at"] = f"2026-08-2{i}T00:00:00Z"
    store = ReadOnlyMediaStore(assets=assets)
    queue = shr.build_review_queue(GYM, FakeCalendar([]), store, FakeHistory(),
                                   reviewed_before="2026-08-22T00:00:00Z")
    assert len(queue) == 3  # reviewed_at 20, 21, 22 only
    queue = shr.build_review_queue(GYM, FakeCalendar([]), store, FakeHistory(),
                                   max_assets=2)
    assert len(queue) == 2


def test_no_history_reader_means_nothing_proves():
    cal = FakeCalendar([published_row("r1", byte_hash=FP_A)])
    queue = shr.build_review_queue(GYM, cal, ReadOnlyMediaStore(), None)
    assert queue[0]["reasons"] == ["no_history_record"]


def test_requires_gym_id():
    with pytest.raises(ValueError):
        shr.build_review_queue("", FakeCalendar([]), ReadOnlyMediaStore())


def test_summarize_counts():
    cal = FakeCalendar([published_row("r1", byte_hash=FP_A),
                        published_row("r2", byte_hash=FP_C)])
    hist = FakeHistory(occupied=[occupied(FP_A, row_id="r1")])
    queue = shr.build_review_queue(GYM, cal, ReadOnlyMediaStore(), hist)
    summary = shr.summarize(queue)
    assert summary["proved_identity"] == 1
    assert summary["unresolved"] == 1
    assert summary["reasons"]["no_history_record"] == 1


# ------------------------------------------------- exact-binding precision

def test_same_fingerprint_and_date_but_different_row_is_unresolved():
    # History binds fingerprint+date to SOME OTHER calendar row: that is a
    # near-match, never proof for THIS row.
    cal = FakeCalendar([published_row("r1", byte_hash=FP_A)])
    hist = FakeHistory(occupied=[occupied(FP_A, row_id="r-other")])
    queue = shr.build_review_queue(GYM, cal, ReadOnlyMediaStore(), hist)
    assert queue[0]["classification"] == shr.UNRESOLVED
    assert queue[0]["reasons"] == ["history_row_binding_mismatch"]


def test_same_fingerprint_and_date_but_different_group_is_unresolved():
    cal = FakeCalendar([published_row("r1", byte_hash=FP_A, group="g1")])
    hist = FakeHistory(occupied=[occupied(FP_A, row_id="r1", group="g2")])
    queue = shr.build_review_queue(GYM, cal, ReadOnlyMediaStore(), hist)
    assert queue[0]["classification"] == shr.UNRESOLVED
    assert queue[0]["reasons"] == ["history_row_binding_mismatch"]


def test_blank_row_date_never_proves_even_with_history():
    row = published_row("r1", date=None, byte_hash=FP_A)
    hist = FakeHistory(occupied=[occupied(FP_A, row_id="r1")])
    queue = shr.build_review_queue(GYM, FakeCalendar([row]),
                                   ReadOnlyMediaStore(), hist)
    assert queue[0]["classification"] == shr.UNRESOLVED
    assert queue[0]["reasons"] == ["missing_date"]


def test_unapproved_bound_asset_never_proves_despite_history():
    asset = make_asset("a1", gym_id=GYM, content_hash="a" * 32)
    asset["review_status"] = "pending_review"
    cal = FakeCalendar([published_row("r1", asset_id="a1")])
    hist = FakeHistory(occupied=[occupied(FP_A, row_id="r1")])
    queue = shr.build_review_queue(GYM, cal,
                                   ReadOnlyMediaStore(assets=[asset]), hist)
    item = next(i for i in queue if i["lane"] == "published_row")
    assert item["classification"] == shr.UNRESOLVED
    assert item["reasons"] == ["asset_not_approved"]


def test_same_byte_distinct_asset_is_used_bytes_not_proved_identity():
    # Two approved assets share the exact same bytes; history binds only a2.
    a1 = make_asset("a1", gym_id=GYM, source_id="srcA", content_hash="a" * 32)
    a2 = make_asset("a2", gym_id=GYM, source_id="srcB", content_hash="a" * 32)
    hist = FakeHistory(occupied=[occupied(FP_A, row_id="rx", asset_id="a2")])
    store = ReadOnlyMediaStore(assets=[a1, a2])
    queue = shr.build_review_queue(GYM, FakeCalendar([]), store, hist)
    by_ref = {i["ref"]: i for i in queue}
    assert by_ref["a1"]["classification"] == shr.USED_BYTES
    assert by_ref["a1"]["reasons"] == ["scene_bytes_on_history_asset_unbound"]
    assert by_ref["a2"]["classification"] == shr.PROVED
    assert by_ref["a2"]["reasons"] == ["asset_bound_on_history"]


def test_approved_asset_on_history_without_asset_binding_is_used_bytes():
    asset = make_asset("a1", gym_id=GYM, content_hash="a" * 32)
    hist = FakeHistory(occupied=[occupied(FP_A, row_id="r9")])
    queue = shr.build_review_queue(GYM, FakeCalendar([]),
                                   ReadOnlyMediaStore(assets=[asset]), hist)
    assert queue[0]["classification"] == shr.USED_BYTES


def test_row_without_group_key_is_unresolved():
    cal = FakeCalendar([published_row("r1", byte_hash=FP_A, group=None)])
    hist = FakeHistory(occupied=[occupied(FP_A, row_id="r1")])
    queue = shr.build_review_queue(GYM, cal, ReadOnlyMediaStore(), hist)
    assert queue[0]["reasons"] == ["group_missing"]
