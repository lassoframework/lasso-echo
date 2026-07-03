"""
Monthly review loop tests. Offline. Asserts: the metrics math flows through; the
CITATION GATE adversarially (a winning post whose caption carries an uncleared
claim is dropped WITH the reason, never proposed); the PDF renders; --dry posts
and writes nothing; fully inert while the flag is OFF; no dash characters in
the digest.
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, db, monthly_review, pdf_report  # noqa: E402

NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


class RecordingPoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


def _seed(caption="Plain winner story", likes=50, creative="lasso_p1_win.jpg"):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, media_id, "
            "mode, published_at, creative_key, likes, comments, saves, shares) "
            "VALUES ('d','lasso_ig','instagram',?,?,'published',"
            "'2026-07-15T10:00:00',?,?,1,0,0)",
            (caption, f"M{likes}", creative, likes))
        conn.execute(
            "INSERT OR REPLACE INTO snapshots (account_key, date, metrics) VALUES "
            "('lasso_ig','2026-07-10',?)",
            (json.dumps({"views": 400, "likes": 30, "followers": 500}),))
        conn.commit()


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MONTHLY_REVIEW_ENABLED", "true")
    monkeypatch.setenv("AGENT_REPORTS_DIR", str(tmp_path / "reports"))
    src = tmp_path / "lasso_now.md"
    src.write_text("""# LASSO Now
## Pillars
- Speed To Lead
## Pillar copy bank
### Pillar: Speed To Lead
Hook: Leads go cold in minutes.
Body: Answer fast.
## CTAs
- Save this post.
## Hashtags
#LASSOFramework
""", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)


def test_inert_when_flag_off(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_MONTHLY_REVIEW_ENABLED", raising=False)
    assert monthly_review.run() is None
    assert "OFF" in capsys.readouterr().out


def test_review_math_digest_and_pdf(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    _seed("Plain winner story", likes=50)
    _seed("Second plain post", likes=5, creative="lasso_p2_b.jpg")
    poster = RecordingPoster()
    out = monthly_review.run(account="lasso_ig", poster=poster, now=NOW,
                             base_dir=str(tmp_path))
    r = out["lasso_ig"]
    assert r["report"]["top_posts"][0]["caption"].startswith("Plain winner")
    assert r["report"]["health"] in ("growing", "flat", "declining")
    digest = r["digest"]
    assert digest.startswith("MONTHLY REVIEW lasso_ig")
    assert "before Echo" in digest
    for ch in ("—", "–"):
        assert ch not in digest
    assert poster.notices == [digest]                    # the Slack digest card
    assert os.path.getsize(r["pdf"]) > 1000              # the white label PDF
    text = pdf_report.pdf_text(r["pdf"])
    assert "Monthly review" in text and "Proposed angles" in text


def test_citation_gate_drops_unapproved_winner(monkeypatch, tmp_path):
    """ADVERSARIAL: the biggest winner of the cycle carries the uncleared 80
    percent claim. It must be DROPPED with a reason, never proposed."""
    _arm(monkeypatch, tmp_path)
    _seed("We lift conversions 80 percent when you answer in 5 minutes", likes=999,
          creative="lasso_p2_stat.jpg")
    _seed("A plain member story", likes=10)
    out = monthly_review.run(account="lasso_ig", dry=True, now=NOW,
                             base_dir=str(tmp_path))
    r = out["lasso_ig"]
    joined = " ".join(r["proposals"])
    assert "80 percent" not in joined                    # never proposed
    assert any("not cleared in the approved sources" in d for d in r["dropped"])
    assert any("member story" in p for p in r["proposals"])  # clean winner proposed
    assert any("lasso_now.md" in p for p in r["proposals"])  # source-doc angle cited


def test_dry_posts_and_writes_nothing(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    _seed()
    poster = RecordingPoster()
    out = monthly_review.run(account="lasso_ig", dry=True, poster=poster, now=NOW,
                             base_dir=str(tmp_path))
    assert "pdf" not in out["lasso_ig"]                  # nothing written
    assert poster.notices == []                          # nothing posted
    assert not (tmp_path / "reports").exists()
