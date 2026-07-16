"""
Platform ad set tests (grammar V2 Part B). Offline. Asserts: all 10
platform_ads concepts load with the brief's exact canvas/layout (including
the V2 chart/diagram/device layouts); every concept carries a citation that
resolves to a platform_2026 USE line and stat headlines clear the gate; every
CTA routes the quiz; copy is dash free (adversarial scan); the prior sets
(house 16, b2b 10, platform 10) are byte untouched (frozen hashes); the
variance guard stays green across the 46 concept library; regen-library
--set platform_ads selects exactly the ten.
"""

import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import creative_studio, knowledge, regen_library, rotation  # noqa: E402

_DASH_RE = re.compile(r"[‐‑‒–—―−-]")

# (canvas, layout) per the brief, verbatim
BRIEF = {
    "platform_ads_stuck": ("navy", "chart"),
    "platform_ads_handoffs": ("navy", "diagram"),
    "platform_ads_booking_bars": ("split", "chart"),
    "platform_ads_six_engines": ("cream", "diagram"),
    "platform_ads_watched": ("navy", "device"),
    "platform_ads_35k": ("red", "chart"),
    "platform_ads_budget_flow": ("split", "diagram"),
    "platform_ads_five_minutes": ("cream", "device"),
    "platform_ads_quiet_page": ("navy", "device"),
    "platform_ads_websites": ("split", "device"),
}

HOUSE_SHA256 = "7ba719559c5244f4998aa269d59b4da81573d3b69bc004af1a03db0c0be13378"
# stat-slab retired 2026-07-16: the b2b + platform stat concepts remap stat_hero
# -> chart, so these frozen-set hashes were updated in the same commit.
B2B_SHA256 = "fa926da98a6128a4e2fcc001e126c3b3588b142758874ab35c2b91f720a9dd83"
PLATFORM_SHA256 = (
    "56e5a0d10fab98de245e89744eb68a7bcab536328b6fd6cc1cff57b61abe1b6e")


def _ads(key):
    return regen_library.CONCEPTS[key]


def test_all_10_load_with_brief_canvas_and_layout():
    for key, (canvas, layout) in BRIEF.items():
        spec = _ads(key)
        assert spec["set"] == "platform_ads", key
        assert regen_library.variant_for(key) == (canvas, layout), key
        assert len(spec["headline"]) <= 80, key
        assert any(l.startswith("Tension:") for l in spec["concept"]), key
        assert any(l.startswith("Resolution:") for l in spec["concept"]), key
        prompt = regen_library.assemble_prompts(key)[0][1]
        low = prompt.lower()
        assert f"canvas token {canvas}" in low, key
        assert f"layout token {layout}" in low, key
        assert "readability bar" in low, key
        assert spec["headline"] in prompt, key
    assert len([k for k, v in regen_library.CONCEPTS.items()
                if v.get("set") == "platform_ads"]) == 10


def test_every_concept_cited_and_stats_clear_gate(monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    stats = knowledge.usable_stats()
    claims = rotation._approved_claims()
    for key in BRIEF:
        spec = _ads(key)
        assert spec.get("cite"), key                    # EVERY concept cites
        for cite in spec["cite"]:
            hits = [s for s in stats if cite in s]
            assert hits, f"{key}: cite unresolved: {cite!r}"
            assert any("platform_2026" in s for s in hits), key
        # a digit or dollar bearing headline must itself clear the gate
        if re.search(r"[\d%$]", spec["headline"]):
            assert rotation.is_gate_clean(spec["headline"], claims), key
    # the $35K+ support copy clears too (the added receipt phrasing)
    assert rotation.is_gate_clean(
        "Over $17,000 in one audit cycle alone.", claims)


def test_every_cta_routes_the_quiz():
    for key in BRIEF:
        joined = " ".join(_ads(key)["concept"])
        assert "CTA copy (caption, never rendered):" in joined, key
        assert "quiz.lassoframework.com" in joined, key


def test_copy_dash_free_adversarial():
    assert _DASH_RE.search("planted—dash") and _DASH_RE.search("hy-phen")
    for key in BRIEF:
        spec = _ads(key)
        for text in ([spec["headline"]] + spec["concept"]
                     + list(spec.get("cite", []))):
            assert not _DASH_RE.search(text), f"{key}: {text!r}"


def test_prior_sets_byte_untouched():
    h = lambda d: hashlib.sha256(  # noqa: E731
        json.dumps(d, sort_keys=True).encode()).hexdigest()
    by_set = {}
    for k, v in regen_library.CONCEPTS.items():
        by_set.setdefault(v.get("set", "brand")
                          if v.get("set") in ("b2b", "platform", "platform_ads",
                                              "summit_campaign")
                          else "house", {})[k] = v
    assert len(by_set["house"]) == 16 and h(by_set["house"]) == HOUSE_SHA256
    assert len(by_set["b2b"]) == 21 and h(by_set["b2b"]) == B2B_SHA256
    assert len(by_set["platform"]) == 10 and h(by_set["platform"]) == PLATFORM_SHA256


def test_variance_spread_across_46_and_guard_green(monkeypatch, tmp_path):
    counts = {c: 0 for c in creative_studio.CANVAS_ORDER}
    for key in regen_library.CONCEPTS:
        canvas, _layout = regen_library.variant_for(key)
        counts[canvas or "cream"] += 1
    assert len(regen_library.CONCEPTS) == 67
    for canvas, n in counts.items():
        assert n >= 3, counts                           # every canvas represented
    # the guard still alternates with the V2 sidecar canvases in play
    src = tmp_path / "empty_now.md"
    src.write_text("", encoding="utf-8")
    from agent import config
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    lib = tmp_path / "library"
    lib.mkdir()
    for name, canvas in (("lasso_p1_a.jpg", "split"), ("lasso_p2_b.jpg", "split"),
                         ("lasso_p3_c.jpg", "navy")):
        (lib / name).write_bytes(b"img")
        (lib / name.replace(".jpg", ".json")).write_text(
            json.dumps({"note": "A story.", "canvas": canvas}))
    rotation.record_canvas("lasso_ig", "2026-07-06", "split")
    kind, creative = rotation.choose("lasso_ig", "2026-07-07", str(lib))
    assert kind == "library"
    assert rotation.sidecar_canvas(creative.path) == "navy"


def test_set_platform_ads_selects_exactly_the_ten():
    _only, set_name, _dry, err = regen_library.parse_args(["--set", "platform_ads"])
    assert err is None and set_name == "platform_ads"
    out = regen_library.run(set_name="platform_ads", dry_run=True)
    assert sorted(out) == sorted(BRIEF)
