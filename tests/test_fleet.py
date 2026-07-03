"""
Fleet hardening tests. Asserts: an injected failure on account A never blocks
account B's draft cycle (B completes, A alerts once + lands in the audit trail
as account_error); fleet-status renders a fixture fleet one line per account.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, ops_alerts, runner  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402


class RecordingPoster:
    def __init__(self):
        self.cards = []
        self.notices = []

    def post_approval_card(self, draft):
        self.cards.append(draft)
        return {"channel": "C1", "ts": "1.1"}

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


def _accounts():
    a = Account(key="lasso_ig", display_name="A", platform=Platform.INSTAGRAM,
                token_env="FL_T", target_id_env="FL_I")
    b = Account(key="lasso_fb", display_name="B", platform=Platform.FACEBOOK_PAGE,
                token_env="FL_T", target_id_env="FL_I")
    return a, b


def test_account_a_failure_never_blocks_account_b(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)
    # a voice doc + a library card so account B can draft normally
    voice = tmp_path / "voice.md"
    voice.write_text("# Voice\nWe help gym owners grow.\n#LASSOFramework",
                     encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "lasso_p1_a.jpg").write_bytes(b"img")
    (lib / "lasso_p1_a.txt").write_text("A plain story.", encoding="utf-8")

    a, b = _accounts()

    def exploding_book(account, day_key, **kw):
        if account.key == "lasso_ig":
            raise RuntimeError("Graph 500 for account A")
        return None

    import agent.book_campaign as bc
    monkeypatch.setattr(bc, "build_book_draft", exploding_book)

    poster = RecordingPoster()
    out = runner.run_daily(poster=poster, voice_path=str(voice),
                           library_path=str(lib), accounts=[a, b],
                           scheduled_for="2026-07-06T18:30:00+00:00")
    drafted_for = {d.account_key for d in out["drafts"]}
    assert "lasso_fb" in drafted_for                      # B completed
    assert "lasso_ig" not in drafted_for                  # A failed, isolated
    fail_alerts = [n for n in rec.notices
                   if "lasso_ig failed its draft cycle" in n]
    assert len(fail_alerts) == 1                          # alerts ONCE
    rows = [r for r in db.audit_rows() if r["kind"] == "account_error"]
    assert len(rows) == 1 and rows[0]["account_key"] == "lasso_ig"
    assert "Graph 500" in rows[0]["reason"]


def test_fleet_status_renders_fixture_fleet(monkeypatch, tmp_path, capsys):
    from agent.__main__ import main
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, media_id, "
            "mode, published_at) VALUES ('d','lasso_ig','instagram','c','M',"
            "'published','2026-07-02T18:30:00')")
        conn.commit()
    db.audit("account_error", "lasso_fb", "RuntimeError: token expired", "lasso_fb")
    main(["fleet-status"])
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.startswith("lasso_")]
    assert len(lines) >= 2                                 # one line per account
    ig = next(l for l in lines if l.startswith("lasso_ig"))
    fb = next(l for l in lines if l.startswith("lasso_fb"))
    assert "trust L0" in ig and "2026-07-02" in ig
    assert "token expired" in fb
    assert all("runway" in l for l in lines)
