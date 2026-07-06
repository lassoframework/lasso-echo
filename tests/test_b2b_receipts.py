"""
B2B Ad Swipe File receipts (July 2026, Blake approved) in the approved claims
source the fabrication gate reads. Asserts: each receipt is a USE stat; a draft
citing each receipt clears the gate; an uncited claim is still blocked
(adversarial); the 500+ claim stays single (referenced, never duplicated); with
the knowledge flag OFF the source is silent and the gate stays conservative.
Offline.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import knowledge, rotation  # noqa: E402

# the exact receipt wording shipped in 02_verified_stats.md (B2B swipe section)
RECEIPTS = (
    "$16 blended cost per lead. Blended across the LASSO portfolio, roughly "
    "half typical industry cost.",
    "The Ad Engine has caught more than $35,000 in wasted gym ad spend. One "
    "recent audit cycle flagged over $17,000.",
    "Ad billing reconciled line by line twice a month.",
    "A recent gym website audit found 7 dead buttons including the primary CTA.",
)


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")


def test_receipts_are_use_stats(monkeypatch):
    _arm(monkeypatch)
    stats = knowledge.usable_stats()
    for receipt in RECEIPTS:
        assert any(receipt in s for s in stats), receipt


def test_gate_clears_drafts_citing_each_receipt(monkeypatch):
    _arm(monkeypatch)
    claims = rotation._approved_claims()
    for receipt in RECEIPTS:
        assert rotation.is_gate_clean(receipt, claims), receipt
    # citing a receipt sentence inside a longer draft also clears
    assert rotation.is_gate_clean(
        "The receipts are real. One recent audit cycle flagged over $17,000.",
        claims)


def test_500_claim_referenced_not_duplicated(monkeypatch):
    _arm(monkeypatch)
    stats = knowledge.usable_stats()
    assert sum("Trusted by 500+ gym owners" in s for s in stats) == 1
    assert rotation.is_gate_clean("Trusted by 500+ gym owners.",
                                  rotation._approved_claims())


def test_gate_still_blocks_uncited_claims(monkeypatch):
    _arm(monkeypatch)
    claims = rotation._approved_claims()
    for bogus in ("We cut every gym's CPL to $4 last month.",
                  "$99,000 in wasted spend recovered this week.",
                  "Our engine saves 80 percent of your ad budget."):
        assert not rotation.is_gate_clean(bogus, claims), bogus


def test_receipts_carry_no_dash_characters():
    import re
    for receipt in RECEIPTS:
        assert not re.search(r"[‐-―−-]", receipt), receipt


def test_flag_off_generation_silent_gate_still_reads_use_lines(monkeypatch):
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    # usable_stats (generation path) is correctly empty while the flag is OFF
    assert knowledge.usable_stats() == []
    # _approved_claims always reads USE lines so an approved receipt still clears
    claims = rotation._approved_claims()
    assert len(claims) > 0, "usable_stats_always must load claims regardless of flag"
    assert rotation.is_gate_clean(RECEIPTS[0], claims), (
        "a USE-line receipt must pass the gate even with knowledge flag OFF")
    # genuinely uncited claims still fail
    assert not rotation.is_gate_clean(
        "Guaranteed $99,000 in wasted spend recovered this week.", claims)
