"""Owned LASSO month-route Story deferral (recurrence prevention for the standalone
Story cutover, 2026-10-08).

Under owned LASSO 3x AND the armed daily paired-Story recovery feature,
real_month_run.plan_and_build removes monthly Story slots BEFORE any builder/renderer
is invoked; the daily paired-Story job is the sole Story creator from staged feed
UUIDs. Asserts:

  * owned LASSO (lasso_ig/lasso_fb) 3x + recovery armed: no Story slot reaches the
    builders, feed slots intact, deferral reported explicitly.
  * feed slots are byte-for-byte the disarmed plan's feed slots.
  * other clients' Story slots are untouched by the same armed flags.
  * recovery disarmed (e.g. 3x off) leaves the plan shape unchanged.
  * no production caller bypasses the entrypoint: build_month_drafts is only invoked
    from real_month_run (which filters) in agent/ source.

All offline: the planner is pure, builders are fakes, no network/store.
"""

import os
import re
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent import real_month_run as rmr  # noqa: E402
from agent import real_month_planner as rmp  # noqa: E402

START = "2026-10-12"  # inside the one-month planning horizon
DAYS = 3


def _account(key):
    platform = "instagram" if key.endswith("_ig") else "facebook"
    return SimpleNamespace(key=key, platform=platform)


def _capture_plan(monkeypatch):
    """Run plan_and_build with every builder/story seam faked; return the plan the
    month builders would have been invoked with, plus story-builder invocations."""
    captured = {}
    story_calls = []
    monkeypatch.setattr(rmr, "real_builders_map", lambda *a, **kw: {
        cat: (lambda *x: pytest.fail("feed builders must not run"))
        for cat in ("platform", "b2b", "doctrine", "summit", "podcast")})
    monkeypatch.setattr(rmr, "sprint_builders",
                        lambda *a, **kw: (lambda *a: None, lambda *a: None))
    monkeypatch.setattr(rmr, "_real_story_builder",
                        lambda *a, **kw: (lambda *x: story_calls.append(x) or None))
    import agent.lasso_campaign_assets as campaign
    monkeypatch.setattr(campaign, "wrap_builders", lambda acct, b, s, sf, ss: (b, s, sf, ss))

    def fake_build(plan, builders, **kw):
        captured["plan"] = list(plan)
        captured["story_builder"] = kw.get("story_builder")
        return []

    monkeypatch.setattr(rmr._rmp, "build_month_drafts", fake_build)
    return captured, story_calls


def _arm_recovery(monkeypatch, armed=True):
    from agent import variant_regen, visual_writer_prepare
    from agent.jobs import lasso_cadence_recovery
    monkeypatch.setattr(config, "lasso_three_feed_enabled", lambda: armed)
    monkeypatch.setattr(config, "calendar_autopublish_enabled", lambda: armed)
    monkeypatch.setattr(config, "lasso_infographic_quality_enabled", lambda *a: armed)
    monkeypatch.setattr(variant_regen, "enabled", lambda: armed)
    monkeypatch.setattr(visual_writer_prepare, "enabled", lambda: not armed)
    monkeypatch.setattr(lasso_cadence_recovery, "enabled", lambda: armed)


def _run(monkeypatch, account_key):
    from agent import cadence
    monkeypatch.setenv("AGENT_REAL_MONTH_PLAN", "true")
    monkeypatch.setattr(cadence, "resolve_posts_per_day_live", lambda _: 3)
    captured, story_calls = _capture_plan(monkeypatch)
    logs = []
    out = rmr.plan_and_build(account_key, START, DAYS, account=_account(account_key),
                             logger=logs.append,
                             book_dates=[], welcome_dates=[],
                             summit_day_fn=lambda d: False,
                             sprint_day_fn=lambda d: False,
                             sprint_feed_count_fn=lambda d: 0)
    assert out == []  # the fake build returns nothing; only the plan matters
    return captured["plan"], story_calls, logs


@pytest.mark.parametrize("account_key", ["lasso_ig", "lasso_fb"])
def test_owned_lasso_story_slots_filtered_before_builders(monkeypatch, account_key):
    _arm_recovery(monkeypatch, armed=True)
    plan, story_calls, logs = _run(monkeypatch, account_key)
    assert plan, "feed slots must remain"
    assert all(slot.fmt == rmp.FEED for slot in plan)
    # 3 days at 3x cadence: every feed slot survives untouched.
    assert len(plan) == DAYS * 3
    assert {slot.post_date for slot in plan} == {"2026-10-12", "2026-10-13", "2026-10-14"}
    # No story builder ever invoked, and the deferral is reported explicitly.
    assert story_calls == []
    assert any("deferred" in line and "daily paired-Story" in line for line in logs)


def test_feed_slots_unaffected_by_deferral(monkeypatch):
    from agent import variant_regen
    _arm_recovery(monkeypatch, armed=True)
    armed_plan, _, _ = _run(monkeypatch, "lasso_ig")
    # Same owned 3x cadence, one recovery gate open: the deferral is off but the
    # plan shape (3 pairs/day) is identical, isolating the Story filter.
    monkeypatch.setattr(variant_regen, "enabled", lambda: False)
    unfiltered_plan, _, _ = _run(monkeypatch, "lasso_ig")
    assert [s for s in unfiltered_plan if s.fmt == rmp.STORY], "gate open: stories plan"
    unfiltered_feeds = [s for s in unfiltered_plan if s.fmt == rmp.FEED]
    assert [ (s.post_date, s.category, s.slot_index, s.cadence_slot) for s in armed_plan ] == \
           [ (s.post_date, s.category, s.slot_index, s.cadence_slot) for s in unfiltered_feeds ]


def test_other_clients_stories_untouched(monkeypatch):
    _arm_recovery(monkeypatch, armed=True)
    plan, _, logs = _run(monkeypatch, "gym_alpha_ig")
    stories = [s for s in plan if s.fmt == rmp.STORY]
    feeds = [s for s in plan if s.fmt == rmp.FEED]
    assert stories and feeds
    assert not any("deferred" in line for line in logs)


def test_recovery_disarmed_leaves_behavior_unchanged(monkeypatch):
    _arm_recovery(monkeypatch, armed=False)
    plan, _, logs = _run(monkeypatch, "lasso_ig")
    stories = [s for s in plan if s.fmt == rmp.STORY]
    assert stories, "disarmed: monthly Story slots plan exactly as before"
    assert not any("deferred" in line for line in logs)


def test_cadence_recovery_flag_off_preserves_monthly_story_slots(monkeypatch):
    """Every prior gate ON but AGENT_LASSO_CADENCE_RECOVERY OFF: the monthly Story
    slots plan exactly as before — suppression requires the cadence-recovery lane."""
    from agent.jobs import lasso_cadence_recovery
    _arm_recovery(monkeypatch, armed=True)
    monkeypatch.setattr(lasso_cadence_recovery, "enabled", lambda: False)
    plan, _, logs = _run(monkeypatch, "lasso_ig")
    stories = [s for s in plan if s.fmt == rmp.STORY]
    feeds = [s for s in plan if s.fmt == rmp.FEED]
    assert stories, "recovery flag OFF: monthly Story slots plan exactly as before"
    assert len(feeds) == DAYS * 3
    assert not any("deferred" in line for line in logs)


def test_month_flag_off_still_inert(monkeypatch):
    _arm_recovery(monkeypatch, armed=True)
    monkeypatch.delenv("AGENT_REAL_MONTH_PLAN", raising=False)
    monkeypatch.setattr(rmr, "real_builders_map",
                        lambda *a, **kw: pytest.fail("must not build while flag off"))
    assert rmr.plan_and_build("lasso_ig", START, DAYS, account=_account("lasso_ig")) == []


def test_no_production_bypass_of_the_filtering_entrypoint():
    """build_month_drafts must only be invoked from real_month_run.plan_and_build (the
    filtering entrypoint) in production source; every other live caller routes through
    plan_and_build, so the owned-only deferral cannot be bypassed at staging."""
    agent_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agent")
    offenders = []
    for root, _dirs, files in os.walk(agent_dir):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, agent_dir)
            if rel in ("real_month_run.py", "real_month_planner.py"):
                continue
            text = open(path, encoding="utf-8").read()
            if re.search(r"(?<!def )(?<!\w)build_month_drafts\(", text):
                offenders.append(rel)
    assert offenders == []
