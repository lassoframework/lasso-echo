"""
Per-account category frequency and consecutive-day cap.

Two guards, both checked BEFORE any campaign builder fires and both configurable
via env vars:

  FREQUENCY:   a category may post at most once every N days per account.
               every_n_days=1 means uncapped (today's default).
               Controlled per-builder via the caller; the book builder uses
               AGENT_BOOK_CAMPAIGN_EVERY_N_DAYS (default 3).

  CONSECUTIVE: no single campaign category posts more than X days in a row.
               max_consecutive=0 means no cap. Default: AGENT_CATEGORY_MAX_CONSECUTIVE=2.

record_win() must be called once per day per account, AFTER the winning
category is decided. It is idempotent: a second call for the same day_key
replaces the previous entry.

is_allowed() is called BEFORE the builder runs. If it returns False the
builder is skipped and the priority chain continues.

The fallback (rotation / plain feed draft) is never gated — it always runs
when all campaign categories are capped, ensuring the daily slot is never
empty.
"""

import json

from . import db

_HISTORY_KEY = "cat_history:{}"
_HISTORY_MAX = 60  # rolling window; never grows unbounded


def _key(account_key: str) -> str:
    return _HISTORY_KEY.format(account_key)


def _load(account_key: str) -> list:
    """[[day_key, category], ...] oldest first. Empty list on any read failure."""
    try:
        return json.loads(db.kv_get(_key(account_key), "[]") or "[]")
    except Exception:
        return []


def _save(account_key: str, history: list) -> None:
    try:
        db.kv_set(_key(account_key), json.dumps(history[-_HISTORY_MAX:]))
    except Exception as e:
        print(f"[category_cap] could not save history: {type(e).__name__}: {e}")


def record_win(account_key: str, category: str, day_key: str) -> None:
    """Record that `category` won the daily slot for `account_key` on `day_key`.
    Idempotent: a second call for the same day_key replaces the entry."""
    history = _load(account_key)
    history = [e for e in history if e[0] != day_key]
    history.append([day_key, category])
    history.sort(key=lambda e: e[0])
    _save(account_key, history)


def is_allowed(
    account_key: str,
    category: str,
    day_key: str,
    *,
    every_n_days: int = 1,
    max_consecutive: int = 0,
) -> bool:
    """
    True when `category` is allowed to run for `account_key` on `day_key`.

    every_n_days=1  — uncapped (runs every day if eligible).
    every_n_days=3  — at most once every 3 days; a post on day D blocks
                      the category on days D+1 and D+2.
    max_consecutive=0 — no consecutive cap.
    max_consecutive=2 — blocked after 2 consecutive days of the same category.
    """
    history = _load(account_key)
    past = [e for e in history if e[0] < day_key]  # entries strictly before today

    # Frequency cap: must not have run in the last (every_n_days - 1) days
    if every_n_days > 1:
        from datetime import date, timedelta
        cutoff = (
            date.fromisoformat(day_key) - timedelta(days=every_n_days - 1)
        ).isoformat()
        recent_cats = [e[1] for e in past if e[0] >= cutoff]
        if category in recent_cats:
            return False

    # Consecutive cap: count the unbroken tail of this category before today
    if max_consecutive > 0:
        tail = 0
        for _d, cat in sorted(past, key=lambda e: e[0], reverse=True):
            if cat == category:
                tail += 1
            else:
                break
        if tail >= max_consecutive:
            return False

    return True


def get_history(account_key: str) -> list:
    """Read-only view for tests and debugging."""
    return _load(account_key)
