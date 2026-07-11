"""
Regression: store read methods crash on legacy PENDING rows missing fields.

Prior fix (545d0e2) rescued only draft_id. _from_dict() still does strict
r["account_key"], r["platform"], r["caption"] access which crash on any row
whose data JSON was migrated without those fields.

Worst-case legacy row: data = '{}' (completely empty JSON). After the fix,
every column-backed field (draft_id, account_key, status, day_key,
draft_type) is rescued via setdefault from the DB row, and the two
non-column fields (platform, caption) fall back to safe defaults in
_from_dict itself.

Crash chain: run_daily -> expire_past_due -> store.list_pending()
                       -> _from_dict -> r["account_key"] (KeyError)
  expire_past_due runs at runner.py before the per-account try/except, so
  the KeyError propagates out of run_daily to _fire_daily.
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


def _seed_minimal_row(db_path, draft_id, account_key, day_key, status="pending"):
    """Insert a PENDING row with an empty data JSON — the minimal legacy shape
    that exercises every strict _from_dict access simultaneously."""
    from agent import db
    with db.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO drafts "
            "(draft_id, account_key, status, day_key, draft_type, data) "
            "VALUES (?,?,?,?,?,?)",
            (draft_id, account_key, status, day_key, "feed", "{}"))
        conn.commit()


def _lasso_account():
    return Account(key="lasso_ig", display_name="LASSO IG",
                   platform=Platform.INSTAGRAM,
                   token_env="DUMMY_TOK", target_id_env="DUMMY_TGT")


# ---------------------------------------------------------------------------
# Reproduce: these tests FAIL before the fix (KeyError on account_key etc.)
#            and PASS after — confirming the full set of strict accesses
#            is hardened.
# ---------------------------------------------------------------------------

def test_list_pending_no_crash_on_minimal_row(monkeypatch, tmp_path):
    """list_pending() must return a well-formed Draft even when data == '{}'.
    Before fix: KeyError: 'account_key' (or draft_id on un-patched build).
    After fix: rescues column-backed fields, defaults non-column fields."""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    _seed_minimal_row(db_path, "min-001", "lasso_ig", "2020-01-01")
    store = PendingStore(path=db_path)

    drafts = store.list_pending()   # must not raise

    assert len(drafts) == 1
    d = drafts[0]
    assert d.draft_id == "min-001"        # rescued from draft_id column
    assert d.account_key == "lasso_ig"   # rescued from account_key column
    assert d.platform == ""              # no column — safe default
    assert d.caption == ""              # no column — safe default
    assert d.status.value == "pending"  # rescued from status column


def test_get_no_crash_on_minimal_row(monkeypatch, tmp_path):
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    _seed_minimal_row(db_path, "min-002", "lasso_ig", "2020-01-01")
    store = PendingStore(path=db_path)

    d = store.get("min-002")

    assert d is not None
    assert d.draft_id == "min-002"
    assert d.account_key == "lasso_ig"
    assert d.platform == ""
    assert d.caption == ""


def test_find_pending_no_crash_on_minimal_row(monkeypatch, tmp_path):
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    _seed_minimal_row(db_path, "min-003", "lasso_ig", "2020-01-01")
    store = PendingStore(path=db_path)

    d = store.find_pending("lasso_ig", "2020-01-01", "feed")

    assert d is not None
    assert d.draft_id == "min-003"
    assert d.account_key == "lasso_ig"


def test_find_for_day_no_crash_on_minimal_row(monkeypatch, tmp_path):
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    _seed_minimal_row(db_path, "min-004", "lasso_ig", "2020-01-01")
    store = PendingStore(path=db_path)

    d = store.find_for_day("lasso_ig", "2020-01-01", "feed")

    assert d is not None
    assert d.draft_id == "min-004"
    assert d.account_key == "lasso_ig"


def test_run_daily_no_crash_with_minimal_row(monkeypatch, tmp_path):
    """run_daily must complete when the store has a minimal-data legacy row.
    expire_past_due (runner.py before per-account try/except) calls
    list_pending — this is the exact production crash path."""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("AGENT_ENABLED", "true")
    _seed_minimal_row(db_path, "min-005", "lasso_ig", "2020-01-01")

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
    # legacy row was expired (past-due), new draft was produced
    pending = store.list_pending()
    assert all(d.draft_id != "" for d in pending)


# ---------------------------------------------------------------------------
# Hardening round 2: rows whose data blob is NULL, malformed, non-dict JSON,
# or carries a status the enum no longer knows. json.loads / DraftStatus()
# used to raise straight out of the read funnel.
# ---------------------------------------------------------------------------

def _seed_raw_row(db_path, draft_id, data, status="pending",
                  day_key="2026-07-01", draft_type="feed"):
    from agent import db
    with db.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO drafts "
            "(draft_id, account_key, status, day_key, draft_type, data) "
            "VALUES (?,?,?,?,?,?)",
            (draft_id, "lasso_ig", status, day_key, draft_type, data))
        conn.commit()


def test_null_data_blob_survives(monkeypatch, tmp_path):
    """data == NULL: json.loads(None) used to raise TypeError."""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    _seed_raw_row(db_path, "null-001", None)
    store = PendingStore(path=db_path)
    drafts = store.list_pending()
    assert len(drafts) == 1
    assert drafts[0].draft_id == "null-001"
    assert drafts[0].account_key == "lasso_ig"


def test_malformed_json_survives(monkeypatch, tmp_path):
    """data == broken JSON: JSONDecodeError used to kill list_pending."""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    _seed_raw_row(db_path, "garbage-001", "{not json at all")
    store = PendingStore(path=db_path)
    drafts = store.list_pending()
    assert len(drafts) == 1
    assert drafts[0].draft_id == "garbage-001"


def test_non_dict_json_survives(monkeypatch, tmp_path):
    """data == a JSON list: .setdefault on a list used to raise."""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    _seed_raw_row(db_path, "list-001", '["a", "b"]')
    store = PendingStore(path=db_path)
    drafts = store.list_pending()
    assert len(drafts) == 1
    assert drafts[0].draft_id == "list-001"


def test_unknown_status_quarantines_as_blocked(monkeypatch, tmp_path):
    """A retired/unknown status string maps to BLOCKED (can never publish),
    instead of raising ValueError out of store.get on the Approve tap."""
    from agent.drafter import DraftStatus
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    _seed_raw_row(db_path, "status-001",
                  '{"draft_id": "status-001", "status": "shipped_v1"}')
    store = PendingStore(path=db_path)
    d = store.get("status-001")
    assert d is not None
    assert d.status == DraftStatus.BLOCKED
    assert d.blocked_reason


def test_healthy_rows_unaffected_alongside_bad(monkeypatch, tmp_path):
    """One corrupt row must not hide the healthy rows around it."""
    from agent.drafter import DraftStatus
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    _seed_raw_row(db_path, "bad-001", "{broken", day_key="2026-07-03")
    _seed_raw_row(
        db_path, "good-001",
        json.dumps({"draft_id": "good-001", "account_key": "lasso_ig",
                    "caption": "Fine.", "status": "pending",
                    "day_key": "2026-07-04", "draft_type": "feed"}),
        day_key="2026-07-04")
    store = PendingStore(path=db_path)
    drafts = store.list_pending()
    assert {d.draft_id for d in drafts} == {"bad-001", "good-001"}
    good = next(d for d in drafts if d.draft_id == "good-001")
    assert good.caption == "Fine."
    assert good.status == DraftStatus.PENDING


def test_column_values_win_over_json_for_backed_fields(monkeypatch, tmp_path):
    """If the JSON blob has a stale value for a column-backed field but the
    column has the authoritative value, setdefault only fills when MISSING,
    so existing JSON values are preserved. Verifies setdefault semantics."""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    from agent import db
    # Row where JSON has draft_id but it matches; status in JSON differs from col
    data = {"draft_id": "col-001", "account_key": "lasso_ig",
            "platform": "instagram", "caption": "test",
            "status": "pending"}
    with db.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO drafts "
            "(draft_id, account_key, status, day_key, draft_type, data) "
            "VALUES (?,?,?,?,?,?)",
            ("col-001", "lasso_ig", "approved", "2026-01-01", "feed",
             json.dumps(data)))
        conn.commit()
    store = PendingStore(path=db_path)
    d = store.get("col-001")
    # JSON had "pending", column has "approved": setdefault doesn't override
    # existing JSON value — JSON wins when present
    assert d.draft_id == "col-001"
    assert d.account_key == "lasso_ig"
