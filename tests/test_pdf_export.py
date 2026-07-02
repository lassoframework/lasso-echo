"""
White label PDF export tests. Real reportlab render (pure pip dependency, zero
network). Asserts: a nonzero PDF lands from fixture snapshots; white label
fields (client display name) land in the text layer; the text layer is
dash-free; LASSO branding is the default.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, monthly_report, pdf_report  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402


def _seed(account_key="lasso_ig"):
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO snapshots (account_key, date, metrics) VALUES (?,?,?)",
            (account_key, "2026-07-10",
             json.dumps({"views": 400, "likes": 30, "comments": 6, "saves": 8,
                         "shares": 3, "reach": 280, "followers": 550})))
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, media_id, "
            "mode, published_at, creative_key, archetype, set_name, likes, comments, "
            "saves, shares) VALUES ('d',?, 'instagram','alpha post','M','published',"
            "'2026-07-15T10:00:00','lasso_p1_a.jpg','flow','brand',10,1,1,0)",
            (account_key,))
        conn.commit()


def test_pdf_export_nonzero_with_white_label(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_REPORTING_ENABLED", "true")
    monkeypatch.setenv("AGENT_REPORTS_DIR", str(tmp_path / "reports"))
    _seed()
    from datetime import datetime, timezone
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    out = monthly_report.run(account="lasso_ig", now=now,
                             base_dir=str(tmp_path), pdf=True)
    pdf_path = out["lasso_ig:pdf"]
    assert os.path.getsize(pdf_path) > 1000                     # real, nonzero PDF
    text = pdf_report.pdf_text(pdf_path)
    assert "30 day report" in text
    assert "lasso_ig" in text                                    # account subtitle
    assert "LASSO" in text                                       # default branding
    for ch in ("—", "–"):
        assert ch not in text                                    # dash-free text layer
    assert "impressions" not in text.lower()


def test_white_label_uses_client_display_name(tmp_path):
    acct = Account(key="iron_ig", display_name="Iron Path Gym",
                   platform=Platform.INSTAGRAM, token_env="T", target_id_env="I",
                   library_prefix="content_library/iron_path")
    brand = pdf_report.brand_for(acct)
    assert brand["name"] == "Iron Path Gym"
    path = str(tmp_path / "card.pdf")
    pdf_report.build_pdf(path, "30 day report", "iron_ig",
                         [("para", "A clean month.")], brand=brand)
    text = pdf_report.pdf_text(path)
    assert "Iron Path Gym" in text


def test_scrub_kills_dashes():
    assert "—" not in pdf_report._scrub("a — b")
    assert "–" not in pdf_report._scrub("5–10")
    assert pdf_report._scrub("5–10") == "5 to 10"
