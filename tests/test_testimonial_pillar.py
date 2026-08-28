"""
tests/test_testimonial_pillar.py — the LASSO owner-voice proof pillar
(AGENT_LASSO_TESTIMONIAL_PILLAR, default OFF; report-card build 2026-08-28).

  * PLAN: the pillar recurs (alternate Tuesdays borrow the doctrine day);
    flag/param off -> byte-for-byte no testimonial anywhere.
  * SOURCE: the builder drafts ONLY from approved social-proof entries
    (Permission: yes + Verified date); the caption carries only approved lines.
  * NO-FABRICATION FALLBACK (the hard rail): no approved source -> the builder
    returns None and the month build fills the day from an EXISTING real
    pillar; no quote or number is ever invented.
All offline.
"""
from __future__ import annotations

import pytest

from agent import real_month_planner as rmp
from agent import testimonial_pillar as tp


APPROVED_DOC = """# Social proof

## Entry
Stat: Fit Mamas Tribe took monthly revenue from $19K to $47K on the LASSO system.
Support: Average client value up from $99 to $167 at the same time.
Attribution: Lauren and Christina, Fit Mamas Tribe
Permission: yes
Verified: 2026-08-01

## Entry
Quote: The LASSO system gave us our weekends back.
Attribution: A partner gym owner
Permission: yes
Verified: 2026-08-01
"""

UNAPPROVED_DOC = """# Social proof

## Entry
Quote: Nobody signed off on this quote.
Attribution: Somebody
Permission: no
Verified:
"""


class _Acct:
    key = "lasso"
    platform = "instagram"
    social_proof_doc = ""


# ---------------------------------------------------------------------------
# Plan: the pillar recurs, flag-gated
# ---------------------------------------------------------------------------

def test_plan_gets_recurring_testimonial_slot_when_armed():
    plan = rmp.plan_month("lasso", "2026-11-01", days=30, testimonial=True)
    tdays = sorted({s.post_date for s in plan if s.category == "testimonial"})
    assert len(tdays) >= 2, "expected a recurring owner-voice slot"
    from datetime import date
    for d in tdays:
        assert date.fromisoformat(d).weekday() == 1  # the borrowed Tuesday
    # doctrine stays represented on the off weeks
    assert any(s.category == "doctrine" for s in plan)


def test_plan_flag_off_and_env_default_have_no_testimonial(monkeypatch):
    monkeypatch.delenv("AGENT_LASSO_TESTIMONIAL_PILLAR", raising=False)
    for kwargs in ({"testimonial": False}, {}):
        plan = rmp.plan_month("lasso", "2026-11-01", days=30, **kwargs)
        assert not any(s.category == "testimonial" for s in plan)


# ---------------------------------------------------------------------------
# Builder: approved source only
# ---------------------------------------------------------------------------

def test_builder_uses_only_approved_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_TESTIMONIAL_PILLAR", "true")
    doc = tmp_path / "social_proof.md"
    doc.write_text(APPROVED_DOC, encoding="utf-8")

    from agent import creative_studio, media_host
    monkeypatch.setattr(creative_studio, "generate_social_proof",
                        lambda *a, **k: {"path": "/tmp/fake_card.png"})
    monkeypatch.setattr(media_host, "host_media",
                        lambda *a, **k: "https://cdn.test/card.png")

    draft = tp.build_testimonial_draft(_Acct(), "2026-11-10", path=str(doc))
    assert draft is not None
    assert draft.category == "testimonial"
    assert str(getattr(draft.status, "value", draft.status)).lower() == "pending"
    # the caption is BUILT from the approved entry lines and nothing else
    entries = tp.approved_entries(_Acct(), path=str(doc))
    all_approved_lines = {ln for e in entries for ln in e.approved_lines()}
    for line in (draft.caption or "").split("\n\n"):
        line = line.strip()
        if line:
            assert line in all_approved_lines, f"invented line: {line!r}"
    # audit trail carries the verified entry text only
    assert draft.source_fragments
    assert set(draft.source_fragments) <= all_approved_lines
    # house style: no dashes
    from agent import copy_gate
    assert copy_gate.violations(draft.caption) == []


def test_builder_flag_off_is_none(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_LASSO_TESTIMONIAL_PILLAR", raising=False)
    doc = tmp_path / "social_proof.md"
    doc.write_text(APPROVED_DOC, encoding="utf-8")
    assert tp.build_testimonial_draft(_Acct(), "2026-11-10", path=str(doc)) is None


def test_builder_returns_none_without_approved_source(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_TESTIMONIAL_PILLAR", "true")
    # missing file
    assert tp.build_testimonial_draft(
        _Acct(), "2026-11-10", path=str(tmp_path / "missing.md")) is None
    # entries exist but NOTHING is approved (no permission / no verified date)
    doc = tmp_path / "unapproved.md"
    doc.write_text(UNAPPROVED_DOC, encoding="utf-8")
    assert tp.build_testimonial_draft(_Acct(), "2026-11-10", path=str(doc)) is None


def test_builder_returns_none_when_studio_or_hosting_dark(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_TESTIMONIAL_PILLAR", "true")
    doc = tmp_path / "social_proof.md"
    doc.write_text(APPROVED_DOC, encoding="utf-8")
    from agent import creative_studio, media_host
    monkeypatch.setattr(creative_studio, "generate_social_proof",
                        lambda *a, **k: None)
    assert tp.build_testimonial_draft(_Acct(), "2026-11-10", path=str(doc)) is None
    monkeypatch.setattr(creative_studio, "generate_social_proof",
                        lambda *a, **k: {"path": "/tmp/fake.png"})
    monkeypatch.setattr(media_host, "host_media", lambda *a, **k: None)
    assert tp.build_testimonial_draft(_Acct(), "2026-11-10", path=str(doc)) is None


# ---------------------------------------------------------------------------
# The no-fabrication fallback: a dark testimonial slot fills from a REAL pillar
# ---------------------------------------------------------------------------

def test_dark_testimonial_slot_falls_back_to_existing_pillar():
    class _Draft:
        def __init__(self, caption, category=""):
            self.caption = caption
            self.category = category
            self.day_key = ""
            self.draft_type = ""
            self.is_story = False

    builders = {
        # testimonial has NO approved source: returns None, exactly like the
        # real builder's hard rail
        "testimonial": lambda t, d: None,
        # the existing real pillars still have content
        "podcast": lambda t, d: _Draft("A real podcast concept for the day."),
    }
    plan = [rmp.PlanSlot(post_date="2026-11-10", category="testimonial",
                         fmt=rmp.FEED)]
    drafts = rmp.build_month_drafts(plan, builders)
    assert len(drafts) == 1
    # the day filled from an EXISTING pillar; nothing testimonial was invented
    assert drafts[0].category == "podcast"
    assert drafts[0].caption == "A real podcast concept for the day."


def test_real_builders_map_wires_testimonial():
    from agent import real_month_run as rmr

    class _A:
        key = "lasso"
        platform = "instagram"
        voice_path = None

        def library_path(self):
            return None

    builders = rmr.real_builders_map(_A())
    assert "testimonial" in builders and callable(builders["testimonial"])
    # flag off -> the wired builder is inert (returns None, never raises)
    import os
    os.environ.pop("AGENT_LASSO_TESTIMONIAL_PILLAR", None)
    assert builders["testimonial"](None, "2026-11-10") is None
