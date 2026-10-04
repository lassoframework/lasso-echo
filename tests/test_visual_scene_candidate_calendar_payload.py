"""Calendar persistence boundary for advisory visual scene candidates."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_calendar_store as pcs


class _Response:
    status_code = 201
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _HTTP:
    def __init__(self):
        self.calls = []

    def post(self, url, *, headers, json, timeout):
        self.calls.append((url, json))
        return _Response([{**row, "id": f"calendar-{index}"}
                          for index, row in enumerate(json)])


def _store(monkeypatch):
    """Isolate the persistence payload from unrelated staging belts."""
    for name in ("_media_stage_belt", "_preserve_held_slots"):
        monkeypatch.setattr(pcs, name, lambda *args: args[-1])
    monkeypatch.setattr(pcs, "_stage_belts", lambda account_key, rows: rows)
    monkeypatch.setattr(pcs, "_dedupe_slots", lambda store, account_key, rows: rows)
    monkeypatch.setattr(pcs, "_reconcile_story_media_holds",
                        lambda store, account_key, rows: (rows, []))
    monkeypatch.setattr(pcs, "_retry_story_hold_provenance", lambda *args: None)
    monkeypatch.setattr("agent.plan_horizon.belt_filter",
                        lambda account_key, rows: (rows, []))
    http = _HTTP()
    return pcs.SupabaseCalendarStore(url="https://db.example", service_key="test", http=http), http


def _row():
    return {"post_date": "2026-10-04", "status": "pending",
            "image_url": "https://media.example/post.jpg"}


def test_scene_candidate_flag_off_keeps_existing_calendar_payload(monkeypatch):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    monkeypatch.delenv("AGENT_VISUAL_SCENE_CANDIDATE", raising=False)
    store, http = _store(monkeypatch)

    inserted = store.insert_rows("gym-a", [_row()])

    assert inserted == [{**_row(), "gym_id": "gym-a", "id": "calendar-0"}]
    assert len(http.calls) == 1
    assert "scene_candidate" not in http.calls[0][1][0]


def test_armed_scene_candidate_never_reaches_calendar_payload_or_claims_registration(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setenv("AGENT_VISUAL_SCENE_CANDIDATE", "1")
    candidate = {"kind": "visual_scene_candidate", "stage": "candidate",
                 "usage_claimed": False, "counts_as_use": False}

    def prepare(store, account_key, row, **kwargs):
        return {**row, "visual_group_key": "vg_test", "byte_hash": "derived:test",
                "scene_candidate": candidate}

    monkeypatch.setattr("agent.visual_writer_prepare.prepare", prepare)
    store, http = _store(monkeypatch)

    inserted = store.insert_rows("gym-a", [_row()])

    assert len(http.calls) == 1
    posted = http.calls[0][1][0]
    assert posted["visual_group_key"] == "vg_test"
    assert posted["byte_hash"] == "derived:test"
    assert "scene_candidate" not in posted
    assert "scene_candidate" not in inserted[0]
    # The sole call is the calendar INSERT: candidate registration remains pending.
    assert http.calls[0][0].endswith("/content_calendar")
