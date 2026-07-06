"""
Runway explain tests (operator hygiene Part D). Offline. Asserts: the explain
numbers reconcile with the SAME functions the digest reads (runway_days and
eligible_creatives are literally built on the shared classifier, not a copy);
every exclusion carries a summarized reason; the output is dash free; the run
is read only (store byte identical); flag state is irrelevant (explain is a
by-hand read, not a daily surface).
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, db, rotation, runway  # noqa: E402

_DASH_RE = re.compile(r"[‐‑‒–—―−-]")

CLEAN = [("lasso_p1_a.jpg", "A story."), ("lasso_p2_b.jpg", "Another."),
         ("lasso_p3_c.jpg", "A third."), ("lasso_p4_d.jpg", "A fourth.")]


def _lib(tmp_path, cards):
    lib = tmp_path / "library"
    lib.mkdir(parents=True, exist_ok=True)
    for name, note in cards:
        (lib / name).write_bytes(b"img-" + name.encode())
        (lib / (os.path.splitext(name)[0] + ".txt")).write_text(note, encoding="utf-8")
    return str(lib)


def _arm(monkeypatch, tmp_path):
    src = tmp_path / "empty_now.md"
    src.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)


def test_explain_reconciles_with_digest_math(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(runway, "v2_library_concepts", lambda lib: [])
    lib = _lib(tmp_path, CLEAN + [
        ("lasso_p2_statcard.jpg", "Convert 80 percent more with speed."),
        ("lasso_v2_x_story.png", "A story variant."),
    ])
    (tmp_path / "library" / "style_exclusions.json").write_text(
        json.dumps({"off_style": ["lasso_p4_d.jpg"]}), encoding="utf-8")
    rotation.record_served("lasso_ig", "lasso_p1_a.jpg", "p1", "2026-07-05")
    out = runway.explain("lasso_ig", lib)
    # the days number IS runway_days (same function the digest imports)
    assert out["days"] == runway.runway_days("lasso_ig", lib)
    # the eligible list IS eligible_creatives (built on the shared classifier)
    assert sorted(out["eligible"]) == sorted(
        os.path.basename(c.path)
        for c in runway.eligible_creatives("lasso_ig", lib))
    assert out["days"] == round(len(out["eligible"]) / out["posts_per_day"], 1)
    # every exclusion carries its reason
    assert out["excluded"]["lasso_p4_d.jpg"].startswith("off style")
    assert out["excluded"]["lasso_p1_a.jpg"].startswith("already used")
    assert "fabrication gate" in out["excluded"]["lasso_p2_statcard.jpg"]
    assert "story variant" in out["excluded"]["lasso_v2_x_story.png"]


def test_explain_output_lines_and_dash_free(monkeypatch, tmp_path, capsys):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(runway, "v2_library_concepts", lambda lib: [])
    lib = _lib(tmp_path, CLEAN)
    runway.explain("lasso_ig", lib)
    printed = capsys.readouterr().out
    assert "eligible: 4 creative(s)" in printed
    assert "excluded: 0 creative(s)" in printed
    assert "posting day(s) per week" in printed
    assert "the same number the digest prints" in printed
    # dash free: filenames carry underscores, prose carries no dash family char
    assert not _DASH_RE.search(printed), _DASH_RE.search(printed)


def test_explain_is_read_only(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(runway, "v2_library_concepts", lambda lib: [])
    lib = _lib(tmp_path, CLEAN)
    rotation.record_served("lasso_ig", "lasso_p1_a.jpg", "p1", "2026-07-05")
    with db.connect() as conn:
        before = "\n".join(conn.iterdump())
    files_before = sorted(os.listdir(lib))
    runway.explain("lasso_ig", lib)
    with db.connect() as conn:
        assert "\n".join(conn.iterdump()) == before     # store byte identical
    assert sorted(os.listdir(lib)) == files_before       # library untouched
