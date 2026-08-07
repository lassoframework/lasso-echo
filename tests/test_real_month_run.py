"""
Real month RUN wiring + the month-ahead podcast builder, all offline (fake nano/S3,
injected episode pool). Asserts:

  * podcast_month.build_month_podcast_draft returns DISTINCT real drafts for distinct
    podcast days (deterministic per day), draws ONLY from the real episode pool, and
    returns None (never a fake) when the pool is exhausted or the flag is off.
  * real_month_run.real_builders_map wires every real pillar and, with only some
    builders producing content, the plan still FILLS every day via the real fallback
    (never a blank, never fabricated).
  * a 30-day dry-run fills ~all 60 slots, all pillars present, none dominating, 2/day
    feed + paired 9:16 story, deterministic.
  * plan_and_build is inert while AGENT_REAL_MONTH_PLAN is OFF (returns [], invokes
    nothing).

No live Gemini / R2 / Supabase / publish: every render is a fake, no network.
"""

import os
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, creative_studio, media_host  # noqa: E402
from agent import podcast_feed, podcast_month  # noqa: E402
from agent import real_month_planner as rmp  # noqa: E402
from agent import real_month_run as rmr  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402

ACCT = "lasso"
MON = "2026-08-03"  # a Monday: a 7-day window walks Mon..Sun cleanly


def _acct():
    return Account(key="lasso", display_name="LASSO", platform=Platform.INSTAGRAM,
                   token_env="X", target_id_env="Y")


# ---- a real (not fabricated) episode pool, injected ------------------------------------

def _episodes():
    """Three real stored episodes (number + title + description). The month builder
    draws its topics straight from these; the test asserts it never invents beyond
    them."""
    return [
        {"episode": 131, "title": "Episode 131: The Follow Up Problem",
         "description": "Most gyms do not have a lead problem. They have a follow up problem.",
         "guid": "g131"},
        {"episode": 132, "title": "Episode 132: Pricing With Confidence",
         "description": "Discounting your membership trains the wrong buyer.",
         "guid": "g132"},
        {"episode": 133, "title": "Episode 133: The Front Desk Script",
         "description": "The first ninety seconds on the phone decide the sale.",
         "guid": "g133"},
    ]


def _arm(monkeypatch, tmp_path):
    """Arm the podcast pool + a FAKE studio/host (no network) and inject the episode
    pool. No transcripts stored, so the pool is the episode title/about topics only."""
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "true")
    monkeypatch.setattr(podcast_feed, "list_episodes", _episodes)
    # Fake render: creative_studio.generate -> a path; media_host.host_media -> a URL.
    monkeypatch.setattr(creative_studio, "generate",
                        lambda headline, facts, **kw: {"path": f"/tmp/{abs(hash(headline)) % 999}.png",
                                                       "prompt": "p"})
    monkeypatch.setattr(media_host, "host_media",
                        lambda path, key, **kw: f"https://cdn.test/{os.path.basename(path)}")


# ---- podcast month-ahead builder -------------------------------------------------------

def test_podcast_month_distinct_real_topics_for_distinct_days(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    acct = _acct()
    # Three consecutive days: their date ordinals are n, n+1, n+2, so seq % pool_size
    # (pool size 3 here) gives three DISTINCT indices -> three distinct real topics.
    days = ["2026-08-03", "2026-08-04", "2026-08-05"]
    drafts = [podcast_month.build_month_podcast_draft(acct, d) for d in days]
    assert all(d is not None for d in drafts)
    # every draft is a real podcast draft cited to a real episode; captions differ
    # across days (distinct real topics, deterministic per day).
    captions = [d.caption for d in drafts]
    assert len(set(captions)) == 3  # distinct real topics across the podcast days
    for d in drafts:
        assert d.draft_type == "podcast"
        cites = [f for f in d.source_fragments if f.startswith("cite:podcast_ep")]
        assert len(cites) == 1  # cited to exactly one real episode, never invented
        # the episode number in the cite is one of the real pool's numbers
        n = int(cites[0].split("podcast_ep")[1])
        assert n in (131, 132, 133)


def test_podcast_month_is_deterministic(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    acct = _acct()
    a = podcast_month.build_month_podcast_draft(acct, "2026-08-06")
    b = podcast_month.build_month_podcast_draft(acct, "2026-08-06")
    assert a is not None and b is not None
    assert a.caption == b.caption and a.source_fragments == b.source_fragments


def test_podcast_month_exhausted_pool_returns_none_never_fake(monkeypatch, tmp_path):
    # No stored episodes at all: the pool is empty. The builder returns None (the month
    # planner then falls back to another real pillar) and NEVER fabricates a topic.
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "true")
    monkeypatch.setattr(podcast_feed, "list_episodes", lambda: [])
    monkeypatch.setattr(creative_studio, "generate",
                        lambda *a, **k: pytest.fail("studio must not be called on an "
                                                    "empty pool"))
    assert podcast_month.build_month_podcast_draft(_acct(), "2026-08-06") is None


def test_podcast_month_flag_off_inert(monkeypatch):
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    monkeypatch.setattr(creative_studio, "generate",
                        lambda *a, **k: pytest.fail("no render while flag off"))
    assert podcast_month.build_month_podcast_draft(_acct(), "2026-08-06") is None


def test_podcast_month_dark_studio_returns_none(monkeypatch):
    # Pool present but the studio is dark (generate -> None): no fabrication, just None.
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "true")
    monkeypatch.setattr(podcast_feed, "list_episodes", _episodes)
    monkeypatch.setattr(creative_studio, "generate", lambda *a, **k: None)
    assert podcast_month.build_month_podcast_draft(_acct(), "2026-08-06") is None


# ---- run wiring: real_builders_map + plan_and_build ------------------------------------

def test_real_builders_map_wires_every_pillar():
    builders = rmr.real_builders_map(_acct())
    for cat in ("podcast", "platform", "b2b", "doctrine", "summit", "book", "welcome"):
        assert cat in builders and callable(builders[cat])


def test_plan_and_build_flag_off_is_inert(monkeypatch):
    monkeypatch.delenv("AGENT_REAL_MONTH_PLAN", raising=False)
    # Nothing should be invoked while the flag is off.
    monkeypatch.setattr(rmr, "real_builders_map",
                        lambda a: pytest.fail("must not build while flag off"))
    assert rmr.plan_and_build(ACCT, MON, days=30) == []


# ---- a 30-day dry-run: fills ~60, all pillars, none dominating -------------------------

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
    """A builders map that produces a real feed for each category in `available` and
    None (no content) for the rest, so the fallback chain is exercised for real."""
    def _mk(cat):
        def _b(target, day):
            return _fake_feed(cat, day) if cat in available else None
        return _b
    cats = ("podcast", "platform", "b2b", "summit", "book", "doctrine", "welcome")
    return {c: _mk(c) for c in cats}


def test_30_day_dryrun_fills_all_and_all_pillars_present():
    # Real dated overrides: some book dates in the window, a couple of welcome dates,
    # summit on its extra weekday during the run-up (the default predicate).
    book_dates = {"2026-08-05", "2026-08-08", "2026-08-12", "2026-08-15"}
    welcome_dates = {"2026-08-11", "2026-08-20"}
    # This test exercises the NON-sprint rotation/fallback path; sprint days have their own
    # coverage in test_real_month_sprint.py, so hold the sprint off here.
    plan = rmp.plan_month(ACCT, MON, days=30, book_dates=book_dates,
                          welcome_dates=welcome_dates,  # default summit predicate
                          sprint_day_fn=lambda d: False)
    # Every pillar can build (the real, armed-fleet case): the whole month fills.
    builders = _all_pillar_builders(
        {"podcast", "platform", "b2b", "summit", "book", "doctrine", "welcome"})
    drafts = rmp.build_month_drafts(plan, builders, story_builder=_fake_story)

    feeds = [d for d in drafts if not d.is_story]
    stories = [d for d in drafts if d.is_story]
    assert len(feeds) == 30           # every one of the 30 days has a feed
    assert len(stories) == 30         # and a paired 9:16 story
    assert len(drafts) == 60          # 2/day, fully filled

    # every day is present exactly once as a feed
    feed_days = Counter(d.day_key for d in feeds)
    assert len(feed_days) == 30 and all(v == 1 for v in feed_days.values())

    # ALL pillars represented, none dominating (no pillar > half the feed days).
    dist = Counter(d.category for d in feeds)
    for pillar in ("podcast", "platform", "b2b", "summit", "book", "doctrine", "welcome"):
        assert dist[pillar] >= 1, f"{pillar} missing from the month"
    assert max(dist.values()) <= len(feeds) // 2, f"a pillar dominates: {dist}"


def test_30_day_dryrun_fills_via_fallback_when_a_pillar_is_dark():
    # A realistic partial arm: podcast + platform + b2b + doctrine available; summit,
    # book, welcome dark (their builders return None). Every day STILL fills from the
    # available real pillars via fallback; nothing is fabricated, no blank days.
    plan = rmp.plan_month(ACCT, MON, days=30, book_dates=set(),
                          summit_day_fn=lambda d: False, welcome_dates=set(),
                          sprint_day_fn=lambda d: False)
    builders = _all_pillar_builders({"podcast", "platform", "b2b", "doctrine"})
    drafts = rmp.build_month_drafts(plan, builders, story_builder=_fake_story)
    feeds = [d for d in drafts if not d.is_story]
    stories = [d for d in drafts if d.is_story]
    assert len(feeds) == 30 and len(stories) == 30  # no empty day
    # only the available pillars ever appear (no summit/book/welcome, none faked)
    dist = Counter(d.category for d in feeds)
    assert set(dist) <= {"podcast", "platform", "b2b", "doctrine"}
    for pillar in ("podcast", "platform", "b2b", "doctrine"):
        assert dist[pillar] >= 1


def test_30_day_dryrun_is_deterministic():
    book_dates = {"2026-08-05", "2026-08-08"}
    kw = dict(book_dates=book_dates, welcome_dates={"2026-08-11"})
    b1 = _all_pillar_builders({"podcast", "platform", "b2b", "summit", "book",
                               "doctrine", "welcome"})
    b2 = _all_pillar_builders({"podcast", "platform", "b2b", "summit", "book",
                               "doctrine", "welcome"})
    a = rmp.build_month_drafts(rmp.plan_month(ACCT, MON, days=30, **kw), b1,
                               story_builder=_fake_story)
    b = rmp.build_month_drafts(rmp.plan_month(ACCT, MON, days=30, **kw), b2,
                               story_builder=_fake_story)
    assert [(d.day_key, d.category, d.is_story) for d in a] == \
           [(d.day_key, d.category, d.is_story) for d in b]


def test_stories_are_9_16_and_paired_to_feed():
    plan = rmp.plan_month(ACCT, MON, days=7, book_dates=set(),
                          summit_day_fn=lambda d: False, welcome_dates=set())
    builders = _all_pillar_builders({"podcast", "platform", "b2b", "doctrine"})
    drafts = rmp.build_month_drafts(plan, builders, story_builder=_fake_story)
    feeds = {d.day_key for d in drafts if not d.is_story}
    stories = [d for d in drafts if d.is_story]
    assert stories and all(s.is_story and s.draft_type == "story" for s in stories)
    # every story pairs to a day that has a feed, and shares its pillar
    for s in stories:
        assert s.day_key in feeds
    by_day_feed = {d.day_key: d.category for d in drafts if not d.is_story}
    for s in stories:
        assert s.category == by_day_feed[s.day_key]  # paired story shares the pillar
