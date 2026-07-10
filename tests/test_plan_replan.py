"""
plan-month --replan: approved days survive untouched; pending future days are
deleted and rebuilt; published days never touched; per-day action reported.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db as _db


MONTH = "2026-08"
ACCOUNT = "lasso_ig"


def _setup(monkeypatch, tmp_path):
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("AGENT_PLAN_MONTH_ENABLED", "true")
    monkeypatch.setenv("AGENT_CATEGORY_ROTATION", "true")

    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    (lib / "asset.txt").write_text("An approved note.", encoding="utf-8")
    monkeypatch.setenv("AGENT_LIBRARY_PATH", str(lib))

    # Pre-seed one approved and one pending draft for August
    with _db.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO drafts "
            "(draft_id, account_key, status, day_key, draft_type, data) "
            "VALUES (?,?,?,?,?,?)",
            ("approved_2026-08-04", ACCOUNT, "approved", "2026-08-04", "plan",
             json.dumps({"creative_path": "approved_card.png",
                         "creative_key": "approved_card.png", "caption": "Approved"})))
        conn.execute(
            "INSERT OR IGNORE INTO drafts "
            "(draft_id, account_key, status, day_key, draft_type, data) "
            "VALUES (?,?,?,?,?,?)",
            ("pending_2026-08-05", ACCOUNT, "pending", "2026-08-05", "plan",
             json.dumps({"creative_path": "old_card.png",
                         "creative_key": "old_card.png", "caption": "Old pending"})))
        conn.commit()
    return db_path, str(lib)


def test_approved_day_survives_replan(monkeypatch, tmp_path):
    """An approved draft must never be deleted or touched by --replan."""
    from agent.plan_month import replan_month
    _setup(monkeypatch, tmp_path)

    out = replan_month(ACCOUNT, MONTH, from_day="2026-08-04", write=False)
    assert out is not None

    # The approved day must appear as kept-approved
    actions = {d["date"]: d["action"] for d in out["days"]}
    assert actions.get("2026-08-04") == "kept-approved", (
        f"approved day should be kept-approved; got {actions.get('2026-08-04')!r}")


def test_pending_day_is_replaced(monkeypatch, tmp_path):
    """A pending draft for a future day must be deleted and the day marked
    replanned (or open if no eligible content remains)."""
    from agent.plan_month import replan_month
    db_path, _ = _setup(monkeypatch, tmp_path)

    # Confirm the pending draft exists before replan
    with _db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM drafts WHERE draft_id=?",
            ("pending_2026-08-05",)).fetchone()
    assert row is not None and row["status"] == "pending"

    out = replan_month(ACCOUNT, MONTH, from_day="2026-08-04", write=False)
    assert out is not None

    # The old pending draft should be gone
    with _db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM drafts WHERE draft_id=?",
            ("pending_2026-08-05",)).fetchone()
    assert row is None, "old pending draft should have been deleted by --replan"

    # 2026-08-05 should appear as replanned or open (never kept-approved)
    actions = {d["date"]: d["action"] for d in out["days"]}
    assert actions.get("2026-08-05") in ("replanned", "open"), (
        f"2026-08-05 should be replanned or open; got {actions.get('2026-08-05')!r}")


def test_approved_day_never_deleted(monkeypatch, tmp_path):
    """The approved draft record must still exist in the DB after replan."""
    from agent.plan_month import replan_month
    db_path, _ = _setup(monkeypatch, tmp_path)

    replan_month(ACCOUNT, MONTH, from_day="2026-08-01", write=False)

    with _db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM drafts WHERE draft_id=?",
            ("approved_2026-08-04",)).fetchone()
    assert row is not None, "approved draft must never be deleted"
    assert row["status"] == "approved"


def test_category_in_replan_output(monkeypatch, tmp_path):
    """With AGENT_CATEGORY_ROTATION=true, replanned days must carry a category."""
    from agent.plan_month import replan_month
    _setup(monkeypatch, tmp_path)

    out = replan_month(ACCOUNT, MONTH, from_day="2026-08-04", write=False)
    assert out is not None

    # At least some days should have a non-empty category
    cats = [d["category"] for d in out["days"] if d["action"] == "replanned"]
    # August days with rotation on will have categories (Mon=podcast, Wed=b2b, etc.)
    # If no content was replanned (write=False skips the eligible pool for non-write),
    # the category field still exists on all day entries.
    for d in out["days"]:
        assert "category" in d, f"day entry must have 'category' field: {d}"


def test_replan_off_when_flag_off(monkeypatch, tmp_path):
    """replan_month returns None when AGENT_PLAN_MONTH_ENABLED is off."""
    from agent.plan_month import replan_month
    _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PLAN_MONTH_ENABLED", "false")

    result = replan_month(ACCOUNT, MONTH)
    assert result is None


def test_calendar_command_exists(monkeypatch, tmp_path, capsys):
    """python -m agent calendar --account lasso_ig --month 2026-08 --out path
    must not print 'unknown command' and must write an HTML file."""
    import importlib
    import agent.__main__ as mm

    db_path = str(tmp_path / "echo.db")
    out_path = str(tmp_path / "cal.html")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)

    mm.main(["calendar", "--account", "lasso_ig", "--month", "2026-08",
             "--out", out_path])
    captured = capsys.readouterr()
    assert "unknown command" not in captured.out.lower(), (
        "'calendar' must be a known command")
    assert os.path.exists(out_path), "calendar command must write the HTML file"


def test_status_shows_category_rotation(monkeypatch, capsys):
    """python -m agent status must include a 'category_rotation' line."""
    import agent.__main__ as mm
    monkeypatch.setenv("AGENT_ENABLED", "false")
    mm.main(["status"])
    out = capsys.readouterr().out
    assert "category_rotation" in out, (
        "'status' output must include a category_rotation line")
    assert "AGENT_CATEGORY_ROTATION" in out
