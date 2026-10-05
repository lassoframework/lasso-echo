"""
Bounded forward-only logical_post_id in the real-month planner lane
(agent/real_month_planner.py), all offline.

Contract under test:
- Each NEW logical content post's IG feed row, FB mirror clone and genuinely paired
  Story row share ONE logical_post_id UUID.
- The story pairing is carried ONLY across the explicit construction relation
  (story_builder(target, day_key, feed_draft)); membership is never inferred from
  date, photo URL/basename or caption.
- An unrelated post on the same date with the same photo gets a DIFFERENT UUID.
- An existing valid logical_post_id on a draft (retry of the same draft) is preserved.
- A forged/mismatched pair (story carrying a DIFFERENT valid id than its source
  feed) or any present-but-invalid logical_post_id is a FATAL planning error:
  LogicalPostIdError aborts the whole build before apply_month_plan can delete
  existing calendar rows (P1 safety, 2026-10-04). No pair is invented and no
  asserted identity is repaired.
- A new feed draft whose logical_post_id cannot be minted/assigned aborts the lane:
  the feed is never staged, and its paired story has no source to pair to.
- A draft with no logical_post_id emits a row WITHOUT the key (no backfill, no
  unknown column for pre-migration inserts).
"""

import os
import sys
import uuid as _uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from agent import real_month_planner as rmp  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402

ACCT = "testgym"          # not 'lasso': avoids the editorial caption/scheduled_at lanes
DAY = "2026-08-03"        # a Monday; category is irrelevant beyond having a builder
PHOTO = "https://cdn.example/same-photo.jpg"


def _draft(draft_id, *, day_key=DAY, caption="real caption", url=PHOTO,
           is_story=False, draft_type="feed"):
    return Draft(
        draft_id=draft_id, account_key=ACCT, platform="instagram", caption=caption,
        hashtags=[], creative_path="x.png", creative_public_url=url,
        scheduled_for="", status=DraftStatus.PENDING, is_story=is_story,
        day_key=day_key, draft_type=draft_type, category="platform")


def _is_uuid(value):
    try:
        return str(_uuid.UUID(str(value))) == str(value).lower()
    except (ValueError, AttributeError, TypeError):
        return False


def _plan(*slots):
    return list(slots)


def _feed_slot(cadence_slot=None):
    return rmp.PlanSlot(post_date=DAY, category="platform", fmt="feed",
                        cadence_slot=cadence_slot)


def _story_slot(cadence_slot=None):
    return rmp.PlanSlot(post_date=DAY, category="platform", fmt="story",
                        cadence_slot=cadence_slot)


def _builders(feed):
    return {"platform": lambda _t, _d: feed}


def _story_builder(story):
    return lambda _t, _d, _feed: story


# ---- explicit pair shares one UUID ----------------------------------------

def test_explicit_feed_story_pair_shares_one_uuid():
    feed = _draft("f1")
    story = _draft("s1", is_story=True, draft_type="story", caption="")
    drafts = rmp.build_month_drafts(
        _plan(_feed_slot(), _story_slot()), _builders(feed),
        story_builder=_story_builder(story), account=ACCT, logger=lambda m: None)
    assert len(drafts) == 2
    feed_id = getattr(feed, "logical_post_id", "")
    assert _is_uuid(feed_id), "a new feed draft must be minted a valid UUID"
    assert getattr(story, "logical_post_id", "") == feed_id


def test_fb_mirror_clone_shares_the_feed_uuid():
    feed = _draft("f1")
    story = _draft("s1", is_story=True, draft_type="story", caption="")
    drafts = rmp.build_month_drafts(
        _plan(_feed_slot(), _story_slot()), _builders(feed),
        story_builder=_story_builder(story), account=ACCT, logger=lambda m: None)
    rows = rmp.to_calendar_rows(drafts, ACCT)
    ig = [r for r in rows if r["account"] == "instagram" and r["format"] == "feed"]
    fb = [r for r in rows if r["account"] == "facebook" and r["format"] == "feed"]
    st = [r for r in rows if r["format"] == "story"]
    assert len(ig) == len(fb) == len(st) == 1
    shared = ig[0]["logical_post_id"]
    assert _is_uuid(shared)
    assert fb[0]["logical_post_id"] == shared
    assert st[0]["logical_post_id"] == shared


def test_lasso_fb_story_mirrors_reviewed_ig_pair():
    feed = _draft("lasso-feed")
    story = _draft("lasso-story", is_story=True, draft_type="story", caption="")
    drafts = rmp.build_month_drafts(
        _plan(_feed_slot(0), _story_slot(0)), _builders(feed),
        story_builder=_story_builder(story), account="lasso", logger=lambda m: None)
    rows = rmp.to_calendar_rows(drafts, "lasso")
    assert {(r["account"], r["format"]) for r in rows} == {
        ("instagram", "feed"), ("instagram", "story"),
        ("facebook", "feed"), ("facebook", "story")}
    assert len({r["logical_post_id"] for r in rows}) == 1
    ig_story = next(r for r in rows if r["account"] == "instagram" and r["format"] == "story")
    fb_story = next(r for r in rows if r["account"] == "facebook" and r["format"] == "story")
    assert fb_story["image_url"] == ig_story["image_url"]
    assert fb_story["slot_index"] == ig_story["slot_index"] == 0


# ---- independent same-day same-photo post gets a different UUID ------------

def test_same_day_same_photo_independent_posts_get_different_uuids():
    # Two genuinely independent posts: same date, same photo URL, different captions
    # (a 2x cadence AM/PM pair of unrelated content). Neither identity nor pairing
    # may be inferred from date/photo/caption.
    feed_am = _draft("f-am", caption="morning post")
    feed_pm = _draft("f-pm", caption="evening post")
    calls = iter([feed_am, feed_pm])
    builders = {"platform": lambda _t, _d: next(calls)}
    drafts = rmp.build_month_drafts(
        _plan(_feed_slot(cadence_slot=0), _feed_slot(cadence_slot=1)),
        builders, account=ACCT, logger=lambda m: None)
    assert len(drafts) == 2
    id_am = getattr(feed_am, "logical_post_id", "")
    id_pm = getattr(feed_pm, "logical_post_id", "")
    assert _is_uuid(id_am) and _is_uuid(id_pm)
    assert id_am != id_pm
    rows = rmp.to_calendar_rows(drafts, ACCT)
    ids = {r["logical_post_id"] for r in rows if r["account"] == "instagram"}
    assert ids == {id_am, id_pm}


# ---- no guessed pairing ----------------------------------------------------

def test_no_pairing_inferred_for_bare_drafts_sharing_date_photo_caption():
    # Hand to_calendar_rows a feed and a story that merely share date/photo/caption,
    # neither carrying a logical_post_id: no identity may be invented or guessed.
    feed = _draft("f1")
    story = _draft("s1", is_story=True, draft_type="story",
                   caption=feed.caption)
    rows = rmp.to_calendar_rows([feed, story], ACCT)
    assert all("logical_post_id" not in r for r in rows)


def test_ambiguous_story_identity_is_a_fatal_planning_error():
    # P1 safety (2026-10-04): a story whose builder stamped a DIFFERENT valid id
    # than its source feed is a forged/ambiguous pairing -- a FATAL planning error.
    # build_month_drafts raises LogicalPostIdError so the whole plan aborts BEFORE
    # apply_month_plan can delete existing pending rows; no pair is invented and no
    # identity is repaired.
    feed = _draft("f1")
    story = _draft("s1", is_story=True, draft_type="story", caption="")
    story_own = str(_uuid.uuid4())
    story.logical_post_id = story_own   # builder stamped a DIFFERENT valid id
    with pytest.raises(rmp.LogicalPostIdError):
        rmp.build_month_drafts(
            _plan(_feed_slot(), _story_slot()), _builders(feed),
            story_builder=_story_builder(story), account=ACCT,
            logger=lambda m: None)
    assert getattr(story, "logical_post_id", "") == story_own, \
        "a forged identity must never be overwritten"
    assert getattr(feed, "logical_post_id", "") != story_own


def test_feed_with_unkeyable_draft_aborts_plan(monkeypatch):
    # A mint failure aborts the full plan so a later calendar apply cannot
    # replace existing pending rows with a reduced draft list.
    class _BoomUuid:
        UUID = _uuid.UUID

        @staticmethod
        def uuid4():
            raise RuntimeError("forced mint failure")

    monkeypatch.setattr(rmp, "_uuid", _BoomUuid)
    feed = _draft("f1")
    story = _draft("s1", is_story=True, draft_type="story", caption="")
    with pytest.raises(rmp.LogicalPostIdError, match="could not mint or assign"):
        rmp.build_month_drafts(
            _plan(_feed_slot(), _story_slot()), _builders(feed),
            story_builder=_story_builder(story), account=ACCT,
            logger=lambda m: None)
    assert getattr(feed, "logical_post_id", "") in ("", None)


def test_story_that_cannot_accept_the_paired_id_aborts_plan():
    # A paired story that rejects its source feed's id aborts the plan before
    # a reduced feed-only month can replace an existing story.
    class _NonAssignableStory:
        def __init__(self):
            object.__setattr__(self, "is_story", True)
            object.__setattr__(self, "draft_type", "story")
            object.__setattr__(self, "caption", "")
            object.__setattr__(self, "day_key", DAY)

        def __setattr__(self, name, value):
            if name == "logical_post_id":
                raise AttributeError("logical_post_id is read-only here")
            object.__setattr__(self, name, value)

    feed = _draft("f1")
    story = _NonAssignableStory()
    with pytest.raises(rmp.LogicalPostIdError, match="cannot carry"):
        rmp.build_month_drafts(
            _plan(_feed_slot(), _story_slot()), _builders(feed),
            story_builder=lambda _t, _d, _f: story, account=ACCT,
            logger=lambda m: None)


def test_malformed_story_id_is_fatal_and_is_never_overwritten():
    # P1 safety (2026-10-04): a story arriving with a MALFORMED nonempty
    # logical_post_id asserts an identity. The lane must NOT overwrite it with the
    # feed's id (no identity repair) and must NOT silently skip: it raises
    # LogicalPostIdError, aborting the whole plan before any store mutation.
    feed = _draft("f1")
    story = _draft("s1", is_story=True, draft_type="story", caption="")
    story.logical_post_id = "not-a-uuid"
    with pytest.raises(rmp.LogicalPostIdError):
        rmp.build_month_drafts(
            _plan(_feed_slot(), _story_slot()), _builders(feed),
            story_builder=_story_builder(story), account=ACCT,
            logger=lambda m: None)
    assert getattr(story, "logical_post_id", "") == "not-a-uuid", \
        "a malformed asserted identity must never be overwritten"


def test_malformed_feed_id_is_fatal_and_is_never_reminted():
    # P1 safety (2026-10-04): a feed draft arriving with a MALFORMED nonempty
    # logical_post_id raises LogicalPostIdError -- the plan aborts before apply,
    # the draft is never staged, and the asserted identity is never reminted over.
    feed = _draft("f1")
    feed.logical_post_id = "corrupt-not-a-uuid"
    story = _draft("s1", is_story=True, draft_type="story", caption="")
    with pytest.raises(rmp.LogicalPostIdError):
        rmp.build_month_drafts(
            _plan(_feed_slot(), _story_slot()), _builders(feed),
            story_builder=_story_builder(story), account=ACCT,
            logger=lambda m: None)
    assert getattr(feed, "logical_post_id", "") == "corrupt-not-a-uuid", \
        "a malformed asserted identity must never be reminted over"


def test_nonstring_feed_id_is_fatal():
    # P1 safety (2026-10-04): a nonstring nonempty value is also an asserted
    # (unusable) identity: fatal, never minted over.
    feed = _draft("f1")
    feed.logical_post_id = 12345
    with pytest.raises(rmp.LogicalPostIdError):
        rmp.build_month_drafts(
            _plan(_feed_slot()), _builders(feed), account=ACCT,
            logger=lambda m: None)
    assert getattr(feed, "logical_post_id", "") == 12345


def test_flag_off_malformed_ids_pass_through_unchanged(monkeypatch):
    # OFF (default): the identity lane never runs -- malformed pre-stamped ids are
    # neither repaired nor cause skips, exactly as before the feature.
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)
    feed = _draft("f1")
    feed.logical_post_id = "corrupt-not-a-uuid"
    story = _draft("s1", is_story=True, draft_type="story", caption="")
    story.logical_post_id = "also-not-a-uuid"
    drafts = rmp.build_month_drafts(
        _plan(_feed_slot(), _story_slot()), _builders(feed),
        story_builder=_story_builder(story), account=ACCT, logger=lambda m: None)
    assert len(drafts) == 2, "OFF must stage exactly as before"
    assert feed.logical_post_id == "corrupt-not-a-uuid"
    assert story.logical_post_id == "also-not-a-uuid"


# ---- retry preserves an existing valid id ----------------------------------

def test_existing_valid_feed_id_is_preserved_not_reminted():
    feed = _draft("f1")
    keep = str(_uuid.uuid4())
    feed.logical_post_id = keep
    story = _draft("s1", is_story=True, draft_type="story", caption="")
    drafts = rmp.build_month_drafts(
        _plan(_feed_slot(), _story_slot()), _builders(feed),
        story_builder=_story_builder(story), account=ACCT, logger=lambda m: None)
    assert feed.logical_post_id == keep
    assert story.logical_post_id == keep
    rows = rmp.to_calendar_rows(drafts, ACCT)
    assert {r["logical_post_id"] for r in rows} == {keep}


# ---- rollout flag (ECHO_LOGICAL_POST_ID_ENABLED, default OFF) --------------


@pytest.fixture(autouse=True)
def _logical_post_id_flag_on(monkeypatch):
    """Existing tests in this file exercise the ON behavior."""
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")


def test_flag_defaults_off_mints_nothing_pairs_nothing_and_stages(monkeypatch):
    # OFF: no minting, no pairing, no skip/hold caused by stamping — the feed and
    # its story stage exactly as before the identity feature, with no new key.
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)
    from agent import config
    assert config.logical_post_id_enabled() is False
    feed = _draft("f1")
    story = _draft("s1", is_story=True, draft_type="story", caption="")
    drafts = rmp.build_month_drafts(
        _plan(_feed_slot(), _story_slot()), _builders(feed),
        story_builder=_story_builder(story), account=ACCT, logger=lambda m: None)
    assert len(drafts) == 2, "OFF must not skip or hold anything for stamping"
    assert (getattr(feed, "logical_post_id", "") or "") == ""
    assert (getattr(story, "logical_post_id", "") or "") == ""
    rows = rmp.to_calendar_rows(drafts, ACCT)
    assert all("logical_post_id" not in r for r in rows)


def test_flag_off_unkeyable_draft_is_not_a_failure(monkeypatch):
    # The fail-closed mint path is an ON-only behavior: with the flag OFF a forced
    # uuid4 failure causes no skip.
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)

    class _BoomUuid:
        UUID = _uuid.UUID

        @staticmethod
        def uuid4():
            raise RuntimeError("forced mint failure")

    monkeypatch.setattr(rmp, "_uuid", _BoomUuid)
    feed = _draft("f1")
    drafts = rmp.build_month_drafts(
        _plan(_feed_slot()), _builders(feed), account=ACCT, logger=lambda m: None)
    assert len(drafts) == 1


def test_flag_off_pre_stamped_draft_row_omits_logical_post_id(monkeypatch):
    # The leak fix: to_calendar_rows must not forward a pre-stamped
    # logical_post_id into content_calendar rows while the rollout flag is OFF.
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)
    feed = _draft("f1")
    feed.logical_post_id = "11111111-2222-3333-4444-555555555555"
    story = _draft("s1", is_story=True, draft_type="story", caption="")
    story.logical_post_id = "11111111-2222-3333-4444-555555555555"
    rows = rmp.to_calendar_rows([feed, story], ACCT)
    assert rows, "rows still stage with the flag OFF"
    assert all("logical_post_id" not in r for r in rows)


def test_flag_on_pre_stamped_draft_row_keeps_logical_post_id(monkeypatch):
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")
    feed = _draft("f1")
    feed.logical_post_id = "11111111-2222-3333-4444-555555555555"
    rows = rmp.to_calendar_rows([feed], ACCT)
    assert {r["logical_post_id"] for r in rows} == {
        "11111111-2222-3333-4444-555555555555"}
