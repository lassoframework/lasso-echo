"""
Tests for agent/jobs/dedupe_forward_book.py — Wave 0.2.

Offline. No Supabase calls; the store is injected with a fake.
Coverage:
  - caption_hash: tag/handle stripping, case normalization, punctuation removal,
    truncation at 200 chars.
  - find_duplicates: single row (no dupes), all-unique, all-same, mixed, tiebreak
    on post_date then id.
  - dedupe_gym: dry-run makes no writes; live run calls deny_with_reason for each
    dupe; partial-error path returns correct error count.
  - run(): AGENT_DEDUPE_FORWARD_BOOK=false forces dry-run even when not explicitly
    requested; flag=true + dry_run=False allows real writes.
  - reject_reason: every denied row carries 'duplicate_purge_2026_08'.
  - Keeper is always the earliest post_date in a group.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.jobs.dedupe_forward_book import (
    caption_hash,
    dedupe_gym,
    find_duplicates,
    run,
)
from agent.portal_calendar_store import PortalStoreError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(row_id, post_date, caption, gym_id="lasso", status="pending"):
    return {
        "id": row_id,
        "gym_id": gym_id,
        "post_date": post_date,
        "caption": caption,
        "status": status,
    }


class FakeStore:
    """Injectable fake for SupabaseCalendarStore."""

    def __init__(self, pending_rows=None, deny_error=False):
        self._pending_rows = list(pending_rows or [])
        self._deny_error = deny_error
        self.denied_calls = []   # list of (gym_id, row_id, reject_reason)

    def list_pending_future(self, gym_id, today_iso):
        return [r for r in self._pending_rows if r.get("gym_id") == gym_id]

    def deny_with_reason(self, gym_id, row_id, reject_reason):
        self.denied_calls.append((gym_id, row_id, reject_reason))
        if self._deny_error:
            raise PortalStoreError(500, "forced test error")
        # Simulate PostgREST returning the patched row.
        return {"id": row_id, "gym_id": gym_id, "status": "denied", "reject_reason": reject_reason}


# ---------------------------------------------------------------------------
# caption_hash tests
# ---------------------------------------------------------------------------

class TestCaptionHash:
    def test_same_text_same_hash(self):
        assert caption_hash("Hello world") == caption_hash("Hello world")

    def test_case_insensitive(self):
        assert caption_hash("Hello World") == caption_hash("hello world")

    def test_hashtags_stripped(self):
        assert caption_hash("hello #fitness #gym") == caption_hash("hello")

    def test_handles_stripped(self):
        assert caption_hash("tag @coach_amanda today") == caption_hash("tag  today")

    def test_punctuation_removed(self):
        assert caption_hash("Hello, world!") == caption_hash("hello world")

    def test_extra_whitespace_collapsed(self):
        assert caption_hash("hello  world") == caption_hash("hello world")

    def test_truncation_at_200_chars(self):
        long_text = "a" * 300
        # The hash is computed on first 200 chars; both 250 and 300 produce same hash.
        assert caption_hash("a" * 250) == caption_hash(long_text)

    def test_different_texts_different_hashes(self):
        assert caption_hash("Join us for a free class") != caption_hash("Transform your body in 30 days")

    def test_returns_16_hex_chars(self):
        h = caption_hash("some caption")
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_empty_string(self):
        h = caption_hash("")
        assert isinstance(h, str) and len(h) == 16

    def test_none_coerced(self):
        h = caption_hash(None)  # type: ignore[arg-type]
        assert isinstance(h, str) and len(h) == 16


# ---------------------------------------------------------------------------
# find_duplicates tests
# ---------------------------------------------------------------------------

class TestFindDuplicates:
    def test_single_row_is_keeper(self):
        rows = [_row("id1", "2026-09-01", "Join us today")]
        keepers, dupes = find_duplicates(rows)
        assert len(keepers) == 1
        assert len(dupes) == 0

    def test_all_unique_captions_all_keepers(self):
        rows = [
            _row("id1", "2026-09-01", "Caption A"),
            _row("id2", "2026-09-02", "Caption B"),
            _row("id3", "2026-09-03", "Caption C"),
        ]
        keepers, dupes = find_duplicates(rows)
        assert len(keepers) == 3
        assert len(dupes) == 0

    def test_all_same_caption_keeps_earliest(self):
        rows = [
            _row("id3", "2026-09-03", "Same caption text"),
            _row("id1", "2026-09-01", "Same caption text"),
            _row("id2", "2026-09-02", "Same caption text"),
        ]
        keepers, dupes = find_duplicates(rows)
        assert len(keepers) == 1
        assert len(dupes) == 2
        assert keepers[0]["id"] == "id1"
        assert set(d["id"] for d in dupes) == {"id2", "id3"}

    def test_mixed_groups(self):
        rows = [
            _row("id1", "2026-09-01", "Unique A"),
            _row("id2", "2026-09-02", "Shared caption"),
            _row("id3", "2026-09-03", "Shared caption"),
            _row("id4", "2026-09-04", "Unique B"),
        ]
        keepers, dupes = find_duplicates(rows)
        assert len(keepers) == 3   # Unique A, earliest Shared, Unique B
        assert len(dupes) == 1
        assert dupes[0]["id"] == "id3"

    def test_keeper_is_earliest_post_date(self):
        rows = [
            _row("id2", "2026-10-05", "Duplicate caption"),
            _row("id1", "2026-09-01", "Duplicate caption"),
        ]
        keepers, dupes = find_duplicates(rows)
        assert keepers[0]["id"] == "id1"
        assert dupes[0]["id"] == "id2"

    def test_same_date_siblings_are_one_post_never_duplicates(self):
        """CONTRACT FIX (2026-08-31): a feed cross-posts to Instagram AND Facebook on
        the SAME date — those rows are ONE post by design. The old tiebreak denied the
        Facebook mirror of every kept feed (89 healthy LASSO mirrors were purged).
        Only a LATER date repeating the caption is a true duplicate."""
        rows = [
            _row("id_b", "2026-09-01", "Same caption for tiebreak"),
            _row("id_a", "2026-09-01", "Same caption for tiebreak"),
            _row("id_c", "2026-09-05", "Same caption for tiebreak"),   # true dup (later date)
        ]
        keepers, dupes = find_duplicates(rows)
        assert {k["id"] for k in keepers} == {"id_a", "id_b"}
        assert [d["id"] for d in dupes] == ["id_c"]

    def test_hash_normalization_treated_as_duplicate(self):
        # These captions differ only by case and hashtag — same hash, so duplicate.
        rows = [
            _row("id1", "2026-09-01", "Join our community #gym"),
            _row("id2", "2026-09-02", "JOIN OUR COMMUNITY"),
        ]
        keepers, dupes = find_duplicates(rows)
        assert len(keepers) == 1
        assert len(dupes) == 1
        assert keepers[0]["id"] == "id1"

    def test_empty_list(self):
        keepers, dupes = find_duplicates([])
        assert keepers == []
        assert dupes == []

    def test_empty_and_none_captions_are_never_duplicates(self):
        """CONTRACT FIX (2026-08-31): hash('')==hash('') says nothing about repeats —
        a story's caption lives burned on its media and a GBP photo post is captionless
        BY DESIGN. The old grouping denied 112 healthy LASSO stories + 2 GBP photos as
        'duplicates'. Empty/None captions (and stories) are always keepers."""
        rows = [
            _row("id1", "2026-09-01", None),
            _row("id2", "2026-09-02", None),
            _row("id3", "2026-09-03", ""),
            dict(_row("id4", "2026-09-04", "burned on media"), format="story"),
            dict(_row("id5", "2026-09-08", "burned on media"), format="story"),
        ]
        keepers, dupes = find_duplicates(rows)
        assert len(keepers) == 5 and dupes == []


# ---------------------------------------------------------------------------
# dedupe_gym tests
# ---------------------------------------------------------------------------

class TestDedupeGym:
    def _run(self, rows, dry_run=False, deny_error=False):
        store = FakeStore(pending_rows=rows, deny_error=deny_error)
        result = dedupe_gym("lasso", store, "2026-09-01", dry_run)
        return result, store

    def test_dry_run_makes_no_writes(self):
        rows = [
            _row("id1", "2026-09-02", "Same caption"),
            _row("id2", "2026-09-03", "Same caption"),
        ]
        result, store = self._run(rows, dry_run=True)
        assert store.denied_calls == []
        assert result["duplicates_found"] == 1
        assert result["duplicates_denied"] == 0

    def test_live_run_denies_duplicates(self):
        rows = [
            _row("id1", "2026-09-02", "Same caption"),
            _row("id2", "2026-09-03", "Same caption"),
        ]
        result, store = self._run(rows, dry_run=False)
        assert len(store.denied_calls) == 1
        gym_id, row_id, reason = store.denied_calls[0]
        assert gym_id == "lasso"
        assert row_id == "id2"
        assert reason == "duplicate_purge_2026_08"
        assert result["duplicates_denied"] == 1
        assert result["errors"] == 0

    def test_reject_reason_is_duplicate_purge_2026_08(self):
        rows = [
            _row("id1", "2026-09-02", "Repeated"),
            _row("id2", "2026-09-03", "Repeated"),
            _row("id3", "2026-09-04", "Repeated"),
        ]
        result, store = self._run(rows, dry_run=False)
        for _gym, _row_id, reason in store.denied_calls:
            assert reason == "duplicate_purge_2026_08"

    def test_no_duplicates_makes_no_writes(self):
        rows = [
            _row("id1", "2026-09-02", "Caption A"),
            _row("id2", "2026-09-03", "Caption B"),
        ]
        result, store = self._run(rows, dry_run=False)
        assert store.denied_calls == []
        assert result["duplicates_found"] == 0
        assert result["duplicates_denied"] == 0

    def test_error_in_deny_counted(self):
        rows = [
            _row("id1", "2026-09-02", "Same caption"),
            _row("id2", "2026-09-03", "Same caption"),
        ]
        result, store = self._run(rows, dry_run=False, deny_error=True)
        assert result["errors"] == 1
        assert result["duplicates_denied"] == 0

    def test_result_counts_total_pending(self):
        rows = [_row(f"id{i}", f"2026-09-0{i+1}", f"Caption {i}") for i in range(5)]
        result, _ = self._run(rows, dry_run=True)
        assert result["total_pending"] == 5

    def test_gym_id_scoped_correctly(self):
        # Store has rows for two gyms; only 'lasso' rows should be returned by the fake.
        rows = [
            _row("id1", "2026-09-02", "Same", gym_id="lasso"),
            _row("id2", "2026-09-03", "Same", gym_id="lasso"),
            _row("id3", "2026-09-02", "Same", gym_id="other_gym"),
        ]
        store = FakeStore(pending_rows=rows)
        result = dedupe_gym("lasso", store, "2026-09-01", dry_run=True)
        # Only the two 'lasso' rows are considered.
        assert result["total_pending"] == 2

    def test_multiple_dupe_groups(self):
        rows = [
            _row("id1", "2026-09-02", "Caption A"),
            _row("id2", "2026-09-03", "Caption A"),
            _row("id3", "2026-09-02", "Caption B"),
            _row("id4", "2026-09-03", "Caption B"),
            _row("id5", "2026-09-04", "Caption C"),  # unique
        ]
        result, store = self._run(rows, dry_run=False)
        # Two groups of two: one dupe per group = 2 denied.
        assert result["duplicates_found"] == 2
        assert result["duplicates_denied"] == 2
        assert result["errors"] == 0


# ---------------------------------------------------------------------------
# run() tests — flag behaviour
# ---------------------------------------------------------------------------

class TestRunFunction:
    def test_flag_off_forces_dry_run(self, monkeypatch):
        """When AGENT_DEDUPE_FORWARD_BOOK is not set (OFF), run() must not write."""
        monkeypatch.delenv("AGENT_DEDUPE_FORWARD_BOOK", raising=False)
        rows = [
            _row("id1", "2026-09-02", "Dup caption", gym_id="lasso"),
            _row("id2", "2026-09-03", "Dup caption", gym_id="lasso"),
        ]
        store = FakeStore(pending_rows=rows)
        # Suppress ops_alerts posting (no Slack token in tests).
        monkeypatch.setattr("agent.ops_alerts.alert", lambda *a, **kw: None)
        results = run(gym_ids=["lasso"], store=store, dry_run=False)
        # Even though dry_run=False was passed, the missing flag should force dry-run.
        assert store.denied_calls == [], "flag OFF must prevent writes"
        assert results[0]["duplicates_found"] == 1

    def test_flag_on_dry_run_false_allows_writes(self, monkeypatch):
        """When flag is ON and dry_run=False, real writes happen."""
        monkeypatch.setenv("AGENT_DEDUPE_FORWARD_BOOK", "true")
        rows = [
            _row("id1", "2026-09-02", "Dup", gym_id="lasso"),
            _row("id2", "2026-09-03", "Dup", gym_id="lasso"),
        ]
        store = FakeStore(pending_rows=rows)
        monkeypatch.setattr("agent.ops_alerts.alert", lambda *a, **kw: None)
        results = run(gym_ids=["lasso"], store=store, dry_run=False)
        assert len(store.denied_calls) == 1
        assert results[0]["duplicates_denied"] == 1

    def test_flag_on_dry_run_true_no_writes(self, monkeypatch):
        """When flag is ON but dry_run=True, no writes happen."""
        monkeypatch.setenv("AGENT_DEDUPE_FORWARD_BOOK", "true")
        rows = [
            _row("id1", "2026-09-02", "Dup", gym_id="lasso"),
            _row("id2", "2026-09-03", "Dup", gym_id="lasso"),
        ]
        store = FakeStore(pending_rows=rows)
        monkeypatch.setattr("agent.ops_alerts.alert", lambda *a, **kw: None)
        results = run(gym_ids=["lasso"], store=store, dry_run=True)
        assert store.denied_calls == []
        assert results[0]["duplicates_denied"] == 0

    def test_multiple_gyms_processed(self, monkeypatch):
        monkeypatch.setenv("AGENT_DEDUPE_FORWARD_BOOK", "true")
        rows = [
            _row("id1", "2026-09-02", "Same", gym_id="gym_a"),
            _row("id2", "2026-09-03", "Same", gym_id="gym_a"),
            _row("id3", "2026-09-02", "Same", gym_id="gym_b"),
            _row("id4", "2026-09-03", "Same", gym_id="gym_b"),
        ]
        store = FakeStore(pending_rows=rows)
        monkeypatch.setattr("agent.ops_alerts.alert", lambda *a, **kw: None)
        results = run(gym_ids=["gym_a", "gym_b"], store=store, dry_run=False)
        assert len(results) == 2
        assert results[0]["gym_id"] == "gym_a"
        assert results[1]["gym_id"] == "gym_b"
        assert results[0]["duplicates_denied"] == 1
        assert results[1]["duplicates_denied"] == 1

    def test_returns_result_list(self, monkeypatch):
        monkeypatch.delenv("AGENT_DEDUPE_FORWARD_BOOK", raising=False)
        store = FakeStore(pending_rows=[])
        monkeypatch.setattr("agent.ops_alerts.alert", lambda *a, **kw: None)
        results = run(gym_ids=["lasso"], store=store)
        assert isinstance(results, list)
        assert len(results) == 1
        assert "gym_id" in results[0]
        assert "total_pending" in results[0]
        assert "duplicates_found" in results[0]
        assert "duplicates_denied" in results[0]
        assert "errors" in results[0]


class _XStore:
    """dedupe_gym store fake: pending-future + the published window read."""

    def __init__(self, pending, window):
        self._pending = pending
        self._window = window
        self.denied = []

    def list_pending_future(self, gym_id, today_iso):
        return [dict(r) for r in self._pending]

    def rows_in_range(self, gym_id, start, end):
        return [dict(r) for r in self._window]

    def deny_with_reason(self, gym_id, row_id, reason):
        self.denied.append((row_id, reason))
        return {"id": row_id}


def test_pending_row_matching_a_published_caption_is_denied():
    """2026-08-31: the grader counts published+pending caption repeats, so the cleaner
    must too — a pending row whose caption already went out would show the audience the
    same post twice. Stories and empty captions stay excluded."""
    from agent.jobs.dedupe_forward_book import dedupe_gym
    pending = [
        _row("p1", "2026-09-05", "Join our community and win"),
        dict(_row("p2", "2026-09-06", "Join our community and win"), format="story"),
        _row("p3", "2026-09-07", "A totally fresh caption"),
    ]
    window = pending + [
        dict(_row("pub1", "2026-08-20", "Join our community and win"),
             status="published"),
    ]
    store = _XStore(pending, window)
    out = dedupe_gym("lasso", store, "2026-08-31", dry_run=False)
    assert out["duplicates_found"] == 1
    assert [d[0] for d in store.denied] == ["p1"]     # story + fresh caption untouched
