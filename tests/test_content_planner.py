"""
Content-brain planner tests. Uses a SYNTHETIC fixture doc (test data, not the real
brand doc). Verifies: the doc loads, a missing doc blocks, a plan has the four parts,
rotation is deterministic + varied, the caption is a subset of approved lines (no
fabrication), and the CTA is growth-biased.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import content_planner  # noqa: E402


FIXTURE = """# LASSO Now (TEST FIXTURE, synthetic)

## Story
We help gym owners grow without burning out.

## Pillars
- Speed To Lead
- Retention
- Offers

## Pillar copy bank

### Pillar: Speed To Lead
Hook: Leads go cold in minutes.
Body: Answer inside five minutes and you book three times more.
Body: Every hour you wait cuts the odds.

### Pillar: Retention
Hook: Keeping a member beats chasing a new one.
Body: A simple onboarding call lifts ninety day retention.

### Pillar: Offers
Hook: Your offer is the lever.
Body: Name the outcome, not the equipment.

## CTAs
- Read our story.
- Save this post for later.
- Tag a gym owner who needs this.

## Hashtags
#LASSOFramework #GymMarketingMadeSimple #SpeedToLead
"""


def _doc_path(tmp_path):
    p = tmp_path / "lasso_now.md"
    p.write_text(FIXTURE, encoding="utf-8")
    return str(p)


# ---- 1. loads the doc -------------------------------------------------------
def test_loads_doc(tmp_path):
    doc = content_planner.load_source_doc(_doc_path(tmp_path))
    assert doc is not None
    assert doc.story.strip() != ""
    assert set(doc.pillars_with_copy()) == {"Speed To Lead", "Retention", "Offers"}
    assert len(doc.ctas) == 3
    assert "#LASSOFramework" in doc.hashtags


# ---- 2. missing doc blocks --------------------------------------------------
def test_missing_doc_blocks(tmp_path):
    plan = content_planner.plan_for("2026-07-01", path=str(tmp_path / "nope.md"))
    assert plan.get("blocked") is True
    assert "missing" in plan["reason"].lower()


# ---- 3. plan has pillar / caption / cta / hashtags --------------------------
def test_plan_has_all_parts(tmp_path):
    plan = content_planner.plan_for("2026-07-01", path=_doc_path(tmp_path))
    assert not plan.get("blocked")
    assert plan["pillar"] in {"Speed To Lead", "Retention", "Offers"}
    assert plan["caption"].strip() != ""
    assert plan["cta"].strip() != ""
    assert isinstance(plan["hashtags"], list) and plan["hashtags"]


# ---- 4. rotation is deterministic and varied --------------------------------
def test_rotation_deterministic_and_varied(tmp_path):
    doc = content_planner.load_source_doc(_doc_path(tmp_path))
    # deterministic: same day_key -> same pillar
    assert content_planner.pick_pillar(doc, "2026-07-01") == content_planner.pick_pillar(doc, "2026-07-01")
    # varied: consecutive days rotate across pillars
    picks = [content_planner.pick_pillar(doc, f"2026-07-0{d}") for d in range(1, 7)]
    assert len(set(picks)) > 1


# ---- 5. caption is a subset of approved lines (no fabrication) --------------
def test_caption_is_subset_of_approved_lines(tmp_path):
    path = _doc_path(tmp_path)
    doc = content_planner.load_source_doc(path)
    approved = doc.approved_lines()
    for day in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-15"):
        plan = content_planner.plan_for(day, path=path)
        segments = [s.strip() for s in plan["caption"].split("\n\n") if s.strip()]
        assert segments, "caption should not be empty"
        for seg in segments:
            assert seg in approved, f"fabricated line not in source doc: {seg!r}"


# ---- 6. CTA is growth-biased ------------------------------------------------
def test_cta_is_growth_biased(tmp_path):
    doc = content_planner.load_source_doc(_doc_path(tmp_path))
    # "Read our story." is non-growth; save/tag CTAs exist, so a growth CTA must win.
    for seed in ("a", "b", "2026-07-01|Retention", "xyz"):
        cta = content_planner.pick_cta(doc, seed)
        assert any(h in cta.lower() for h in content_planner.GROWTH_CTA_HINTS), cta
