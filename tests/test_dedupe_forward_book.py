"""
tests/test_dedupe_forward_book.py
==================================
Wave 0 — dedupe_forward_book unit tests.

Covers:
  1. caption_hash normalizes correctly (tags stripped, case folded, punct removed).
  2. dry-run does not issue any writes.
  3. When 3 rows share a hash, 2 become denied and 1 (earliest by post_date) survives.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.jobs.dedupe_forward_book import caption_hash, _dedupe_gym  # noqa: E402


# ---------------------------------------------------------------------------
# 1. caption_hash normalisation
# ---------------------------------------------------------------------------

class TestCaptionHash:
    def test_basic_hash_is_16_chars_hex(self):
        h = caption_hash("Hello world")
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_case_insensitive(self):
        assert caption_hash("Hello World") == caption_hash("hello world")
        assert caption_hash("UPPER CASE TEXT") == caption_hash("upper case text")

    def test_hashtags_stripped(self):
        a = caption_hash("Great class today #fitness #crossfit")
        b = caption_hash("Great class today")
        assert a == b

    def test_at_mentions_stripped(self):
        a = caption_hash("Tag us @lassoframework when you show up")
        b = caption_hash("Tag us  when you show up")
        assert a == b

    def test_punctuation_removed(self):
        a = caption_hash("Ready. Set. Go! Your best self is waiting...")
        b = caption_hash("ready set go your best self is waiting")
        assert a == b

    def test_whitespace_collapsed(self):
        a = caption_hash("  lots   of   spaces  ")
        b = caption_hash("lots of spaces")
        assert a == b

    def test_emojis_and_unicode_stripped(self):
        # Non-[a-z0-9 ] chars are removed, so emojis vanish
        a = caption_hash("Join us \U0001f4aa for the best workout")
        b = caption_hash("join us  for the best workout")
        assert a == b

    def test_truncation_at_200_chars(self):
        # Two strings that differ only after position 200 should hash the same
        base = "a" * 200
        long_a = base + "extra stuff here"
        long_b = base + "completely different"
        assert caption_hash(long_a) == caption_hash(long_b)

    def test_none_input_doesnt_crash(self):
        # caption can be None in the DB; we coerce with str()
        h = caption_hash(None)  # type: ignore[arg-type]
        assert len(h) == 16

    def test_empty_string(self):
        h = caption_hash("")
        assert len(h) == 16

    def test_only_tags_gives_stable_hash(self):
        # After stripping all tags you have empty string — stable, not an error
        a = caption_hash("#hashtag1 #hashtag2 @user")
        b = caption_hash("#different #tags @other")
        # Both reduce to empty => same hash
        assert a == b

    def test_different_captions_give_different_hashes(self):
        a = caption_hash("You deserve the best training in town")
        b = caption_hash("We help busy parents get strong without sacrificing family time")
        assert a != b


# ---------------------------------------------------------------------------
# Fake store + HTTP layer
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = ""

    def json(self):
        return self._payload


class _FakeHTTP:
    """
    Captures GET and PATCH calls.  GET returns the pre-loaded rows list.
    PATCH is recorded but returns 200 with the patched row.
    """
    def __init__(self, rows=None):
        self.get_calls = []
        self.patch_calls = []
        self._rows = rows or []

    def get(self, url, params=None, headers=None, timeout=None):
        self.get_calls.append({"url": url, "params": params or {}})
        return _FakeResp(200, list(self._rows))

    def patch(self, url, params=None, headers=None, json=None, timeout=None):
        self.patch_calls.append({"url": url, "params": params or {}, "json": json})
        # Return the row that was patched (simplified)
        return _FakeResp(200, [{"id": (params or {}).get("id", "").replace("eq.", "")}])


class _FakeStore:
    """
    Minimal stand-in for SupabaseCalendarStore.
    Implements only the interface _dedupe_gym touches.
    """
    def __init__(self, rows=None):
        self._http = _FakeHTTP(rows=rows)
        self._url = "https://fake.supabase.co"
        self._key = "fake-key"
        self.set_status_calls = []
        self.patch_reason_calls = []

    def _client(self):
        return self._http

    def _headers(self, extra=None):
        h = {"apikey": self._key}
        if extra:
            h.update(extra)
        return h

    def _rest(self, table):
        return f"{self._url}/rest/v1/{table}"

    def set_status(self, account_key, row_id, new_status):
        self.set_status_calls.append((account_key, row_id, new_status))
        return {"id": row_id, "status": new_status}


def _row(row_id, post_date, caption, gym_id="lasso", status="pending"):
    return {
        "id": row_id,
        "gym_id": gym_id,
        "post_date": post_date,
        "caption": caption,
        "status": status,
    }


# ---------------------------------------------------------------------------
# 2. dry-run does not write
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_issues_no_writes(self, monkeypatch):
        """dry_run=True must never call set_status or issue a PATCH."""
        caption = "You deserve to feel strong and energized every single day"
        rows = [
            _row("id-1", "2026-09-01", caption),
            _row("id-2", "2026-09-02", caption),
            _row("id-3", "2026-09-03", caption),
        ]
        store = _FakeStore(rows=rows)

        result = _dedupe_gym(store, "lasso", "2026-08-26", dry_run=True)

        # No set_status calls
        assert store.set_status_calls == [], (
            "dry-run must not call set_status"
        )
        # No PATCH calls from the HTTP layer
        assert store._http.patch_calls == [], (
            "dry-run must not issue any PATCH"
        )
        # But the result should still report what WOULD have been denied
        assert result["rows_denied"] == 2
        assert result["duplicate_groups"] == 1

    def test_dry_run_with_no_duplicates_is_silent(self, monkeypatch):
        rows = [
            _row("id-1", "2026-09-01", "First unique post about accountability"),
            _row("id-2", "2026-09-02", "Second unique post about community strength"),
        ]
        store = _FakeStore(rows=rows)

        result = _dedupe_gym(store, "lasso", "2026-08-26", dry_run=True)

        assert store.set_status_calls == []
        assert result["rows_denied"] == 0
        assert result["duplicate_groups"] == 0


# ---------------------------------------------------------------------------
# 3. 3 rows same hash -> 2 denied, earliest survives
# ---------------------------------------------------------------------------

class TestDedupeLogic:
    def test_three_rows_same_hash_denies_two_keeps_earliest(self, monkeypatch):
        """
        When 3 rows share the same caption hash:
          - The row with the EARLIEST post_date is kept (not denied).
          - The 2 later rows are denied.
        """
        caption = "We help busy moms get strong without giving up family time"
        rows = [
            _row("id-early",  "2026-09-01", caption),
            _row("id-middle", "2026-09-10", caption),
            _row("id-late",   "2026-09-20", caption),
        ]
        store = _FakeStore(rows=rows)

        result = _dedupe_gym(store, "lasso", "2026-08-26", dry_run=False)

        assert result["rows_denied"] == 2
        assert result["duplicate_groups"] == 1
        assert result["total_pending"] == 3

        denied_ids = [call[1] for call in store.set_status_calls]
        assert "id-early" not in denied_ids, "earliest row must survive"
        assert "id-middle" in denied_ids
        assert "id-late" in denied_ids
        assert all(call[2] == "denied" for call in store.set_status_calls)

    def test_earliest_post_date_is_kept_regardless_of_insertion_order(self, monkeypatch):
        """Rows arrive in reverse chronological order; earliest still survives."""
        caption = "The coach sees what you cannot see in your own form and fixes it"
        rows = [
            _row("id-late",   "2026-10-15", caption),  # listed first, latest date
            _row("id-early",  "2026-09-01", caption),  # listed last, earliest date
            _row("id-middle", "2026-09-30", caption),
        ]
        store = _FakeStore(rows=rows)

        result = _dedupe_gym(store, "lasso", "2026-08-26", dry_run=False)

        denied_ids = [call[1] for call in store.set_status_calls]
        assert "id-early" not in denied_ids
        assert "id-late" in denied_ids
        assert "id-middle" in denied_ids

    def test_unique_captions_are_untouched(self, monkeypatch):
        rows = [
            _row("id-a", "2026-09-01", "Unique caption about accountability partners"),
            _row("id-b", "2026-09-02", "Unique caption about community and belonging"),
            _row("id-c", "2026-09-03", "Unique caption about momentum over motivation"),
        ]
        store = _FakeStore(rows=rows)

        result = _dedupe_gym(store, "lasso", "2026-08-26", dry_run=False)

        assert result["rows_denied"] == 0
        assert result["duplicate_groups"] == 0
        assert store.set_status_calls == []

    def test_two_groups_of_duplicates(self, monkeypatch):
        """Two separate duplicate groups are independently resolved."""
        cap_a = "You showed up. That is where the transformation begins every time."
        cap_b = "Small group coaching means every rep gets expert eyes on your form."
        rows = [
            _row("a1", "2026-09-01", cap_a),
            _row("a2", "2026-09-08", cap_a),
            _row("b1", "2026-09-02", cap_b),
            _row("b2", "2026-09-09", cap_b),
            _row("b3", "2026-09-16", cap_b),
        ]
        store = _FakeStore(rows=rows)

        result = _dedupe_gym(store, "lasso", "2026-08-26", dry_run=False)

        assert result["duplicate_groups"] == 2
        assert result["rows_denied"] == 3  # 1 from group_a + 2 from group_b

        denied_ids = [call[1] for call in store.set_status_calls]
        assert "a1" not in denied_ids  # earliest of group_a
        assert "a2" in denied_ids
        assert "b1" not in denied_ids  # earliest of group_b
        assert "b2" in denied_ids
        assert "b3" in denied_ids

    def test_empty_gym_returns_zeros(self, monkeypatch):
        store = _FakeStore(rows=[])
        result = _dedupe_gym(store, "lasso", "2026-08-26", dry_run=False)

        assert result["total_pending"] == 0
        assert result["rows_denied"] == 0
        assert result["duplicate_groups"] == 0

    def test_hashtag_variants_treated_as_same_caption(self, monkeypatch):
        """Same body text with different hashtag sets should hash identically."""
        base = "Come find out why our members never miss a Monday"
        cap_a = base + " #fitness #gym #goals"
        cap_b = base + " #strongertogether #crossfit"
        rows = [
            _row("id-1", "2026-09-01", cap_a),
            _row("id-2", "2026-09-08", cap_b),
        ]
        store = _FakeStore(rows=rows)

        result = _dedupe_gym(store, "lasso", "2026-08-26", dry_run=False)

        assert result["duplicate_groups"] == 1
        assert result["rows_denied"] == 1
        denied_ids = [call[1] for call in store.set_status_calls]
        assert "id-1" not in denied_ids
        assert "id-2" in denied_ids
