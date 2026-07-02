"""
Social Grade tests. Offline fixtures hitting at least three letter outcomes, the
flag-off no-op, honest gaps (missing inputs never fake a score), and the
before/after posting-frequency compare from a baseline JSON.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import reporting  # noqa: E402


def _report(**kw):
    base = {
        "account_key": "lasso_ig",
        "engagement_rate": 0.05, "engagement_rate_baseline": 0.04,
        "followers_growth_rate": 0.03,
        "posting_freq_current": 26,
    }
    base.update(kw)
    return base


def _grade(monkeypatch, tmp_path, report, **kw):
    monkeypatch.setenv("AGENT_GRADE_ENABLED", "true")
    kw.setdefault("base_dir", str(tmp_path))
    return reporting.compute_grade(report, **kw)


# ---- flag off ----------------------------------------------------------------
def test_flag_off_returns_none(monkeypatch):
    monkeypatch.delenv("AGENT_GRADE_ENABLED", raising=False)
    assert reporting.compute_grade(_report()) is None


# ---- three letter outcomes ------------------------------------------------------
def test_a_grade(monkeypatch, tmp_path):
    g = _grade(monkeypatch, tmp_path, _report(),
               planned_posts=26, pillar_counts={"p1": 5, "p2": 5, "p3": 4},
               proof_posts=2)
    # consistency 100, mix 80, engagement 90 (+25%), growth 90, proof 100 -> 92 A
    assert g["letter"] == "A"
    assert g["score"] >= 90


def test_mid_grade(monkeypatch, tmp_path):
    g = _grade(monkeypatch, tmp_path,
               _report(engagement_rate=0.04, engagement_rate_baseline=0.04,
                       followers_growth_rate=0.01, posting_freq_current=20),
               planned_posts=26, pillar_counts={"p1": 8, "p2": 4}, proof_posts=0)
    # consistency 77, mix 50, engagement 70, growth 70, proof 40 -> 61 D
    assert g["letter"] == "D"


def test_f_grade(monkeypatch, tmp_path):
    g = _grade(monkeypatch, tmp_path,
               _report(engagement_rate=0.02, engagement_rate_baseline=0.04,
                       followers_growth_rate=-0.02, posting_freq_current=8),
               planned_posts=26, pillar_counts={"p1": 8}, proof_posts=0)
    # consistency 31, mix 40 (single pillar), engagement 40, growth 40, proof 40 -> F
    assert g["letter"] == "F"
    assert g["score"] < 60


# ---- honest gaps -----------------------------------------------------------------
def test_missing_inputs_are_gaps_never_fake_scores(monkeypatch, tmp_path):
    g = _grade(monkeypatch, tmp_path, _report(), planned_posts=None,
               pillar_counts=None, proof_posts=None)
    assert g["subscores"]["consistency"] is None
    assert g["subscores"]["mix"] is None
    assert g["subscores"]["proof"] is None
    assert len(g["gaps"]) >= 3
    assert g["letter"] in ("A", "B", "C", "D", "F")  # graded on what IS known


def test_nothing_known_means_no_grade(monkeypatch, tmp_path):
    g = _grade(monkeypatch, tmp_path,
               {"account_key": "x", "engagement_rate": None,
                "engagement_rate_baseline": None, "followers_growth_rate": None,
                "posting_freq_current": None})
    assert g["letter"] is None
    line = reporting.grade_summary_line("x", g)
    assert "not enough data" in line


# ---- baseline before/after compare -------------------------------------------
def test_baseline_before_after_compare(monkeypatch, tmp_path):
    (tmp_path / "baseline_2026-07.json").write_text(json.dumps(
        {"accounts": {"lasso_ig": {"avg_posts_per_week": 2.5}}}), encoding="utf-8")
    g = _grade(monkeypatch, tmp_path, _report(), planned_posts=26,
               pillar_counts={"p1": 3, "p2": 3}, proof_posts=1)
    assert g["posting_freq_before"] == 2.5
    assert g["posting_freq_after"] == 26
    line = reporting.grade_summary_line("lasso_ig", g)
    assert line.startswith("GRADE lasso_ig:")
    assert "2.5 before -> 26 now" in line


def test_missing_baseline_is_a_gap(monkeypatch, tmp_path):
    g = _grade(monkeypatch, tmp_path, _report(), planned_posts=26,
               pillar_counts={"p1": 3, "p2": 3}, proof_posts=1)
    assert any("baseline_2026-07" in gap for gap in g["gaps"])
