"""The Oct 2-5 2026 prepared-backlog caption hold transition (release blocker).

Four LASSO feeds hold 'prepared_backlog_waiting_for_story_and_capacity' and
need their approved caption/image applied. patch_pending_plan must move that
hold to 'caption_changed_needs_new_visual' ATOMICALLY with the exact new
caption CAS -- only for the canonical owned LASSO feed, only with the
autonomous Lasso3x flag armed. Everything else fails closed, and no other
hold is relaxed.
"""
from types import SimpleNamespace

import pytest

from agent import config
from agent.portal_calendar_store import SupabaseCalendarStore

HOLD = "prepared_backlog_waiting_for_story_and_capacity"
NEW_HOLD = "caption_changed_needs_new_visual"


@pytest.fixture(autouse=True)
def _armed_lasso_autonomous(monkeypatch):
    monkeypatch.setattr(config, "lasso_three_feed_enabled", lambda: True)


def _feed(**overrides):
    row = {
        "id": "backlog-feed-1", "gym_id": "lasso", "account": "instagram",
        "post_date": "2026-10-03", "slot_index": 0, "format": "feed",
        "status": "pending", "variant_status": "active",
        "caption": "Old approved caption", "image_url": "https://cdn.test/0.jpg",
        "source_media_url": "https://cdn.test/0.jpg",
        "source_media_asset_id": None, "thumbnail_url": None,
        "logical_post_id": None, "pillar": "doctrine",
        "scheduled_at": None, "created_at": "2026-10-01T00:00:00Z",
        "media_not_ready_reason": HOLD,
        "published_at": None, "late_post_id": None, "publish_claim_token": None,
        "publish_reservation_day": None,
    }
    row.update(overrides)
    return row


def _store(current):
    calls = []

    class HTTP:
        def patch(self, url, *, params, headers, json, timeout):
            calls.append((params, json))
            return SimpleNamespace(status_code=200,
                                   json=lambda: [dict(current, **json)])

    return SupabaseCalendarStore(url="https://example.test", service_key="test",
                                 http=HTTP()), calls


def _apply(store, current, **kwargs):
    return store.patch_pending_plan(
        "lasso", current["id"], caption="New reviewed approved caption",
        expected_row=dict(current), **kwargs)


def test_flag_off_rejects_the_transition(monkeypatch):
    monkeypatch.setattr(config, "lasso_three_feed_enabled", lambda: False)
    current = _feed()
    store, calls = _store(current)
    # Flag OFF keeps the old mechanical behavior: the foreign hold is refused,
    # no transition and no partial reservation of anything.
    assert _apply(store, current) is None
    assert calls == []
    assert current["media_not_ready_reason"] == HOLD


@pytest.mark.parametrize("override", [
    {"gym_id": "gritx"},                            # other tenant
    {"format": "story"},                            # Story, not a feed
    {"account": "googlebusiness"},                  # non canonical account
    {"post_date": "2026-10-01"},                    # before the window
    {"post_date": "2026-10-06"},                    # after / future dated
    {"slot_index": 3},                              # slot out of range
    {"status": "approved"},                         # stale snapshot status
    {"publish_claim_token": "tok"},                 # claimed row
    {"late_post_id": "18123456789012345"},          # receipt carrying row
    {"published_at": "2026-10-03T12:00:00Z"},       # publish receipt
    {"media_not_ready_reason": "some_other_hold"},  # foreign hold
])
def test_invalid_rows_are_rejected_without_any_call(override):
    current = _feed(**override)
    store, calls = _store(current)
    assert _apply(store, current) is None
    assert calls == []


def test_valid_feed_transitions_hold_atomically_with_caption_cas():
    current = _feed()
    store, calls = _store(current)
    after = _apply(store, current)
    assert after is not None
    assert after["caption"] == "New reviewed approved caption"
    assert after["media_not_ready_reason"] == NEW_HOLD
    assert len(calls) == 1
    params, payload = calls[0]
    # ONE atomic PATCH: new caption plus the transitioned hold together.
    assert payload["caption"] == "New reviewed approved caption"
    assert payload["media_not_ready_reason"] == NEW_HOLD
    # The old hold is never CLEARED by this call: the CAS pins it server-side
    # as the required pre-image and the payload replaces it (transition only).
    assert params["media_not_ready_reason"] == f"eq.{HOLD}"
    assert payload["media_not_ready_reason"] is not None
    # The exact-row CAS pins the rest of the identity too.
    assert params["status"] == "eq.pending"
    assert params["variant_status"] == "eq.active"
    assert params["post_date"] == "eq.2026-10-03"
    assert params["caption"] == "eq.Old approved caption"
    assert params["publish_claim_token"] == "is.null"


def test_existing_whitelist_holds_still_transition_and_unrelated_hold_refused():
    # Regression: the pre-existing whitelist behavior is unchanged.
    current = _feed(media_not_ready_reason="cross_date_media_repeat_needs_new_visual")
    store, calls = _store(current)
    assert _apply(store, current) is not None
    assert calls[0][1]["media_not_ready_reason"] == NEW_HOLD

    no_hold = _feed(media_not_ready_reason=None)
    store2, calls2 = _store(no_hold)
    assert _apply(store2, no_hold) is not None
    assert calls2[0][1]["media_not_ready_reason"] == NEW_HOLD

    foreign = _feed(media_not_ready_reason="prepared_backlog_waiting_for_story_and_capacity ",
                    )
    # A lookalike but non-exact hold string stays refused.
    store3, calls3 = _store(foreign)
    assert _apply(store3, foreign) is None
    assert calls3 == []
