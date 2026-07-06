"""
plan-month and approve-month tests (PART C). Offline. Asserts: plan-month fills
open posting days from the eligible pool, respects the 14-day rotation window
and canvas guard, never double-books existing days, returns None while the flag
is OFF, and approve-month holds the first post for accounts with no history.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, db, rotation, schedule  # noqa: E402
from agent import plan_month as pm  # noqa: E402
from agent.library import Creative  # noqa: E402

MONTH = "2026-09"


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_PLAN_MONTH_ENABLED", "true")


def _make_eligibles(monkeypatch, tmp_path, n=50):
    """Patch eligible_creatives to return n simple v2-style gate-clean creatives."""
    concepts = [
        Creative(path=str(tmp_path / f"lasso_v2_concept_{i:02d}.png"),
                 media_type="image")
        for i in range(n)
    ]
    monkeypatch.setattr(pm.runway, "eligible_creatives", lambda acct, lib: concepts)
    return concepts


def test_plan_month_flag_off_returns_none(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PLAN_MONTH_ENABLED", raising=False)
    assert pm.plan_month("lasso_ig", MONTH) is None


def test_plan_month_dry_run_no_writes(monkeypatch, tmp_path):
    _arm(monkeypatch)
    _make_eligibles(monkeypatch, tmp_path)
    out = pm.plan_month("lasso_ig", MONTH, library_path=str(tmp_path), write=False)
    assert out is not None
    assert out["wrote"] == 0
    assert len(out["planned"]) > 0
    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM drafts WHERE account_key='lasso_ig' "
            "AND draft_type='plan'").fetchone()["n"]
    assert n == 0


def test_plan_month_fills_all_posting_days(monkeypatch, tmp_path):
    _arm(monkeypatch)
    _make_eligibles(monkeypatch, tmp_path, n=50)
    out = pm.plan_month("lasso_ig", MONTH, library_path=str(tmp_path), write=True)
    assert out is not None
    # Every planned day must be a valid posting day and have a creative assigned
    for day_key, creative_key in out["planned"]:
        assert schedule.should_post_on(day_key), f"{day_key} is not a posting day"
        assert creative_key is not None
    # All planned days have a pending draft in the DB
    with db.connect() as conn:
        db_days = {r["day_key"] for r in conn.execute(
            "SELECT day_key FROM drafts WHERE account_key='lasso_ig' AND "
            "draft_type='plan' AND status='pending'").fetchall()}
    assert {d for d, _ in out["planned"]} == db_days
    assert out["wrote"] == len(out["planned"])


def test_plan_month_skips_existing_days(monkeypatch, tmp_path):
    _arm(monkeypatch)
    _make_eligibles(monkeypatch, tmp_path, n=50)
    # Seed an existing pending draft on 2026-09-02 (a posting day: Wed)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO drafts (draft_id, account_key, status, day_key, "
            "draft_type, data) VALUES (?,?,?,?,?,?)",
            ("existing_1", "lasso_ig", "pending", "2026-09-02", "feed",
             json.dumps({"creative_path": "old.png"})))
        conn.commit()
    out = pm.plan_month("lasso_ig", MONTH, library_path=str(tmp_path), write=False)
    planned_days = {d for d, _ in out["planned"]}
    assert "2026-09-02" not in planned_days


def test_plan_month_respects_rotation_window(monkeypatch, tmp_path):
    _arm(monkeypatch)
    # Only one creative — once served it must be excluded for window days
    only_one = Creative(path=str(tmp_path / "lasso_v2_only_one.png"), media_type="image")
    monkeypatch.setattr(pm.runway, "eligible_creatives", lambda acct, lib: [only_one])
    # Serve it 5 days before Sep 1 → it is in the window for the first 9 days of Sep
    rotation.record_served("lasso_ig", "lasso_v2_only_one.png", "misc", "2026-08-27")
    out = pm.plan_month("lasso_ig", MONTH, library_path=str(tmp_path), write=False)
    # Sep 1 = 5 days after Aug 27, still in the 14-day window → must be skipped
    assert "2026-09-01" in out["skipped"]
    # After the window (Aug 27 + 14 = Sep 10), it may appear again
    # Sep 11 is the first eligible posting day after the window clears
    planned_days = {d for d, _ in out["planned"]}
    assert any(d >= "2026-09-11" for d in planned_days), (
        "concept should re-enter pool after the 14-day window")


def test_plan_month_canvas_guard(monkeypatch, tmp_path):
    """Canvas guard picks a different canvas on day 2 when an alternative exists."""
    _arm(monkeypatch)
    # Three concepts: A and B share cream canvas, C has navy canvas
    c_A = Creative(path=str(tmp_path / "lasso_v2_concept_a.png"), media_type="image")
    c_B = Creative(path=str(tmp_path / "lasso_v2_concept_b.png"), media_type="image")
    c_C = Creative(path=str(tmp_path / "lasso_v2_concept_c.png"), media_type="image")
    monkeypatch.setattr(pm.runway, "eligible_creatives",
                        lambda acct, lib: [c_A, c_B, c_C])

    def mock_canvas(c):
        return "navy" if "concept_c" in c.path else "cream"
    monkeypatch.setattr(pm, "_creative_canvas", mock_canvas)

    out = pm.plan_month("lasso_ig", MONTH, library_path=str(tmp_path), write=False)
    planned = out["planned"]
    assert len(planned) >= 2
    # Day 1: concept_a (cream). Day 2: canvas guard must prefer concept_c (navy)
    assert "concept_a" in planned[0][1], f"expected concept_a first, got {planned[0][1]}"
    assert "concept_c" in planned[1][1], (
        f"canvas guard should pick concept_c (navy) not {planned[1][1]} (cream)")


def test_approve_month_first_post_held(monkeypatch, tmp_path):
    _arm(monkeypatch)
    _make_eligibles(monkeypatch, tmp_path, n=50)
    out = pm.plan_month("lasso_ig", MONTH, library_path=str(tmp_path), write=True)
    assert len(out["planned"]) >= 2
    # No published posts for lasso_ig → first draft held
    result = pm.approve_month("lasso_ig", MONTH)
    assert len(result["held"]) == 1
    assert len(result["approved"]) == len(out["planned"]) - 1
    # The held day is the earliest planned day and remains pending
    held_day = result["held"][0]
    assert held_day == min(d for d, _ in out["planned"])
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM drafts WHERE account_key='lasso_ig' AND "
            "day_key=? AND draft_type='plan'", (held_day,)).fetchone()
    assert row["status"] == "pending"


def test_approve_month_all_approved_with_history(monkeypatch, tmp_path):
    _arm(monkeypatch)
    _make_eligibles(monkeypatch, tmp_path, n=50)
    # Seed a published post so the first-post guard does not apply
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, "
            "media_id, mode, published_at, creative_key) VALUES (?,?,?,?,?,?,?,?)",
            ("pub1", "lasso_ig", "instagram", "prior post", "m1", "published",
             "2026-08-01T14:00:00", "old.png"))
        conn.commit()
    out = pm.plan_month("lasso_ig", MONTH, library_path=str(tmp_path), write=True)
    result = pm.approve_month("lasso_ig", MONTH)
    assert len(result["held"]) == 0
    assert len(result["approved"]) == len(out["planned"])


def test_approve_month_through_filter(monkeypatch, tmp_path):
    _arm(monkeypatch)
    _make_eligibles(monkeypatch, tmp_path, n=50)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, "
            "media_id, mode, published_at, creative_key) VALUES (?,?,?,?,?,?,?,?)",
            ("pub2", "lasso_ig", "instagram", "prior post", "m1", "published",
             "2026-08-01T14:00:00", "old.png"))
        conn.commit()
    pm.plan_month("lasso_ig", MONTH, library_path=str(tmp_path), write=True)
    # Only approve through the first 10 days
    result = pm.approve_month("lasso_ig", MONTH, through="2026-09-10")
    for day_key in result["approved"]:
        assert day_key <= "2026-09-10"
