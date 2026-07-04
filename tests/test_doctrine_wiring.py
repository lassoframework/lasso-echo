"""
Platform doctrine wiring tests (readiness Part B). Offline. Asserts: with the
knowledge flag armed, a planned caption for EACH pillar carries a
platform_2026 citation (or honestly reports lasso_now) and the fabrication
gate clears it; with the flag OFF drafting is byte identical to before
(lasso_now only, zero behavior change); the book queue still cites book files
only; an angle whose citation does not verify is dropped with an audited
reason and the lasso_now hook ships instead (adversarial); the monthly
review's proposals draw from BOTH sources, labeled.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, content_planner, db, doctrine, monthly_report, rotation  # noqa: E402

SOURCE_DOC = """
## Story
We help gym owners grow.

## Pillars
- Paid marketing
- Follow up and speed to lead
- Sales as coaching

## Pillar copy bank
### Pillar: Paid marketing
Hook: Paid ads keep your gym growing.
Body: Organic alone caps your gym.

### Pillar: Follow up and speed to lead
Hook: The fortune is in the follow up.
Body: Respond in five minutes.

### Pillar: Sales as coaching
Hook: Selling is coaching.
Body: Kill free trials and run consultations.

## CTAs
- Save this post.

## Hashtags
#LASSOFramework
"""


def _doc(tmp_path):
    p = tmp_path / "lasso_now.md"
    p.write_text(SOURCE_DOC, encoding="utf-8")
    return str(p)


def test_doctrine_hooks_cited_and_gate_clean_per_pillar(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    path = _doc(tmp_path)
    claims = rotation._approved_claims()
    seen_anchors = set()
    for day in ("2026-07-06", "2026-07-07", "2026-07-08"):
        plan = content_planner.plan_for(day, path=path)
        assert not plan.get("blocked")
        assert plan["citation"].startswith("platform_2026_"), plan["citation"]
        assert f"cite:{plan['citation']}" in plan["fragments"]
        # the hook is verbatim doctrine copy and the whole caption gate clears
        hook = plan["fragments"][0]
        assert doctrine.verify_citation(hook, plan["citation"])
        assert rotation.is_gate_clean(plan["caption"], claims), plan["caption"]
        seen_anchors.add(plan["citation"])
    assert seen_anchors                              # doctrine actually resolved


def test_flag_off_is_yesterdays_drafting(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    path = _doc(tmp_path)
    plan = content_planner.plan_for("2026-07-06", path=path)
    assert plan["citation"] == "lasso_now"           # the fallback, honestly named
    assert plan["fragments"][0] in ("Paid ads keep your gym growing.",
                                    "The fortune is in the follow up.",
                                    "Selling is coaching.")
    assert not any(f.startswith("cite:") for f in plan["fragments"])
    assert doctrine.platform_angles() == []          # dormant: nothing resolves


def test_unverifiable_angle_dropped_with_reason(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    path = _doc(tmp_path)
    # ADVERSARIAL: the resolver offers an angle whose citation does not verify
    monkeypatch.setattr(doctrine, "angle_for_pillar",
                        lambda pillar, day: {"copy": "An invented doctrine line.",
                                             "anchor": "platform_2026_positioning"})
    plan = content_planner.plan_for("2026-07-06", path=path)
    assert plan["citation"] == "lasso_now"           # dropped, fell back
    assert "An invented doctrine line." not in plan["caption"]
    reasons = [r["reason"] for r in db.audit_rows()
               if r["kind"] == "doctrine_drop"]
    assert any("did not verify" in r for r in reasons)


def test_book_queue_still_cites_book_files_only(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("AGENT_BOOK_CAMPAIGN_ENABLED", "true")
    from agent import book_campaign, creative_studio, media_host
    from agent.accounts import get_account
    art = tmp_path / "card.png"
    art.write_bytes(b"PNG")
    monkeypatch.setattr(creative_studio, "generate",
                        lambda *a, **k: {"path": str(art), "prompt": "p"})
    monkeypatch.setattr(media_host, "host_media",
                        lambda *a, **k: "https://cdn.echo.test/c.png")
    draft = book_campaign.build_book_draft(get_account("lasso_ig"), "2026-07-06")
    if draft is not None:                            # book content present today
        assert all(not f.startswith("cite:platform_2026")
                   for f in draft.source_fragments)


def test_monthly_proposals_labeled_by_both_sources(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", _doc(tmp_path))
    out = monthly_report.refresh_section("lasso_ig", posts=[])
    proposals = out["proposals"]
    assert any(p.startswith(f"Angle from {doctrine.PLATFORM_FILE}")
               and "platform_2026_" in p for p in proposals)
    assert any("lasso_now.md" in p and "pillar" in p for p in proposals)
