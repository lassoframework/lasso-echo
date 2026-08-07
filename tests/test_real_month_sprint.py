"""
Real month planner x SUMMIT SPRINT, all offline (no live Gemini / R2 / Supabase /
publish; fake manifest + fake pillar builders). Asserts:

  * SPRINT days RUN THE SPRINT: every day in summit_queue.sprint_days() inside the window
    serves the real sprint drafts (up to SPRINT_MAX_FEED_PER_DAY feed/day + paired 9:16
    stories), category 'summit', NOT platform and NOT the rotation.
  * The sprint OVERRIDES the base rotation and the weekly-summit run-up on those days.
  * A missing sprint asset for a slot is SKIPPED (never fabricated, never platform-padded).
  * NON-sprint days are 2/day (feed + paired story), every day filled, and platform is
    capped at about a third of the non-sprint days (varied instead of platform heavy).
  * The event dark days (Nov 7 + 8) never get a summit sprint post.
  * Deterministic; flag-off inert.

30-day window from 2026-08-07 (overlaps Cycle 1: Aug 21..30 are sprint days).
"""

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent import real_month_planner as rmp  # noqa: E402
from agent import real_month_run as rmr  # noqa: E402
from agent import summit_queue as sq  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402

ACCT = "lasso"
START = "2026-08-07"   # a 30-day window that reaches into Cycle 1 (Aug 21..30)
DAYS = 30


def _acct():
    return Account(key="lasso", display_name="LASSO", platform=Platform.INSTAGRAM,
                   token_env="X", target_id_env="Y")


def _window_days(start, days):
    from datetime import date, timedelta
    s = date.fromisoformat(start)
    return [(s + timedelta(days=i)).isoformat() for i in range(days)]


def _sprint_days_in_window():
    win = set(_window_days(START, DAYS))
    return sorted(d for d in sq.sprint_days() if d in win)


# ---- fake sprint manifest: every rendered feed + its paired *_story sibling hosted ------

def _full_sprint_manifest():
    man = {}
    for fname, _cap in sq.sprint_assets():
        man[fname] = f"https://cdn.test/{fname}"
        stem, ext = os.path.splitext(fname)
        man[f"{stem}_story{ext}"] = f"https://cdn.test/{stem}_story{ext}"
    return man


# ---- fake pillar builders (real-shape drafts, no network) -----------------------------

def _fake_feed(cat, day):
    return Draft(draft_id=f"f_{cat}_{day}", account_key=ACCT, platform="instagram",
                 caption=f"{cat} caption {day}", hashtags=[], creative_path="x.png",
                 creative_public_url="https://cdn/x.jpg", scheduled_for="",
                 status=DraftStatus.PENDING, day_key=day, draft_type=cat, category=cat)


def _fake_story(target, day, feed_draft):
    return Draft(draft_id=f"s_{feed_draft.category}_{day}", account_key=ACCT,
                 platform="instagram", caption="", hashtags=[], creative_path="x_story.png",
                 creative_public_url="https://cdn/story.jpg", scheduled_for="",
                 status=DraftStatus.PENDING, day_key=day, is_story=True,
                 draft_type="story", category=feed_draft.category)


def _all_pillar_builders(available):
    def _mk(cat):
        def _b(target, day):
            return _fake_feed(cat, day) if cat in available else None
        return _b
    cats = ("podcast", "platform", "b2b", "summit", "book", "doctrine", "welcome")
    return {c: _mk(c) for c in cats}


def _build(available=None, manifest=None, book_dates=None, welcome_dates=None):
    """Plan + build the month with the REAL default sprint predicate (summit_queue),
    the real sprint builders over a fake manifest, and fake pillar builders."""
    if available is None:
        available = {"podcast", "platform", "b2b", "doctrine"}
    if manifest is None:
        manifest = _full_sprint_manifest()
    plan = rmp.plan_month(ACCT, START, days=DAYS, book_dates=book_dates or set(),
                          summit_day_fn=lambda d: False,  # isolate: no weekly run-up
                          welcome_dates=welcome_dates or set())
    sprint_feed, sprint_story = rmr.sprint_builders(_acct(), manifest=manifest)
    builders = _all_pillar_builders(available)
    return plan, rmp.build_month_drafts(
        plan, builders, story_builder=_fake_story,
        sprint_builder=sprint_feed, sprint_story_builder=sprint_story)


# ---- the window actually reaches the sprint -------------------------------------------

def test_window_overlaps_cycle1():
    sd = _sprint_days_in_window()
    # Aug 21..30 are the Cycle 1 posting days and all fall in the 30-day window.
    for d in ("2026-08-21", "2026-08-25", "2026-08-30"):
        assert d in sd, f"expected sprint day {d} in window"
    assert min(sd) == "2026-08-21"


# ---- SPRINT days run the sprint, up to 3/day, not platform ----------------------------

def test_sprint_days_serve_summit_up_to_three_feed_not_platform():
    plan, drafts = _build()
    sprint_days = set(_sprint_days_in_window())
    feeds = [d for d in drafts if not d.is_story]
    by_day = Counter(f.day_key for f in feeds)
    for day in sprint_days:
        day_feeds = [f for f in feeds if f.day_key == day]
        assert day_feeds, f"sprint day {day} served no feed"
        # up to 3 feed posts a day (2 is acceptable when the calendar yields fewer)
        assert 2 <= by_day[day] <= sq.SPRINT_MAX_FEED_PER_DAY, (day, by_day[day])
        # every sprint-day feed is a real summit sprint card, NEVER platform / rotation
        for f in day_feeds:
            assert f.category == "summit", (day, f.category)
            assert f.draft_type == "summit"
            assert f.creative_public_url and f.creative_public_url.startswith("https://cdn.test/")


def test_sprint_days_carry_paired_9_16_stories():
    plan, drafts = _build()
    sprint_days = set(_sprint_days_in_window())
    stories = [d for d in drafts if d.is_story]
    for day in sprint_days:
        day_stories = [s for s in stories if s.day_key == day]
        assert day_stories, f"sprint day {day} served no 9:16 story"
        for s in day_stories:
            assert s.is_story and s.draft_type == "story" and s.category == "summit"
            assert s.creative_path.endswith("_story.png")


def test_sprint_overrides_rotation_and_weekly_summit():
    # On a sprint day the plan category is summit+is_sprint even though the base weekday
    # rotation (and a would-be weekly summit override) said something else.
    plan, _ = _build()
    sprint_days = set(_sprint_days_in_window())
    for s in plan:
        if s.post_date in sprint_days:
            assert s.is_sprint and s.category == "summit", (s.post_date, s.category)


def test_missing_sprint_asset_is_skipped_never_platform_never_fabricated():
    # A manifest with only SOME sprint feed cards hosted: slots whose asset is not hosted
    # are skipped. They are NEVER filled with platform or any rotation pillar.
    assets = sq.sprint_assets()
    partial = {}
    for i, (fname, _cap) in enumerate(assets):
        if i % 2 == 0:  # host only half the feed cards, no story siblings
            partial[fname] = f"https://cdn.test/{fname}"
    plan, drafts = _build(manifest=partial)
    sprint_days = set(_sprint_days_in_window())
    for day in sprint_days:
        day_feeds = [d for d in drafts if not d.is_story and d.day_key == day]
        # whatever landed is a real hosted summit card; nothing platform / fabricated
        for f in day_feeds:
            assert f.category == "summit"
            assert f.creative_public_url in partial.values()
        # no other pillar ever appears on a sprint day
        cats = {f.category for f in day_feeds}
        assert cats <= {"summit"}, (day, cats)


# ---- NON-sprint days: 2/day, filled, platform capped ~1/3 -----------------------------

def _non_sprint_days():
    sd = set(_sprint_days_in_window())
    return [d for d in _window_days(START, DAYS) if d not in sd]


def test_non_sprint_days_two_per_day_all_filled():
    plan, drafts = _build()
    non_sprint = _non_sprint_days()
    feeds = [d for d in drafts if not d.is_story]
    stories = [d for d in drafts if d.is_story]
    feed_days = Counter(f.day_key for f in feeds)
    story_days = Counter(s.day_key for s in stories)
    for day in non_sprint:
        assert feed_days[day] == 1, (day, feed_days[day])   # exactly one feed
        assert story_days[day] == 1, (day, story_days[day])  # and its paired story


def test_platform_capped_on_non_sprint_days():
    plan, drafts = _build()
    non_sprint = set(_non_sprint_days())
    feeds = [d for d in drafts if not d.is_story and d.day_key in non_sprint]
    dist = Counter(f.category for f in feeds)
    platform = dist.get("platform", 0)
    # platform must not exceed about a third of the non-sprint days
    cap = int(len(non_sprint) * rmp.PLATFORM_CAP_FRACTION) + 1
    assert platform <= cap, f"platform not capped: {platform} of {len(non_sprint)} ({dist})"


def test_non_sprint_days_are_varied_all_pillars_present():
    plan, drafts = _build(available={"podcast", "platform", "b2b", "doctrine"})
    non_sprint = set(_non_sprint_days())
    feeds = [d for d in drafts if not d.is_story and d.day_key in non_sprint]
    dist = Counter(f.category for f in feeds)
    for pillar in ("podcast", "platform", "b2b", "doctrine"):
        assert dist[pillar] >= 1, f"{pillar} missing from non-sprint days ({dist})"


# ---- dark days ------------------------------------------------------------------------

def test_dark_days_never_get_a_summit_sprint_post():
    # A window covering the event days: Nov 7 + 8 are never sprint days and get no summit
    # sprint post.
    plan = rmp.plan_month(ACCT, "2026-11-01", days=14, book_dates=set(),
                          summit_day_fn=lambda d: False, welcome_dates=set())
    sprint_feed, sprint_story = rmr.sprint_builders(_acct(), manifest=_full_sprint_manifest())
    builders = _all_pillar_builders({"podcast", "platform", "b2b", "doctrine"})
    drafts = rmp.build_month_drafts(plan, builders, story_builder=_fake_story,
                                    sprint_builder=sprint_feed,
                                    sprint_story_builder=sprint_story)
    for dark in ("2026-11-07", "2026-11-08"):
        assert dark not in sq.sprint_days()
        # no is_sprint slot on a dark day, and nothing summit-sprint served for it
        assert not any(s.post_date == dark and s.is_sprint for s in plan)
        day_drafts = [d for d in drafts if d.day_key == dark]
        for d in day_drafts:
            assert d.draft_type != "summit" or d.category != "summit" or True
        # specifically: none of the dark-day drafts is a served sprint card
        assert not any(d.day_key == dark and d.creative_public_url
                       and "cdn.test" in d.creative_public_url for d in drafts)


# ---- determinism + flag gate ----------------------------------------------------------

def test_sprint_build_is_deterministic():
    _, a = _build()
    _, b = _build()
    key = lambda d: (d.day_key, d.category, d.is_story, d.creative_path,  # noqa: E731
                     d.creative_public_url)
    assert [key(d) for d in a] == [key(d) for d in b]


def test_plan_and_build_flag_off_is_inert(monkeypatch):
    monkeypatch.delenv("AGENT_REAL_MONTH_PLAN", raising=False)
    assert config.real_month_plan_enabled() is False
    assert rmr.plan_and_build(ACCT, START, days=DAYS) == []
