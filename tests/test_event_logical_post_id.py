"""Forward-only logical-post identity coverage for staged event arc posts."""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import event_calendar as ec  # noqa: E402
from agent import gym_event as ge  # noqa: E402


def _row(*, event_id="evt_one", post_date="2026-10-05", image_url="https://cdn/x.jpg"):
    return {
        "gym_id": "gritx",
        "event_id": event_id,
        "post_date": post_date,
        "account": "instagram",
        "format": "feed",
        "image_url": image_url,
        "status": "pending",
    }


def test_event_arc_posts_get_distinct_ids_even_with_same_event_date_and_image():
    first = _row()
    second = _row()

    assert ec._stamp_logical_post_ids([first, second], lambda _message: None)
    assert first["event_id"] == second["event_id"]
    assert first["post_date"] == second["post_date"]
    assert first["image_url"] == second["image_url"]
    assert first["logical_post_id"] != second["logical_post_id"]
    assert str(uuid.UUID(first["logical_post_id"])) == first["logical_post_id"]
    assert str(uuid.UUID(second["logical_post_id"])) == second["logical_post_id"]


def test_same_event_row_retry_preserves_its_existing_logical_post_id():
    row = _row()
    assert ec._stamp_logical_post_ids([row], lambda _message: None)
    logical_post_id = row["logical_post_id"]

    assert ec._stamp_logical_post_ids([row], lambda _message: None)
    assert row["logical_post_id"] == logical_post_id


def test_stage_arc_carries_each_new_event_post_id_to_the_insert_payload():
    class Store:
        def __init__(self):
            self.inserted = []

        def list_month(self, gym_id, month):
            return []

        def insert_rows(self, gym_id, rows):
            self.inserted = [dict(row) for row in rows]
            return self.inserted

    event = ge.GymEvent.from_row({
        "id": "evt_one", "gym_id": "gritx", "name": "Open House",
        "type": "open_house", "starts_on": "2026-10-05", "ends_on": "2026-10-05",
        "tz": "America/New_York", "offer_text": "Meet the coaches", "status": "scheduled",
    })
    store = Store()
    result = ec.stage_arc(store, event, [_row(), _row(post_date="2026-10-06")])

    assert result["ok"] is True and result["staged"] == 2
    assert len({row["logical_post_id"] for row in store.inserted}) == 2
    assert all(row["event_id"] == event.id for row in store.inserted)


def test_event_writer_fails_closed_for_an_invalid_existing_logical_post_id():
    row = _row()
    row["logical_post_id"] = "not-a-uuid"

    assert not ec._stamp_logical_post_ids([row], lambda _message: None)


# ---- rollout flag (ECHO_LOGICAL_POST_ID_ENABLED, default OFF) --------------

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _logical_post_id_flag_on(monkeypatch):
    """Existing tests in this file exercise the ON behavior."""
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")


def test_flag_defaults_off_and_stamps_nothing(monkeypatch):
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)
    from agent import config
    assert config.logical_post_id_enabled() is False
    first, second = _row(), _row()
    assert ec._stamp_logical_post_ids([first, second], lambda _message: None)
    assert "logical_post_id" not in first
    assert "logical_post_id" not in second


def test_flag_off_never_fails_on_a_bad_row(monkeypatch):
    # OFF means no new skip/hold/failure caused by stamping: a non-dict row that
    # would fail closed when ON passes through untouched when OFF.
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)
    assert ec._stamp_logical_post_ids(["not-a-dict"], lambda _message: None)


def test_flag_off_pre_stamped_id_never_reaches_insert_and_caller_row_unchanged(monkeypatch):
    # P1 fix: with the flag OFF a caller-supplied pre-stamped logical_post_id
    # must be stripped from the store payload (old schema has no such column)
    # WITHOUT mutating the caller-owned row.
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)

    class Store:
        def __init__(self):
            self.inserted = []

        def list_month(self, gym_id, month):
            return []

        def insert_rows(self, gym_id, rows):
            self.inserted = [dict(row) for row in rows]
            return self.inserted

    event = ge.GymEvent.from_row({
        "id": "evt_one", "gym_id": "gritx", "name": "Open House",
        "type": "open_house", "starts_on": "2026-10-05", "ends_on": "2026-10-05",
        "tz": "America/New_York", "offer_text": "Meet the coaches", "status": "scheduled",
    })
    row = _row()
    row["logical_post_id"] = "11111111-2222-3333-4444-555555555555"
    store = Store()
    result = ec.stage_arc(store, event, [row])

    assert result["ok"] is True and result["staged"] == 1
    assert all("logical_post_id" not in payload for payload in store.inserted)
    assert row["logical_post_id"] == "11111111-2222-3333-4444-555555555555"


def test_flag_on_duplicate_pre_stamped_ids_within_one_arc_abort_before_insert():
    # P1 fix: two independent arc rows must never share one valid pre-stamped ID.
    dupe = "11111111-2222-3333-4444-555555555555"
    first, second = _row(), _row(post_date="2026-10-06")
    first["logical_post_id"] = dupe
    second["logical_post_id"] = dupe

    assert not ec._stamp_logical_post_ids([first, second], lambda _message: None)

    class Store:
        def list_month(self, gym_id, month):
            return []

        def insert_rows(self, gym_id, rows):  # pragma: no cover - must not run
            raise AssertionError("insert must never be attempted")

    event = ge.GymEvent.from_row({
        "id": "evt_one", "gym_id": "gritx", "name": "Open House",
        "type": "open_house", "starts_on": "2026-10-05", "ends_on": "2026-10-05",
        "tz": "America/New_York", "offer_text": "Meet the coaches", "status": "scheduled",
    })
    result = ec.stage_arc(Store(), event, [first, second])
    assert result["ok"] is False and result["staged"] == 0
