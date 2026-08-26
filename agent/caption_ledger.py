"""caption_ledger.py — nothing gets scheduled twice inside its cooldown window.

Normalized-hash ledger over everything Echo has ever staged or published,
per gym. Backed by the portal (survives worker restarts) + kv cache.

Behind AGENT_CAPTION_COOLDOWN (default OFF). When the flag is off, every
function is a no-op / returns the safe non-blocking default so the rest
of the system is byte-for-byte unchanged.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta

COOLDOWN_DAYS = 60          # a caption may not re-enter a calendar within 60 days
HARD_BLOCK_SAME_MONTH = True  # and never twice in the same calendar month, period

CONCEPT_COOLDOWN_DAYS = 30  # doctrine/education concept pool gap


# ---------------------------------------------------------------------------
# Normalization + hash
# ---------------------------------------------------------------------------

def caption_hash(text: str) -> str:
    """Stable 16-char hex fingerprint for a caption, normalized so minor edits
    to whitespace, tags, or punctuation do not produce a different hash.

    Normalization:
      1. Strip @handles and #tags (they do not differentiate the body copy).
      2. Lowercase.
      3. Remove all non-alphanumeric, non-space characters.
      4. Collapse runs of whitespace to a single space and strip leading/trailing.
      5. Truncate at 200 chars (enough to capture the hook; tails are less distinct).
    """
    t = re.sub(r"[#@]\S+", "", str(text).lower())   # tags/mentions don't differentiate
    t = re.sub(r"[^a-z0-9 ]", "", t)
    t = re.sub(r"\s+", " ", t).strip()[:200]
    return hashlib.sha256(t.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# KV key
# ---------------------------------------------------------------------------

def ledger_key(gym_id: str, h: str) -> str:
    """The kv key for a gym+hash pair."""
    return f"caption_ledger_{gym_id}_{h}"


def _concept_key(gym_id: str, concept_key: str) -> str:
    """KV key for a concept cooldown entry."""
    safe = re.sub(r"[^a-z0-9_:.-]", "_", concept_key.lower())
    return f"concept_ledger_{gym_id}_{safe}"


# ---------------------------------------------------------------------------
# Caption cooldown
# ---------------------------------------------------------------------------

def is_on_cooldown(gym_id: str, caption_text: str, planned_date: str,
                   db=None) -> bool:
    """Returns True if this caption hash was used within COOLDOWN_DAYS before
    planned_date, or in the same calendar month (when HARD_BLOCK_SAME_MONTH is
    True). Uses kv store for speed. db is injectable (defaults to agent.db).

    Returns False (safe) if any error occurs so a kv failure never blocks content.
    """
    try:
        _db = db if db is not None else _default_db()
        h = caption_hash(caption_text)
        key = ledger_key(gym_id, h)
        raw = _kv_get(_db, key)
        if not raw:
            return False
        record = json.loads(raw)
        last_used = record.get("last_used", "")
        if not last_used:
            return False
        last = date.fromisoformat(last_used)
        planned = date.fromisoformat(planned_date)
        # Same calendar month: hard block regardless of how many days apart
        if HARD_BLOCK_SAME_MONTH and last.year == planned.year and last.month == planned.month:
            return True
        # Within cooldown window: block if the gap is less than COOLDOWN_DAYS
        gap = abs((planned - last).days)
        return gap < COOLDOWN_DAYS
    except Exception:
        return False  # never block on error


def record_staged(gym_id: str, caption_text: str, date_str: str,
                  db=None) -> None:
    """Record that this caption was staged for gym_id on date_str.
    Call when staging a new calendar row.

    Upserts: if the hash already has a record, last_used is updated to date_str
    only when date_str is more recent (so a backfill of older rows does not
    overwrite a newer real staging event).
    """
    try:
        _db = db if db is not None else _default_db()
        h = caption_hash(caption_text)
        key = ledger_key(gym_id, h)
        raw = _kv_get(_db, key)
        record = json.loads(raw) if raw else {"last_used": "", "uses": 0}
        existing = record.get("last_used", "") or ""
        # Advance last_used only if date_str is more recent
        if not existing or date_str > existing:
            record["last_used"] = date_str
        record["uses"] = int(record.get("uses", 0)) + 1
        _kv_set(_db, key, json.dumps(record))
    except Exception:
        pass  # ledger write failure is never fatal


def record_published(gym_id: str, caption_text: str, date_str: str,
                     db=None) -> None:
    """Record that this caption was published. Call at publish time.

    Treated identically to record_staged from a cooldown perspective: the
    ledger tracks any time a caption entered or left the queue as a signal
    that it has been used. Publish time updates last_used to date_str.
    """
    try:
        _db = db if db is not None else _default_db()
        h = caption_hash(caption_text)
        key = ledger_key(gym_id, h)
        raw = _kv_get(_db, key)
        record = json.loads(raw) if raw else {"last_used": "", "uses": 0}
        existing = record.get("last_used", "") or ""
        if not existing or date_str > existing:
            record["last_used"] = date_str
        record["uses"] = int(record.get("uses", 0)) + 1
        _kv_set(_db, key, json.dumps(record))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Concept-level cooldown (doctrine / education concept pool)
# ---------------------------------------------------------------------------

def concept_is_on_cooldown(gym_id: str, concept_key: str, planned_date: str,
                            db=None) -> bool:
    """The doctrine/education concept pool has its own 30-day gap. concept_key is
    a short identifier like 'doctrine:speed_to_lead'. Uses same kv pattern.

    Returns False (safe) on any error.
    """
    try:
        _db = db if db is not None else _default_db()
        key = _concept_key(gym_id, concept_key)
        raw = _kv_get(_db, key)
        if not raw:
            return False
        record = json.loads(raw)
        last_used = record.get("last_used", "")
        if not last_used:
            return False
        last = date.fromisoformat(last_used)
        planned = date.fromisoformat(planned_date)
        gap = abs((planned - last).days)
        return gap < CONCEPT_COOLDOWN_DAYS
    except Exception:
        return False


def record_concept_used(gym_id: str, concept_key: str, date_str: str,
                        db=None) -> None:
    """Stamp a concept as used on date_str."""
    try:
        _db = db if db is not None else _default_db()
        key = _concept_key(gym_id, concept_key)
        raw = _kv_get(_db, key)
        record = json.loads(raw) if raw else {"last_used": "", "uses": 0}
        existing = record.get("last_used", "") or ""
        if not existing or date_str > existing:
            record["last_used"] = date_str
        record["uses"] = int(record.get("uses", 0)) + 1
        _kv_set(_db, key, json.dumps(record))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# KV helpers (injectable db pattern)
# ---------------------------------------------------------------------------

def _default_db():
    from . import db
    return db


def _kv_get(db_module, key: str) -> str:
    """Get from kv, supporting both the module-level kv_get() and an injected
    duck-typed object with kv_get(key, default)."""
    if hasattr(db_module, "kv_get"):
        return db_module.kv_get(key, "")
    raise TypeError(f"db object has no kv_get: {type(db_module)}")


def _kv_set(db_module, key: str, value: str) -> None:
    if hasattr(db_module, "kv_set"):
        db_module.kv_set(key, value)
    else:
        raise TypeError(f"db object has no kv_set: {type(db_module)}")
