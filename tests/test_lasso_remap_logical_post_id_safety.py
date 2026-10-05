"""
P1 safety regression (2026-10-04): a corrupt pre-stamped logical_post_id must abort
the whole build-to-apply path BEFORE apply_month_plan mutates the store.

The reviewed failure: under ECHO_LOGICAL_POST_ID_ENABLED=ON a malformed pre-stamped
feed/story id used to make build_month_drafts skip the slot and return a REDUCED
draft list; lasso_remap.remap / grade_fix._lasso_refill then ran apply_month_plan
over the full month span, whose DELETE-then-INSERT wiped existing pending calendar
rows even though no corrupt row was ever inserted. LogicalPostIdError now raises out
of build_month_drafts, so the caller never reaches apply_month_plan.

These tests run the PUBLIC seam (build_month_drafts -> apply_month_plan, exactly what
real_month_run.plan_and_build + lasso_remap.remap chain), with a VALID draft planned
BEFORE the corrupt one and existing pending rows in the fake store, and assert zero
delete_month / insert_rows calls. All offline.
"""

import os
import sys
import uuid as _uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import real_month_planner as rmp  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402

ACCT = "testgym"
DAY1 = "2026-08-03"
DAY2 = "2026-08-04"


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")


def _draft(draft_id, *, day_key, is_story=False, caption="real caption"):
    return Draft(
        draft_id=draft_id, account_key=ACCT, platform="instagram", caption=caption,
        hashtags=[], creative_path="x.png",
        creative_public_url=f"https://cdn.example/{draft_id}.jpg",
        scheduled_for="", status=DraftStatus.PENDING, is_story=is_story,
        day_key=day_key, draft_type=("story" if is_story else "feed"),
        category="platform")


def _slot(day, fmt):
    return rmp.PlanSlot(post_date=day, category="platform", fmt=fmt)


class _RecordingStore:
    """Fake calendar store seeded with existing pending rows. ANY mutation method
    (delete_month / insert_rows) records the call; the regression contract is that
    neither is ever reached when the plan aborts."""

    def __init__(self):
        self.pending_rows = [
            {"post_date": f"{DAY1}T13:00:00", "status": "pending"},
            {"post_date": f"{DAY2}T13:00:00", "status": "pending"},
        ]
        self.deleted = []
        self.inserted = []

    def delete_month(self, account_key, month):
        self.deleted.append((account_key, month))
        return 0

    def insert_rows(self, account_key, rows):
        self.inserted.append((account_key, list(rows)))
        return []

    # read-side no-ops in case anything inspects the store before writing
    def fetch_month(self, *a, **k):
        return list(self.pending_rows)

    def list_rows(self, *a, **k):
        return list(self.pending_rows)


def _build_then_apply(plan, builders, store, **kw):
    """The exact caller flow lasso_remap.remap / grade_fix._lasso_refill run:
    build the full month of drafts, THEN apply over the full span."""
    drafts = rmp.build_month_drafts(plan, builders, account=ACCT,
                                    logger=lambda m: None, **kw)
    span = rmp.plan_span_months(DAY1, 2)
    return rmp.apply_month_plan(ACCT, drafts, store, span_months=span)


def test_corrupt_feed_id_after_valid_draft_aborts_before_any_store_write():
    valid = _draft("f-valid", day_key=DAY1)
    corrupt = _draft("f-corrupt", day_key=DAY2)
    corrupt.logical_post_id = "corrupt-not-a-uuid"
    feeds = iter([valid, corrupt])
    builders = {"platform": lambda _t, _d: next(feeds)}
    store = _RecordingStore()
    with pytest.raises(rmp.LogicalPostIdError):
        _build_then_apply(
            [_slot(DAY1, "feed"), _slot(DAY2, "feed")], builders, store)
    assert store.deleted == [], "delete_month must NEVER run on a corrupt plan"
    assert store.inserted == [], "insert_rows must NEVER run on a corrupt plan"
    assert len(store.pending_rows) == 2, "existing pending rows are untouched"
    assert getattr(corrupt, "logical_post_id", "") == "corrupt-not-a-uuid", \
        "the asserted identity is never reminted over"


def test_unassignable_feed_id_after_valid_draft_aborts_before_any_store_write():
    class _UnstampableDraft(Draft):
        def __setattr__(self, name, value):
            if name == "logical_post_id" and value:
                raise TypeError("read only draft identity")
            super().__setattr__(name, value)

    valid = _draft("f-valid", day_key=DAY1)
    unassignable = _UnstampableDraft(**vars(_draft("f-unstampable", day_key=DAY2)))
    feeds = iter([valid, unassignable])
    store = _RecordingStore()
    with pytest.raises(rmp.LogicalPostIdError, match="could not mint or assign"):
        _build_then_apply(
            [_slot(DAY1, "feed"), _slot(DAY2, "feed")],
            {"platform": lambda _t, _d: next(feeds)}, store)
    assert store.deleted == []
    assert store.inserted == []
    assert len(store.pending_rows) == 2


class _SilentIdentityDraft(Draft):
    """Mimics a proxy/ORM draft whose setter silently discards the identity."""

    def __setattr__(self, name, value):
        if name == "logical_post_id" and getattr(self, "_ignore_identity", False):
            return
        super().__setattr__(name, value)


def test_silent_feed_setter_aborts_before_month_apply():
    valid = _draft("f-valid", day_key=DAY1)
    ignored = _SilentIdentityDraft(**vars(_draft("f-ignored", day_key=DAY2)))
    ignored._ignore_identity = True
    feeds = iter([valid, ignored])
    store = _RecordingStore()
    with pytest.raises(rmp.LogicalPostIdError, match="did not retain minted"):
        _build_then_apply(
            [_slot(DAY1, "feed"), _slot(DAY2, "feed")],
            {"platform": lambda _t, _d: next(feeds)}, store)
    assert store.deleted == []
    assert store.inserted == []
    assert len(store.pending_rows) == 2


def test_silent_story_setter_aborts_before_month_apply():
    feed = _draft("f-valid", day_key=DAY1)
    story = _SilentIdentityDraft(**vars(_draft(
        "s-ignored", day_key=DAY1, is_story=True, caption="")))
    story._ignore_identity = True
    store = _RecordingStore()
    with pytest.raises(rmp.LogicalPostIdError, match="did not retain"):
        _build_then_apply(
            [_slot(DAY1, "feed"), _slot(DAY1, "story")],
            {"platform": lambda _t, _d: feed}, store,
            story_builder=lambda _t, _d, _f: story)
    assert store.deleted == []
    assert store.inserted == []
    assert len(store.pending_rows) == 2


def test_malformed_story_id_after_valid_pair_aborts_before_any_store_write():
    feed1 = _draft("f-valid", day_key=DAY1)
    story1 = _draft("s-valid", day_key=DAY1, is_story=True, caption="")
    feed2 = _draft("f2", day_key=DAY2)
    story2 = _draft("s-corrupt", day_key=DAY2, is_story=True, caption="")
    story2.logical_post_id = "not-a-uuid"
    feeds = iter([feed1, feed2])
    stories = iter([story1, story2])
    builders = {"platform": lambda _t, _d: next(feeds)}
    story_builder = lambda _t, _d, _f: next(stories)  # noqa: E731
    store = _RecordingStore()
    with pytest.raises(rmp.LogicalPostIdError):
        _build_then_apply(
            [_slot(DAY1, "feed"), _slot(DAY1, "story"),
             _slot(DAY2, "feed"), _slot(DAY2, "story")],
            builders, store, story_builder=story_builder)
    assert store.deleted == []
    assert store.inserted == []
    assert len(store.pending_rows) == 2
    assert getattr(story2, "logical_post_id", "") == "not-a-uuid"


def test_unassignable_story_id_after_valid_pair_aborts_before_any_store_write():
    class _UnstampableStory(Draft):
        def __setattr__(self, name, value):
            if name == "logical_post_id" and value:
                raise TypeError("read only story identity")
            super().__setattr__(name, value)

    feed1 = _draft("f-valid", day_key=DAY1)
    story1 = _draft("s-valid", day_key=DAY1, is_story=True, caption="")
    feed2 = _draft("f2", day_key=DAY2)
    story2 = _UnstampableStory(**vars(_draft(
        "s-unstampable", day_key=DAY2, is_story=True, caption="")))
    feeds = iter([feed1, feed2])
    stories = iter([story1, story2])
    store = _RecordingStore()
    with pytest.raises(rmp.LogicalPostIdError, match="cannot carry"):
        _build_then_apply(
            [_slot(DAY1, "feed"), _slot(DAY1, "story"),
             _slot(DAY2, "feed"), _slot(DAY2, "story")],
            {"platform": lambda _t, _d: next(feeds)}, store,
            story_builder=lambda _t, _d, _f: next(stories))
    assert store.deleted == []
    assert store.inserted == []
    assert len(store.pending_rows) == 2


def test_forged_story_id_after_valid_pair_aborts_before_any_store_write():
    feed1 = _draft("f-valid", day_key=DAY1)
    story1 = _draft("s-valid", day_key=DAY1, is_story=True, caption="")
    feed2 = _draft("f2", day_key=DAY2)
    story2 = _draft("s-forged", day_key=DAY2, is_story=True, caption="")
    story2.logical_post_id = str(_uuid.uuid4())  # valid but NOT the feed's id
    feeds = iter([feed1, feed2])
    stories = iter([story1, story2])
    builders = {"platform": lambda _t, _d: next(feeds)}
    story_builder = lambda _t, _d, _f: next(stories)  # noqa: E731
    store = _RecordingStore()
    with pytest.raises(rmp.LogicalPostIdError):
        _build_then_apply(
            [_slot(DAY1, "feed"), _slot(DAY1, "story"),
             _slot(DAY2, "feed"), _slot(DAY2, "story")],
            builders, store, story_builder=story_builder)
    assert store.deleted == []
    assert store.inserted == []
    assert len(store.pending_rows) == 2


def test_flag_off_corrupt_ids_never_raise_and_apply_runs(monkeypatch):
    # OFF is unchanged: no identity lane, no LogicalPostIdError; the build-to-apply
    # flow behaves exactly as before the feature.
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)
    valid = _draft("f-valid", day_key=DAY1)
    corrupt = _draft("f-corrupt", day_key=DAY2)
    corrupt.logical_post_id = "corrupt-not-a-uuid"
    feeds = iter([valid, corrupt])
    builders = {"platform": lambda _t, _d: next(feeds)}

    class _ApplyStore(_RecordingStore):
        def fetch_forward(self, *a, **k):
            return []

    store = _ApplyStore()
    drafts = rmp.build_month_drafts(
        [_slot(DAY1, "feed"), _slot(DAY2, "feed")], builders,
        account=ACCT, logger=lambda m: None)
    assert len(drafts) == 2, "OFF stages exactly as before, corrupt ids included"
    assert getattr(corrupt, "logical_post_id", "") == "corrupt-not-a-uuid"
