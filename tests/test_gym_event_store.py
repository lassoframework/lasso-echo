"""Focused contract tests for atomic gym_event persistence boundaries."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

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


def _row(**overrides):
    row = {
        "id": "event-1", "gym_id": "pete", "status": "live",
        "name": "Fall Cohort", "starts_on": "2026-10-01",
        "ends_on": "2026-10-14",
    }
    row.update(overrides)
    return row


def test_conditional_update_fences_identity_and_exact_status():
    stored = _row(starts_on="2026-10-03")
    http = _HTTP(_Response([stored]))
    store = SupabaseGymEventStore(
        url="https://db.test", service_key="secret", http=http)

    result = store.update_event_if_status(
        "pete", "event-1", "live", stored)

    assert result == stored
    url, call = http.calls[0]
    assert url == "https://db.test/rest/v1/gym_event"
    assert call["params"] == {
        "id": "eq.event-1", "gym_id": "eq.pete", "status": "eq.live",
    }
    assert call["json"] == stored
    assert call["headers"]["Prefer"] == "return=representation"


def test_conditional_update_returns_none_when_status_changed():
    http = _HTTP(_Response([]))
    store = SupabaseGymEventStore(
        url="https://db.test", service_key="secret", http=http)

    assert store.update_event_if_status(
        "pete", "event-1", "live", _row()) is None


def test_conditional_update_rejects_payload_identity_mismatch_without_http():
    http = _HTTP(_Response([]))
    store = SupabaseGymEventStore(
        url="https://db.test", service_key="secret", http=http)

    with pytest.raises(GymEventStoreError):
        store.update_event_if_status(
            "pete", "event-1", "live", _row(gym_id="other"))
    assert http.calls == []
