"""
Real month planner (agent/real_month_planner.py), all offline.

plan_month emits exactly 2 slots/day (feed + story), the correct weekly-rotation
category per weekday, is deterministic, folds book/summit/welcome overrides onto the
right days, and is safe for days <= 0. build_month_drafts uses ONLY the injected
builders, skips (never fakes) a missing-source slot, keeps feed and story as separate
drafts with the correct format, and respects the 9:16 story assertion. to_calendar_rows
has the content_calendar shape (NO id: the DB generates the uuid), is gym-scoped, status
pending. apply_month_plan is DELETE-then-INSERT: it deletes ALL of the gym's rows across
the full planned span (demo and prior real) then inserts the fresh real rows without an
id, never touches another gym, and leaves no demo id behind.

Nothing here publishes, hosts, or writes to a live store: every store is an injected fake.
"""

import os
import sys
import uuid as _uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import real_month_planner as rmp  # noqa: E402
from agent import demo_calendar_queue as demo  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402

ACCT = "lasso"
# 2026-08-03 is a Monday, so a 7-day window from here walks Mon..Sun cleanly.
MON = "2026-08-03"


def _reject_non_uuid_id(row):
    """Model the real DB: an insert that carries an `id` at all is rejected. The insert
    path MUST omit id so gen_random_uuid fires; a draft id is a non-uuid string that
    Postgres would refuse with 22P02. A fake that silently accepted it hid the live bug."""
    if "id" in row and row.get("id") not in (None, ""):
        try:
            _uuid.UUID(str(row["id"]))
        except (ValueError, AttributeError, TypeError):
            raise AssertionError(
                f"non-uuid id sent to insert (22P02 in real DB): {row.get('id')!r}")
        raise AssertionError("insert must not send id; the DB generates the uuid")


# ---- fakes ----------------------------------------------------------------

def _draft(draft_id, *, day_key, category, platform="instagram",
           caption="real caption", url="https://cdn/x.jpg", is_story=False,
           draft_type="feed"):
    return Draft(
        draft_id=draft_id, account_key=ACCT, platform=platform, caption=caption,
        hashtags=[], creative_path="x.png", creative_public_url=url,
        scheduled_for="", status=DraftStatus.PENDING, is_story=is_story,
        day_key=day_key, draft_type=draft_type, category=category)


class _FakeSB:
    """Stands in for SupabaseCalendarStore; records writes, enforces gym isolation, and
    models the REAL schema: content_calendar.id is a DB-generated uuid. insert_rows MUST
    NOT receive an id (a non-uuid id would be rejected 22P02 by Postgres); the fake
    generates the uuid itself, exactly as gen_random_uuid would."""

    def __init__(self, rows=None):
        self._rows = {}
        for r in (rows or []):
            rid = r.get("id") or _uuid.uuid4().hex
            self._rows[rid] = dict(r, id=rid)
        self.upserts = []
        self.inserts = []
        self.deletes = []

    def list_month(self, account_key, month):
        return [dict(r) for r in self._rows.values()
                if str(r.get("gym_id")) == str(account_key)
                and (r.get("post_date") or "").startswith(month)]

    def insert_rows(self, account_key, rows):
        out = []
        for row in (rows or []):
            # The real DB rejects a non-uuid id (22P02). The insert path must send NO id
            # so the DB can generate one; a fake that accepts a draft id would have hidden
            # the live bug, so this fake REJECTS any id here.
            _reject_non_uuid_id(row)
            assert str(row.get("gym_id")) == str(account_key), "cross-gym insert"
            rid = _uuid.uuid4().hex  # DB-generated uuid
            saved = dict(row, id=rid, gym_id=account_key)
            self._rows[rid] = saved
            self.inserts.append((account_key, dict(saved)))
            out.append(dict(saved))
        return out

    def delete_month(self, account_key, month):
        victims = [rid for rid, r in self._rows.items()
                   if str(r.get("gym_id")) == str(account_key)
                   and (r.get("post_date") or "").startswith(month)]
        for rid in victims:
            del self._rows[rid]
        self.deletes.append((account_key, month, len(victims)))
        return len(victims)

    def delete_row(self, account_key, row_id):
        self.deletes.append((account_key, row_id))
        r = self._rows.get(row_id)
        if r is not None and str(r.get("gym_id")) == str(account_key):
            del self._rows[row_id]
            return 1
        return 0


def _builders_all(record=None):
    """A builders map that produces a real feed draft for EVERY category. `record` (a
    list) captures each (category, day) call so a test can prove the injected builder,
    not the planner, produced the content."""
    def _mk(category):
        def _b(target, day_key):
            if record is not None:
                record.append((category, day_key))
            return _draft(f"f_{category}_{day_key}", day_key=day_key,
                          category=category, draft_type=category if category in
                          ("podcast", "book", "summit", "b2b") else "feed")
        return _b
    cats = ("podcast", "platform", "b2b", "summit", "book", "doctrine", "welcome")
    return {c: _mk(c) for c in cats}


def _story_builder_ok(target, day_key, feed_draft):
    """A story builder that always returns a genuine 9:16 story draft anchored to the
    day's feed draft (mirrors stories.py output: is_story True, 9:16)."""
    return _draft(f"s_{feed_draft.category}_{day_key}", day_key=day_key,
                  category=feed_draft.category, is_story=True, draft_type="story",
                  url="https://cdn/story.jpg")


# ---- plan_month -----------------------------------------------------------

def test_plan_month_two_slots_per_day_feed_and_story():
    plan = rmp.plan_month(ACCT, MON, days=30, book_dates=set(),
                          summit_day_fn=lambda d: False, welcome_dates=set(),
                          sprint_day_fn=lambda d: False)
    assert len(plan) == 60  # exactly 2 per day
    from collections import Counter
    by_date = Counter(s.post_date for s in plan)
    assert all(v == 2 for v in by_date.values())
    for i in range(0, len(plan), 2):
        assert plan[i].fmt == "feed"
        assert plan[i + 1].fmt == "story"
        assert plan[i].post_date == plan[i + 1].post_date
        assert plan[i].category == plan[i + 1].category  # paired story shares pillar


def test_plan_month_weekday_categories():
    # Mon..Sun from 2026-08-03, no overrides. The BALANCED month rotation drives:
    # doctrine / platform / b2b / podcast / summit spread so no everyday pillar dominates
    # and doctrine (absent from the podcast-heavy live daily schedule) is present.
    plan = rmp.plan_month(ACCT, MON, days=7, book_dates=set(),
                          summit_day_fn=lambda d: False, welcome_dates=set())
    feeds = [s for s in plan if s.fmt == "feed"]
    got = [(s.post_date, s.category) for s in feeds]
    assert got == [
        ("2026-08-03", "platform"),  # Mon
        ("2026-08-04", "doctrine"),  # Tue
        ("2026-08-05", "b2b"),       # Wed
        ("2026-08-06", "podcast"),   # Thu
        ("2026-08-07", "summit"),    # Fri
        ("2026-08-08", "platform"),  # Sat
        ("2026-08-09", "podcast"),   # Sun
    ]


def test_plan_month_is_deterministic():
    a = rmp.plan_month(ACCT, MON, days=30, book_dates=set(),
                       summit_day_fn=lambda d: False, welcome_dates=set())
    b = rmp.plan_month(ACCT, MON, days=30, book_dates=set(),
                       summit_day_fn=lambda d: False, welcome_dates=set())
    assert a == b


def test_plan_month_days_zero_or_negative_safe():
    assert rmp.plan_month(ACCT, MON, days=0) == []
    assert rmp.plan_month(ACCT, MON, days=-5) == []
    assert rmp.plan_month(ACCT, MON, days=None) == []


def test_plan_month_book_override_lands_on_book_dates():
    # 2026-08-05 is a Wed (b2b in the rotation); a dated book post overrides it to book.
    plan = rmp.plan_month(ACCT, MON, days=7, book_dates={"2026-08-05"},
                          summit_day_fn=lambda d: False, welcome_dates=set())
    wed_feed = next(s for s in plan if s.post_date == "2026-08-05" and s.fmt == "feed")
    assert wed_feed.category == "book"
    assert wed_feed.base_category == "b2b"
    assert wed_feed.overridden is True
    # the paired story is overridden too
    wed_story = next(s for s in plan if s.post_date == "2026-08-05" and s.fmt == "story")
    assert wed_story.category == "book"


def test_plan_month_summit_override_lands_on_summit_days():
    # Force Tuesdays to be summit days; 2026-08-04 is a Tue (doctrine in the balanced
    # month rotation).
    def _tue_summit(day_key):
        from datetime import date
        return date.fromisoformat(day_key).weekday() == 1
    plan = rmp.plan_month(ACCT, MON, days=7, book_dates=set(),
                          summit_day_fn=_tue_summit, welcome_dates=set())
    tue_feed = next(s for s in plan if s.post_date == "2026-08-04" and s.fmt == "feed")
    assert tue_feed.category == "summit"
    assert tue_feed.base_category == "doctrine"
    assert tue_feed.overridden is True


def test_plan_month_welcome_override_lands_on_welcome_dates():
    plan = rmp.plan_month(ACCT, MON, days=7, book_dates=set(),
                          summit_day_fn=lambda d: False,
                          welcome_dates={"2026-08-08"})  # a Sat (platform)
    sat_feed = next(s for s in plan if s.post_date == "2026-08-08" and s.fmt == "feed")
    assert sat_feed.category == "welcome"
    assert sat_feed.base_category == "platform"


def test_plan_month_override_precedence_book_over_summit_over_welcome():
    # A single day flagged in all three sets resolves to book (highest precedence).
    plan = rmp.plan_month(ACCT, MON, days=1, book_dates={MON},
                          summit_day_fn=lambda d: True, welcome_dates={MON})
    assert plan[0].category == "book"


def test_plan_month_default_book_dates_from_book_queue():
    # With defaults (no injected book_dates), the real dated book posts override their
    # days. Pick a known BOOK_POSTS date and assert it reads 'book'.
    from agent import book_queue
    a_book_date = book_queue.BOOK_POSTS[0]["date"]
    from datetime import date
    start = date.fromisoformat(a_book_date)
    plan = rmp.plan_month(ACCT, start.isoformat(), days=1)
    assert plan[0].category == "book"


# ---- build_month_drafts ---------------------------------------------------

def test_build_uses_injected_builders_and_pairs_story():
    record = []
    plan = rmp.plan_month(ACCT, MON, days=7, book_dates=set(),
                          summit_day_fn=lambda d: False, welcome_dates=set())
    drafts = rmp.build_month_drafts(plan, _builders_all(record),
                                    story_builder=_story_builder_ok, account=None)
    # 7 feed + 7 story = 14 drafts
    feeds = [d for d in drafts if not d.is_story]
    stories = [d for d in drafts if d.is_story]
    assert len(feeds) == 7
    assert len(stories) == 7
    # the injected builder produced the content, once per feed slot
    assert len(record) == 7
    # every story is a separate object anchored to the SAME day as a feed
    feed_days = {d.day_key for d in feeds}
    story_days = {d.day_key for d in stories}
    assert feed_days == story_days


def test_build_story_is_9_16_and_feed_is_not():
    plan = rmp.plan_month(ACCT, MON, days=1, book_dates=set(),
                          summit_day_fn=lambda d: False, welcome_dates=set())
    drafts = rmp.build_month_drafts(plan, _builders_all(),
                                    story_builder=_story_builder_ok)
    feed = next(d for d in drafts if not d.is_story)
    story = next(d for d in drafts if d.is_story)
    assert feed.draft_type != "story"
    assert story.is_story is True and story.draft_type == "story"
    # feed and story are DIFFERENT draft objects
    assert feed.draft_id != story.draft_id


def test_build_missing_source_slot_falls_back_to_real_pillar_not_faked():
    # When a slot's own category has no builder, the day FILLS from the next REAL pillar
    # with content (never a blank, never fabricated). Only 'podcast' can build here, so
    # every non-podcast day falls back to the real podcast pillar and the whole week fills.
    plan = rmp.plan_month(ACCT, MON, days=7, book_dates=set(),
                          summit_day_fn=lambda d: False, welcome_dates=set())
    builders = {"podcast": _builders_all()["podcast"]}  # others absent -> no builder
    drafts = rmp.build_month_drafts(plan, builders, story_builder=_story_builder_ok)
    feeds = [d for d in drafts if not d.is_story]
    stories = [d for d in drafts if d.is_story]
    # every one of the 7 days fills (feed + story), all via the one real pillar available
    assert len(feeds) == 7
    assert len(stories) == 7
    assert all(d.category == "podcast" for d in drafts)
    # no fabricated caption ever appears: every draft came from the injected builder
    assert all(d.caption == "real caption" for d in drafts)


def test_build_empty_when_no_pillar_has_content():
    # No builder at all for any pillar: no real pillar can fill any day, so nothing is
    # built and NOTHING is fabricated to fill the blanks.
    plan = rmp.plan_month(ACCT, MON, days=7, book_dates=set(),
                          summit_day_fn=lambda d: False, welcome_dates=set())
    drafts = rmp.build_month_drafts(plan, {}, story_builder=_story_builder_ok)
    assert drafts == []


def test_build_fallback_relabels_pillar_to_the_one_that_built():
    # A platform day with no platform builder but a b2b builder present fills from b2b,
    # and the calendar row shows the TRUE pillar (b2b), feed and paired story alike.
    # 2026-08-04 is a Tue (doctrine in the balanced month rotation).
    plan = rmp.plan_month(ACCT, MON, days=2, book_dates=set(),
                          summit_day_fn=lambda d: False, welcome_dates=set())
    builders = {"b2b": _builders_all()["b2b"]}  # only b2b can build
    drafts = rmp.build_month_drafts(plan, builders, story_builder=_story_builder_ok)
    tue = [d for d in drafts if d.day_key == "2026-08-04"]
    assert tue and all(d.category == "b2b" for d in tue)
    # the row mapping shows b2b (the real pillar that filled the day), not platform
    rows = rmp.to_calendar_rows(tue, ACCT)
    assert rows and all(r["pillar"] == "b2b" for r in rows)


def test_build_story_skipped_when_no_genuine_9_16():
    # story_builder returns None (no genuine 9:16): the feed still builds, the story is
    # dropped (never a cropped feed card).
    plan = rmp.plan_month(ACCT, MON, days=1, book_dates=set(),
                          summit_day_fn=lambda d: False, welcome_dates=set())
    drafts = rmp.build_month_drafts(plan, _builders_all(),
                                    story_builder=lambda t, d, f: None)
    assert any(not d.is_story for d in drafts)
    assert not any(d.is_story for d in drafts)


def test_build_no_feed_means_no_story_for_that_day():
    # If a day's feed builder returns None, its story slot has nothing to anchor to.
    plan = rmp.plan_month(ACCT, MON, days=1, book_dates=set(),  # Mon -> podcast
                          summit_day_fn=lambda d: False, welcome_dates=set())
    builders = {}  # no builders at all
    drafts = rmp.build_month_drafts(plan, builders, story_builder=_story_builder_ok)
    assert drafts == []


# ---- to_calendar_rows -----------------------------------------------------

def test_rows_shape_gym_scoped_pending():
    plan = rmp.plan_month(ACCT, MON, days=3, book_dates=set(),
                          summit_day_fn=lambda d: False, welcome_dates=set())
    drafts = rmp.build_month_drafts(plan, _builders_all(),
                                    story_builder=_story_builder_ok)
    rows = rmp.to_calendar_rows(drafts, ACCT)
    assert rows, "expected calendar rows"
    for r in rows:
        assert set(r.keys()) >= {"gym_id", "account", "post_date", "pillar",
                                 "format", "caption", "image_url", "status"}
        assert "id" not in r, "row must not carry an id (the DB generates the uuid)"
        assert r["gym_id"] == ACCT
        assert r["status"] == "pending"
        assert r["format"] in ("feed", "story")
        assert r["pillar"]  # the plan category rode through
    # a feed row reads 'feed', a story row reads 'story'
    fmts = {r["format"] for r in rows}
    assert fmts == {"feed", "story"}


def test_rows_drop_draft_with_no_post_date():
    d = _draft("nodate", day_key="", category="podcast")
    d.scheduled_for = ""
    rows = rmp.to_calendar_rows([d], ACCT)
    assert rows == []


# ---- apply_month_plan -----------------------------------------------------

def test_apply_upserts_real_and_deletes_all_demo_for_gym():
    # Seed the store with demo rows for the gym across TWO months of the planned span,
    # plus a real gym's rows on ANOTHER gym that must never be touched.
    demo_feed_id = demo._draft_id(ACCT, "2026-08-10", "feed")   # demof_...
    demo_story_id = demo._draft_id(ACCT, "2026-09-02", "story")  # demos_... next month
    other_gym_id = "keep_me"
    sb = _FakeSB(rows=[
        {"id": demo_feed_id, "gym_id": ACCT, "post_date": "2026-08-10",
         "format": "feed", "status": "pending"},
        {"id": demo_story_id, "gym_id": ACCT, "post_date": "2026-09-02",
         "format": "story", "status": "pending"},
        {"id": other_gym_id, "gym_id": "northside_ig", "post_date": "2026-08-10",
         "format": "feed", "status": "pending"},
    ])
    assert demo.is_demo_draft_id(demo_feed_id)
    assert demo.is_demo_draft_id(demo_story_id)

    plan = rmp.plan_month(ACCT, MON, days=30, book_dates=set(),
                          summit_day_fn=lambda d: False, welcome_dates=set(),
                          sprint_day_fn=lambda d: False)
    drafts = rmp.build_month_drafts(plan, _builders_all(),
                                    story_builder=_story_builder_ok)
    span = rmp.plan_span_months(MON, days=30)  # sweeps Aug AND Sep
    out = rmp.apply_month_plan(ACCT, drafts, sb, span_months=span)

    assert out["ok"] is True
    assert out["inserted"] == len(drafts) > 0
    # DELETE-then-INSERT swept the WHOLE Aug AND Sep months for the gym, so BOTH demo rows
    # (including the next-month one a narrow sweep would have missed) are gone -> the
    # month-range gap is closed and a re-run is idempotent.
    assert out["deleted"] == 2
    # no demo id survives on the gym
    surviving = [r for r in sb._rows.values() if str(r.get("gym_id")) == ACCT]
    assert not any(demo.is_demo_draft_id(r["id"]) for r in surviving)
    # every surviving gym row carries a real DB uuid, never a draft/demo id
    for r in surviving:
        assert _uuid.UUID(r["id"])
    # the other gym's row is untouched
    assert other_gym_id in sb._rows
    # every delete was scoped to our gym
    assert all(acct == ACCT for acct, *_ in sb.deletes)
    # every insert carried our gym_id and NO id was sent
    assert all(str(row["gym_id"]) == ACCT for _, row in sb.inserts)


def test_apply_refuses_demo_gym_id():
    sb = _FakeSB()
    from agent import config
    out = rmp.apply_month_plan(config.demo_calendar_gym_id(), [], sb)
    assert out["ok"] is False
    assert not sb.inserts and not sb.deletes


def test_apply_missing_store_is_safe():
    out = rmp.apply_month_plan(ACCT, [], None)
    assert out["ok"] is False


def test_apply_never_inserts_a_demo_id():
    # Even if a demo-id draft somehow reached apply, it is filtered out before insert
    # (keyed off the draft's OWN id, since the row no longer carries one).
    sb = _FakeSB()
    d = _draft(demo._draft_id(ACCT, "2026-08-10", "feed"),
               day_key="2026-08-10", category="podcast")
    out = rmp.apply_month_plan(ACCT, [d], sb, span_months=["2026-08"])
    assert out["inserted"] == 0
    assert not sb.inserts
