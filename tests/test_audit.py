"""
Decision audit log tests. Asserts: selections write their WHY; gate exclusions
write their cause; ops alerts land in the trail even when Slack is dormant; the
CLI renders fixture trails; secret material NEVER lands in the table.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, db, ops_alerts, rotation  # noqa: E402


def _lib(tmp_path, cards):
    lib = tmp_path / "library"
    lib.mkdir(parents=True, exist_ok=True)
    for name, note, sidecar in cards:
        (lib / name).write_bytes(b"img-" + name.encode())
        (lib / (os.path.splitext(name)[0] + ".txt")).write_text(note, encoding="utf-8")
        if sidecar is not None:
            (lib / (os.path.splitext(name)[0] + ".json")).write_text(
                json.dumps(sidecar), encoding="utf-8")
    return str(lib)


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ROTATION_ENABLED", "true")
    src = tmp_path / "empty_now.md"
    src.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)


def test_selection_writes_reason(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    lib = _lib(tmp_path, [("lasso_p1_a.jpg", "clean", None)])
    rotation.choose("lasso_ig", "2026-07-06", lib)
    rows = db.audit_rows(day="2026-07-06", account_key="lasso_ig")
    sel = [r for r in rows if r["kind"] == "selection"]
    assert len(sel) == 1
    assert sel[0]["subject"] == "lasso_p1_a.jpg"
    assert "kind=library" in sel[0]["reason"] and "window_ok=yes" in sel[0]["reason"]


def test_exclusions_write_causes(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_CONSENT_GUARD_ENABLED", "true")
    lib = _lib(tmp_path, [
        ("lasso_p1_ok.jpg", "clean", {"people": False}),
        ("lasso_p2_face.jpg", "clean", {"people": True, "consent": "denied"}),
        ("lasso_p3_stat.jpg", "unused",
         {"people": False, "note": "Convert 80 percent more."}),
    ])
    rotation.choose("lasso_ig", "2026-07-06", lib)
    rows = db.audit_rows(day="2026-07-06")
    reasons = {r["subject"]: r["reason"] for r in rows if r["kind"] == "exclusion"}
    assert "consent guard" in reasons["lasso_p2_face.jpg"]
    assert "fabrication gate" in reasons["lasso_p3_stat.jpg"]


def test_ops_alert_lands_in_trail_even_when_slack_dormant(monkeypatch):
    monkeypatch.delenv("AGENT_OPS_ALERTS_ENABLED", raising=False)  # Slack quiet
    ops_alerts.alert("runway low for lasso_ig")
    rows = db.audit_rows()
    assert any(r["kind"] == "ops_alert" and "runway low" in r["reason"] for r in rows)


def test_secrets_never_land_in_audit(monkeypatch):
    monkeypatch.setenv("AGENT_META_TOKEN_TEST", "supersecrettoken12345")
    db.audit("test", "subject", "the token is supersecrettoken12345 leaked?")
    rows = db.audit_rows()
    joined = json.dumps(rows)
    assert "supersecrettoken12345" not in joined         # scrubbed on write


def test_audit_cli_renders(monkeypatch, capsys):
    db.audit("selection", "lasso_p1_a.jpg", "kind=library window_ok=yes",
             "lasso_ig", "2026-07-06")
    from agent.__main__ import main
    main(["audit", "--day", "2026-07-06", "--account", "lasso_ig"])
    out = capsys.readouterr().out
    assert "selection" in out and "lasso_p1_a.jpg" in out
