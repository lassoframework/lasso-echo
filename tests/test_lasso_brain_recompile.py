"""
LASSO Brain recompile tests (Blake 2026-08-31). Offline; the committed
brand_voice docs ARE the units under test, exactly like the demo-calendar
approved-source tests. Asserts the five recompile targets hold:

  1. VOICE  — lasso_voice.md still parses (CTA rotation + hashtags non-empty)
     and carries the 40-line Full Gym Voice Bank.
  2. PILLARS — the 22 book pillars live in the lasso_now copy bank, every
     line is dash clean, and the daily rotation reaches all of them.
  3. HOOKS  — objection-compiled hooks are reachable as pillar hooks.
  4. PROOF  — the fabrication gate CLEARS the KB-exact 71.9% booking stat with
     its qualifier, and still BLOCKS blends/inventions (adversarial).
  5. HIERARCHY — doctrine.angle_for_pillar stands down for Book: pillars, so
     the book hook leads even with the knowledge flag armed.
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import content_planner, copy_gate, doctrine, knowledge, rotation  # noqa: E402
from agent.voice import load_voice  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VOICE = os.path.join(_ROOT, "brand_voice", "lasso_voice.md")
_NOW = os.path.join(_ROOT, "brand_voice", "lasso_now.md")
_KNOWLEDGE = os.path.join(_ROOT, "brand_voice", "knowledge")


# ---- 1. voice ---------------------------------------------------------------------
def test_voice_bible_still_parses_with_voice_bank():
    v = load_voice(_VOICE)
    assert v is not None
    assert v.ctas, "CTA rotation must still parse after the voice-bank edit"
    assert v.hashtags, "approved hashtags must still parse"
    # a hex color code must never leak in as a hashtag
    assert all(len(h) > 4 for h in v.hashtags)
    # the 40-line Voice Bank is present, quotable lines verbatim
    assert "The Full Gym Voice Bank" in v.raw
    assert '"If you\'re speaking to everyone, you\'re speaking to no one."' in v.raw
    assert '"Clarity reduces friction. Confusion increases it."' in v.raw
    # dash-carrying quotes are marked reference only, never shipped verbatim
    assert v.raw.count("(reference only") >= 3


# ---- 2 + 3. pillars and objection hooks ------------------------------------------
def test_book_pillars_compiled_into_copy_bank():
    doc = content_planner.load_source_doc(_NOW)
    assert doc is not None
    book = [n for n in doc.pillars_with_copy() if n.startswith("Book:")]
    assert len(book) == 22, f"expected 22 book pillars, found {len(book)}"
    # the original five LASSO pillars survive untouched
    for name in ("All in one offer", "Sales are now", "We do the heavy lifting",
                 "The portal", "Proof"):
        assert name in doc.copy_bank, f"original pillar lost: {name}"
    # every hook/body line ships clean through the house-style dash gate
    for name, blk in doc.copy_bank.items():
        for line in blk["hooks"] + blk["bodies"]:
            assert not copy_gate.violations(line), (name, line)
            assert len(line) > 20, (name, line)  # gbp_dogfood fact contract


def test_rotation_reaches_every_book_pillar():
    doc = content_planner.load_source_doc(_NOW)
    n = len(doc.pillars_with_copy())
    seen = set()
    start = datetime.date(2026, 9, 1)
    for i in range(n):
        day = (start + datetime.timedelta(days=i)).isoformat()
        plan = content_planner.plan_for(day, path=_NOW)
        assert not plan.get("blocked"), plan
        assert plan["caption"].strip()
        assert not copy_gate.violations(plan["caption"]), plan["caption"]
        seen.add(plan["pillar"])
    assert sum(1 for p in seen if p.startswith("Book:")) == 22


def test_objection_hooks_are_reachable():
    """Objection-compiled hooks (book-objection-answers.md) sit in the copy
    bank as pillar hooks, so the daily hook rotation can serve them."""
    doc = content_planner.load_source_doc(_NOW)
    all_hooks = [h for blk in doc.copy_bank.values() for h in blk["hooks"]]
    for hook in (
        "You tried Facebook ads and got burned. The ads were never the problem.",
        "Word of mouth built your gym. It will not double it.",
        "The dashboard says your ads are failing. Your front door says otherwise.",
        "Facebook leads are not junk. Unmanaged leads are.",
    ):
        assert hook in all_hooks, f"objection hook not reachable: {hook}"
    # no inline source tags may leak into shippable hook text
    assert not any("(A" in h for h in all_hooks)


# ---- 4. proof stats clear the fabrication gate -----------------------------------
def test_gate_clears_the_71_9_booking_stat_verbatim():
    claims = knowledge.usable_stats_always(_KNOWLEDGE)
    caption = ("71.9% booking rate vs. 18.5% industry average. "
               "297 leads nurtured across 4 gyms.")
    assert rotation.is_gate_clean(caption, approved_claims=claims)


def test_gate_still_blocks_blends_and_inventions():
    claims = knowledge.usable_stats_always(_KNOWLEDGE)
    # blending the pilot stat with the homepage 60% stat must block
    assert not rotation.is_gate_clean(
        "71.9% booking rate vs. 60% industry average.", approved_claims=claims)
    # an invented figure must block
    assert not rotation.is_gate_clean(
        "We book 84.2% of every lead for gyms.", approved_claims=claims)


def test_website_kb_source_registered_with_lead_stat_first():
    path = os.path.join(_KNOWLEDGE, "09_website_kb_2026.md")
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    first = raw.index("71.9% booking rate vs. 18.5% industry average")
    # the lead stat appears before every other proof stat in the file
    for later in ("60% top booking rate", "150K+ total leads generated",
                  "70% close rate"):
        assert first < raw.index(later), f"71.9% must lead, before {later}"
    assert "297 leads nurtured across 4 gyms" in raw
    assert "lassoframework.com/lead-nurture" in raw  # source page citation


# ---- 5. citation hierarchy: the book outranks the platform doc -------------------
def test_doctrine_stands_down_for_book_pillars(monkeypatch):
    fake_pool = [("One platform. Every lead. Zero blind spots.",
                  "platform_2026_positioning")]
    monkeypatch.setattr(doctrine, "platform_angles", lambda: fake_pool)
    # a non-book pillar still resolves a platform angle
    assert doctrine.angle_for_pillar("Sales are now", "2026-09-01") is not None
    # a book pillar keeps its own approved hook (resolver returns None)
    assert doctrine.angle_for_pillar("Book: Copy That Sells", "2026-09-01") is None
    assert doctrine.angle_for_pillar("Book: The Halo Effect", "2026-09-02") is None
