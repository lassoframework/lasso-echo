"""
Regression: "scheduled draft run produced no cards - KeyError: 'draft_id'"

Root cause: store.list_pending() builds a list via _from_dict() which does
r["draft_id"] (strict dict access, store.py:47). A PENDING row whose data JSON
was migrated from legacy pending_drafts.json without "draft_id" as a field
(because draft_id was the dict KEY, not a field in the old format) raises
KeyError in the list comprehension.

Call chain: run_daily -> expire_past_due -> store.list_pending() -> _from_dict()
                                                                    -> r["draft_id"]  <-- crash

expire_past_due is called at runner.py:227, BEFORE the per-account try/except,
so the KeyError propagates directly out of run_daily to _fire_daily, which
formats it as the prod alert.

Fix: rescue draft_id from the drafts.draft_id column when the data JSON
is missing it (store.py _from_dict consumers select draft_id alongside data).
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.accounts import Account, Platform
from agent.runner import run_daily
from agent.store import PendingStore

DAY = "2026-07-10"

_VOICE = """# Voice
We help gym owners grow.
## CTAs
- Save this post.
## Hashtags
#LASSOFramework
"""


class _FakePoster:
    def post_approval_card(self, draft):
        return {"channel": "C1", "ts": "ts1"}

    def post_notice(self, text):
        return {"ok": True}

    def mark_superseded(self, draft):
        pass

    def mark_expired(self, draft):
        pass


def _seed_legacy_row(db_path, draft_id, account_key, day_key):
    """Insert a PENDING row whose data JSON has NO 'draft_id' key — exactly
    the shape produced by migrating old pending_drafts.json where draft_id
    was the outer key, not a field in the value dict."""
    from agent import db

    data = {
        # intentionally NO "draft_id" key — this is the legacy migration shape
        "account_key": account_key,
        "platform": "instagram",
        "caption": "legacy caption",
        "hashtags": [],
        "creative_path": "",
        "creative_public_url": "",
        "scheduled_for": f"{day_key}T06:00:00+00:00",
        "status": "pending",
        "blocked_reason": "",
        "source_fragments": [],
        "slides": [],
        "slide_urls": [],
        "is_story": False,
        "day_key": day_key,
        "draft_type": "feed",
        "slack_channel": "",
        "slack_ts": "",
    }
    assert "draft_id" not in data, "test setup error: this row must lack draft_id"
    with db.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO drafts "
            "(draft_id, account_key, status, day_key, draft_type, data) "
            "VALUES (?,?,?,?,?,?)",
            (draft_id, account_key, "pending", day_key, "feed", json.dumps(data)))
        conn.commit()


def _lasso_account():
    return Account(key="lasso_ig", display_name="LASSO IG",
                   platform=Platform.INSTAGRAM,
                   token_env="DUMMY_TOK", target_id_env="DUMMY_TGT")


# ---------------------------------------------------------------------------
# Part 1: reproduction tests — confirm the crash is FIXED
# (Before the fix, list_pending and run_daily both raised KeyError: 'draft_id'
# on any store containing a legacy-migrated row without draft_id in its JSON.)
# ---------------------------------------------------------------------------

def test_list_pending_no_longer_raises_on_legacy_row(monkeypatch, tmp_path):
    """store.list_pending() must NOT raise KeyError when a row's data JSON is
    missing draft_id — the fix rescues it from the drafts.draft_id column.
    Root cause was store.py:47 r["draft_id"] strict access."""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    _seed_legacy_row(db_path, "legacy-001", "lasso_ig", "2020-01-01")
    store = PendingStore(path=db_path)
    drafts = store.list_pending()   # must not raise
    assert len(drafts) == 1
    assert drafts[0].draft_id == "legacy-001"   # rescued from the column


def test_run_daily_no_longer_raises_on_legacy_row(monkeypatch, tmp_path):
    """run_daily must NOT raise KeyError('draft_id') when the store has a
    legacy-migrated row — the exact production crash scenario.

    expire_past_due (runner.py:227) runs before the per-account try/except;
    the fix ensures list_pending() never blows up on a malformed row.
    """
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("AGENT_ENABLED", "true")
    _seed_legacy_row(db_path, "legacy-002", "lasso_ig", "2020-01-01")

    voice = tmp_path / "voice.md"
    voice.write_text(_VOICE, encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")

    store = PendingStore(path=db_path)
    poster = _FakePoster()

    out = run_daily(    # must not raise
        poster=poster,
        voice_path=str(voice),
        library_path=str(lib),
        scheduled_for=f"{DAY}T14:00:00+00:00",
        accounts=[_lasso_account()],
        store=store,
    )
    assert out["status"] == "drafted"


# ---------------------------------------------------------------------------
# Part 2: regression guards (these must pass after the fix)
# ---------------------------------------------------------------------------

def test_list_pending_survives_legacy_row_after_fix(monkeypatch, tmp_path):
    """After the fix, list_pending() returns a Draft even when draft_id is
    missing from the data JSON — rescued from the drafts.draft_id column."""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    _seed_legacy_row(db_path, "rescued-001", "lasso_ig", "2020-01-01")
    store = PendingStore(path=db_path)
    drafts = store.list_pending()
    assert len(drafts) == 1
    assert drafts[0].draft_id == "rescued-001"  # rescued from the column


def test_run_daily_completes_after_fix_with_legacy_row(monkeypatch, tmp_path):
    """After the fix, run_daily completes normally even with a legacy row in
    the store — the stale row is expired and the daily run produces its card."""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("AGENT_ENABLED", "true")
    _seed_legacy_row(db_path, "legacy-003", "lasso_ig", "2020-01-01")

    voice = tmp_path / "voice.md"
    voice.write_text(_VOICE, encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    (lib / "asset.txt").write_text("An approved note.", encoding="utf-8")

    store = PendingStore(path=db_path)
    poster = _FakePoster()

    out = run_daily(
        poster=poster,
        voice_path=str(voice),
        library_path=str(lib),
        scheduled_for=f"{DAY}T14:00:00+00:00",
        accounts=[_lasso_account()],
        store=store,
    )
    assert out["status"] == "drafted"
    # the legacy stale row did not block the run
    pending = store.list_pending()
    assert all(d.draft_id != "" for d in pending), \
        "every pending draft must have a non-empty draft_id"


def test_find_pending_survives_legacy_row(monkeypatch, tmp_path):
    """find_pending() also rescues draft_id from the column."""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    _seed_legacy_row(db_path, "find-001", "lasso_ig", "2020-01-01")
    store = PendingStore(path=db_path)
    draft = store.find_pending("lasso_ig", "2020-01-01", "feed")
    assert draft is not None
    assert draft.draft_id == "find-001"


def test_find_for_day_survives_legacy_row(monkeypatch, tmp_path):
    """find_for_day() also rescues draft_id from the column."""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    _seed_legacy_row(db_path, "day-001", "lasso_ig", "2020-01-01")
    store = PendingStore(path=db_path)
    draft = store.find_for_day("lasso_ig", "2020-01-01", "feed")
    assert draft is not None
    assert draft.draft_id == "day-001"


def test_get_survives_legacy_row(monkeypatch, tmp_path):
    """store.get() also rescues draft_id from the column."""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    _seed_legacy_row(db_path, "get-001", "lasso_ig", "2020-01-01")
    store = PendingStore(path=db_path)
    draft = store.get("get-001")
    assert draft is not None
    assert draft.draft_id == "get-001"
