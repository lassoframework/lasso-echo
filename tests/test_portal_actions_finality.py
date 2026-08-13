"""
Published/publishing rows are FINAL to portal actions (both route families):
  * published: edit/deny/kill -> 409; approve -> idempotent 200 no-op
  * publishing (mid atomic claim): every action -> 409 so a status flip can never
    make the row claimable again mid-flight (the double-post race)
  * legacy /portal/<token>/edit runs the SAME fabrication gate as the Part-B route
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_routes, portal_social as ps  # noqa: E402
from agent import portal_calendar_store as pcs  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setenv("AGENT_SOCIAL_BILLING_DELEGATED", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-secret")
    yield


class _Store:
    def __init__(self, rows):
        self._rows = {r["id"]: dict(r) for r in rows}
        self.writes = []

    def get_row(self, account_key, row_id):
        r = self._rows.get(row_id)
        if r is None or r.get("gym_id") != account_key:
            return None
        return dict(r)

    def set_status(self, account_key, row_id, new_status):
        self.writes.append(("status", row_id, new_status))
        return dict(self._rows[row_id], status=new_status)

    def patch_caption(self, account_key, row_id, new_caption):
        self.writes.append(("caption", row_id, new_caption))
        return dict(self._rows[row_id], caption=new_caption, status="pending")


def _row(row_id, status):
    return {"id": row_id, "gym_id": "eng", "post_date": "2026-08-13",
            "account": "instagram", "format": "feed", "status": status,
            "caption": "live text", "image_url": "https://r2/x.jpg"}


def _patch_store(monkeypatch, store):
    monkeypatch.setattr(pcs, "SupabaseCalendarStore", lambda *a, **k: store)
    monkeypatch.setattr(portal_routes._pcs, "SupabaseCalendarStore",
                        lambda *a, **k: store)


@pytest.mark.parametrize("action", ["edit", "deny", "kill"])
def test_legacy_actions_on_published_row_409_no_write(monkeypatch, action):
    store = _Store([_row("r1", "published")])
    _patch_store(monkeypatch, store)
    status, body = portal_routes.handle_portal_action(
        action, "eng", "r1", "actor", note="new words", confirm=True)
    assert status == 409
    assert store.writes == []


@pytest.mark.parametrize("action", ["approve", "edit", "deny", "kill"])
def test_legacy_actions_on_publishing_row_409_no_write(monkeypatch, action):
    store = _Store([_row("r1", "publishing")])
    _patch_store(monkeypatch, store)
    status, body = portal_routes.handle_portal_action(
        action, "eng", "r1", "actor", note="new words", confirm=True)
    assert status == 409, "an action mid-claim must never flip status (double-post)"
    assert store.writes == []


def test_legacy_edit_runs_fabrication_gate(monkeypatch):
    store = _Store([_row("r1", "pending")])
    _patch_store(monkeypatch, store)
    status, body = portal_routes.handle_portal_action(
        "edit", "eng", "r1", "actor",
        note="Say we cut costs by 80 percent.")
    assert status == 422
    assert "fabrication" in body["error"].lower()
    assert store.writes == [], "a refused claim must never reach the caption"


def test_legacy_edit_clean_note_writes(monkeypatch):
    store = _Store([_row("r1", "approved")])
    _patch_store(monkeypatch, store)
    status, body = portal_routes.handle_portal_action(
        "edit", "eng", "r1", "actor", note="Warmer tone please, coach")
    assert status == 200
    assert ("caption", "r1", "Warmer tone please, coach") in store.writes


def test_partb_edit_on_published_row_409(monkeypatch):
    store = _Store([_row("r1", "published")])
    status, body = ps._handle_edit_supabase(
        "eng", "r1", "actor", "new words", None, store)
    assert status == 409 and store.writes == []


def test_partb_approve_on_published_is_idempotent_200(monkeypatch):
    store = _Store([_row("r1", "published")])
    status, body = ps._handle_approve_supabase("eng", "r1", "actor", None, store)
    assert status == 200 and body.get("idempotent") is True
    assert store.writes == []


# ---- Blake ruling 2026-08-13: NO route family offers an unconfirmed kill ----

def test_legacy_kill_without_confirm_is_400_no_write(monkeypatch):
    store = _Store([_row("r1", "pending")])
    _patch_store(monkeypatch, store)
    status, body = portal_routes.handle_portal_action(
        "kill", "eng", "r1", "actor")                    # no confirm
    assert status == 400
    assert "confirm" in body["error"]
    assert store.writes == [], "an unconfirmed kill must never write"


def test_legacy_kill_with_confirm_kills(monkeypatch):
    store = _Store([_row("r1", "pending")])
    _patch_store(monkeypatch, store)
    status, body = portal_routes.handle_portal_action(
        "kill", "eng", "r1", "actor", confirm=True)
    assert status == 200
    assert ("status", "r1", "killed") in store.writes


def test_legacy_kill_confirm_gate_fires_before_any_lookup(monkeypatch):
    # the gate is route-level: even a nonexistent row 400s (never a probe-able 404)
    store = _Store([])
    _patch_store(monkeypatch, store)
    status, body = portal_routes.handle_portal_action(
        "kill", "eng", "ghost", "actor")
    assert status == 400 and "confirm" in body["error"]
