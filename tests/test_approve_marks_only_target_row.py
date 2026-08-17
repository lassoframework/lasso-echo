"""
ISSUE 4 (Dale, CrossFit ENG, round 2, 2026-08-17): approving one day's post
auto-advanced the UI to the NEXT day and showed that next post as "Approved" even
though it was never approved (a refresh revealed it was NOT approved).

These tests PROVE the backend approve marks EXACTLY the row whose id was submitted and
never a sibling: the PATCH is filtered by BOTH id and gym_id, exactly one row flips, and
the next day's row is untouched. The false "Approved" on the next card is therefore a
PORTAL optimistic-state bug (specced in
docs/PORTAL_SPEC_disconnect_and_scheduled_time.md §5b).

Fully offline: a fake http client records every PATCH's params/body; a fake store proves
the handler layer touches only the target row.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_social as ps                 # noqa: E402
from agent.portal_calendar_store import SupabaseCalendarStore  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-secret")
    monkeypatch.setenv("AGENT_SOCIAL_BILLING_DELEGATED", "true")
    yield


# ---- store level: the PATCH is scoped to exactly the submitted id --------------

class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class _RecordingHttp:
    """Records every PATCH. Returns the one row matching id+gym_id (mimics PostgREST)."""

    def __init__(self, rows):
        self._rows = {r["id"]: dict(r) for r in rows}
        self.patches = []

    def patch(self, url, params=None, headers=None, json=None, timeout=None):
        self.patches.append({"params": dict(params or {}), "json": dict(json or {})})
        row_id = (params or {}).get("id", "").split("eq.", 1)[-1]
        gym = (params or {}).get("gym_id", "").split("eq.", 1)[-1]
        r = self._rows.get(row_id)
        if r is None or str(r.get("gym_id")) != gym:
            return _Resp(200, [])
        r.update({k: v for k, v in (json or {}).items()})
        return _Resp(200, [dict(r)])


def test_set_status_patch_targets_only_the_submitted_id():
    http = _RecordingHttp([
        {"id": "day-N", "gym_id": "eng", "status": "pending", "post_date": "2026-08-18"},
        {"id": "day-N1", "gym_id": "eng", "status": "pending", "post_date": "2026-08-19"},
    ])
    store = SupabaseCalendarStore(url="https://proj.supabase.co",
                                  service_key="k", http=http)
    updated = store.set_status("eng", "day-N", "approved")
    assert updated["id"] == "day-N"
    # exactly ONE patch, filtered by the target id AND gym_id, never the sibling
    assert len(http.patches) == 1
    assert http.patches[0]["params"]["id"] == "eq.day-N"
    assert http.patches[0]["params"]["gym_id"] == "eq.eng"
    assert http.patches[0]["json"] == {"status": "approved"}
    # the sibling day never appeared in any patch filter
    assert all(p["params"]["id"] != "eq.day-N1" for p in http.patches)


# ---- handler level: approving day N leaves day N+1 untouched --------------------

class _FakeStore:
    def __init__(self, rows):
        self._rows = {r["id"]: dict(r) for r in rows}
        self.status_writes = []

    def get_row(self, account_key, row_id):
        r = self._rows.get(row_id)
        if r is None or r.get("gym_id") != account_key:
            return None
        return dict(r)

    def set_status(self, account_key, row_id, new_status):
        self.status_writes.append((row_id, new_status))
        r = self._rows.get(row_id)
        if r is None or r.get("gym_id") != account_key:
            return None
        r["status"] = new_status
        return dict(r)


def test_handle_approve_marks_only_target_row_leaves_next_day_pending():
    store = _FakeStore([
        {"id": "day-N", "gym_id": "eng", "status": "pending", "format": "feed",
         "post_date": "2026-08-18", "caption": "a", "image_url": "https://cdn/a.jpg"},
        {"id": "day-N1", "gym_id": "eng", "status": "pending", "format": "feed",
         "post_date": "2026-08-19", "caption": "b", "image_url": "https://cdn/b.jpg"},
    ])
    status, body = ps.handle_approve("eng", "day-N", "U1", sb_store=store)
    assert status == 200 and body["ok"] is True and body["draft_id"] == "day-N"
    # exactly one status write, to day N only
    assert store.status_writes == [("day-N", "approved")]
    # the NEXT day is still pending server-side (the false "Approved" was frontend)
    assert store.get_row("eng", "day-N1")["status"] == "pending"
    assert store.get_row("eng", "day-N")["status"] == "approved"
