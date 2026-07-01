"""
Caption SEO tests (content brain, flag AGENT_CAPTION_SEO_ENABLED, default OFF).

The SEO pass only REORDERS approved body lines so a line carrying the hook's key
topic terms sits first after the hook. Asserts: flag OFF leaves today's order
untouched; flag ON front-loads the topic-bearing body while the hook stays the
first caption line; every fragment is still an approved source-doc line (no
fabrication); and when no reorder can satisfy placement the original order is kept.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import content_planner  # noqa: E402

DAY = "2026-07-01"

# One pillar so the rotation is deterministic. Hook topic terms: leads, cold,
# minutes. Body 1 carries none; body 2 carries "leads".
FIXTURE = """# LASSO Now (SEO TEST FIXTURE)

## Pillars
- Speed To Lead

## Pillar copy bank

### Pillar: Speed To Lead
Hook: Leads go cold in minutes.
Body: Answer fast and you book three times as many consults.
Body: Speed to lead wins because leads cool off fast.

## CTAs
- Save this post for later.

## Hashtags
#LASSOFramework #GymMarketingMadeSimple
"""

BODY_1 = "Answer fast and you book three times as many consults."
BODY_2 = "Speed to lead wins because leads cool off fast."
HOOK = "Leads go cold in minutes."

# No body carries a hook topic term here.
NO_MATCH_FIXTURE = FIXTURE.replace(BODY_2, "Answer the phone and book the consult.")


def _doc(tmp_path, content=FIXTURE):
    p = tmp_path / "lasso_now.md"
    p.write_text(content, encoding="utf-8")
    return str(p)


# ---- 1. flag OFF -> today's order, byte for byte ------------------------------
def test_flag_off_keeps_original_order(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_CAPTION_SEO_ENABLED", raising=False)
    plan = content_planner.plan_for(DAY, path=_doc(tmp_path))
    lines = plan["caption"].split("\n\n")
    assert lines[0] == HOOK
    assert lines[1] == BODY_1
    assert lines[2] == BODY_2


# ---- 2. flag ON -> topic-bearing body moves first, hook still leads ------------
def test_flag_on_front_loads_topic_body_hook_first(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CAPTION_SEO_ENABLED", "true")
    plan = content_planner.plan_for(DAY, path=_doc(tmp_path))
    lines = plan["caption"].split("\n\n")
    assert lines[0] == HOOK                    # front-loaded: hook is the first line
    assert lines[1] == BODY_2                  # the "leads" body moved up
    assert lines[2] == BODY_1                  # the rest keep their order


# ---- 3. no fabrication: every fragment is an approved line --------------------
def test_fragments_all_approved_with_seo_on(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CAPTION_SEO_ENABLED", "true")
    path = _doc(tmp_path)
    plan = content_planner.plan_for(DAY, path=path)
    approved = content_planner.load_source_doc(path).approved_lines()
    assert plan["fragments"]
    for frag in plan["fragments"]:
        assert frag in approved, f"fabricated fragment: {frag!r}"
    # nothing dropped either: both bodies still present
    assert BODY_1 in plan["fragments"] and BODY_2 in plan["fragments"]


# ---- 4. no reorder can satisfy placement -> keep the original order ------------
def test_no_match_keeps_original_order(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CAPTION_SEO_ENABLED", "true")
    plan = content_planner.plan_for(DAY, path=_doc(tmp_path, NO_MATCH_FIXTURE))
    lines = plan["caption"].split("\n\n")
    assert lines[0] == HOOK
    assert lines[1] == BODY_1                  # original order, nothing invented
    assert lines[2] == "Answer the phone and book the consult."


# ---- 5. the reorder helper is stable and selection-only ------------------------
def test_seo_order_bodies_reorders_only(monkeypatch):
    monkeypatch.setenv("AGENT_CAPTION_SEO_ENABLED", "true")
    bodies = [BODY_1, BODY_2, "A third approved line about retention."]
    out = content_planner.seo_order_bodies(HOOK, bodies)
    assert sorted(out) == sorted(bodies)       # same lines, nothing added or lost
    assert out[0] == BODY_2                    # topic-bearing line leads
    assert out[1:] == [BODY_1, "A third approved line about retention."]
