"""HTTP boundary regressions for token-scoped event edits.

These drive the real intake_web POST router with a stubbed socket. The event handler
still receives injected offline stores, so the tests cover route parsing, token-to-gym
resolution, status propagation, and the terminal-status write boundary without network
or Supabase access.
"""

import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import intake_web, portal_events  # noqa: E402


def _handler():
    server = intake_web.build_server(0)
    try:
        return server.RequestHandlerClass
    finally:
        server.server_close()


class _Headers(dict):
    def get(self, key, default=None):
        for candidate, value in self.items():
            if candidate.lower() == key.lower():
                return value
        return default


def _make(path, body):
    handler = _handler()
    inst = handler.__new__(handler)
    inst.path = path
    inst.headers = _Headers({
        "Host": "portal.lassoframework.com",
        "Content-Length": str(len(body)),
    })
    inst.client_address = ("10.0.0.1", 5555)
    inst.rfile = io.BytesIO(body)
    inst.wfile = io.BytesIO()
    captured = {}
    inst._deny = lambda code=404, msg="not found": captured.update(deny=(code, msg))
    inst._send_json = lambda obj, status=200, cors_origin="": captured.update(
        json=(status, obj))
    return inst, captured


class _EventStore:
    def __init__(self, status):
        self.row = {
            "id": "event-1", "gym_id": "gritx", "name": "Fall Cohort",
            "type": "new_offer", "starts_on": "2026-10-01",
            "ends_on": "2026-10-14", "tz": "America/New_York",
            "offer_text": "Reserve a place", "link": "", "brief": "",
            "media_ids": [], "status": status, "created_by": "owner",
            "audit": [],
        }
        self.writes = 0

    def get_event(self, gym_id, event_id):
        if gym_id == self.row["gym_id"] and event_id == self.row["id"]:
            return dict(self.row)
        return None

    def upsert_event(self, row):
        self.writes += 1
        self.row = dict(row)
        return dict(self.row)

    def update_event_if_status(self, gym_id, event_id, expected_status,
                               expected_row, row):
        if (gym_id != self.row["gym_id"] or event_id != self.row["id"]
                or self.row.get("status") != expected_status
                or self.row != expected_row):
            return None
        self.writes += 1
        self.row = dict(row)
        return dict(self.row)


class _CalendarStore:
    def __init__(self):
        self.reads = 0

    def list_event_rows(self, gym_id, event_id):
        self.reads += 1
        return []


@pytest.fixture(autouse=True)
def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GRITX", "gritxtoken12345")
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_GRITX", "true")
    intake_web._token_hits.clear()


def _post_edit(monkeypatch, status):
    events = _EventStore(status)
    calendar = _CalendarStore()
    real_edit = portal_events.handle_edit_event

    def _edit(account_key, event_id, body):
        return real_edit(
            account_key, event_id, body, store=calendar, event_store=events)

    monkeypatch.setattr("agent.intake_web._pe.handle_edit_event", _edit)
    body = json.dumps({
        "action": "edit", "starts_on": "2026-11-01",
        "ends_on": "2026-11-14", "actor_id": "portal-owner",
    }).encode()
    inst, captured = _make(
        "/portal/gritxtoken12345/event/event-1/edit", body)
    inst.do_POST()
    return captured, events, calendar


@pytest.mark.parametrize("noncanonical_status", [
    "cancelled", "ended", " LIVE ", "Scheduled", "archived", "", None,
    {"value": "live"},
])
def test_http_edit_propagates_status_conflict_without_mutation(
        monkeypatch, noncanonical_status):
    captured, events, calendar = _post_edit(monkeypatch, noncanonical_status)

    assert captured["json"] == (
        409, {"error": "this promotion can no longer be edited"})
    assert events.writes == 0
    assert events.row["starts_on"] == "2026-10-01"
    assert calendar.reads == 0


def test_http_edit_keeps_live_promotions_editable(monkeypatch):
    captured, events, calendar = _post_edit(monkeypatch, "live")

    assert captured["json"][0] == 200
    assert captured["json"][1]["event"]["status"] == "live"
    assert events.writes == 1
    assert events.row["starts_on"] == "2026-11-01"
    assert calendar.reads == 1
