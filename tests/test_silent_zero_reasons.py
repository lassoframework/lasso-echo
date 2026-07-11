"""
Silent-zero law: a command that does nothing says WHY.

"plan-month: 0 written" with no cause burned weeks of confusion. Every
bare-zero path fixed in this pass must state its reason: backfill's empty
window, seed-calendar's missing approval evidence, an account filter that
matches nothing, and runway explain at zero eligible.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_backfill_empty_window_names_cause(monkeypatch, capsys):
    from agent import backfill
    monkeypatch.setenv("AGENT_REPORTING_ENABLED", "true")
    out = backfill.backfill_insights("lasso_ig", since="2026-01-01", dry=True)
    text = capsys.readouterr().out
    assert out is not None
    assert "nothing to backfill" in text
    assert "Widen --since" in text


def test_seed_calendar_no_evidence_names_cause(capsys):
    from agent import seed_calendar
    seed_calendar.run("lasso_ig", "2031-01", write=False)
    text = capsys.readouterr().out
    assert "no approval evidence" in text
    assert "none exist for this month yet" in text


def test_monthly_report_unknown_account_says_so(monkeypatch, capsys):
    from agent import monthly_report
    monkeypatch.setenv("AGENT_REPORTING_ENABLED", "true")
    out = monthly_report.run(account="typo_gym")
    text = capsys.readouterr().out
    assert out == {}
    assert "no account matches 'typo_gym'" in text
    assert "lasso_ig" in text  # the known keys are listed


def test_grade_card_unknown_account_says_so(monkeypatch, capsys):
    from agent import grade_card
    monkeypatch.setenv("AGENT_GRADE_ENABLED", "true")
    out = grade_card.run(account="typo_gym")
    text = capsys.readouterr().out
    assert out == {}
    assert "no account matches 'typo_gym'" in text


def test_monthly_review_unknown_account_says_so(monkeypatch, capsys):
    from agent import monthly_review
    out = monthly_review.run(account="typo_gym", dry=True)
    text = capsys.readouterr().out
    assert "no account matches 'typo_gym'" in text


def test_runway_explain_zero_eligible_warns(monkeypatch, capsys):
    from agent import runway
    monkeypatch.setattr(runway, "classify_creatives", lambda *a, **kw: ([], {}))
    runway.explain("lasso_ig")
    text = capsys.readouterr().out
    assert "0 days of approved content" in text
    assert "run dry" in text
