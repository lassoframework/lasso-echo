"""Focused contract tests for atomic gym_event persistence boundaries."""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_events
from agent import gym_event_store as event_store_module
from agent.gym_event_store import GymEventStoreError, SupabaseGymEventStore


class _Response:
    def __init__(self, rows, status_code=200):
        self._rows = rows
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._rows


class _HTTP:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def patch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class _ConcurrentHTTP:
    """Stateful PostgREST double that permits a nested edit between read and PATCH."""

    def __init__(self, row):
        self.row = dict(row)
        self.before_first_patch = None
        self.patch_calls = []

    def get(self, url, **kwargs):
        return _Response([dict(self.row)])

    def patch(self, url, **kwargs):
        self.patch_calls.append(kwargs)
        hook = self.before_first_patch
        self.before_first_patch = None
        if hook is not None:
            hook()
        if not self._matches(kwargs["params"]):
            return _Response([])
        self.row = {**self.row, **kwargs["json"]}
        return _Response([dict(self.row)])

    def _matches(self, params):
        for field, expected_filter in params.items():
            if expected_filter != event_store_module._exact_filter(self.row.get(field)):
                return False
        return True


class _CalendarStore:
    def __init__(self):
        self.inserted = []

    def list_event_rows(self, gym_id, event_id):
        return [dict(row) for row in self.inserted
                if row.get("gym_id") == gym_id and row.get("event_id") == event_id]

    def list_month(self, gym_id, month):
        return [dict(row) for row in self.inserted
                if row.get("gym_id") == gym_id
                and str(row.get("post_date"))[:7] == month]

    def insert_rows(self, gym_id, rows, *, preserve_ids=False):
        stored = [dict(row, gym_id=gym_id) for row in rows]
        self.inserted.extend(stored)
        return stored


def _row(**overrides):
    row = {
        "id": "event-1", "gym_id": "pete", "status": "live",
        "name": "Fall Cohort", "starts_on": "2026-10-01",
        "ends_on": "2026-10-14", "tz": "America/New_York",
        "type": "new_offer", "offer_text": "Reserve a place", "link": "",
        "brief": "", "media_ids": [], "created_by": "owner",
        "created_at": "2026-09-01T12:00:00+00:00", "audit": [],
    }
    row.update(overrides)
    return row


def test_conditional_update_fences_identity_and_exact_status():
    stored = _row(starts_on="2026-10-03")
    http = _HTTP(_Response([stored]))
    store = SupabaseGymEventStore(
        url="https://db.test", service_key="secret", http=http)

    result = store.update_event_if_status(
        "pete", "event-1", "live", _row(), stored)

    assert result == stored
    url, call = http.calls[0]
    assert url == "https://db.test/rest/v1/gym_event"
    assert call["params"]["id"] == "eq.event-1"
    assert call["params"]["gym_id"] == "eq.pete"
    assert call["params"]["status"] == "eq.live"
    assert call["params"]["starts_on"] == "eq.2026-10-01"
    assert call["params"]["offer_text"] == "eq.Reserve a place"
    assert call["params"]["media_ids"] == "eq.[]"
    assert call["params"]["audit"] == "eq.[]"
    assert set(call["params"]) == {
        "id", "gym_id", "name", "type", "starts_on", "ends_on", "tz",
        "offer_text", "link", "brief", "media_ids", "status", "created_by",
        "created_at", "audit",
    }
    assert call["json"] == stored
    assert call["headers"]["Prefer"] == "return=representation"


def test_conditional_update_returns_none_when_status_changed():
    http = _HTTP(_Response([]))
    store = SupabaseGymEventStore(
        url="https://db.test", service_key="secret", http=http)

    assert store.update_event_if_status(
        "pete", "event-1", "live", _row(), _row()) is None


def test_conditional_update_rejects_payload_identity_mismatch_without_http():
    http = _HTTP(_Response([]))
    store = SupabaseGymEventStore(
        url="https://db.test", service_key="secret", http=http)

    with pytest.raises(GymEventStoreError):
        store.update_event_if_status(
            "pete", "event-1", "live", _row(), _row(gym_id="other"))
    assert http.calls == []


def test_real_store_snapshot_cas_allows_only_one_same_status_edit(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    monkeypatch.setattr(
        "agent.event_calendar._attach_media",
        lambda gym_id, rows, log, picker=None, host=None: (
            [dict(row, image_url="https://cdn.test/event.jpg",
                  source_media_asset_id="media-1") for row in rows], []))
    initial = _row(
        media_ids=["media-1"], starts_on="2026-10-01", ends_on="2026-10-14")
    http = _ConcurrentHTTP(initial)
    events = SupabaseGymEventStore(
        url="https://db.test", service_key="secret", http=http)
    calendar = _CalendarStore()
    winner = {}

    def _winning_edit():
        winner["status"], winner["response"] = portal_events.handle_edit_event(
            "pete", "event-1",
            {"starts_on": "2026-11-01", "ends_on": "2026-11-14",
             "actor_id": "winning-editor"},
            store=calendar, event_store=events, today=date(2026, 9, 1))

    http.before_first_patch = _winning_edit
    losing_status, losing_response = portal_events.handle_edit_event(
        "pete", "event-1",
        {"starts_on": "2026-12-01", "ends_on": "2026-12-14",
         "actor_id": "stale-editor"},
        store=calendar, event_store=events, today=date(2026, 9, 1))

    assert winner["status"] == 200
    assert losing_status == 409
    assert losing_response == {"error": "this promotion can no longer be edited"}
    assert http.row["starts_on"] == "2026-11-01"
    assert http.row["ends_on"] == "2026-11-14"
    assert [entry["actor"] for entry in http.row["audit"]] == ["winning-editor"]
    assert len(http.patch_calls) == 2
    assert http.patch_calls[0]["params"]["audit"] == "eq.[]"
    assert http.patch_calls[1]["params"]["audit"] == "eq.[]"
    assert calendar.inserted
    assert len(calendar.inserted) == winner["response"]["restaged"]
    assert all(row["event_id"] == "event-1" for row in calendar.inserted)
