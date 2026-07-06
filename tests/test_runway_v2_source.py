"""
Runway v2 source tests (PART A). Offline. Asserts: all 46 v2 regen library
concept definitions enter the eligible pool from an empty library folder; old
format files remain present; off-style exclusion applies to old-format files only
and never to lasso_v2_* concepts; explain shows the per-set breakdown.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import runway  # noqa: E402


def test_v2_concepts_enter_pool(monkeypatch, tmp_path):
    """All 46 concept definitions appear as eligible when the folder has no old files."""
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    lib = str(tmp_path / "empty_lib")
    os.makedirs(lib)
    from agent.regen_library import CONCEPTS
    eligible, excluded = runway.classify_creatives("lasso_ig", lib)
    eligible_keys = {os.path.basename(c.path) for c in eligible}
    expected = {f"lasso_v2_{k}.png" for k in CONCEPTS}
    assert len(expected) == 46
    missing = expected - eligible_keys
    assert not missing, f"v2 concepts missing from eligible pool: {sorted(missing)}"


def test_old_18_still_present(monkeypatch, tmp_path):
    """Old-format physical files still appear in the combined candidate set."""
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    lib = tmp_path / "lib"
    lib.mkdir()
    old_files = [
        ("lasso_p1_a.jpg", "A story."),
        ("lasso_p2_b.jpg", "Another."),
        ("lasso_p3_c.jpg", "A third."),
    ]
    for name, note in old_files:
        (lib / name).write_bytes(b"img")
        (lib / (os.path.splitext(name)[0] + ".txt")).write_text(note, encoding="utf-8")
    eligible, excluded = runway.classify_creatives("lasso_ig", str(lib))
    all_keys = {os.path.basename(c.path) for c in eligible} | set(excluded.keys())
    for name, _ in old_files:
        assert name in all_keys, f"old format file {name!r} disappeared from candidate set"


def test_off_style_only_excludes_old_format(monkeypatch, tmp_path):
    """style_exclusions.json only excludes old-format files; lasso_v2_* are immune."""
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "lasso_p1_old.jpg").write_bytes(b"img")
    (lib / "lasso_p1_old.txt").write_text("", encoding="utf-8")
    (lib / "style_exclusions.json").write_text(
        json.dumps({"off_style": ["lasso_p1_old.jpg"]}), encoding="utf-8")
    eligible, excluded = runway.classify_creatives("lasso_ig", str(lib))
    # Old-format file is excluded as off_style
    assert "lasso_p1_old.jpg" in excluded
    assert excluded["lasso_p1_old.jpg"].startswith("off style")
    # No v2 concept is ever off-style
    for key, reason in excluded.items():
        if key.startswith("lasso_v2_"):
            assert not reason.startswith("off style"), (
                f"v2 concept {key!r} wrongly excluded as off style")
    # All 46 v2 concepts are eligible
    eligible_v2 = [os.path.basename(c.path) for c in eligible
                   if os.path.basename(c.path).startswith("lasso_v2_")]
    assert len(eligible_v2) == 46


def test_explain_per_set_breakdown(monkeypatch, tmp_path, capsys):
    """Explain output includes per-set counts (house/b2b/platform/platform_ads)."""
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    lib = str(tmp_path / "empty_lib")
    os.makedirs(lib)
    out = runway.explain("lasso_ig", lib)
    # All four sets must appear in the breakdown line
    assert "by set:" in out["text"]
    assert "house" in out["text"]
    assert "b2b" in out["text"]
    assert "platform_ads" in out["text"]
    # Correct counts: brand(8) + service(8) = 16 house, 10 b2b, 10 platform, 10 platform_ads
    assert "16 house" in out["text"]
    assert "10 b2b" in out["text"]
    assert "10 platform_ads" in out["text"]
    by_set_line = next(l for l in out["text"].splitlines() if "by set:" in l)
    # "platform" not followed by "_ads" (i.e. the pure platform count)
    assert re.search(r"10 platform(?!_)", by_set_line)
    # Output is dash free
    _DASH_RE = re.compile(r"[‐‑‒–—―−-]")
    assert not _DASH_RE.search(out["text"]), "explain output contains a dash"
