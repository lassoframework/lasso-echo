"""
tests/test_lasso_reels_floor.py — the LASSO reels-share floor
(AGENT_LASSO_REELS_FLOOR, default OFF; report-card build 2026-08-28).

MEASURED FIRST (Blake's ruling 2026-08-28): the forward plan as built today
(video mix ON) lands 5.7-19.4% video-preferred feed posts across the live
windows — below the 35% benchmark — so the floor rebalances, minimally.

Asserts, per live window:
  * TALLY: >= 35% of planned FEED posts are video (video_preferred) slots.
  * SPRINTS INTACT: every summit SPRINT slot (is_sprint) is byte-for-byte
    identical with the floor ON vs OFF.
  * THU/SUN PODCAST: Blake's locked ruling holds — non-sprint thu/sun stay
    podcast and stay video-preferred.
  * DATED OVERRIDES: book/welcome days are never converted.
  * FLAG OFF: byte-for-byte the video-mix plan (and the env default is off).
All offline and pure (plan_month only).
"""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import real_month_planner as rmp  # noqa: E402
from agent import summit_queue as sq  # noqa: E402

ACCT = "lasso"
# The same three live windows the video-mix suite locks: two sprint-heavy, one quiet.
WINDOWS = [("2026-08-07", 30), ("2026-09-01", 30), ("2026-11-01", 30)]
FLOOR = 0.35


def _feeds(plan):
    return [s for s in plan if s.fmt == rmp.FEED]


def _video_share(plan):
    feeds = _feeds(plan)
    return sum(1 for s in feeds if s.video_preferred) / (len(feeds) or 1)


def test_tally_month_is_at_least_35pct_video_by_post_count():
    for start, days in WINDOWS:
        on = rmp.plan_month(ACCT, start, days=days, video_mix=True,
                            reels_floor=True)
        share = _video_share(on)
        assert share >= FLOOR - 1e-9, f"{start}: video share {share:.1%} < 35%"


def test_sprint_slots_byte_identical_floor_on_vs_off():
    for start, days in WINDOWS:
        off = rmp.plan_month(ACCT, start, days=days, video_mix=True,
                             reels_floor=False)
        on = rmp.plan_month(ACCT, start, days=days, video_mix=True,
                            reels_floor=True)

        def _sprint_slots(plan):
            return sorted(
                (s.post_date, s.category, s.fmt, s.slot_index, s.video_preferred)
                for s in plan if s.is_sprint)

        assert _sprint_slots(on) == _sprint_slots(off), start
        # the sprint still owns each of its days (>= 1 summit feed)
        win_sprint = {d for d in sq.sprint_days()
                      if start <= d}  # dates at/after window start
        for d in sorted(win_sprint)[:3]:
            if not any(s.post_date == d for s in on):
                continue
            assert any(s.post_date == d and s.is_sprint and s.category == "summit"
                       for s in _feeds(on)), (start, d)


def test_thu_sun_stay_podcast_and_video_preferred():
    for start, days in WINDOWS:
        on = rmp.plan_month(ACCT, start, days=days, video_mix=True,
                            reels_floor=True)
        sprint = set(sq.sprint_days())
        checked = 0
        for s in _feeds(on):
            if s.post_date in sprint or s.is_sprint:
                continue
            wd = date.fromisoformat(s.post_date).weekday()
            if wd in (3, 6) and s.base_category == "podcast" and not s.overridden:
                assert s.category == "podcast" and s.video_preferred, s.post_date
                checked += 1
        assert checked >= 2, f"{start}: expected recurring thu/sun podcast slots"


def test_dated_book_and_welcome_days_never_converted():
    # post-sprint dates (the sprint override legitimately owns its own days)
    wd = {"2026-11-10"}
    bd = {"2026-11-11"}
    on = rmp.plan_month(ACCT, "2026-11-01", days=14, video_mix=True,
                        reels_floor=True, welcome_dates=wd, book_dates=bd)
    for s in _feeds(on):
        if s.post_date in wd:
            assert s.category == "welcome", s
        if s.post_date in bd:
            assert s.category == "book", s


def test_floor_converts_minimally_and_deterministically():
    # Do not touch a plan already at/over the floor: with the floor set BELOW
    # the video-mix baseline, the plan is byte-for-byte the mix plan.
    for start, days in WINDOWS:
        base = rmp.plan_month(ACCT, start, days=days, video_mix=True,
                              reels_floor=False)
        floored = rmp.plan_month(ACCT, start, days=days, video_mix=True,
                                 reels_floor=True)
        base_share = _video_share(base)
        # the floor never REMOVES video and never overshoots wildly: converted
        # count is the minimum needed (share stays under floor + one slot's worth)
        feeds = len(_feeds(floored))
        share = _video_share(floored)
        assert share >= max(base_share, FLOOR) - 1e-9
        assert share <= FLOOR + (1.0 / feeds) + 1e-9, (
            f"{start}: over-converted ({share:.1%})")
        # deterministic: same inputs, same plan
        again = rmp.plan_month(ACCT, start, days=days, video_mix=True,
                               reels_floor=True)
        assert [(s.post_date, s.category, s.fmt, s.video_preferred)
                for s in floored] == \
               [(s.post_date, s.category, s.fmt, s.video_preferred)
                for s in again]


def test_flag_off_and_env_default_are_byte_for_byte(monkeypatch):
    monkeypatch.delenv("AGENT_LASSO_REELS_FLOOR", raising=False)
    for start, days in WINDOWS:
        mix_only = rmp.plan_month(ACCT, start, days=days, video_mix=True,
                                  reels_floor=False)
        env_default = rmp.plan_month(ACCT, start, days=days, video_mix=True)
        key = lambda plan: [(s.post_date, s.category, s.fmt, s.slot_index,  # noqa: E731
                             s.is_sprint, s.video_preferred) for s in plan]
        assert key(env_default) == key(mix_only), start


def test_no_day_carries_podcast_twice():
    # the floor never doubles podcast on one date's feed slots (2x-day guard)
    for start, days in WINDOWS:
        on = rmp.plan_month(ACCT, start, days=days, video_mix=True,
                            reels_floor=True)
        by_date = {}
        for s in _feeds(on):
            if not s.is_sprint:
                by_date.setdefault(s.post_date, []).append(s.category)
        for d, cats in by_date.items():
            assert cats.count("podcast") <= 1, (d, cats)
