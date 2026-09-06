"""
DAY SHAPE: two slots on one day are two DIFFERENT posts, asserted at plan time.

The incident this covers, verified against production on 2026-09-05:

  * piercefitness 2026-09-27 carries SIX pending rows built in one pass
    (created_at 2026-08-30T19:10:23), slot_index 0 and slot_index 1, every one of
    them opening "You remember when five minutes of jogging felt impossible."
    Two different videos, ONE caption, twice on each account. Nothing in the
    build stopped it and nothing in the write path noticed.
  * lasso 2026-09-16 carries two facebook feed rows with one identical platform
    caption, out of the LASSO month lane.
  * That plan shape is what published Tough Temple six times in forty seconds.

So the assertions here are about the WRITE, not about a builder's good intentions:
whatever lane produced the rows, a batch that would put the same caption or the
same photo twice on one account on one day must FAIL the pass and write nothing.

Also covers the content contract (slot 0 is PROOF in the morning, slot 1 is the
INVITATION in the evening) and the opening FORMULA cap that catches what
drafter.openings_collide structurally cannot: fifteen Tough Temple captions in a
row opening on the second person pronoun, no two of them sharing four words.

Fully offline.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, day_shape  # noqa: E402
from agent import client_month_run as cmr  # noqa: E402
from agent.drafter import (formula_run_exceeded, opening_formula,  # noqa: E402
                           openings_collide)


# The REAL Tough Temple captions, read from production content_calendar on
# 2026-09-05 (gym_id toughtemple52040e, instagram feed, 2026-09-02 .. 2026-09-16).
# Six of these are denied rows. Every one opens on the second person pronoun.
TOUGH_TEMPLE_OPENINGS = (
    "You walk in and see the space where your excuses end.",
    "You show up consistent. You do the work.",
    "You showed up even though the treadmill felt like the last place you wanted.",
    "You walk in and it's not what you expected.",
    "You've been meaning to get stronger.",
    "You showed up to the gym thinking you'd find the same routine.",
    "You walk in hoping for a gym.",
    "You walked into that big box gym expecting something different.",
    "You're holding the rings, and your shoulders are screaming.",
    "You walked past a gym before and thought \"maybe someday.\"",
    "You showed up when it would've been easier to skip.",
    "You start training consistently and something shifts.",
    "You've tried the big box gyms.",
    "You've been spinning your wheels at the same big box gym for years.",
    "You showed up to find your old gym had become a treadmill farm.",
)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """The same posture test_cadence builds under, since the integration tests here
    drive the real build_client_month through that harness."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv("AGENT_CLIENT_MONTH", "true")
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    monkeypatch.delenv("ECHO_CADENCE_2X_ENABLED", raising=False)
    monkeypatch.delenv("ECHO_DAY_SHAPE_ASSERT", raising=False)
    monkeypatch.delenv("ECHO_DAY_SHAPE_ROLES", raising=False)
    monkeypatch.delenv("ECHO_OPENING_FORMULA_CAP", raising=False)
    monkeypatch.delenv("ECHO_OPENING_FORMULA_MAX_RUN", raising=False)
    monkeypatch.delenv("ECHO_GYM_ASK_COVERAGE", raising=False)
    yield


def _row(gym_id="eng", account="instagram", post_date="2026-09-20", fmt="feed",
         caption="A real caption for the day.", image_url="https://cdn/a.jpg",
         status="pending", **extra):
    row = {"gym_id": gym_id, "account": account, "post_date": post_date,
           "format": fmt, "caption": caption, "image_url": image_url,
           "status": status}
    row.update(extra)
    return row


# ---- the contract: what a two post day IS ------------------------------------------

def test_slot_zero_is_proof_slot_one_is_the_invitation():
    assert day_shape.role_for_slot(0) == day_shape.PROOF
    assert day_shape.role_for_slot(1) == day_shape.INVITATION
    # A 1x day (no cadence ordinal) reads as the morning proof post, today's shape.
    assert day_shape.role_for_slot(None) == day_shape.PROOF


def test_the_two_roles_draw_from_different_pillars_and_different_angles():
    proof = set(day_shape.pillars_for_role(day_shape.PROOF))
    invite = set(day_shape.pillars_for_role(day_shape.INVITATION))
    assert proof and invite
    assert not (proof & invite), "a pillar may not carry both jobs in one day"
    assert not (set(day_shape.angles_for_role(day_shape.PROOF))
                & set(day_shape.angles_for_role(day_shape.INVITATION)))


def test_slot_angles_differ_within_a_day_and_rotate_across_days():
    # The two slots of one day never lead from the same angle.
    for rotation in range(8):
        assert (day_shape.angle_for_slot(0, rotation)
                != day_shape.angle_for_slot(1, rotation))
    # And the same role does not lead the same way every single day.
    assert len({day_shape.angle_for_slot(0, r) for r in range(4)}) > 1


# ---- the assertion: a repeat never reaches the calendar -----------------------------

def test_clean_two_slot_day_passes():
    rows = [_row(caption="Karen ran her first mile at 56.", image_url="a.jpg"),
            _row(caption="Two intro spots left for Monday. Claim one.",
                 image_url="b.jpg")]
    assert day_shape.assert_day_distinct(rows) == []


def test_piercefitness_2026_09_27_shape_fails_the_pass():
    """The exact production row shape: one caption, two slots, two videos."""
    caption = ("You remember when five minutes of jogging felt impossible. "
               "Karen does too. Now at 56, she's crossing finish lines.")
    rows = [
        _row(gym_id="piercefitness", post_date="2026-09-27", caption=caption,
             image_url="https://r2/20260825T155821Z_IMG_9408.mp4", slot_index=0),
        _row(gym_id="piercefitness", post_date="2026-09-27", caption=caption,
             image_url="https://r2/20260825T155821Z_IMG_9409.mov", slot_index=1),
    ]
    with pytest.raises(day_shape.DayShapeViolation) as exc:
        day_shape.assert_day_distinct(rows)
    kinds = {v.kind for v in exc.value.violations}
    assert kinds == {"caption"}
    assert "piercefitness" in exc.value.violations[0].message()
    assert "2026-09-27" in exc.value.violations[0].message()


def test_two_slots_sharing_one_photo_fails_the_pass():
    """Pete's B5 repeat: distinct words, the same picture twice in one day."""
    rows = [_row(caption="One caption.", image_url="https://r2/same.jpg"),
            _row(caption="A genuinely different caption.",
                 image_url="https://r2/same.jpg")]
    with pytest.raises(day_shape.DayShapeViolation) as exc:
        day_shape.assert_day_distinct(rows)
    assert {v.kind for v in exc.value.violations} == {"image"}


def test_a_day_with_one_honest_post_passes():
    """A 2x day that can only produce one distinct concept posts ONCE. That is the
    truth, not a violation, and it must not fail the pass."""
    assert day_shape.assert_day_distinct([_row()]) == []


# ---- the three legitimate repeats the guard must NOT fire on -----------------------

def test_facebook_mirror_of_an_instagram_feed_is_not_a_repeat():
    caption = "One caption, cross posted."
    rows = [_row(account="instagram", caption=caption, image_url="a.jpg"),
            _row(account="facebook", caption=caption, image_url="a.jpg")]
    assert day_shape.assert_day_distinct(rows) == []


def test_a_paired_story_carrying_its_feeds_caption_is_not_a_repeat():
    caption = "One caption on the feed and on its paired story."
    rows = [_row(fmt="feed", caption=caption, image_url="feed.jpg"),
            _row(fmt="story", caption=caption, image_url="story.mp4")]
    assert day_shape.assert_day_distinct(rows) == []


def test_the_same_caption_on_two_different_days_is_not_this_guards_business():
    caption = "A caption reused a fortnight later."
    rows = [_row(post_date="2026-09-20", caption=caption, image_url="a.jpg"),
            _row(post_date="2026-10-04", caption=caption, image_url="b.jpg")]
    assert day_shape.assert_day_distinct(rows) == []


def test_empty_captions_and_empty_media_are_holds_not_repeats():
    rows = [_row(caption="", image_url=""), _row(caption="", image_url="")]
    assert day_shape.assert_day_distinct(rows) == []


def test_deleted_rows_are_already_off_the_calendar():
    caption = "A repeated caption on a soft deleted row."
    rows = [_row(caption=caption, image_url="a.jpg"),
            _row(caption=caption, image_url="b.jpg", status="deleted")]
    assert day_shape.assert_day_distinct(rows) == []


def test_captions_differing_only_in_whitespace_and_case_are_the_same_post():
    rows = [_row(caption="Karen ran her first mile.", image_url="a.jpg"),
            _row(caption="karen  ran\nher   first mile.", image_url="b.jpg")]
    with pytest.raises(day_shape.DayShapeViolation):
        day_shape.assert_day_distinct(rows)


def test_every_broken_day_is_reported_not_only_the_first():
    rows = []
    for day in ("2026-09-20", "2026-09-21", "2026-09-22"):
        rows.append(_row(post_date=day, caption="same words", image_url=f"{day}a"))
        rows.append(_row(post_date=day, caption="same words", image_url=f"{day}b"))
    with pytest.raises(day_shape.DayShapeViolation) as exc:
        day_shape.assert_day_distinct(rows)
    assert len(exc.value.violations) == 3


# ---- the escape hatch ---------------------------------------------------------------

def test_the_guard_is_armed_by_default():
    assert config.day_shape_assert_enabled() is True


def test_escape_hatch_restores_the_old_silent_behaviour(monkeypatch):
    monkeypatch.setenv("ECHO_DAY_SHAPE_ASSERT", "false")
    assert config.day_shape_assert_enabled() is False
    rows = [_row(caption="same", image_url="a"), _row(caption="same", image_url="b")]
    # Disarmed, the batch passes exactly as it did before the guard existed.
    assert day_shape.assert_day_distinct(
        rows, enabled=config.day_shape_assert_enabled()) == []
    # And the violations are still measurable without raising.
    assert len(day_shape.day_violations(rows)) == 1


def test_the_producer_half_ships_off():
    assert config.day_shape_roles_enabled() is False
    assert config.opening_formula_cap_enabled() is False


# ---- the ENG shape, end to end through the real build ------------------------------

def _eng_rows_two_slots():
    """The exact ENG shape: one gym, one account, one date, two cadence slots."""
    return [
        _row(gym_id="eng", account="instagram", post_date="2026-09-20", fmt="feed",
             caption=("Brought a friend and now you're both hooked? That's what "
                      "happens when you find a space that actually gets you."),
             image_url="https://r2/echo/eng/06ae8c9530ee4c2b/Twins.jpg",
             slot_index=0),
        _row(gym_id="eng", account="instagram", post_date="2026-09-20", fmt="feed",
             caption=("Two intro spots left this week. Grab one and we will build "
                      "the first month around your schedule."),
             image_url="https://r2/echo/eng/3e249f4e73ae6376/friendsgiving-83.jpg",
             slot_index=1),
    ]


def test_the_eng_shape_is_what_the_guard_calls_clean():
    rows = _eng_rows_two_slots()
    slot0, slot1 = rows
    assert slot0["post_date"] == slot1["post_date"]
    assert slot0["account"] == slot1["account"]
    assert slot0["slot_index"] != slot1["slot_index"]
    assert slot0["caption"].strip() != slot1["caption"].strip()
    assert slot0["image_url"] != slot1["image_url"]
    assert day_shape.assert_day_distinct(rows) == []


def test_eng_two_slots_on_one_day_carry_two_captions_and_two_images(monkeypatch,
                                                                    tmp_path):
    """THE regression test, on real build output rather than a fixture.

    ENG is one of the four gyms on posts_per_day=2 in production. For every day a
    2x build covers: same date, same account, two slots, and the two captions and
    the two image_urls must differ."""
    from tests.test_cadence import _FakeStore, _build

    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    store = _FakeStore(ppd=2)
    out = _build(tmp_path, store, days=3, n_media=8)
    assert out["ok"] and out["posts_per_day"] == 2

    by_day_account = {}
    for r in store.inserted:
        if r.get("format") != "feed":
            continue
        by_day_account.setdefault((r["post_date"], r["account"]), []).append(r)

    two_slot_days = {k: v for k, v in by_day_account.items() if len(v) == 2}
    assert two_slot_days, "the 2x build produced no two slot day to check"
    for (day, account), feeds in sorted(two_slot_days.items()):
        assert sorted(f.get("slot_index") for f in feeds) == [0, 1], (day, account)
        captions = {(f.get("caption") or "").strip() for f in feeds}
        images = {(f.get("image_url") or "").strip() for f in feeds}
        assert len(captions) == 2, f"{day} {account}: one caption in two slots"
        assert len(images) == 2, f"{day} {account}: one photo in two slots"
    # And the guard agrees with every one of them.
    assert day_shape.assert_day_distinct(store.inserted) == []


def test_build_client_month_writes_nothing_when_a_day_would_repeat(monkeypatch,
                                                                   tmp_path):
    """Whatever lane produced the duplicate, it never reaches content_calendar.

    The 2026-08-30 piercefitness pass wrote a repeated caption because no check
    ran between the drafts and the insert. This asserts on the WRITE: the store
    must be untouched and the caller must be told which day broke."""
    from tests.test_cadence import _FakeStore, _build  # reuse the real harness

    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    real_to_rows = cmr._to_rows

    def _duplicating_to_rows(base_key, drafts):
        rows = real_to_rows(base_key, drafts)
        # Simulate ANY lane that bypasses the per-day caption guard: put the first
        # feed row's caption onto a second row for the same day and account.
        feeds = [r for r in rows
                 if r.get("format") == "feed" and r.get("account") == "instagram"]
        if len(feeds) >= 2:
            feeds[1]["post_date"] = feeds[0]["post_date"]
            feeds[1]["caption"] = feeds[0]["caption"]
        return rows

    monkeypatch.setattr(cmr, "_to_rows", _duplicating_to_rows)
    store = _FakeStore(ppd=2)
    out = _build(tmp_path, store, days=3, n_media=8)

    assert out["ok"] is False
    assert out["reason"] == "day shape: same post twice in one day"
    assert out["day_shape_violations"], "the broken day must be named"
    assert store.inserted == [], "a repeat must never reach content_calendar"
    assert store.deleted == [], "a failed pass must not wipe the existing month"


def test_build_client_month_still_writes_a_clean_two_slot_month(monkeypatch,
                                                                tmp_path):
    """The guard is a backstop, not a brake: a clean 2x build is unaffected, and
    every day it wrote passes the assertion it just ran."""
    from tests.test_cadence import _FakeStore, _build

    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    store = _FakeStore(ppd=2)
    out = _build(tmp_path, store, days=3, n_media=8)
    assert out["ok"] is True and out["posts_per_day"] == 2
    assert store.inserted
    assert day_shape.assert_day_distinct(store.inserted) == []


class _FakeLassoStore:
    def __init__(self):
        self.deleted = []
        self.inserted = []

    def delete_month(self, gym_id, month):
        self.deleted.append((gym_id, month))
        return 0

    def insert_rows(self, gym_id, rows):
        self.inserted.extend(rows)
        return rows


def _lasso_apply(monkeypatch, rows):
    from agent import real_month_planner as rmp

    monkeypatch.setattr(rmp, "to_calendar_rows", lambda drafts, key: list(rows))
    monkeypatch.setattr(rmp, "preserve_and_prune", None, raising=False)
    monkeypatch.setattr("agent.portal_calendar_store.preserve_and_prune",
                        lambda store, key, months, rs: (rs, []))
    store = _FakeLassoStore()
    return rmp.apply_month_plan("lasso", [object()], store), store


def test_lasso_month_lane_also_fails_the_pass_on_a_repeated_day(monkeypatch):
    """The lasso 2026-09-16 shape: two facebook feed rows, one platform caption."""
    caption = ("Leads do not die in your ads. They die in the handoffs. "
               "Ads, lead nurture, your website, one system.")
    rows = [_row(gym_id="lasso", account="facebook", post_date="2026-09-16",
                 caption=caption, image_url="a.jpg"),
            _row(gym_id="lasso", account="facebook", post_date="2026-09-16",
                 caption=caption, image_url="b.jpg")]
    out, store = _lasso_apply(monkeypatch, rows)
    assert out["ok"] is False
    assert out["reason"] == "day shape: same post twice in one day"
    assert store.inserted == [] and store.deleted == []


def test_a_day_a_coach_already_owns_cannot_trip_the_guard(monkeypatch):
    """The guard runs AFTER preserve_and_prune, on the rows actually being inserted.
    A duplicate whose twin is pruned as a human owned slot is not a duplicate, and
    failing that build would darken a gym for no reason."""
    from agent import real_month_planner as rmp

    caption = "One caption on two rows, one of which a coach already owns."
    rows = [_row(gym_id="lasso", account="facebook", post_date="2026-09-16",
                 caption=caption, image_url="a.jpg", slot_index=0),
            _row(gym_id="lasso", account="facebook", post_date="2026-09-16",
                 caption=caption, image_url="b.jpg", slot_index=1)]
    monkeypatch.setattr(rmp, "to_calendar_rows", lambda drafts, key: list(rows))
    # preserve_and_prune drops the locked twin, so only one row reaches the insert.
    monkeypatch.setattr("agent.portal_calendar_store.preserve_and_prune",
                        lambda store, key, months, rs: (rs[:1], 1))
    store = _FakeLassoStore()
    out = rmp.apply_month_plan("lasso", [object()], store)
    assert out["ok"] is True
    assert len(store.inserted) == 1


def test_lasso_month_lane_still_writes_a_clean_month(monkeypatch):
    rows = [_row(gym_id="lasso", account="facebook", post_date="2026-09-16",
                 caption="One platform, every lead.", image_url="a.jpg"),
            _row(gym_id="lasso", account="facebook", post_date="2026-09-16",
                 caption="One room, two days, your 2027 plan.", image_url="b.jpg")]
    out, store = _lasso_apply(monkeypatch, rows)
    assert out["ok"] is True
    assert len(store.inserted) == 2


# ---- the opening FORMULA cap (Tough Temple) ----------------------------------------

def test_the_existing_four_word_dedup_barely_sees_the_tough_temple_repetition():
    """AUD-202 note: this measures the CURRENT behaviour on purpose, as the proof
    that a new check was needed. openings_collide is not weakened anywhere.

    Against every earlier caption in the real production run, the four word
    compare fires on 2 of 15. The client denied 13 rows across five straight
    days (2026-09-09 to 2026-09-13). The repetition a reader
    sees is the frame, and the frame is what openings_collide cannot reach."""
    passed = [c for i, c in enumerate(TOUGH_TEMPLE_OPENINGS)
              if not openings_collide(c, TOUGH_TEMPLE_OPENINGS[:i])]
    assert len(passed) >= 13, (
        f"only {15 - len(passed)} of 15 collided in production; the fixture no "
        "longer reproduces the shape")


def test_the_formula_signature_does_see_it():
    frames = [opening_formula(c) for c in TOUGH_TEMPLE_OPENINGS]
    assert set(frames) == {"second_person"}, frames
    assert len(frames) == 15


def test_a_run_of_three_is_a_voice_and_the_fourth_must_vary():
    frames = []
    assert not formula_run_exceeded(TOUGH_TEMPLE_OPENINGS[0], frames, 3)
    frames = ["second_person"] * 2
    assert not formula_run_exceeded(TOUGH_TEMPLE_OPENINGS[3], frames, 3)
    frames = ["second_person"] * 3
    assert formula_run_exceeded(TOUGH_TEMPLE_OPENINGS[3], frames, 3)


def test_a_different_frame_always_breaks_the_run():
    frames = ["second_person"] * 6
    assert not formula_run_exceeded("We built this for people who tried everything.",
                                    frames, 3)
    assert not formula_run_exceeded("Ready to start on Monday?", frames, 3)
    assert not formula_run_exceeded("3 things nobody tells you about week one.",
                                    frames, 3)


def test_second_person_is_rationed_never_banned():
    """Direct response copy leads on "you". The cap limits a RUN, so a month may
    still be mostly second person; it just may not be fifteen in a row."""
    frames = ["second_person", "second_person", "question", "second_person"]
    assert not formula_run_exceeded(TOUGH_TEMPLE_OPENINGS[0], frames, 3)


def test_formula_families_do_not_over_collapse():
    assert opening_formula("Ready to start Monday?") == "question"
    assert opening_formula("3 spots left.") == "number"
    assert opening_formula("We opened at six.") == "first_person"
    assert opening_formula("This is week one.") == "deictic"
    # Two genuinely different openers must not be treated as one frame.
    assert (opening_formula("Karen ran her first mile.")
            != opening_formula("Monday starts the kickstart."))
    assert opening_formula("") == ""


def test_the_run_cap_is_configurable_and_can_be_disabled(monkeypatch):
    assert config.opening_formula_max_run() == 3
    monkeypatch.setenv("ECHO_OPENING_FORMULA_MAX_RUN", "5")
    assert config.opening_formula_max_run() == 5
    frames = ["second_person"] * 4
    assert not formula_run_exceeded(TOUGH_TEMPLE_OPENINGS[0], frames, 5)
    monkeypatch.setenv("ECHO_OPENING_FORMULA_MAX_RUN", "0")
    assert config.opening_formula_max_run() == 0
    assert not formula_run_exceeded(TOUGH_TEMPLE_OPENINGS[0], ["second_person"] * 9,
                                    config.opening_formula_max_run())


class _Draft:
    def __init__(self, caption, day_key):
        self.caption = caption
        self.day_key = day_key
        self.scheduled_for = None


def _formula_probe(monkeypatch, captions_by_day):
    """Drive _clean_draft_for_day against a stubbed builder keyed by day, with SB7
    off so only the banned word bar applies and the formula cap is what is measured."""
    monkeypatch.setenv("AGENT_SB7_ENABLED", "false")

    def _build_draft(account, day_key, voice, library_path, **kw):
        text = captions_by_day.get(str(day_key)[:10])
        return _Draft(text, day_key) if text else None

    monkeypatch.setattr(cmr.client_content, "build_client_draft", _build_draft)
    return lambda **kw: cmr._clean_draft_for_day(
        None, "2026-09-20", None, "", (), lambda m: None, **kw)


def test_the_formula_cap_reaches_past_the_repeat_when_a_different_frame_exists(
        monkeypatch):
    """Not a no-op: with the run already at three second person opens, the walk
    finds the neighbouring day whose frame differs and places THAT."""
    monkeypatch.setenv("ECHO_OPENING_FORMULA_CAP", "true")
    probe = _formula_probe(monkeypatch, {
        "2026-09-20": "You showed up again and that is the whole thing.",
        "2026-09-21": "You walked in and everything looked different.",
        "2026-09-22": "Ready to start on Monday? Two intro spots are open.",
    })
    draft, drop = probe(recent_formulas=("second_person",) * 3)
    assert drop is None and draft is not None
    assert opening_formula(draft.caption) == "question"
    # And it is re-homed onto the real day, never the neighbour's date.
    assert str(draft.day_key)[:10] == "2026-09-20"


def test_without_the_cap_the_repeat_is_placed_exactly_as_before(monkeypatch):
    monkeypatch.setenv("ECHO_OPENING_FORMULA_CAP", "false")
    probe = _formula_probe(monkeypatch, {
        "2026-09-20": "You showed up again and that is the whole thing.",
        "2026-09-22": "Ready to start on Monday? Two intro spots are open.",
    })
    draft, drop = probe(recent_formulas=("second_person",) * 3)
    assert drop is None
    assert opening_formula(draft.caption) == "second_person"


def test_when_nothing_varies_the_frame_the_day_is_still_filled(monkeypatch):
    """The escape valve: every approved source opens the same way, so the run
    stands and the post is placed. A repeated frame is a quality problem; an empty
    day is a client problem, and the cap never trades one for the other."""
    monkeypatch.setenv("ECHO_OPENING_FORMULA_CAP", "true")
    probe = _formula_probe(monkeypatch, {
        f"2026-09-{d:02d}": f"You showed up on day {d} and kept going."
        for d in range(20, 29)
    })
    draft, drop = probe(recent_formulas=("second_person",) * 3)
    assert drop is None and draft is not None
    assert str(draft.day_key)[:10] == "2026-09-20"


# ---- the OTHER half of the Tough Temple D: no ask, anywhere -------------------------

def _voice_with(ctas):
    from agent.voice import VoiceDoc
    return VoiceDoc(raw="We help members win.\n#GetFit", hashtags=["#GetFit"],
                    ctas=list(ctas))


def test_the_gym_ask_lane_uses_the_gyms_own_cta_never_lassos():
    from agent import ask_coverage
    voice = _voice_with(["Save this post.", "Book your free intro today."])
    picked = cmr._approved_gym_ask(voice)
    assert picked == "Book your free intro today."
    assert picked != ask_coverage.DEFAULT_ASK, "a gym never gets LASSO's B2B ask"


def test_a_gym_with_no_usable_approved_cta_is_skipped_not_given_an_invented_one():
    # "Save this post." is a real CTA but reads as no ask family, so it cannot lift
    # path_to_join and must not be used to fake coverage.
    assert cmr._approved_gym_ask(_voice_with(["Save this post."])) == ""
    assert cmr._approved_gym_ask(_voice_with([])) == ""
    # A dashed CTA is passed over per the copy rules rather than emitted.
    assert cmr._approved_gym_ask(_voice_with(["Book now - free week."])) == ""


def test_only_a_single_ask_family_cta_qualifies():
    from agent.publish_guard import ask_families
    picked = cmr._approved_gym_ask(
        _voice_with(["Book your free intro today."]))
    assert len(ask_families(picked)) == 1


def test_gym_ask_coverage_ships_off():
    assert config.gym_ask_coverage_enabled() is False


def test_armed_the_gym_month_carries_asks_where_it_carried_none(monkeypatch,
                                                                tmp_path):
    """Tough Temple scores path_to_join 0/10 with 'no ask in caption' on every
    eligible post because ask_coverage has never run on a gym. Armed, the same
    build raises ask coverage using the gym's own approved CTA."""
    from tests.test_cadence import _FakeStore, _account, _lib, _stock_clean
    from agent.publish_guard import ask_families
    from datetime import date

    voice = _voice_with(["Book your free intro today."])

    def _run(flag):
        monkeypatch.setenv("ECHO_GYM_ASK_COVERAGE", flag)
        lib_dir = tmp_path / f"lib_{flag}"
        lib_dir.mkdir()
        account = _account()
        _stock_clean(account.key)
        store = _FakeStore()
        out = cmr.build_client_month(
            account, "gritx", date.today().isoformat(), 6, voice=voice,
            library_path=_lib(lib_dir, n=8), store=store, banned_words=())
        assert out["ok"], out
        feeds = [r for r in store.inserted
                 if r["format"] == "feed" and r["account"] == "instagram"]
        assert feeds
        rate = sum(1 for r in feeds if ask_families(r["caption"] or "")) / len(feeds)
        return rate, feeds

    off_rate, off_rows = _run("false")
    on_rate, on_rows = _run("true")
    assert on_rate >= config.ask_coverage_floor() / 100.0, (
        f"armed coverage {on_rate:.0%} is below the floor")
    assert on_rate >= off_rate
    # Raising coverage may not cost quality: every enforced caption still clears
    # copy_gate (no dash, no banned construction) exactly as it did before.
    from agent import copy_gate
    for r in on_rows:
        cap = r["caption"] or ""
        assert copy_gate.violations(cap) == [], (copy_gate.violations(cap), cap)
        assert "no_ask" not in copy_gate.soft_flags(cap) or not cap.strip()


def test_the_formula_cap_never_drops_a_day(monkeypatch, tmp_path):
    """A gym whose every approved source opens the same way still gets its full
    calendar. The cap improves variety when variety exists; it may never trade a
    client's posting volume for it (that is the B8 under post, from the other end)."""
    from tests.test_cadence import _FakeStore, _build
    from agent import client_content

    monkeypatch.setenv("ECHO_OPENING_FORMULA_CAP", "true")
    real = client_content.build_client_draft

    def _always_second_person(account, day_key, voice, library_path, **kw):
        d = real(account, day_key, voice, library_path, **kw)
        if d is not None:
            d.caption = (f"You showed up on {day_key} even though it was easier to "
                         "skip, and that is the whole thing. Save this post.")
        return d

    monkeypatch.setattr(cmr.client_content, "build_client_draft",
                        _always_second_person)
    capped_dir = tmp_path / "capped"
    capped_dir.mkdir()
    store_capped = _FakeStore()
    out_capped = _build(capped_dir, store_capped, days=4, n_media=8)

    monkeypatch.setenv("ECHO_OPENING_FORMULA_CAP", "false")
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    store_plain = _FakeStore()
    out_plain = _build(plain_dir, store_plain, days=4, n_media=8)

    assert out_capped["ok"] and out_plain["ok"]
    assert out_capped["days"] == out_plain["days"], (
        "the formula cap thinned the calendar; it must never drop a day")
    assert out_capped["feeds"] == out_plain["feeds"]
