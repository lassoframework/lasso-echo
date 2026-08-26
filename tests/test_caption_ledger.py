"""
tests/test_caption_ledger.py — Wave 3 caption ledger unit tests.

All 8 required tests from the spec, plus a concept cooldown round-trip.
Every test is offline: no Supabase, no SQLite on disk; a fake kv-duck-type
is injected so the ledger never touches the real db.
"""
from __future__ import annotations

import json
import pytest

from agent.caption_ledger import (
    COOLDOWN_DAYS,
    CONCEPT_COOLDOWN_DAYS,
    HARD_BLOCK_SAME_MONTH,
    caption_hash,
    concept_is_on_cooldown,
    is_on_cooldown,
    ledger_key,
    record_concept_used,
    record_staged,
)


# ---------------------------------------------------------------------------
# Minimal fake kv store (duck-types agent.db)
# ---------------------------------------------------------------------------

class _FakeDB:
    """In-memory kv store that satisfies caption_ledger's _kv_get/_kv_set."""

    def __init__(self, initial=None):
        self._store: dict[str, str] = dict(initial or {})

    def kv_get(self, key: str, default: str = "") -> str:
        return self._store.get(key, default)

    def kv_set(self, key: str, value: str) -> None:
        self._store[key] = str(value)


# ---------------------------------------------------------------------------
# 1. caption_hash normalizes correctly
# ---------------------------------------------------------------------------

def test_caption_hash_normalizes_tags_and_punct():
    """Spec test 1: @coach #crossfit Ready, set, GO! hashes same as 'ready set go'."""
    tagged = "@coach #crossfit Ready, set, GO!"
    plain = "ready set go"
    assert caption_hash(tagged) == caption_hash(plain)


# ---------------------------------------------------------------------------
# 2. Two captions that differ only in whitespace get same hash
# ---------------------------------------------------------------------------

def test_caption_hash_whitespace_invariant():
    """Spec test 2: whitespace differences produce the same hash."""
    a = "  join  us   today  "
    b = "join us today"
    assert caption_hash(a) == caption_hash(b)


# ---------------------------------------------------------------------------
# 3. is_on_cooldown returns False for a brand-new caption
# ---------------------------------------------------------------------------

def test_is_on_cooldown_false_for_new_caption():
    """Spec test 3: caption not in kv -> not on cooldown."""
    db = _FakeDB()
    result = is_on_cooldown("gym1", "Brand new caption text here", "2026-09-01", db=db)
    assert result is False


# ---------------------------------------------------------------------------
# 4. is_on_cooldown returns True when last_used is 30 days ago (within 60-day cooldown)
# ---------------------------------------------------------------------------

def test_is_on_cooldown_true_within_cooldown_window():
    """Spec test 4: last_used 30 days ago -> within 60-day window -> True."""
    db = _FakeDB()
    gym_id = "gym1"
    text = "Our coaches are ready to help you reach your goals"
    h = caption_hash(text)
    key = ledger_key(gym_id, h)
    # seed: last used 30 days before planned date
    db.kv_set(key, json.dumps({"last_used": "2026-08-01", "uses": 1}))
    # planned date 30 days later
    result = is_on_cooldown(gym_id, text, "2026-08-31", db=db)
    assert result is True


# ---------------------------------------------------------------------------
# 5. is_on_cooldown returns False when last_used is 61 days ago
# ---------------------------------------------------------------------------

def test_is_on_cooldown_false_outside_cooldown_window():
    """Spec test 5: last_used 61 days ago -> outside 60-day window -> False."""
    db = _FakeDB()
    gym_id = "gym1"
    text = "Our coaches are ready to help you reach your goals"
    h = caption_hash(text)
    key = ledger_key(gym_id, h)
    db.kv_set(key, json.dumps({"last_used": "2026-07-01", "uses": 1}))
    # planned 61 days later
    result = is_on_cooldown(gym_id, text, "2026-08-31", db=db)
    assert result is False


# ---------------------------------------------------------------------------
# 6. HARD_BLOCK_SAME_MONTH: same month blocks even if only 5 days apart
# ---------------------------------------------------------------------------

def test_is_on_cooldown_same_month_hard_block():
    """Spec test 6: same calendar month -> True regardless of day gap."""
    assert HARD_BLOCK_SAME_MONTH, "HARD_BLOCK_SAME_MONTH must be True for this test"
    db = _FakeDB()
    gym_id = "gym2"
    text = "Transform your body and your mindset"
    h = caption_hash(text)
    key = ledger_key(gym_id, h)
    # last_used early in the month, planned date 5 days later — same month
    db.kv_set(key, json.dumps({"last_used": "2026-09-03", "uses": 1}))
    result = is_on_cooldown(gym_id, text, "2026-09-08", db=db)
    assert result is True


# ---------------------------------------------------------------------------
# 7. record_staged / is_on_cooldown round-trip
# ---------------------------------------------------------------------------

def test_record_staged_then_is_on_cooldown_returns_true():
    """Spec test 7: after record_staged, is_on_cooldown returns True for next day."""
    db = _FakeDB()
    gym_id = "gym3"
    text = "Stop waiting, start training"
    staged_date = "2026-09-10"
    record_staged(gym_id, text, staged_date, db=db)
    # next day is still within the 60-day cooldown AND same month
    result = is_on_cooldown(gym_id, text, "2026-09-11", db=db)
    assert result is True


# ---------------------------------------------------------------------------
# 8. concept_is_on_cooldown: blocks within 30 days, allows after 30 days
# ---------------------------------------------------------------------------

def test_concept_cooldown_blocks_within_30_days_allows_after():
    """Spec test 8: concept cooldown round-trip."""
    assert CONCEPT_COOLDOWN_DAYS == 30
    db = _FakeDB()
    gym_id = "gym4"
    concept = "doctrine:speed_to_lead"

    # Before recording: no block
    assert concept_is_on_cooldown(gym_id, concept, "2026-09-01", db=db) is False

    # Record on Sep 1
    record_concept_used(gym_id, concept, "2026-09-01", db=db)

    # 29 days later (Sep 30) -> still blocked
    assert concept_is_on_cooldown(gym_id, concept, "2026-09-30", db=db) is True

    # 30 days later (Oct 1) -> gap == 30 which is NOT < 30, so False
    assert concept_is_on_cooldown(gym_id, concept, "2026-10-01", db=db) is False

    # 31 days later -> also False
    assert concept_is_on_cooldown(gym_id, concept, "2026-10-02", db=db) is False


# ---------------------------------------------------------------------------
# Extra: record_staged increments uses counter
# ---------------------------------------------------------------------------

def test_record_staged_increments_uses():
    db = _FakeDB()
    gym_id = "gym5"
    text = "Ready to commit to your health"
    record_staged(gym_id, text, "2026-08-01", db=db)
    record_staged(gym_id, text, "2026-10-01", db=db)  # later date advances last_used
    h = caption_hash(text)
    key = ledger_key(gym_id, h)
    record = json.loads(db.kv_get(key))
    assert record["uses"] == 2
    assert record["last_used"] == "2026-10-01"


# ---------------------------------------------------------------------------
# Extra: ledger_key is gym-scoped
# ---------------------------------------------------------------------------

def test_ledger_key_is_gym_scoped():
    h = caption_hash("hello world")
    assert "gym1" in ledger_key("gym1", h)
    assert ledger_key("gym1", h) != ledger_key("gym2", h)


# ---------------------------------------------------------------------------
# Extra: errors in kv never block content
# ---------------------------------------------------------------------------

class _BrokenDB:
    def kv_get(self, key, default=""):
        raise RuntimeError("disk full")

    def kv_set(self, key, value):
        raise RuntimeError("disk full")


def test_is_on_cooldown_returns_false_on_kv_error():
    """A broken kv store must never block content scheduling."""
    db = _BrokenDB()
    result = is_on_cooldown("gym1", "some caption", "2026-09-01", db=db)
    assert result is False


def test_record_staged_silently_passes_on_kv_error():
    """A broken kv store must not raise from record_staged."""
    db = _BrokenDB()
    record_staged("gym1", "some caption", "2026-09-01", db=db)  # must not raise
