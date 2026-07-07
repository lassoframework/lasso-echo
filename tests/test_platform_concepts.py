"""
Platform concept set tests (Part B). Offline. Asserts: all 10 load with the
brief's exact canvas/layout through the variant system; every cite resolves to
a platform_2026 USE line and stat headlines clear the gate; the named case
study concepts carry their citations; copy is dash free (adversarial scan);
the prior sets (house 16, b2b 10) are byte untouched (frozen hashes); the
variance guard stays green across the 36 concept library; regen-library
--set platform selects exactly the ten.
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
    "platform_stuck_lasso": ("split", "contrast"),
    "platform_719_booking": ("navy", "stat_hero"),
    "platform_six_engines": ("cream", "framework"),
    "platform_nurture_proof": ("navy", "framework"),
    "platform_8_of_10": ("red", "stat_hero"),
    "platform_fit_mamas": ("cream", "stat_hero"),
    "platform_courage_million": ("navy", "stat_hero"),
    "platform_cockpit": ("navy", "poster"),
    "platform_handoffs": ("red", "poster"),
    "platform_close_first": ("split", "framework"),
}
CASE_CONCEPTS = ("platform_fit_mamas", "platform_courage_million")

# frozen definitions of the prior sets: any byte moved changes these
HOUSE_SHA256 = "7ba719559c5244f4998aa269d59b4da81573d3b69bc004af1a03db0c0be13378"
B2B_SHA256 = "fa0434c42968c9a90bb8e76889c877e7122d363bf919ec920b98e58e053f610d"


def _platform(key):
    return regen_library.CONCEPTS[key]


def test_all_10_load_with_brief_canvas_and_layout():
    for key, (canvas, layout) in BRIEF.items():
        spec = _platform(key)
        assert spec["set"] == "platform", key
        assert regen_library.variant_for(key) == (canvas, layout), key
        assert any(l.startswith("Tension:") for l in spec["concept"]), key
        assert any(l.startswith("Resolution:") for l in spec["concept"]), key
        prompt = regen_library.assemble_prompts(key)[0][1]
        low = prompt.lower()
        assert f"canvas token {canvas}" in low, key
        assert f"layout token {layout}" in low, key
        assert "readability bar" in low, key
        assert spec["headline"] in prompt, key
    assert len([k for k, v in regen_library.CONCEPTS.items()
                if v.get("set") == "platform"]) == 10


def test_citations_resolve_to_platform_anchors(monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    stats = knowledge.usable_stats()
    corpus = knowledge.load_corpus()["08_platform_2026.md"]
    platform_text = " ".join(knowledge.join_items(corpus))
    claims = rotation._approved_claims()
    cited = {k for k in BRIEF if _platform(k).get("cite")}
    assert cited == {"platform_719_booking", "platform_nurture_proof",
                     "platform_8_of_10", "platform_fit_mamas",
                     "platform_courage_million", "platform_close_first"}
    for key in cited:
        for cite in _platform(key)["cite"]:
            hits = [s for s in stats if cite in s]
            assert hits, f"{key}: cite unresolved: {cite!r}"
            # the resolving USE line is a platform_2026 anchored line
            assert any("platform_2026" in s for s in hits), key
            assert cite in platform_text, key
        # the stat headline itself clears the gate
        assert rotation.is_gate_clean(_platform(key)["headline"], claims), key
    # a digit bearing headline in this set ALWAYS carries a cite
    for key in BRIEF:
        if re.search(r"[\d%]", _platform(key)["headline"]):
            assert _platform(key).get("cite"), key


def test_named_case_studies_carry_their_citations(monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    stats = knowledge.usable_stats()
    for key, name in (("platform_fit_mamas", "Fit Mamas Tribe"),
                      ("platform_courage_million", "Courage Fitness")):
        cites = _platform(key)["cite"]
        assert any(name in c for c in cites), key       # the client is NAMED
        for c in cites:
            assert any(c in s for s in stats), (key, c)


def test_copy_dash_free_adversarial():
    assert _DASH_RE.search("planted—dash") and _DASH_RE.search("hy-phen")
    for key in BRIEF:
        spec = _platform(key)
        for text in ([spec["headline"]] + spec["concept"]
                     + list(spec.get("cite", []))):
            assert not _DASH_RE.search(text), f"{key}: {text!r}"


def test_prior_sets_byte_untouched():
    h = lambda d: hashlib.sha256(  # noqa: E731
        json.dumps(d, sort_keys=True).encode()).hexdigest()
    house = {k: v for k, v in regen_library.CONCEPTS.items()
             if v.get("set") not in ("b2b", "platform", "platform_ads",
                                     "summit_campaign")}
    b2b = {k: v for k, v in regen_library.CONCEPTS.items()
           if v.get("set") == "b2b"}
    assert len(house) == 16 and h(house) == HOUSE_SHA256
    assert len(b2b) == 21 and h(b2b) == B2B_SHA256


def test_variance_still_spread_across_36_and_guard_green(monkeypatch, tmp_path):
    # resolved canvases across all VARIANT concepts stay roughly even: every
    # canvas is represented more than once (never a one canvas library)
    counts = {c: 0 for c in creative_studio.CANVAS_ORDER}
    for key in regen_library.CONCEPTS:
        canvas, _layout = regen_library.variant_for(key)
        counts[canvas or "cream"] += 1
    assert len(regen_library.CONCEPTS) == 67
    for canvas, n in counts.items():
        assert n >= 3, counts
    # and the guard itself still alternates on a mixed canvas library
    src = tmp_path / "empty_now.md"
    src.write_text("", encoding="utf-8")
    from agent import config
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    lib = tmp_path / "library"
    lib.mkdir()
    for name, canvas in (("lasso_p1_a.jpg", "navy"), ("lasso_p2_b.jpg", "split"),
                         ("lasso_p3_c.jpg", "red"), ("lasso_p4_d.jpg", "cream")):
        (lib / name).write_bytes(b"img")
        (lib / name.replace(".jpg", ".json")).write_text(
            json.dumps({"note": "A story.", "canvas": canvas}))
    rotation.record_canvas("lasso_ig", "2026-07-06", "navy")
    kind, creative = rotation.choose("lasso_ig", "2026-07-07", str(lib))
    assert kind == "library"
    assert rotation.sidecar_canvas(creative.path) != "navy"


def test_set_platform_selects_exactly_the_ten(capsys):
    _only, set_name, _dry, err = regen_library.parse_args(["--set", "platform"])
    assert err is None and set_name == "platform"
    out = regen_library.run(set_name="platform", dry_run=True)
    assert sorted(out) == sorted(BRIEF)
