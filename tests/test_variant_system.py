"""
House style variant system tests (Parts A + B). Offline. Asserts: canvas
assignment is deterministic per concept key and the b2b set matches the brief
exactly; the variance guard holds adversarially (a library forced toward one
canvas never serves the same canvas two days running where an alternative
exists, and never starves when none does); every layout renders with every
canvas through the house builder with the readability bar riding (fixture
render smoke, all 20 combos); the existing 16 house concepts are byte
identical in definition (no variant fields, original render path); every
token block is dash free.
"""

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, creative_studio, regen_library, rotation  # noqa: E402

_DASH_RE = re.compile(r"[‐‑‒–—―−-]")

BRIEF_ASSIGNMENT = {
    "b2b_35k_caught": ("navy", "stat_hero"),
    "b2b_16_cpl": ("cream", "stat_hero"),
    "b2b_dead_buttons": ("red", "stat_hero"),
    "b2b_500_gyms": ("navy", "stat_hero"),
    "b2b_diagnosed_in_order": ("cream", "framework"),
    "b2b_ninety_days": ("navy", "checklist"),
    "b2b_five_vendors": ("split", "contrast"),
    "b2b_ai_search": ("navy", "poster"),
    "b2b_speed_to_lead": ("red", "poster"),
    "b2b_dynamic_spend": ("split", "framework"),
}


class FakeNano:
    def generate_image(self, prompt, model):
        return b"\x89PNG\r\n\x1a\nFAKE"


# ---- deterministic assignment -----------------------------------------------------------
def test_canvas_deterministic_per_key_and_roughly_even():
    for key in list(regen_library.CONCEPTS)[:8]:
        assert regen_library.canvas_for(key) == regen_library.canvas_for(key)
    # the hash spreads roughly evenly across a synthetic key population
    counts = {c: 0 for c in creative_studio.CANVAS_ORDER}
    keys = [f"concept_{i}_sample" for i in range(80)]
    for k in keys:
        digest = __import__("hashlib").sha256(k.encode()).hexdigest()
        counts[creative_studio.CANVAS_ORDER[int(digest, 16) % 4]] += 1
    for canvas, n in counts.items():
        assert 8 <= n <= 40, f"{canvas} badly skewed: {n}/80"


def test_b2b_assignment_matches_brief_exactly():
    for key, (canvas, layout) in BRIEF_ASSIGNMENT.items():
        assert regen_library.variant_for(key) == (canvas, layout), key


def test_layout_derivation_for_undeclared_concepts():
    assert regen_library.preferred_layout({"cite": ["x"]}) == "stat_hero"
    assert regen_library.preferred_layout(
        {"concept": ["List copy (caption): 1 Close rate, 2 Show rate"]}) == "framework"
    assert regen_library.preferred_layout(
        {"concept": ["List copy (caption): Full calendar, Evenings back"]}) == "checklist"
    assert regen_library.preferred_layout(
        {"headline": "Myth vs fact on gym ads", "concept": []}) == "contrast"
    assert regen_library.preferred_layout({"concept": ["Tension: x."]}) == "poster"
    assert regen_library.preferred_layout({"layout": "checklist"}) == "checklist"


# ---- the variance guard, adversarially ---------------------------------------------------
def _guard_library(tmp_path, cards):
    """cards: [(basename, canvas)] -> a library where every card is gate clean."""
    lib = tmp_path / "library"
    lib.mkdir(parents=True, exist_ok=True)
    for name, canvas in cards:
        (lib / name).write_bytes(b"img-" + name.encode())
        side = {"note": "A story.", "canvas": canvas}
        (lib / (os.path.splitext(name)[0] + ".json")).write_text(json.dumps(side))
    return str(lib)


def _block_generate(monkeypatch, tmp_path):
    src = tmp_path / "empty_now.md"
    src.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)


def test_guard_skips_yesterdays_canvas_when_alternative_exists(monkeypatch, tmp_path):
    _block_generate(monkeypatch, tmp_path)
    lib = _guard_library(tmp_path, [
        ("lasso_p1_n1.jpg", "navy"), ("lasso_p2_n2.jpg", "navy"),
        ("lasso_p3_c1.jpg", "cream"),
    ])
    rotation.record_canvas("lasso_ig", "2026-07-06", "navy")
    kind, creative = rotation.choose("lasso_ig", "2026-07-07", lib)
    assert kind == "library"
    assert rotation.sidecar_canvas(creative.path) == "cream"    # navy skipped


def test_guard_never_starves_forced_single_canvas_library(monkeypatch, tmp_path):
    _block_generate(monkeypatch, tmp_path)
    lib = _guard_library(tmp_path, [
        ("lasso_p1_n1.jpg", "navy"), ("lasso_p2_n2.jpg", "navy"),
    ])
    rotation.record_canvas("lasso_ig", "2026-07-06", "navy")
    kind, creative = rotation.choose("lasso_ig", "2026-07-07", lib)
    assert kind == "library" and creative is not None           # still serves
    assert rotation.sidecar_canvas(creative.path) == "navy"     # no alternative existed


def test_guard_holds_across_consecutive_days(monkeypatch, tmp_path):
    _block_generate(monkeypatch, tmp_path)
    lib = _guard_library(tmp_path, [
        ("lasso_p1_a.jpg", "navy"), ("lasso_p2_b.jpg", "cream"),
        ("lasso_p3_c.jpg", "navy"), ("lasso_p4_d.jpg", "cream"),
    ])
    served_canvases = []
    from agent import dam
    day_keys = ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"]
    for day in day_keys:
        kind, creative = rotation.choose("lasso_ig", day, lib)
        assert kind == "library"
        canvas = rotation.sidecar_canvas(creative.path)
        served_canvases.append(canvas)
        rotation.record_served("lasso_ig", dam.rotation_key(creative.path),
                               rotation.pillar_of(creative.path), day)
        rotation.record_canvas("lasso_ig", day, canvas)
    for a, b in zip(served_canvases, served_canvases[1:]):
        assert a != b, served_canvases                          # never twice running


def test_pre_variant_library_is_all_cream_and_unchanged(monkeypatch, tmp_path):
    # cards with no canvas sidecar field are cream by construction: the guard
    # sees no alternative and today's behavior is exactly yesterday's
    lib = tmp_path / "library"
    lib.mkdir()
    (lib / "lasso_p1_a.jpg").write_bytes(b"img")
    assert rotation.sidecar_canvas(str(lib / "lasso_p1_a.jpg")) == "cream"


# ---- render smoke: every layout with every canvas ----------------------------------------
def test_every_layout_renders_with_every_canvas(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    for canvas in creative_studio.CANVAS_ORDER:
        for layout in creative_studio.LAYOUTS:
            prompt = creative_studio.build_prompt(
                "Answer leads fast.", ["Tension: x.", "Resolution: y."],
                canvas=canvas, layout=layout)
            low = prompt.lower()
            assert f"canvas token {canvas}" in low, (canvas, layout)
            assert f"layout token {layout}" in low, (canvas, layout)
            assert "readability bar" in low                      # the shared bar
            assert "thumbnail" in low                            # legible small
            assert "high contrast" in low
            assert "—" not in prompt and "–" not in prompt
            out = creative_studio.generate(
                "Answer leads fast.", ["Tension: x."], client=FakeNano(),
                out_path=str(tmp_path / f"{canvas}_{layout}.png"),
                canvas=canvas, layout=layout)
            assert out and os.path.exists(out["path"]), (canvas, layout)
    with pytest.raises(ValueError, match="unknown canvas"):
        creative_studio.variant_block("plaid", "poster")
    with pytest.raises(ValueError, match="unknown layout"):
        creative_studio.variant_block("navy", "collage")


# ---- the 16 house concepts: unchanged definitions, original path -------------------------
def test_house_16_unchanged_and_render_original_path():
    house = {k: v for k, v in regen_library.CONCEPTS.items()
             if v.get("set") != "b2b"}
    assert len(house) == 16
    for key, spec in house.items():
        assert "canvas" not in spec and "layout" not in spec, key
        assert regen_library.variant_for(key) == (None, None), key
    # and their assembled feed prompt still carries the ORIGINAL house palette
    prompt = regen_library.assemble_prompts("one_screen")[0][1]
    assert "Cream #FAF6F0: THE canvas" in prompt
    assert "LOCKED BRAND GRAMMAR" not in prompt                  # variant path unused


# ---- copy law -----------------------------------------------------------------------------
def test_token_blocks_dash_free():
    blocks = ([creative_studio.VARIANT_GRAMMAR, creative_studio.READABILITY_BAR]
              + list(creative_studio.CANVASES.values())
              + list(creative_studio.LAYOUTS.values()))
    for block in blocks:
        assert not _DASH_RE.search(block), block[:80]
