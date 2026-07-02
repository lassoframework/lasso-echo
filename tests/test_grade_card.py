"""
Grade card tests. Offline. Asserts: the card renders (HTML + real PDF) from
fixture store data; the baseline before/after compare is shown; the six-area
rubric lands; fully inert without AGENT_GRADE_ENABLED; dash-free text layer.
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, grade_card, pdf_report  # noqa: E402

NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def _seed(tmp_path):
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO snapshots (account_key, date, metrics) VALUES (?,?,?)",
            ("lasso_ig", "2026-07-10",
             json.dumps({"views": 400, "likes": 30, "followers": 500})))
        conn.execute(
            "INSERT OR REPLACE INTO snapshots (account_key, date, metrics) VALUES (?,?,?)",
            ("lasso_ig", "2026-07-25",
             json.dumps({"views": 300, "likes": 20, "followers": 550})))
        for i in range(4):
            conn.execute(
                "INSERT INTO posts (draft_id, account_key, platform, caption, media_id, "
                "mode, published_at, creative_key, likes, comments, saves, shares) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"d{i}", "lasso_ig", "instagram", f"p{i}", "M", "published",
                 "2026-07-15T10:00:00", f"lasso_p{i % 2 + 1}_x.jpg", 10, 1, 0, 0))
        conn.commit()
    (tmp_path / "baseline_2026-07.json").write_text(json.dumps(
        {"accounts": {"lasso_ig": {"avg_posts_per_week": 2.0}}}), encoding="utf-8")


def test_inert_without_flag(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_GRADE_ENABLED", raising=False)
    assert grade_card.run() is None
    assert "OFF" in capsys.readouterr().out


def test_card_renders_with_baseline_compare(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_GRADE_ENABLED", "true")
    monkeypatch.setenv("AGENT_REPORTS_DIR", str(tmp_path / "reports"))
    _seed(tmp_path)
    out = grade_card.run(account="lasso_ig", now=NOW, base_dir=str(tmp_path))
    html = open(out["lasso_ig"], encoding="utf-8").read()
    # the grade renders on live data with the six area rubric
    for area in ("Consistency", "Content mix", "Engagement trend", "Growth trend",
                 "Verified proof", "before Echo vs now"):
        assert area in html
    assert "Social Grade" in html
    # baseline before/after compare shown (2.0 before from the baseline file)
    assert "2.0 before" in html
    # real nonzero PDF, dash free, honest-gaps line present
    pdf_path = out["lasso_ig:pdf"]
    assert os.path.getsize(pdf_path) > 1000
    text = pdf_report.pdf_text(pdf_path)
    assert "Social Grade" in text and "Grade:" in text
    for ch in ("—", "–"):
        assert ch not in text
    assert "fakes nothing" in text
