"""
Summit campaign concept tests. Offline. Asserts: all 10 specs load with a
valid schema; every headline is dash free, vendor free, and <= 80 chars;
every concept has Tension: and Resolution: lines; stat headlines (digits
or %) carry a cite that resolves to a summit_campaign USE line; the house
and b2b sets are byte untouched by this addition; regen-library --set
summit_campaign selects exactly the ten.
"""

import hashlib
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import creative_studio, knowledge, regen_library, rotation  # noqa: E402

SUMMIT_KEYS = [
    "summit_announce",
    "summit_playbook",
    "summit_room",
    "summit_flat_year",
    "summit_leaders",
    "summit_handbook",
    "summit_seats_left",
    "summit_final_call",
    "summit_sold_out",
    "summit_countdown",
]

PILLARS = {"The seat", "The playbook", "The room"}

_DASH_RE = re.compile(r"[‐‑‒–—―−-]")
_VENDOR_RE = re.compile(r"\bvendors?\b", re.IGNORECASE)


def _spec(key):
    return regen_library.CONCEPTS[key]


def _copy_strings(spec):
    yield spec["headline"]
    yield from spec["concept"]
    yield from spec.get("cite", [])


# ---- all 10 load, schema valid --------------------------------------------------
def test_all_10_load_with_valid_schema():
    for key in SUMMIT_KEYS:
        assert key in regen_library.CONCEPTS, key
        spec = _spec(key)
        assert spec["headline"].strip() and len(spec["headline"]) <= 80, key
        assert isinstance(spec["concept"], list) and spec["concept"], key
        assert spec["set"] == "summit_campaign", key
        assert spec["pillar"] in PILLARS, key
        assert spec["archetype"] in creative_studio.ARCHETYPES, key
        assert spec["story"] is False, key
        assert any(l.startswith("Tension:") for l in spec["concept"]), key
        assert any(l.startswith("Resolution:") for l in spec["concept"]), key
    assert len([k for k, v in regen_library.CONCEPTS.items()
                if v.get("set") == "summit_campaign"]) == 10


# ---- dash free and vendor free, adversarially -----------------------------------
def test_copy_dash_free_vendor_free():
    assert _DASH_RE.search("planted em—dash")
    assert _DASH_RE.search("planted hyphen-here")
    assert _VENDOR_RE.search("vendor hall")
    for key in SUMMIT_KEYS:
        for text in _copy_strings(_spec(key)):
            assert not _DASH_RE.search(text), f"{key}: dash in {text!r}"
            assert not _VENDOR_RE.search(text), f"{key}: vendor in {text!r}"


# ---- cite resolution and fabrication gate ---------------------------------------
def test_stat_citations_resolve_and_clear_gate(monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    stats = knowledge.usable_stats()
    claims = rotation._approved_claims()
    cited = {k for k in SUMMIT_KEYS if _spec(k).get("cite")}
    assert cited == {"summit_announce", "summit_leaders", "summit_handbook"}
    for key in cited:
        spec = _spec(key)
        for cite in spec["cite"]:
            assert any(cite in s for s in stats), f"{key}: cite unresolved: {cite!r}"
        assert rotation.is_gate_clean(spec["headline"], claims), key
    for key in SUMMIT_KEYS:
        spec = _spec(key)
        if re.search(r"[\d%]", spec["headline"]):
            assert spec.get("cite"), key


# ---- regen-library --set summit_campaign selects exactly the ten ----------------
def test_set_summit_selects_all_keys():
    out = regen_library.run(set_name="summit_campaign", dry_run=True)
    assert sorted(out) == sorted(SUMMIT_KEYS)
    _only, set_name, _dry, err = regen_library.parse_args(["--set", "summit_campaign"])
    assert err is None and set_name == "summit_campaign"


# ---- each concept composes through the variant system ---------------------------
def test_each_concept_assembles_a_valid_prompt():
    for key in SUMMIT_KEYS:
        variants = regen_library.assemble_prompts(key)
        assert [v for v, _ in variants] == ["feed"], key
        prompt = variants[0][1]
        low = prompt.lower()
        spec = _spec(key)
        assert f"canvas token {spec['canvas']}".lower() in low, key
        assert f"layout token {spec['layout']}".lower() in low, key
        assert "readability bar" in low, key
        assert spec["headline"] in prompt, key
        assert "—" not in prompt and "–" not in prompt, key


# ---- footer line present on every concept ---------------------------------------
def test_footer_present_on_every_concept():
    for key in SUMMIT_KEYS:
        lines = _spec(key)["concept"]
        assert any("Nashville" in l and "lassoframework.com" in l for l in lines), key
        assert any("November 7 and 8 2026" in l for l in lines), key
