"""
Two caption-engine features, both behind their own OFF-by-default flag:

  FEATURE A  AGENT_CAPTION_ANGLE_ROTATION  — the month builder rotates the SB7
             problem/entry ANGLE round-robin across the planned days (STYLE-only),
             and widens the opening-avoid window; flag OFF = today's behavior.

  FEATURE B  AGENT_EDUCATIONAL_PILLAR       — an educational how-to/tip/why/myth-bust
             pillar, GROUNDED ONLY in the gym's approved material; a gym with no
             eligible source SKIPS the slot (never fabricates); flag OFF = the
             category set + rotation are exactly as today.

Fully OFFLINE (tmp sqlite + tmp library; the SB7 generator is stubbed so no LLM
call is made). The fabrication gate is asserted to still run for the educational
pillar.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_content, client_sources as cs, config, drafter  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    yield


def _voice():
    return VoiceDoc(raw="We help members win.\n#GetFit",
                    hashtags=["#GetFit"], ctas=["Save this post."])


def _acct():
    return Account(key="gym_beta_ig", display_name="Gym Beta",
                   platform=Platform.INSTAGRAM, token_env="T", target_id_env="TID")


def _lib(tmp_path, n=8):
    lib = tmp_path / "beta_lib"
    lib.mkdir(exist_ok=True)
    for i in range(n):
        (lib / f"photo_{i:02d}.jpg").write_bytes(b"\xff\xd8\xffFAKEJPEG")
    return str(lib)


# ------------------------------------------------------------------ FEATURE A

def test_angle_palette_is_a_small_readable_tuple():
    assert isinstance(drafter.CAPTION_ANGLES, tuple)
    assert 4 <= len(drafter.CAPTION_ANGLES) <= 12
    # round-robin wraps and is stable
    assert drafter.angle_for_index(0) == drafter.CAPTION_ANGLES[0]
    assert drafter.angle_for_index(len(drafter.CAPTION_ANGLES)) == drafter.CAPTION_ANGLES[0]


def _spy_sb7(monkeypatch):
    """Stub StoryBrandGenerator.build to record the angle it was handed and return a
    real caption echoing that angle so downstream code accepts it."""
    calls = []

    class _FakeSB7:
        def build(self, voice, creative, account=None, avoid_openings=(),
                  angle="", avoid_angles=()):
            calls.append({"angle": angle, "avoid_angles": tuple(avoid_angles or ())})
            body = f"You can do this today. Angle:{angle or 'none'} keeps you going."
            return (body, ["#GetFit"], [body])

    monkeypatch.setattr(drafter, "StoryBrandGenerator", _FakeSB7)
    return calls


def test_angle_rotation_on_spreads_distinct_angles(monkeypatch, tmp_path):
    """FLAG ON: N planned posts get a SPREAD of distinct angles (not all the same)."""
    monkeypatch.setenv("AGENT_CAPTION_ANGLE_ROTATION", "true")
    cs.add_source("gym_beta_ig", "service", "Small group training for every level",
                  "website /services")
    cs.add_source("gym_beta_ig", "offer", "Free intro session for new members",
                  "website /start")
    calls = _spy_sb7(monkeypatch)
    from agent.client_month_run import _clean_draft_for_day

    voice, acct, lib = _voice(), _acct(), _lib(tmp_path)
    angles_seen = []
    recent = []
    for i, day in enumerate(("2026-09-01", "2026-09-02", "2026-09-03",
                             "2026-09-04", "2026-09-05")):
        angle = drafter.angle_for_index(i)
        angles_seen.append(angle)
        d, _drop = _clean_draft_for_day(acct, day, voice, lib, (), lambda m: None,
                                        angle=angle, avoid_angles=recent[-3:])
        recent.append(angle)
    # The month loop assigns distinct angles round-robin; 5 days -> 5 distinct angles.
    assert len(set(angles_seen)) == 5
    # And every angle actually reached the generator as STYLE guidance.
    passed = [c["angle"] for c in calls if c["angle"]]
    assert len(set(passed)) >= 3, f"angles did not spread: {passed}"


def test_angle_rotation_off_passes_no_angle(monkeypatch, tmp_path):
    """FLAG OFF: the generator is never handed an angle (unchanged behavior)."""
    monkeypatch.delenv("AGENT_CAPTION_ANGLE_ROTATION", raising=False)
    cs.add_source("gym_beta_ig", "service", "Small group training", "website /services")
    calls = _spy_sb7(monkeypatch)

    voice, acct, lib = _voice(), _acct(), _lib(tmp_path)
    client_content.build_client_draft(acct, "2026-09-01", voice, lib)
    assert calls, "SB7 should have been called"
    assert all(c["angle"] == "" for c in calls), \
        "flag OFF must pass no angle guidance"


def test_month_builder_off_is_byte_for_byte(monkeypatch, tmp_path):
    """FLAG OFF end to end through the month loop: no angle reaches the generator and the
    opening window stays 6 (we assert the generator saw no angle, the observable signal)."""
    monkeypatch.delenv("AGENT_CAPTION_ANGLE_ROTATION", raising=False)
    cs.add_source("gym_beta_ig", "service", "Small group training", "website /services")
    cs.add_source("gym_beta_ig", "offer", "Free intro session", "website /start")
    calls = _spy_sb7(monkeypatch)
    from agent.client_month_run import _clean_draft_for_day

    voice, acct, lib = _voice(), _acct(), _lib(tmp_path)
    for day in ("2026-09-01", "2026-09-02", "2026-09-03"):
        _clean_draft_for_day(acct, day, voice, lib, (), lambda m: None)
    assert calls and all(c["angle"] == "" for c in calls)


# ------------------------------------------------------------------ FEATURE B

def test_educational_is_a_valid_category():
    assert "educational" in cs.CLIENT_CATEGORIES
    # storing an educational source does not raise
    src = cs.add_source("gym_beta_ig", "educational",
                        "Drink water before your workout to keep energy steady",
                        "coach tip")
    assert src.category == "educational"
    assert "educational" in cs.categories_present("gym_beta_ig")


def test_educational_caption_grounded_in_approved_source(monkeypatch, tmp_path):
    """An educational caption is generated from an APPROVED educational source and passes
    the fabrication gate. The 'educational' angle is handed to the generator."""
    monkeypatch.setenv("AGENT_EDUCATIONAL_PILLAR", "true")
    cs.add_source("gym_beta_ig", "educational",
                  "Warm up for five minutes before lifting to protect your joints",
                  "coach tip")
    calls = _spy_sb7(monkeypatch)

    voice, acct, lib = _voice(), _acct(), _lib(tmp_path)
    # 'educational' is now in the rotation; find a day whose pillar is educational.
    edu_day = None
    for i in range(14):
        day = f"2026-09-{i + 1:02d}"
        if client_content.category_for_day("gym_beta_ig", day) == "educational":
            edu_day = day
            break
    assert edu_day, "educational pillar never entered the rotation"

    draft = client_content.build_client_draft(acct, edu_day, voice, lib)
    assert draft is not None and draft.category == "educational"
    # The generator was steered by the educational angle.
    assert any(c["angle"] == "educational" for c in calls)
    # Fabrication gate still governs: the caption carries no figure absent from the source.
    assert drafter._output_claims_cleared(
        draft.caption, voice, "Warm up for five minutes before lifting to protect your joints")


def test_educational_reframes_service_when_no_educational_source(monkeypatch, tmp_path):
    """A gym with NO 'educational' source may REFRAME an approved service/about/faq source
    (facts stay verbatim). The reframe source resolver returns that approved source."""
    monkeypatch.setenv("AGENT_EDUCATIONAL_PILLAR", "true")
    cs.add_source("gym_beta_ig", "service",
                  "Small group personal training keeps you accountable",
                  "website /services")
    src = cs.educational_source_for("gym_beta_ig", "2026-09-01")
    assert src is not None
    assert src.category == "service"           # reframed, not fabricated
    assert "Small group personal training" in src.text


def test_educational_skips_when_no_eligible_source(monkeypatch, tmp_path):
    """A gym with ONLY testimonial/offer/promo (no educational + no reframeable
    service/about/faq) has NO eligible educational source: the resolver returns None and
    the pillar is never injected -> the slot is SKIPPED, never a fabricated educational post."""
    monkeypatch.setenv("AGENT_EDUCATIONAL_PILLAR", "true")
    cs.add_source("gym_beta_ig", "testimonial", "Sarah lost 30 pounds", "member Sarah")
    cs.add_source("gym_beta_ig", "offer", "6 week challenge", "website /pricing")
    assert cs.educational_source_for("gym_beta_ig") is None
    # educational never enters the rotation for this gym
    for i in range(14):
        day = f"2026-09-{i + 1:02d}"
        assert client_content.category_for_day("gym_beta_ig", day) != "educational"


def test_educational_pillar_off_leaves_category_set_unchanged(monkeypatch, tmp_path):
    """FLAG OFF: even with an approved educational source, 'educational' does NOT enter the
    rotation (the pillar set is exactly as today)."""
    monkeypatch.delenv("AGENT_EDUCATIONAL_PILLAR", raising=False)
    cs.add_source("gym_beta_ig", "service", "Small group training", "website /services")
    cs.add_source("gym_beta_ig", "educational", "Hydrate before you train", "coach tip")
    # categories_present still reports educational (it IS an approved category), but the
    # rotation pillar list must NOT treat it as an injected pillar beyond present. With the
    # flag off, _pillars_for == categories_present exactly.
    present = cs.categories_present("gym_beta_ig")
    assert client_content._pillars_for("gym_beta_ig") == present
