"""
Month-restage postcondition evidence (client_month_run._apply).

A restage deletes and rebuilds the WHOLE calendar month(s) its rows land in:
portal_calendar_store.delete_month is month-grained (its post_date filter spans
the 1st through the last day of the month), so it also wipes wipeable rows on
days OUTSIDE the requested [start, start+days) window (e.g. 2026-09-01..09-18
when restaging 21 days from 2026-09-19). The FIXER restage verifier
(agent/fixer_ops._compare_calendar_snapshots, fed by _calendar_snapshot) instead
compares the build's claimed deleted count against an independent SPAN-scoped
before/after readback: only rows inside the requested day window. A month-scoped
deleted claim can never equal the span-scoped gone-row count whenever such
out-of-span rows exist, so a genuine whole-month restage could never verify.

These tests pin the contract:
  * _apply's reported "deleted" claim is scoped to the SAME day-span the
    independent verifier measures, so a genuine whole-month restage verifies;
  * the raw store-reported count stays available as "deleted_total" (the
    build's own release/restore bookkeeping keeps the full truth);
  * FAIL CLOSED is preserved: the claim is only ever checked against the
    verifier's independent snapshot, so a delete that actually removes fewer
    (or more) in-span rows than claimed still verifies False, never a pass;
  * tenant binding: rows belonging to another gym are never part of the claim.
"""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_month_run as cmr  # noqa: E402
from agent import fixer_ops as FO  # noqa: E402

BASE = "gritx"
START = date(2026, 9, 19)
DAYS = 14                       # span 2026-09-19 .. 2026-10-02
MONTHS = ["2026-09", "2026-10"]
SPAN_FIRST, SPAN_LAST = date(2026, 9, 19), date(2026, 10, 2)


def _row(rid, post_date, *, gym=BASE, status="pending", account="instagram",
         fmt="feed", caption=None, image=None):
    """A calendar row carrying every field the independent readback fingerprints."""
    return {
        "id": rid,
        "gym_id": gym,
        "post_date": post_date,
        "account": account,
        "format": fmt,
        "status": status,
        "caption": caption if caption is not None else f"caption for {rid}",
        "image_url": image if image is not None else f"https://media.example/{rid}.jpg",
        "video_url": None,
        "slot_index": None,
        "source_media_asset_id": None,
    }


class _RestageStore:
    """A calendar store with portal_calendar_store.delete_month's REAL semantics:
    the delete is MONTH-grained (every wipeable row in the calendar month goes,
    including days outside the requested span) and returns the number of rows it
    actually removed. Rows carry ids; insert mints fresh ones like the DB does."""

    def __init__(self, seeded=()):
        self.rows = [dict(r) for r in seeded]
        self._next = 0

    def list_month(self, base_key, month):
        return [dict(r) for r in self.rows
                if str(r.get("gym_id")) == base_key
                and str(r.get("post_date") or "")[:7] == month]

    def delete_month(self, base_key, month, preserve_dates=()):
        keep = {str(d)[:10] for d in (preserve_dates or ()) if d}
        gone, kept = [], []
        for r in self.rows:
            pd = str(r.get("post_date") or "")
            status = str(r.get("status") or "").lower()
            wipeable = (not status) or status in ("pending", "draft", "queued")
            if (str(r.get("gym_id")) == base_key and pd[:7] == month
                    and wipeable and pd[:10] not in keep):
                gone.append(r)
            else:
                kept.append(r)
        self.rows = kept
        return len(gone)

    def insert_rows(self, base_key, rows):
        out = []
        for r in rows:
            row = dict(r)
            self._next += 1
            row["id"] = f"newrow{self._next:04d}"
            self.rows.append(row)
            out.append(row)
        return out


def _seeded_store():
    """The norm for a mid-month restage: wipeable rows INSIDE the requested span,
    wipeable rows EARLIER IN THE SAME MONTH but outside the span, one approved
    (human-owned) row the rebuild must preserve, and a foreign gym's row."""
    return _RestageStore([
        _row("oldinspan01", "2026-09-20"),
        _row("oldinspan02", "2026-09-25"),
        _row("outofspan01", "2026-09-05"),    # same month, outside the day-span
        _row("outofspan02", "2026-09-12"),    # same month, outside the day-span
        _row("approved01", "2026-09-21", status="approved"),
        _row("foreign001", "2026-09-20", gym="othergym"),
    ])


def _incoming(n=5):
    """The rebuild's replacement rows: n distinct in-span IG feed days (n >= the
    existing feed count so the never-shrink guard lets the rebuild proceed)."""
    dates = ["2026-09-20", "2026-09-22", "2026-09-26", "2026-09-29", "2026-10-01"]
    return [_row(f"incoming{i:02d}", dates[i], caption=f"fresh caption {i}",
                 image=f"https://media.example/fresh_{i}.jpg")
            for i in range(n)]


def _snapshot(store):
    """The verifier's own independent span-scoped readback, unmodified."""
    return FO._calendar_snapshot(store, BASE, MONTHS,
                                 first=SPAN_FIRST, last=SPAN_LAST)


def test_whole_month_restage_verifies_with_a_span_scoped_deleted_claim():
    """REPRODUCTION: the month-grained delete removes 4 of this gym's wipeable
    rows (2 in-span + 2 earlier in September), but the independent span-scoped
    readback can only ever see the 2 in-span ones disappear. The reported claim
    must be the number whole-month deletion can actually PROVE at span scope."""
    store = _seeded_store()
    before = _snapshot(store)
    res = cmr._apply(BASE, _incoming(), START, DAYS, store, lambda m: None)
    assert res["ok"] is True
    after = _snapshot(store)
    verified, note = FO._compare_calendar_snapshots(before, after, res)
    assert verified is True, f"a genuine whole-month restage must verify: {note}"
    assert res["deleted"] == 2, "the claim is the in-span deleted count"
    assert res["upserted"] == 5


def test_raw_store_delete_count_stays_available_as_deleted_total():
    """The store really did delete 4 rows (month-grained). That full truth is
    kept for the build's own release/restore bookkeeping; only the verifier-
    facing claim is span-scoped."""
    store = _seeded_store()
    res = cmr._apply(BASE, _incoming(), START, DAYS, store, lambda m: None)
    assert res["ok"] is True
    assert res["deleted"] == 2          # span-scoped claim (verifier-facing)
    assert res["deleted_total"] == 2    # out-of-span rows are preserved
    remaining = {r["id"] for r in store.rows}
    assert {"outofspan01", "outofspan02"} <= remaining


def test_fail_closed_when_the_delete_actually_falls_short_of_the_claim():
    """A store that REPORTS deletions but leaves the in-span rows behind must
    never verify: the independent readback recounts and the mismatch stays a
    hard False. The fix only changes the claim's UNIT, never its enforcement."""

    class _ShortDelete(_RestageStore):
        def delete_month(self, base_key, month, preserve_dates=()):
            # removes ONLY the out-of-span rows; the in-span ones stay behind
            keep = {str(d)[:10] for d in (preserve_dates or ()) if d}
            gone, kept = [], []
            for r in self.rows:
                pd = str(r.get("post_date") or "")
                status = str(r.get("status") or "").lower()
                wipeable = (not status) or status in ("pending", "draft", "queued")
                if (str(r.get("gym_id")) == base_key and pd[:7] == month
                        and wipeable and pd[:10] not in keep
                        and not (SPAN_FIRST.isoformat() <= pd[:10]
                                 <= SPAN_LAST.isoformat())):
                    gone.append(r)
                else:
                    kept.append(r)
            self.rows = kept
            return len(gone)

    store = _ShortDelete(_seeded_store().rows)
    before = _snapshot(store)
    res = cmr._apply(BASE, _incoming(), START, DAYS, store, lambda m: None)
    assert res["ok"] is True
    after = _snapshot(store)
    verified, note = FO._compare_calendar_snapshots(before, after, res)
    assert verified is False, "a delete that falls short of its claim must fail closed"
    assert "removed" in (note or "")


def test_fail_closed_when_a_delete_quietly_removes_more_than_claimed():
    """The other direction: a store that wipes rows the claim could not have
    known about (a concurrent insert landing between the claim read and the
    delete) must also fail closed."""

    class _SneakyStore(_RestageStore):
        def delete_month(self, base_key, month, preserve_dates=()):
            # a concurrent writer's row appears mid-apply and is wiped too
            self.rows.append(_row("sneaky0001", "2026-09-23"))
            return super().delete_month(base_key, month,
                                        preserve_dates=preserve_dates) + 1

    store = _SneakyStore(_seeded_store().rows)
    before = _snapshot(store)
    res = cmr._apply(BASE, _incoming(), START, DAYS, store, lambda m: None)
    assert res["ok"] is True
    after = _snapshot(store)
    verified, note = FO._compare_calendar_snapshots(before, after, res)
    assert verified is False, "extra unclaimed deletions must fail closed"


def test_locked_days_are_preserved_and_never_claimed():
    """A locked (human-owned) day's still-pending siblings survive the delete via
    preserve_dates: they are neither claimed nor counted by the readback, and
    their unchanged fingerprints keep the comparison clean."""
    store = _seeded_store()
    before = _snapshot(store)
    res = cmr._apply(BASE, _incoming(), START, DAYS, store, lambda m: None,
                     locked_days=("2026-09-25",))
    assert res["ok"] is True
    assert res["deleted"] == 1, "only the unlocked in-span pending row is claimed"
    after = _snapshot(store)
    verified, note = FO._compare_calendar_snapshots(before, after, res)
    assert verified is True, f"a restage with a locked day must verify: {note}"
    assert "oldinspan02" in after, "the locked day's row survived the delete"


def test_tenant_binding_foreign_rows_are_never_part_of_the_claim():
    """A broken list_month that leaks ANOTHER gym's rows: the claim counts only
    this gym's rows, and the verifier's own snapshot refuses the foreign
    evidence outright (never a pass on tenant-crossing data)."""

    class _LeakyStore(_RestageStore):
        def list_month(self, base_key, month):
            return [dict(r) for r in self.rows
                    if str(r.get("post_date") or "")[:7] == month]

    store = _LeakyStore(_seeded_store().rows)
    res = cmr._apply(BASE, _incoming(), START, DAYS, store, lambda m: None)
    assert res["ok"] is True
    assert res["deleted"] == 2, "foreign rows never inflate this gym's claim"
    assert any(str(r.get("gym_id")) == "othergym" for r in store.rows), \
        "the other gym's rows were never touched"
    with pytest.raises(FO._ReadbackUnavailable):
        _snapshot(store)


def test_store_without_a_bounded_read_keeps_the_legacy_claim():
    """Legacy/test stores without list_month retain their historical contract;
    production's Portal store takes the bounded preservation path."""

    class _BareStore:
        def __init__(self):
            self.deleted = []
            self.inserted = []

        def delete_month(self, base_key, month, preserve_dates=()):
            self.deleted.append((base_key, month))
            return 7

        def insert_rows(self, base_key, rows):
            self.inserted.extend(rows)
            return rows

    store = _BareStore()
    res = cmr._apply(BASE, _incoming(2), START, DAYS, store, lambda m: None)
    assert res["ok"] is True
    assert res["deleted"] == 14
    assert res["deleted_total"] == 14


def test_bounded_read_failure_cannot_erase_out_of_span_days():
    class _Unreadable(_RestageStore):
        def list_month(self, base_key, month):
            raise RuntimeError("temporary read outage")

    store = _Unreadable(_seeded_store().rows)
    res = cmr._apply(BASE, _incoming(), START, DAYS, store, lambda m: None)
    assert res["ok"] is True
    assert {r["id"] for r in store.rows} >= {
        "outofspan01", "outofspan02"}


def test_concurrent_new_out_of_span_date_is_preserved_without_snapshot_help():
    class _Concurrent(_RestageStore):
        def delete_month(self, base_key, month, preserve_dates=()):
            if month == "2026-09":
                self.rows.append(_row("concurrent01", "2026-09-08"))
            return super().delete_month(base_key, month, preserve_dates=preserve_dates)

    store = _Concurrent(_seeded_store().rows)
    res = cmr._apply(BASE, _incoming(), START, DAYS, store, lambda m: None)
    assert res["ok"] is True
    assert "concurrent01" in {r["id"] for r in store.rows}


def test_bounded_store_without_preserve_support_fails_before_deleting():
    class _OldDelete(_RestageStore):
        def delete_month(self, base_key, month):
            pytest.fail("the legacy delete body must not run after kwarg rejection")

    store = _OldDelete(_seeded_store().rows)
    res = cmr._apply(BASE, _incoming(), START, DAYS, store, lambda m: None)
    assert res["ok"] is False
    assert res["deleted"] == 0
    assert {r["id"] for r in store.rows} >= {
        "oldinspan01", "oldinspan02", "outofspan01", "outofspan02"}
