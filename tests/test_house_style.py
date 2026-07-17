"""
Illustrated-diagram house style tests. Asserts the generated-card prompt carries the
locked style constraints (cream canvas, navy headline, illustrated diagram with
uppercase labels and flow arrows, single red accent, one idea per card, no text-only
slabs), and that the rotation guard excludes OFF-STYLE library cards while IN-STYLE
cards still rotate. Offline.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, creative_studio, rotation  # noqa: E402


# ---- generated-card spec carries the style constraints ---------------------------
def test_prompt_carries_illustrated_diagram_style():
    p = creative_studio.build_prompt("Speed to lead wins.", ["Answer fast and win."])
    low = p.lower()
    # cream canvas, never a slab
    assert "cream #faf6f0: the canvas" in low
    assert "never a full bleed solid color slab" in low
    # navy headline, top, the only large text
    assert "navy" in low and "headline at the top" in low
    assert "the only large text" in low
    # illustrated diagram: line icons, uppercase labels, flow arrows
    assert "illustrated diagram" in low
    assert "line-icon illustrations" in low
    assert "uppercase labels" in low
    assert "flow arrows" in low
    # one idea, no text slabs
    assert "one idea per card" in low
    assert "no multi panel text blocks" in low
    assert "no stacked slogans" in low
    assert "text only" in low
    # palette intact; red is the single accent, never a background
    for hexcode in ("#121E3C", "#FF0000", "#5EB9E6", "#FAF6F0"):
        assert hexcode in p
    assert "never a red background" in low
    # house style section 7 typographic system
    assert "eyebrow" in low
    assert "left-aligned" in low or "left aligned" in low
    assert "deck" in low
    assert "never centered" in low
    assert "asymmetric" in low
    assert "depth layer" in low
    # banned composition instructions must NEVER appear in a generated prompt
    assert "centered composition" not in low
    assert "symmetrical" not in low


def test_story_prompt_same_style_designed_from_scratch():
    p = creative_studio.build_prompt("H", ["a body line"], aspect="9:16",
                                     pixels="1080x1920", surface="story")
    low = p.lower()
    assert "9:16" in p and "1080x1920" in p
    assert "never a cropped, stretched, or reused feed card" in low
    assert "illustrated diagram" in low          # same house style, own composition
    assert "cream #faf6f0: the canvas" in low


def test_social_proof_templates_on_cream_canvas():
    for kind in ("quote", "stat"):
        p = creative_studio.build_social_proof_prompt(kind, "Main line", "Support", "A. Name")
        assert "Cream canvas (never a solid color slab)" in p


# ---- rotation reads the exclusion list -------------------------------------------
def _lib(tmp_path, cards, exclusions=None):
    lib = tmp_path / "library"
    lib.mkdir(parents=True, exist_ok=True)
    for name, note in cards:
        (lib / name).write_bytes(b"img-" + name.encode())
        if note:
            (lib / (os.path.splitext(name)[0] + ".txt")).write_text(note, encoding="utf-8")
    if exclusions is not None:
        (lib / "style_exclusions.json").write_text(
            json.dumps({"off_style": exclusions}), encoding="utf-8")
    return str(lib)


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ROTATION_ENABLED", "true")
    monkeypatch.setenv("AGENT_ROTATION_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(exist_ok=True)
    src = tmp_path / "empty_now.md"
    src.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)


def test_off_style_cards_never_selected_in_style_still_rotate(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    lib = _lib(tmp_path,
               [("lasso_p1_offstyle.jpg", "Old slab card."),
                ("lasso_p2_diagram.jpg", "New house style diagram."),
                ("lasso_p3_diagram.jpg", "Another house style diagram.")],
               exclusions=["lasso_p1_offstyle.jpg"])
    picked = []
    for day in ("2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"):
        kind, creative = rotation.choose("lasso_ig", day, lib)
        if creative is not None:
            name = os.path.basename(creative.path)
            picked.append(name)
            rotation.record_served("lasso_ig", name, rotation.pillar_of(name), day)
    assert picked, "in-style cards must still rotate"
    assert "lasso_p1_offstyle.jpg" not in picked          # excluded every single day
    assert set(picked) <= {"lasso_p2_diagram.jpg", "lasso_p3_diagram.jpg"}


def test_real_exclusion_list_covers_the_seed_batch():
    with open("content_library/style_exclusions.json", encoding="utf-8") as fh:
        excluded = set(json.load(fh)["off_style"])
    # every seed slab card is held out until regenerated in the house style
    for name in ("lasso_card_1_final.png", "lasso_p2_speed_to_lead_stat.png",
                 "lasso_p3_funnel_final.png", "lasso_p4_contrast_ads.png"):
        assert name in excluded
