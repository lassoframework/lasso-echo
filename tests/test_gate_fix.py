"""
Fabrication gate fix tests (PART B). Offline. Asserts: the three percent-bearing
sentences in speed_to_lead_carousel now appear as USE lines and clear the gate
even when AGENT_KNOWLEDGE_ENABLED is OFF; adversarial uncited claims still fail;
gate clean notes still pass; usable_stats_always ignores the flag.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import knowledge, rotation  # noqa: E402

_CAROUSEL_NOTE = (
    "Speed to lead is the cheapest growth lever most gyms ignore. "
    "Contact a new lead within 5 minutes and you can lift conversions up to 80 percent. "
    "Most gyms answer in hours. We answer in minutes. "
    "The benchmark is 60 percent show rate on cold traffic. "
    "If your close rate is under 70 percent, the system is the problem, not the salesperson."
)


def test_carousel_passes_gate_with_knowledge_flag_off(monkeypatch):
    """Three stat sentences clear the gate even when AGENT_KNOWLEDGE_ENABLED is OFF."""
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    claims = rotation._approved_claims()
    assert len(claims) > 0, "usable_stats_always must return claims regardless of flag"
    assert rotation.is_gate_clean(_CAROUSEL_NOTE, claims), (
        "speed_to_lead_carousel note must pass gate after USE lines are added")


def test_adversarial_uncited_claim_still_fails(monkeypatch):
    """A claim sentence absent from all USE lines must still fail the gate."""
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    claims = rotation._approved_claims()
    dirty = "Triple your revenue with our exclusive 300 percent growth system."
    assert not rotation.is_gate_clean(dirty, claims), (
        "genuinely uncited stat must still fail the fabrication gate")


def test_gate_clean_note_always_passes(monkeypatch):
    """A note with no stat claims is clean regardless of flag or claims list."""
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    claims = rotation._approved_claims()
    assert rotation.is_gate_clean(
        "Show up every day. Follow up fast. Stay consistent.", claims)
    assert rotation.is_gate_clean("", claims)


def test_usable_stats_always_ignores_flag(monkeypatch):
    """usable_stats_always returns the carousel sentences regardless of flag."""
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    stats_off = knowledge.usable_stats_always()
    assert any("80 percent" in s for s in stats_off), (
        "80 percent sentence must be a USE line")
    assert any("60 percent" in s for s in stats_off), (
        "60 percent sentence must be a USE line")
    assert any("70 percent" in s for s in stats_off), (
        "70 percent sentence must be a USE line")
    # usable_stats (flag-gated) returns nothing while flag is OFF
    assert knowledge.usable_stats() == []


def test_regression_all_46_v2_concepts_gate_clean(monkeypatch):
    """v2 concepts have empty client_note so they are always gate clean."""
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    claims = rotation._approved_claims()
    from agent.runway import v2_library_concepts
    from agent import config
    for c in v2_library_concepts(config.LIBRARY_PATH):
        assert rotation.is_gate_clean(c.client_note, claims), (
            f"v2 concept {c.path!r} unexpectedly failed the gate")
