"""Offline tests for scripts/audit_pierce_weekly.py against a fake store."""
from datetime import date, timedelta

import pytest

from scripts import audit_pierce_weekly as audit


class FakeStore:
    """list_month-only stand-in; records every query and refuses mutation."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def list_month(self, account_key, month):
        self.calls.append((account_key, month))
        return [r for r in self.rows
                if str(r.get("post_date", ""))[:7] == month]

    def __getattr__(self, name):
        if name.startswith(("delete", "insert", "patch", "update", "upsert")):
            raise AssertionError(f"audit must never call mutation {name}")
        raise AttributeError(name)


def feed_pair(day, slot, caption, media):
    return [{"gym_id": "piercefitness", "account": account, "format": "feed",
             "post_date": day, "slot_index": slot, "caption": caption,
             "image_url": f"https://cdn.example/{media}",
             "status": "pending", "pillar": "service"}
            for account in ("instagram", "facebook")]


def complete_week_rows(start, *, base="piercefitness", gbp_updates=3):
    rows = []
    for i in range(7):
        day = (start + timedelta(days=i)).isoformat()
        for slot in (0, 1):
            n = i * 2 + slot
            for row in feed_pair(day, slot, f"Caption number {n} for the week",
                                 f"photo-{n:02d}.jpg"):
                row["gym_id"] = base
                rows.append(row)
        for slot in (0, 1):
            rows.append({"gym_id": base, "account": "instagram",
                         "format": "story", "post_date": day,
                         "slot_index": slot,
                         "image_url": f"https://cdn.example/story-{i}-{slot}.jpg",
                         "status": "pending"})
    for i in range(gbp_updates):
        day = (start + timedelta(days=i * 2)).isoformat()
        rows.append({"gym_id": base, "account": "googlebusiness",
                     "format": "update", "post_date": day,
                     "caption": f"GBP update {i}", "status": "pending"})
    return rows


def test_complete_week_passes_and_reports_counts():
    start = date(2026, 9, 19)
    store = FakeStore(complete_week_rows(start))
    report = audit.audit_week(store, start)
    assert report["ok"] is True
    assert report["findings"] == []
    assert report["start"] == "2026-09-19"
    assert report["end"] == "2026-09-25"
    assert report["months_read"] == ["2026-09"]
    assert report["counts"]["ig_feeds"] == 14
    assert report["counts"]["fb_feeds"] == 14
    assert report["counts"]["ig_stories"] == 14
    assert report["counts"]["gbp_updates"] == 3
    assert report["counts"]["distinct_ig_captions"] == 14
    assert report["counts"]["distinct_feed_media_keys"] == 14
    assert report["status_counts"] == {"pending": 45}
    assert report["gbp_format_counts"] == {"update": 3}
    # list_month only, tenant-scoped every time
    assert {call[0] for call in store.calls} == {"piercefitness"}
    # no raw captions anywhere in the report
    assert "Caption number" not in repr(report)


def test_missing_second_feed_is_a_structural_gap():
    start = date(2026, 9, 19)
    rows = [r for r in complete_week_rows(start)
            if not (r["account"] == "instagram" and r["format"] == "feed"
                    and r["post_date"] == "2026-09-21"
                    and r["slot_index"] == 1)]
    report = audit.audit_week(FakeStore(rows), start)
    assert report["ok"] is False
    assert {"code": "missing_ig_feed", "date": "2026-09-21",
            "slot": 1} in report["findings"]
    assert report["counts"]["ig_feeds"] == 13
    assert audit.main(["--start", "2026-09-19"],
                      store=FakeStore(rows)) == 1


def test_duplicate_caption_and_duplicate_media_flagged_by_fingerprint():
    start = date(2026, 9, 19)
    rows = complete_week_rows(start)
    ig_feeds = [r for r in rows if r["account"] == "instagram"
                and r["format"] == "feed"]
    ig_feeds[1]["caption"] = ig_feeds[0]["caption"]  # duplicate copy
    ig_feeds[2]["image_url"] = ig_feeds[0]["image_url"]  # duplicate photo
    report = audit.audit_week(FakeStore(rows), start)
    codes = {f["code"] for f in report["findings"]}
    assert "duplicate_ig_caption" in codes
    assert "duplicate_feed_media" in codes
    dup = next(f for f in report["findings"]
               if f["code"] == "duplicate_ig_caption")
    assert len(dup["fingerprint"]) == 12
    assert "Caption number" not in repr(dup)
    assert "photo-00.jpg" not in repr(report)
    media_dup = next(f for f in report["findings"]
                     if f["code"] == "duplicate_feed_media")
    assert len(media_dup["media_fingerprint"]) == 12
    assert report["ok"] is False


def test_wrong_tenant_rows_are_ignored_and_store_stays_scoped():
    start = date(2026, 9, 19)
    rows = complete_week_rows(start, base="someothergym")
    store = FakeStore(rows)
    report = audit.audit_week(store, start)
    assert report["ok"] is False
    assert report["counts"]["rows"] == 0
    missing = [f for f in report["findings"] if f["code"] == "missing_ig_feed"]
    assert len(missing) == 14
    assert {call[0] for call in store.calls} == {"piercefitness"}


def test_week_crossing_month_boundary_reads_both_months():
    start = date(2026, 9, 26)  # Saturday; block runs Sep 26 - Oct 2
    assert start.weekday() == 5
    store = FakeStore(complete_week_rows(start))
    report = audit.audit_week(store, start)
    assert report["ok"] is True
    assert report["months_read"] == ["2026-09", "2026-10"]
    assert ("piercefitness", "2026-09") in store.calls
    assert ("piercefitness", "2026-10") in store.calls
    assert report["per_day"]["2026-09-30"]["ig_feed_slots"] == [0, 1]
    assert report["per_day"]["2026-10-01"]["ig_feed_slots"] == [0, 1]


def test_non_saturday_start_is_rejected():
    assert audit.main(["--start", "2026-09-21"], store=FakeStore([])) == 2
    assert audit.main(["--start", "not-a-date"], store=FakeStore([])) == 2


def test_unreadable_store_returns_two():
    class NoList:
        pass

    assert audit.main(["--start", "2026-09-19"], store=NoList()) == 2


def test_unpaired_mirror_and_story_slot_gap():
    start = date(2026, 9, 19)
    rows = complete_week_rows(start)
    for row in rows:  # break one mirror's caption pairing
        if (row["account"] == "facebook" and row["post_date"] == "2026-09-22"
                and row["slot_index"] == 0):
            row["caption"] = "A different caption entirely"
            row["image_url"] = "https://cdn.example/private-person-name.jpg"
    rows = [r for r in rows  # drop one story
            if not (r["format"] == "story" and r["post_date"] == "2026-09-23"
                    and r["slot_index"] == 1)]
    report = audit.audit_week(FakeStore(rows), start)
    codes = {(f["code"], f["date"]) for f in report["findings"]}
    assert ("unpaired_feed_caption", "2026-09-22") in codes
    assert ("unpaired_feed_media", "2026-09-22") in codes
    assert "private-person-name.jpg" not in repr(report)
    assert ("missing_ig_story", "2026-09-23") in codes
    assert {"code": "missing_ig_story", "date": "2026-09-23",
            "slot": 1} in report["findings"]
    assert report["ok"] is False


def test_sample_rows_do_not_satisfy_slots():
    start = date(2026, 9, 19)
    rows = complete_week_rows(start)
    for row in rows:  # turn every real feed into a seeded sample
        if row["format"] == "feed" and row["account"] == "instagram":
            row["pillar"] = "sample"
    report = audit.audit_week(FakeStore(rows), start)
    assert report["ok"] is False
    assert report["counts"]["ig_feeds"] == 0
    assert report["counts"]["sample_rows_excluded"] == 14


def test_unexpected_feed_slots_are_reported_for_both_platforms():
    start = date(2026, 9, 19)
    rows = complete_week_rows(start)
    for account in ("instagram", "facebook"):
        extra = feed_pair("2026-09-19", "other", "Another post", "extra.jpg")
        rows.extend(r for r in extra if r["account"] == account)
    report = audit.audit_week(FakeStore(rows), start)
    codes = {f["code"] for f in report["findings"]}
    assert "unexpected_ig_feed_slots" in codes
    assert "unexpected_fb_feed_slots" in codes
    assert report["ok"] is False


@pytest.mark.parametrize("bad_status",
                         ["denied", "killed", "deleted", "failed", "candidate"])
def test_non_ready_feed_rows_do_not_satisfy_slots(bad_status):
    start = date(2026, 9, 19)
    rows = complete_week_rows(start)
    for row in rows:  # poison one day's feed pair with a non-ready status
        if (row["format"] == "feed" and row["post_date"] == "2026-09-20"
                and row["slot_index"] == 0):
            row["status"] = bad_status
    report = audit.audit_week(FakeStore(rows), start)
    assert report["ok"] is False
    day = "2026-09-20"
    assert {"code": "non_ready_ig_feed", "date": day, "slot": 0,
            "status": bad_status} in report["findings"]
    assert {"code": "non_ready_fb_feed", "date": day, "slot": 0,
            "status": bad_status} in report["findings"]
    # the poisoned slot is still open -- non-ready rows must not fill it
    assert {"code": "missing_ig_feed", "date": day,
            "slot": 0} in report["findings"]
    assert {"code": "missing_fb_feed_mirror", "date": day,
            "slot": 0} in report["findings"]
    assert report["counts"]["ig_feeds"] == 13
    assert report["counts"]["non_ready_rows"] == 2
    # status counts stay honest: the rows are still reported by status
    assert report["status_counts"][bad_status] == 2
    assert report["status_counts"]["pending"] == 43
    assert report["per_day"][day]["ig_feed_slots"] == [1]


def test_non_ready_story_does_not_satisfy_story_slot():
    start = date(2026, 9, 19)
    rows = complete_week_rows(start)
    for row in rows:
        if (row["format"] == "story" and row["post_date"] == "2026-09-21"
                and row["slot_index"] == 1):
            row["status"] = "failed"
    report = audit.audit_week(FakeStore(rows), start)
    assert report["ok"] is False
    assert {"code": "non_ready_ig_story", "date": "2026-09-21",
            "status": "failed"} in report["findings"]
    assert {"code": "missing_ig_story", "date": "2026-09-21",
            "slot": 1} in report["findings"]
    assert report["counts"]["ig_stories"] == 13
    assert report["status_counts"]["failed"] == 1
    assert report["per_day"]["2026-09-21"]["ig_story_slots"] == [0]


def test_approved_rows_are_ready_and_satisfy_slots():
    start = date(2026, 9, 19)
    rows = complete_week_rows(start)
    for row in rows:
        row["status"] = "approved"
    report = audit.audit_week(FakeStore(rows), start)
    assert report["ok"] is True
    assert report["findings"] == []
    assert report["status_counts"] == {"approved": 45}


def test_two_stories_both_at_slot_zero_fail():
    start = date(2026, 9, 19)
    rows = complete_week_rows(start)
    for row in rows:  # collapse 2026-09-22's stories onto slot 0
        if row["format"] == "story" and row["post_date"] == "2026-09-22":
            row["slot_index"] = 0
    report = audit.audit_week(FakeStore(rows), start)
    assert report["ok"] is False
    assert {"code": "duplicate_ig_story_slot", "date": "2026-09-22",
            "slot": 0, "count": 2} in report["findings"]
    assert {"code": "missing_ig_story", "date": "2026-09-22",
            "slot": 1} in report["findings"]
    assert report["per_day"]["2026-09-22"]["ig_story_slots"] == [0]
