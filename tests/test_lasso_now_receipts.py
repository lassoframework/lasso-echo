"""
lasso_now.md proof points guard (category rotation: receipts fill).

Every quoted receipt in the Proof points section must resolve verbatim to an
approved knowledge USE line (facts only, cited; nothing invented), carry no dash
character inside the quote, and the word vendor must never appear. Pricing must
stay blocked until Blake writes the exact wording (no invented tiers).
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import knowledge  # noqa: E402

_DASH_RE = re.compile(r"[—–‒‐-]")
_LASSO_NOW = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "brand_voice", "lasso_now.md")


def _proof_section():
    text = open(_LASSO_NOW, encoding="utf-8").read()
    return text.split("## Proof points")[1].split("\n## ")[0]


def _quotes(section):
    return re.findall(r'"([^"]+)"', section)


def test_every_receipt_resolves_to_an_approved_use_line(monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    stats = list(knowledge.usable_stats_always())
    quotes = _quotes(_proof_section())
    assert len(quotes) >= 10, "expected the receipts + case studies to be present"
    for q in quotes:
        assert any(q in s for s in stats), f"unresolved receipt (not an approved USE line): {q!r}"


def test_no_dash_inside_any_receipt_quote():
    for q in _quotes(_proof_section()):
        assert not _DASH_RE.search(q), f"dash character inside receipt: {q!r}"


def test_the_headline_receipts_are_present():
    section = _proof_section()
    for needle in ("$16 blended CPL", "71.9% booked", "$35K+", "70%+", "500+ gym owners"):
        assert needle in section, f"missing headline receipt: {needle}"


def test_named_case_studies_present():
    section = _proof_section()
    for gym in ("Fit Mamas Tribe", "Courage Fitness", "North Naples",
                "Old Glory", "Granite Forged"):
        assert gym in section, f"missing named case study: {gym}"


def test_no_vendor_in_lasso_now():
    text = open(_LASSO_NOW, encoding="utf-8").read()
    assert "vendor" not in text.lower()


def test_pricing_stays_blocked_no_invented_tiers():
    text = open(_LASSO_NOW, encoding="utf-8").read()
    pricing = text.split("## Pricing")[1].split("\n## ")[0]
    # the gate must still be present: no dollar figure invented into the pricing block
    assert "BLOCKED" in pricing
    assert not re.search(r"\$\s?\d", pricing), "a price was invented into the pricing block"
