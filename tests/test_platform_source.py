"""
Platform doctrine source tests (Part A). Offline. Asserts: 08_platform_2026.md
registers as an approved source and the gate clears a draft citing EACH
section (positioning, engines, receipts, every named case study); uncited and
near miss claims still block (adversarial); the 500+ claim stays single
(referenced, never duplicated); existing knowledge files byte untouched by the
read path; every USE line dash free; pricing never appears.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import knowledge, rotation  # noqa: E402

_DASH_RE = re.compile(r"[‐‑‒–—―−-]")
KNOWLEDGE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "brand_voice", "knowledge")

POSITIONING = (
    "One platform. Every lead. Zero blind spots.",
    "Your only job is signing people up.",
    "We chase. You close.",
    "Agencies send reports. LASSO hands you the cockpit.",
    "Honest numbers or no numbers.",
    "Leads do not die in your ads. They die in the handoffs.",
    "More leads never fix a broken sales conversation.",
)

STAT_CLAIMS = (
    "$16 blended CPL across the portfolio; the industry pays 2x.",
    "More than $35K in wasted ad spend saved; $17K flagged in one cycle.",
    "Ad billing reconciled line by line twice monthly.",
    "71.9% booked vs an 18.5% industry average.",
    "297 nurtured, 141 responded, 100+ appointments across four gyms.",
    "8 of 10 paid leads never even reach the average gym calendar.",
    "70%+ trained close rates.",
    "7+ dead buttons on a typical audited gym site.",
)

CASES = (
    "Fit Mamas Tribe took monthly revenue from $19K to $47K on the LASSO",
    "Courage Fitness: First $1M year. $84K MRR.",
    "North Naples CrossFit: 14 clients in 14 days and +27% YoY.",
    "Old Glory Gym: 90% close rate and +$21K.",
    "Granite Forged: +300% signups in month one and +60% YoY.",
    "CrossFit Loup: close rate from 20% to 60%+.",
    "Hoosier CrossFit: +49.3% YoY.",
    "CrossFit Liminal: +66% in 12 months.",
)


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")


def test_platform_source_registers_all_sections(monkeypatch):
    _arm(monkeypatch)
    assert "08_platform_2026.md" in knowledge.load_corpus()
    stats = knowledge.usable_stats()
    for text in POSITIONING + STAT_CLAIMS + CASES:
        assert any(text in s for s in stats), text


def test_gate_clears_drafts_citing_each_section(monkeypatch):
    _arm(monkeypatch)
    claims = rotation._approved_claims()
    for text in STAT_CLAIMS + CASES:
        assert rotation.is_gate_clean(text, claims), text
    # a citing sentence inside a longer caption also clears
    assert rotation.is_gate_clean(
        "The receipts say it plainly. 71.9% booked vs an 18.5% industry average.",
        claims)
    assert rotation.is_gate_clean(
        "Average client value up from $99 to $167 at the same time.", claims)


def test_gate_still_blocks_uncited_and_near_miss_claims(monkeypatch):
    _arm(monkeypatch)
    claims = rotation._approved_claims()
    for bogus in ("91.9% booked vs an 18.5% industry average.",      # near miss
                  "Fit Mamas Tribe went from $19K to $99K MRR.",     # wrong number
                  "$4 blended CPL across the portfolio.",
                  "Every gym doubles revenue in 90 days with a $0 budget."):
        assert not rotation.is_gate_clean(bogus, claims), bogus


def test_500_claim_stays_single(monkeypatch):
    _arm(monkeypatch)
    stats = knowledge.usable_stats()
    assert sum("Trusted by 500+ gym owners" in s for s in stats) == 1


def test_use_lines_dash_free_and_no_pricing():
    text = open(os.path.join(KNOWLEDGE, "08_platform_2026.md"),
                encoding="utf-8").read()
    for raw in text.splitlines():
        if "USE:" in raw:
            assert not _DASH_RE.search(raw.replace("- USE:", "", 1)), raw
    low = text.lower()
    # no pricing anywhere: no dollar figure attached to a monthly cadence and
    # no named tier; "pricing" appears only in the exclusion note itself
    assert not re.search(r"\$\d[\d,k]*\s*(?:per month|a month|/mo|monthly)", low)
    assert not re.search(r"tier\s*(?:\d|one|two|three)", low)
    assert "pricing tiers deliberately do not live here" in re.sub(r"\s+", " ", low)


def test_existing_knowledge_files_byte_untouched(monkeypatch):
    _arm(monkeypatch)
    others = [f for f in sorted(os.listdir(KNOWLEDGE))
              if f.endswith(".md") and f != "08_platform_2026.md"]
    before = {f: open(os.path.join(KNOWLEDGE, f), "rb").read() for f in others}
    knowledge.load_corpus()
    knowledge.usable_stats()
    rotation._approved_claims()
    for f, blob in before.items():
        assert open(os.path.join(KNOWLEDGE, f), "rb").read() == blob, f
