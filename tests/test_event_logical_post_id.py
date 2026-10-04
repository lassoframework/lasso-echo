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
