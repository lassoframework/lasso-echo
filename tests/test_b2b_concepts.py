"""
B2B swipe concept tests (10 concepts in the SAME library the house renders
use). Offline. Asserts: all 10 load with a valid schema and render through the
house style builder with no style overrides; every copy string is dash free
(and the scanner itself is proven against a planted dash); every stat concept
carries a cite that resolves against the approved claims source and its
headline clears the fabrication gate while an invented stat still blocks
(adversarial); the 16 existing house concepts are byte untouched (frozen
snapshot hash); regen-library --only works for every new key.
"""

import hashlib
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import creative_studio, knowledge, regen_library, rotation  # noqa: E402

B2B_KEYS = [
    "b2b_five_vendors", "b2b_speed_to_lead", "b2b_35k_caught",
    "b2b_dynamic_spend", "b2b_16_cpl", "b2b_diagnosed_in_order",
    "b2b_ai_search", "b2b_dead_buttons", "b2b_500_gyms", "b2b_ninety_days",
    # July 2026 expansion
    "b2b_flat_revenue", "b2b_dead_leads", "b2b_monday_numbers",
    "b2b_owner_brain", "b2b_thirty_day_diagnose", "b2b_duct_tape",
    "b2b_one_partner", "b2b_done_for_you", "b2b_speed_decay",
    "b2b_retention_first",
]

NEW_B2B_KEYS = B2B_KEYS[10:]  # the July 2026 batch
PILLARS = {"All in one offer", "Sales are now", "The AI agents"}

# every dash family character: em, en, figure, horizontal bar, minus, hyphen
_DASH_RE = re.compile(r"[‐‑‒–—―−-]")

# sha256 of json.dumps(house_concepts, sort_keys=True): the 16 pre-b2b entries
# EXACTLY as shipped. Any byte moved in any of them changes this hash.
HOUSE_SNAPSHOT_SHA256 = (
    "7ba719559c5244f4998aa269d59b4da81573d3b69bc004af1a03db0c0be13378")


def _b2b(key):
    return regen_library.CONCEPTS[key]


def _copy_strings(spec):
    yield spec["headline"]
    yield from spec["concept"]
    yield spec.get("pillar", "")
    yield from spec.get("cite", [])


# ---- all 10 load, schema valid ------------------------------------------------------
def test_all_10_load_with_valid_schema():
    for key in B2B_KEYS:
        assert key in regen_library.CONCEPTS, key
        spec = _b2b(key)
        assert spec["headline"].strip() and len(spec["headline"]) <= 80, key
        assert isinstance(spec["concept"], list) and spec["concept"], key
        assert spec["set"] == "b2b", key
        assert spec["pillar"] in PILLARS, key
        assert spec["archetype"] in creative_studio.ARCHETYPES, key
        assert spec["story"] is False, key
        # story-first context like every house concept
        assert any(l.startswith("Tension:") for l in spec["concept"]), key
        assert any(l.startswith("Resolution:") for l in spec["concept"]), key
    assert len([k for k, v in regen_library.CONCEPTS.items()
                if v.get("set") == "b2b"]) == 20


# ---- dash free, adversarially -------------------------------------------------------
def test_copy_dash_free_adversarial_scan():
    assert _DASH_RE.search("a planted em—dash")        # the scanner works
    assert _DASH_RE.search("a planted hyphen-here")    # ASCII hyphen too
    for key in B2B_KEYS:
        for text in _copy_strings(_b2b(key)):
            assert not _DASH_RE.search(text), f"{key}: dash in {text!r}"


# ---- citations resolve; the gate clears cited stats and still blocks invented ones --
def test_stat_citations_resolve_and_clear_gate(monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    stats = knowledge.usable_stats()
    claims = rotation._approved_claims()
    cited = [k for k in B2B_KEYS if _b2b(k).get("cite")]
    assert set(cited) == {"b2b_35k_caught", "b2b_16_cpl",
                          "b2b_dead_buttons", "b2b_500_gyms",
                          "b2b_speed_decay"}
    for key in cited:
        spec = _b2b(key)
        for cite in spec["cite"]:
            assert any(cite in s for s in stats), f"{key}: cite unresolved: {cite!r}"
        assert rotation.is_gate_clean(spec["headline"], claims), key
    # a digit-bearing headline ALWAYS carries a cite (no uncited stat ships)
    for key in B2B_KEYS:
        spec = _b2b(key)
        if re.search(r"[\d%]", spec["headline"]):
            assert spec.get("cite"), key


def test_invented_stat_still_blocked(monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    claims = rotation._approved_claims()
    for bogus in ("$99,000 in wasted gym ad spend. Found. Named. Fixed.",
                  "$4 blended cost per lead. Verified.",
                  "We recovered $500,000 for gyms last quarter."):
        assert not rotation.is_gate_clean(bogus, claims), bogus


# ---- the 16 existing house concepts are byte untouched ------------------------------
def test_existing_house_concepts_byte_untouched():
    house = {k: v for k, v in regen_library.CONCEPTS.items()
             if v.get("set") not in ("b2b", "platform", "platform_ads")}
    assert len(house) == 16
    blob = json.dumps(house, sort_keys=True).encode()
    assert hashlib.sha256(blob).hexdigest() == HOUSE_SNAPSHOT_SHA256


# ---- regen-library --only works per key; house builder, no style overrides ----------
def test_regen_only_works_for_every_new_key():
    for key in B2B_KEYS:
        only, set_name, dry_run, err = regen_library.parse_args(["--only", key])
        assert err is None and only == key, key
        variants = regen_library.assemble_prompts(key)
        assert [v for v, _ in variants] == ["feed"], key    # no story variants
        prompt = variants[0][1]
        low = prompt.lower()
        # the b2b set now composes through the LOCKED VARIANT SYSTEM (its
        # assigned canvas + layout tokens), still from the shared builder
        spec = _b2b(key)
        assert f"canvas token {spec['canvas']}".lower() in low, key
        assert f"layout token {spec['layout']}".lower() in low, key
        assert "locked brand grammar" in low, key
        assert "readability bar" in low, key
        assert "be clear, not cute" in low, key
        assert spec["headline"] in prompt, key              # slots filled verbatim
        assert "—" not in prompt and "–" not in prompt, key


def test_set_b2b_selects_all_keys(capsys):
    out = regen_library.run(set_name="b2b", dry_run=True)
    assert sorted(out) == sorted(B2B_KEYS)
    _only, set_name, _dry, err = regen_library.parse_args(["--set", "b2b"])
    assert err is None and set_name == "b2b"


# ---- July 2026 expansion: each new concept present, dash free, vendor free --
_VENDOR_RE = re.compile(r"\bvendors?\b", re.IGNORECASE)


def test_new_b2b_10_present_dash_free_vendor_free():
    """Each of the 10 new b2b concepts exists, its headline is dash free and
    does not contain the word vendor or vendors."""
    for key in NEW_B2B_KEYS:
        assert key in regen_library.CONCEPTS, f"missing: {key}"
        spec = regen_library.CONCEPTS[key]
        assert spec["set"] == "b2b", key
        hl = spec["headline"]
        assert not _DASH_RE.search(hl), f"{key}: dash in headline {hl!r}"
        assert not _VENDOR_RE.search(hl), f"{key}: vendor in headline {hl!r}"
        for text in _copy_strings(spec):
            assert not _DASH_RE.search(text), f"{key}: dash in {text!r}"
            assert not _VENDOR_RE.search(text), f"{key}: vendor in {text!r}"
