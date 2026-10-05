"""
LASSO Summit daily extra (Blake's explicit ruling, 2026-09-23). Fully OFFLINE.

Asserts:
  * Flag OFF (default) -> byte-for-byte today: cadence resolves 1/2 as before and
    plan_month emits no summit_daily slots even inside the dated window.
  * Window boundaries: 2026-09-23 and 2026-11-08 are IN, 2026-09-22 and
    2026-11-09 are OUT (inclusive window, config + cadence + planner agree).
  * Other tenants NEVER get capacity 3 (lasso base only, canonical 'lasso').
  * Exactly 3 feed and 3 paired Story slots per in-window LASSO day at 2x:
    TWO regular feeds plus ONE Summit feed. Every feed has its own Story source.
  * Weekly and sprint Summit slots move to the additive ordinal 2; their vacated
    regular slot is replaced with a varied non-Summit topic.
  * The SQL migration keeps the owned-claim signature and only opens capacity 3
    for gym_id 'lasso' inside the window (static contract assertions).
"""

import os
import re
import sys
from datetime import date
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import cadence, config  # noqa: E402
from agent import real_month_planner as rmp  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402

ACCT = "lasso"
# 2026-10-05 is a Monday inside the Summit daily window.
MON = "2026-10-05"

SQL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "migrations", "lasso_summit_daily_capacity_20260923.sql")
DURABLE_SQL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "migrations",
    "lasso_three_feed_capacity_20260930.sql")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AGENT_LASSO_SUMMIT_DAILY_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_LASSO_3X_ENABLED", raising=False)
    monkeypatch.delenv("ECHO_CADENCE_2X_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_LASSO_VIDEO_MIX", raising=False)
    monkeypatch.delenv("AGENT_LASSO_REELS_FLOOR", raising=False)
    monkeypatch.delenv("AGENT_LASSO_TESTIMONIAL_PILLAR", raising=False)
    monkeypatch.delenv("AGENT_LASSO_EDITORIAL_CALENDAR", raising=False)
    yield


def _plan(days=7, **kw):
    kw.setdefault("book_dates", set())
    kw.setdefault("summit_day_fn", lambda dk: False)
    kw.setdefault("sprint_day_fn", lambda dk: False)
    kw.setdefault("sprint_feed_count_fn", lambda dk: 0)
    kw.setdefault("welcome_dates", set())
    kw.setdefault("video_mix", False)
    kw.setdefault("reels_floor", False)
    kw.setdefault("testimonial", False)
    return rmp.plan_month(ACCT, MON, days=days, **kw)


def _by_date(plan):
    out = {}
    for s in plan:
        out.setdefault(s.post_date, []).append(s)
    return out


# ---- config window boundaries ----------------------------------------------

def test_flag_off_never_in_window(monkeypatch):
    monkeypatch.delenv("AGENT_LASSO_SUMMIT_DAILY_ENABLED", raising=False)
    assert config.lasso_summit_daily_enabled("2026-10-15") is False


@pytest.mark.parametrize("day,expected", [
    ("2026-09-22", False),   # day before the window
    ("2026-09-23", True),    # first day IN
    ("2026-10-15", True),
    ("2026-11-08", True),    # last day IN (mirrors SUMMIT_END_DATE)
    ("2026-11-09", False),   # day after the window
])
def test_window_boundaries(monkeypatch, day, expected):
    monkeypatch.setenv("AGENT_LASSO_SUMMIT_DAILY_ENABLED", "true")
    assert config.lasso_summit_daily_enabled(day) is expected
    assert config.lasso_summit_daily_enabled(date.fromisoformat(day)) is expected


def test_window_unparseable_day_fails_closed(monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_SUMMIT_DAILY_ENABLED", "true")
    assert config.lasso_summit_daily_enabled("not-a-date") is False
    assert config.lasso_summit_daily_enabled("") is False


# ---- cadence capacity resolution --------------------------------------------

class _Store:
    def __init__(self, ppd):
        self._ppd = ppd

    def gym_posts_per_day(self, base):
        return self._ppd


@pytest.mark.parametrize("day,expected", [
    ("2026-09-22", 2), ("2026-09-23", 3), ("2026-11-08", 3), ("2026-11-09", 2),
])
def test_cadence_lasso_capacity_3_only_in_window(monkeypatch, day, expected):
    monkeypatch.setenv("AGENT_LASSO_SUMMIT_DAILY_ENABLED", "true")
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    assert cadence.resolve_posts_per_day("lasso", _Store(2), day=day) == expected


def test_cadence_other_tenants_never_3(monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_SUMMIT_DAILY_ENABLED", "true")
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    for base in ("tough_temple", "LASSO ", "lasso_demo", "lasso2", "", None):
        assert cadence.resolve_posts_per_day(base, _Store(2),
                                             day="2026-10-15") in (1, 2)
    assert cadence.resolve_posts_per_day("tough_temple", _Store(2),
                                         day="2026-10-15") == 2


def test_cadence_flag_off_unchanged(monkeypatch):
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    assert cadence.resolve_posts_per_day("lasso", _Store(2), day="2026-10-15") == 2
    monkeypatch.delenv("ECHO_CADENCE_2X_ENABLED", raising=False)
    assert cadence.resolve_posts_per_day("lasso", _Store(2), day="2026-10-15") == 1


def test_cadence_summit_flag_does_not_require_2x(monkeypatch):
    # The Summit extra resolves independently of the 2x kill switch: LASSO in the
    # window gets 3 even if the client-facing 2x flag is off. Clients still get 1.
    monkeypatch.setenv("AGENT_LASSO_SUMMIT_DAILY_ENABLED", "true")
    assert cadence.resolve_posts_per_day("lasso", _Store(1), day="2026-10-15") == 3
    assert cadence.resolve_posts_per_day("tough_temple", _Store(2),
                                         day="2026-10-15") == 1


def test_cadence_live_lasso_window(monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_SUMMIT_DAILY_ENABLED", "true")
    assert cadence.resolve_posts_per_day_live("lasso", day="2026-10-15") == 3
    assert cadence.resolve_posts_per_day_live("lasso", day="2026-11-09") == 1
    assert cadence.resolve_posts_per_day_live("tough_temple", day="2026-10-15") == 1


def test_durable_lasso_three_feed_cadence_after_summit(monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    assert cadence.resolve_posts_per_day("lasso", _Store(1), day="2026-11-09") == 3
    assert cadence.resolve_posts_per_day_live("lasso", day="2027-01-15") == 3
    assert cadence.resolve_posts_per_day("tough_temple", _Store(2),
                                         day="2027-01-15") == 1


def test_durable_lasso_three_feed_plan_pairs_every_story(monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    monkeypatch.setenv("AGENT_LASSO_EDITORIAL_CALENDAR", "true")
    plan = rmp.plan_month("lasso", "2026-11-09", days=3, posts_per_day=3,
                          book_dates=set(), summit_day_fn=lambda _d: False,
                          sprint_day_fn=lambda _d: False,
                          sprint_feed_count_fn=lambda _d: 0,
                          welcome_dates=set(), video_mix=False,
                          reels_floor=False, testimonial=False,
                          summit_daily_fn=lambda _d: False)
    for day_key, slots in _by_date(plan).items():
        feeds = [s for s in slots if s.fmt == "feed"]
        stories = [s for s in slots if s.fmt == "story"]
        assert [s.cadence_slot for s in feeds] == [0, 1, 2], day_key
        assert len({s.category for s in feeds}) == 3, day_key
        assert {s.cadence_slot for s in stories} == {0, 1, 2}, day_key
        assert {s.cadence_slot: s.category for s in stories} == {
            s.cadence_slot: s.category for s in feeds}, day_key
        assert not any(s.summit_daily for s in slots)

    client = rmp.plan_month(
        "tough_temple", "2026-11-09", days=1, posts_per_day=3,
        book_dates=set(), summit_day_fn=lambda _d: False,
        sprint_day_fn=lambda _d: False, sprint_feed_count_fn=lambda _d: 0,
        welcome_dates=set(), video_mix=False, reels_floor=False,
        testimonial=False, summit_daily_fn=lambda _d: False)
    assert len([s for s in client if s.fmt == "feed"]) == 2


@pytest.mark.parametrize("editorial", [False, True])
def test_durable_three_feed_plan_retires_summit_after_campaign(monkeypatch, editorial):
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    monkeypatch.setenv("AGENT_LASSO_EDITORIAL_CALENDAR", str(editorial).lower())
    plan = rmp.plan_month(
        "lasso", "2026-11-09", days=60, posts_per_day=3,
        book_dates=set(), summit_day_fn=lambda _d: True,
        sprint_day_fn=lambda _d: True,
        sprint_feed_count_fn=lambda _d: 1,
        welcome_dates=set(), video_mix=False, reels_floor=False,
        testimonial=False, summit_daily_fn=lambda _d: True)
    for day_key, slots in _by_date(plan).items():
        feeds = [s for s in slots if s.fmt == "feed"]
        stories = [s for s in slots if s.fmt == "story"]
        assert [s.cadence_slot for s in feeds] == [0, 1, 2], day_key
        assert {s.cadence_slot for s in stories} == {0, 1, 2}, day_key
        assert len({s.category for s in feeds}) == 3, day_key
        assert all(s.category != "summit" and not s.summit_daily
                   and not s.is_sprint for s in slots), day_key


def test_expired_summit_is_not_a_builder_fallback():
    called = []

    def builder(category):
        def build(_account, _day):
            called.append(category)
            return SimpleNamespace(caption=f"Source-backed {category} caption")
        return build

    slot = rmp.PlanSlot("2026-11-09", "summit", "feed")
    draft, category = rmp._build_feed_with_fallback(
        slot, {"summit": builder("summit"), "podcast": builder("podcast")},
        None, lambda _message: None)
    assert category == "podcast" and draft is not None
    assert called == ["podcast"]


def test_daily_summit_story_uses_its_own_feed(monkeypatch):
    from agent import lasso_daily_summit

    plan = _plan(days=1, posts_per_day=2,
                 summit_daily_fn=lambda _day: True)
    source_feeds = []

    def draft(category, day, *, story=False):
        return Draft(
            draft_id=f"{day}-{category}-{'story' if story else 'feed'}",
            account_key="lasso_ig", platform="instagram",
            caption=f"Approved {category} source", hashtags=[],
            creative_path="genuine-story.png" if story else "feed.png",
            creative_public_url=("https://cdn.example/genuine-story.png" if story
                                 else "https://cdn.example/feed.png"),
            scheduled_for="", status=DraftStatus.PENDING,
            is_story=story, day_key=day,
            draft_type="story" if story else "feed", category=category)

    monkeypatch.setattr(
        lasso_daily_summit, "build_daily_summit",
        lambda _target, day: draft("summit", day))

    def story_builder(_target, day, feed):
        source_feeds.append((feed.cadence_slot_index, feed.category))
        return draft(feed.category, day, story=True)

    builders = {category: (lambda _target, day, cat=category: draft(cat, day))
                for category in ("podcast", "platform", "b2b", "doctrine")}
    drafts = rmp.build_month_drafts(
        plan, builders, story_builder=story_builder, account="lasso")
    feeds = [d for d in drafts if not d.is_story]
    stories = [d for d in drafts if d.is_story]
    assert {d.cadence_slot_index for d in feeds} == {0, 1, 2}
    assert {d.cadence_slot_index for d in stories} == {0, 1, 2}
    assert (2, "summit") in source_feeds
    assert all(d.creative_public_url.endswith("genuine-story.png") for d in stories)


def test_sprint_summit_third_story_falls_back_to_genuine_story_builder():
    plan = _plan(days=1, posts_per_day=2,
                 sprint_day_fn=lambda _day: True,
                 sprint_feed_count_fn=lambda _day: 1,
                 summit_daily_fn=lambda _day: True)
    feeds_seen = []

    def draft(category, day, *, story=False):
        return Draft(
            draft_id=f"{day}-{category}-{'story' if story else 'feed'}",
            account_key="lasso_ig", platform="instagram",
            caption=f"Approved {category} source", hashtags=[],
            creative_path="story.png" if story else "feed.png",
            creative_public_url=("https://cdn.example/story.png" if story
                                 else "https://cdn.example/feed.png"),
            scheduled_for="", status=DraftStatus.PENDING,
            is_story=story, day_key=day,
            draft_type="story" if story else "feed", category=category)

    def story_builder(_target, day, feed):
        feeds_seen.append(feed.cadence_slot_index)
        return draft(feed.category, day, story=True)

    builders = {category: (lambda _target, day, cat=category: draft(cat, day))
                for category in ("podcast", "platform", "b2b", "doctrine")}
    drafts = rmp.build_month_drafts(
        plan, builders, story_builder=story_builder, account="lasso",
        sprint_builder=lambda _target, day, _index: draft("summit", day),
        sprint_story_builder=lambda *_args: None)
    assert {d.cadence_slot_index for d in drafts if d.is_story} == {0, 1, 2}
    assert 2 in feeds_seen


# ---- planner shape ----------------------------------------------------------

def test_planner_flag_off_byte_for_byte(monkeypatch):
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    a = _plan(posts_per_day=2)
    b = _plan(posts_per_day=2, summit_daily_fn=lambda dk: False)
    assert a == b
    assert not any(s.summit_daily for s in a)


def test_planner_exactly_3_two_regular_one_summit():
    plan = _plan(posts_per_day=2, summit_daily_fn=lambda dk: True)
    days = _by_date(plan)
    assert len(days) == 7
    for d, slots in days.items():
        feeds = [s for s in slots if s.fmt == "feed"]
        stories = [s for s in slots if s.fmt == "story"]
        summit_feeds = [s for s in feeds if s.category == "summit"]
        regular_feeds = [s for s in feeds if not s.summit_daily]
        assert len(feeds) == 3, d                      # exactly 3 total
        assert len(regular_feeds) == 2, d              # two regular
        assert len(summit_feeds) == 1, d               # plus one Summit
        assert summit_feeds[0].summit_daily is True    # additive, marked
        assert summit_feeds[0].is_sprint is False
        assert not any(s.summit_daily for s in regular_feeds)
        assert len(stories) == 3, d
        assert {s.cadence_slot for s in stories} == {0, 1, 2}, d
        # regular slots untouched: the 2x cadence pair is intact
        assert {s.cadence_slot for s in regular_feeds} == {0, 1}
    assert any(s.summit_daily for s in plan)           # the extra fires in-window


def test_planner_summit_extra_never_replaces_regular_categories():
    plan = _plan(posts_per_day=2, summit_daily_fn=lambda dk: True)
    base = _plan(posts_per_day=2, summit_daily_fn=lambda dk: False)
    base_days = _by_date(base)
    for s in plan:
        if not s.summit_daily:
            continue
        # The day's two regular feeds carry the exact categories they would have
        # without the extra slot.
        regular = [b for b in base_days[s.post_date]
                   if b.fmt == "feed" and not b.is_sprint]
        assert len(regular) == 2
        refreshed = [b for b in plan if b.post_date == s.post_date
                     and b.fmt == "feed" and not b.summit_daily]
        assert len(refreshed) == 2
        assert all(r.category != "summit" for r in refreshed)


def test_planner_moves_weekly_summit_to_extra_slot():
    # Friday's weekly Summit becomes the one additive Summit; both regular
    # ordinals remain non-Summit themes.
    plan = rmp.plan_month(ACCT, "2026-10-09", days=1, book_dates=set(),
                          summit_day_fn=lambda dk: False,
                          sprint_day_fn=lambda dk: False,
                          sprint_feed_count_fn=lambda dk: 0,
                          welcome_dates=set(), video_mix=False,
                          reels_floor=False, testimonial=False,
                          posts_per_day=2,
                          summit_daily_fn=lambda dk: True)
    feeds = [s for s in plan if s.fmt == "feed"]
    assert sum(1 for s in feeds if s.category == "summit") == 1
    assert len(feeds) == 3
    assert [s.cadence_slot for s in feeds] == [0, 1, 2]
    assert feeds[2].summit_daily
    assert all(s.category != "summit" for s in feeds[:2])


def test_planner_moves_sprint_artwork_to_extra_slot():
    plan = _plan(days=2, posts_per_day=2, summit_daily_fn=lambda dk: True,
                 sprint_day_fn=lambda dk: True,
                 sprint_feed_count_fn=lambda dk: 1)
    for d, slots in _by_date(plan).items():
        feeds = [s for s in slots if s.fmt == "feed"]
        stories = [s for s in slots if s.fmt == "story"]
        assert len(feeds) == 3 and len(stories) == 3, d
        assert [s.cadence_slot for s in feeds] == [0, 1, 2]
        assert all(s.category != "summit" for s in feeds[:2])
        assert feeds[2].category == "summit" and feeds[2].summit_daily
        assert feeds[2].is_sprint and feeds[2].slot_index == 0


def test_planner_other_account_gets_no_extra():
    plan = rmp.plan_month("tough_temple", MON, days=3, book_dates=set(),
                          summit_day_fn=lambda dk: False,
                          sprint_day_fn=lambda dk: False,
                          sprint_feed_count_fn=lambda dk: 0,
                          welcome_dates=set(), video_mix=False,
                          reels_floor=False, testimonial=False,
                          posts_per_day=2,
                          summit_daily_fn=lambda dk: True)
    assert not any(s.summit_daily for s in plan)


def test_planner_out_of_window_days_untouched():
    # Predicate False outside the window: a plan spanning the Nov 8/9 boundary
    # adds the extra slot only through 2026-11-08.
    plan = rmp.plan_month(ACCT, "2026-11-07", days=3, book_dates=set(),
                          summit_day_fn=lambda dk: False,
                          sprint_day_fn=lambda dk: False,
                          sprint_feed_count_fn=lambda dk: 0,
                          welcome_dates=set(), video_mix=False,
                          reels_floor=False, testimonial=False,
                          posts_per_day=2,
                          summit_daily_fn=lambda dk: dk <= "2026-11-08")
    days = _by_date(plan)
    assert any(s.summit_daily for s in days["2026-11-08"])
    assert not any(s.summit_daily for s in days["2026-11-09"])


def test_planner_real_default_predicate_follows_flag(monkeypatch):
    # With no injection, the default predicate reads the config flag + window.
    kw = dict(book_dates=set(), summit_day_fn=lambda dk: False,
              sprint_day_fn=lambda dk: False,
              sprint_feed_count_fn=lambda dk: 0, welcome_dates=set(),
              video_mix=False, reels_floor=False, testimonial=False,
              posts_per_day=2)
    off = rmp.plan_month(ACCT, "2026-10-12", days=3, **kw)
    assert not any(s.summit_daily for s in off)
    monkeypatch.setenv("AGENT_LASSO_SUMMIT_DAILY_ENABLED", "true")
    on = rmp.plan_month(ACCT, "2026-10-12", days=3, **kw)
    fired = 0
    for d, slots in _by_date(on).items():
        feeds = [s for s in slots if s.fmt == "feed"]
        assert len(feeds) == 3, d
        assert sum(1 for s in feeds if s.summit_daily) == 1, d
        fired += 1
    assert fired >= 1


def test_production_flags_shape_all_47_days_and_preserve_manifest_slots(monkeypatch):
    import json
    from agent.lasso_campaign_assets import MANIFEST

    monkeypatch.setenv("AGENT_LASSO_SUMMIT_DAILY_ENABLED", "true")
    monkeypatch.setenv("AGENT_LASSO_EDITORIAL_CALENDAR", "true")
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    plan = rmp.plan_month("lasso", "2026-09-23", days=47, posts_per_day=3)
    days = _by_date(plan)
    assert len(days) == 47
    for day_key, slots in days.items():
        feeds = [s for s in slots if s.fmt == "feed"]
        stories = [s for s in slots if s.fmt == "story"]
        assert len(feeds) == 3, day_key
        assert len(stories) == 3, day_key
        assert [s.cadence_slot for s in feeds] == [0, 1, 2], day_key
        assert all(s.category != "summit" for s in feeds[:2]), day_key
        assert feeds[2].category == "summit" and feeds[2].summit_daily, day_key
        assert {s.cadence_slot for s in stories} == {0, 1, 2}, day_key

    manifest = json.loads(MANIFEST.read_text())
    for entry in manifest["assets"]:
        if not ("2026-09-23" <= entry["date"] <= "2026-11-08"):
            continue
        feeds = [s for s in days[entry["date"]] if s.fmt == "feed"]
        if entry["category"] == "book":
            assert any(s.category == "book" and
                       s.cadence_slot == entry["slot_index"] for s in feeds)
        else:
            summit = feeds[2]
            assert summit.category == "summit" and summit.is_sprint
            assert summit.slot_index == entry["slot_index"]


# ---- SQL migration contract --------------------------------------------------

def test_sql_preserves_signature_and_security():
    sql = open(SQL_PATH).read()
    assert "create or replace function public.claim_calendar_publish_slot_owned(" in sql
    assert ("p_row_id uuid, p_gym_id text, p_day date, p_timezone text,"
            in sql.replace("\n", " "))
    assert "p_capacity integer, p_approved_only boolean" in sql
    assert "returns uuid" in sql
    assert "security definer set search_path = public" in sql
    assert "pg_advisory_xact_lock(hashtextextended(p_gym_id, 0))" in sql
    assert "status in ('pending', 'approved') and published_at is null" in sql
    assert "late_post_id is null" in sql
    assert "publish_reservation_day = p_day" in sql
    assert "publish_claim_token = v_token" in sql
    assert "grant execute on function public.claim_calendar_publish_slot_owned" in sql
    assert "to service_role" in sql


def test_sql_capacity_3_lasso_window_only():
    sql = open(SQL_PATH).read()
    # The global bound widens to 3 at most...
    assert re.search(r"p_capacity\s*<\s*1\s*or\s*p_capacity\s*>\s*3", sql)
    # ...but capacity 3 is gated to canonical 'lasso' inside the dated window.
    m = re.search(r"if\s+p_capacity\s*=\s*3\s+and\s+not\s*\(p_gym_id\s*=\s*'lasso'"
                  r".*?date\s*'2026-09-23'\s*and\s*date\s*'2026-11-08'",
                  sql, re.S)
    assert m, "capacity-3 gate (lasso + 2026-09-23..2026-11-08) missing"
    # No other gym id is granted 3 anywhere in the migration.
    assert sql.count("p_gym_id = 'lasso'") == 1


def test_durable_sql_capacity_3_remains_lasso_only_without_date_limit():
    sql = open(DURABLE_SQL_PATH).read()
    assert "p_capacity < 1 or p_capacity > 3" in sql
    assert "if p_capacity = 3 and p_gym_id <> 'lasso'" in sql
    assert "slot_index = 2" in sql and "gym_id = 'lasso'" in sql
    assert "post_date between" not in sql
    assert "coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed') <> 'feed'" in sql


def test_sql_slot_two_and_capacity_three_are_feed_only():
    sql = open(SQL_PATH).read()
    assert "drop constraint if exists content_calendar_slot_index_check" in sql
    assert "slot_index = 2" in sql
    assert "post_date between date '2026-09-23' and date '2026-11-08'" in sql
    assert "lower(btrim(format))" in sql
    assert "lower(btrim(v_row.format))" in sql
