"""
ISSUE 3 (Dale, CrossFit ENG, round 2, 2026-08-17): after refresh the edited caption
stuck, BUT the "Why" reason text was pasted directly BELOW the updated post copy — the
reason leaked INTO the caption body.

These tests PROVE the backend never concatenates the reason into the caption: the caption
is set to EXACTLY the new note, and the reason is recorded only as the edit's teaching
rule (tenant_brain). The leak is therefore a PORTAL bug (specced in
docs/PORTAL_SPEC_disconnect_and_scheduled_time.md §5a). Also proves the edit response
echoes reason_captured so the UI can confirm receipt.

Fully offline: an injected fake content_calendar store records exactly what caption bytes
were written; the brain writes to a tmp dir.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_social as ps       # noqa: E402
from agent import portal_routes as pr        # noqa: E402
from agent import tenant_brain               # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-secret")
    monkeypatch.setenv("AGENT_SOCIAL_BILLING_DELEGATED", "true")
    monkeypatch.setenv("AGENT_TENANT_BRAIN_ENABLED", "true")
    monkeypatch.setenv("AGENT_TENANT_BRAIN_DIR", str(tmp_path / "brains"))
    yield


class _FakeStore:
    def __init__(self, rows):
        self._rows = {r["id"]: dict(r) for r in rows}
        self.caption_patches = []

    def get_row(self, account_key, row_id):
        r = self._rows.get(row_id)
        if r is None or r.get("gym_id") != account_key:
            return None
        return dict(r)

    def patch_caption(self, account_key, row_id, new_caption):
        self.caption_patches.append((row_id, new_caption))
        r = self._rows.get(row_id)
        if r is None or r.get("gym_id") != account_key:
            return None
        r["caption"] = new_caption          # EXACTLY the note; nothing appended
        r["status"] = "pending"
        return dict(r)


def _row(caption="old caption"):
    return {"id": "uuid-1", "gym_id": "eng", "post_date": "2026-08-18",
            "account": "instagram", "status": "pending", "caption": caption,
            "image_url": "https://cdn/x.jpg", "format": "feed", "pillar": "youth"}


NEW_CAP = "Your kid's confidence is built here, not on a screen."
REASON = "This is a youth video, keep the caption youth focused, not adult training."


def test_portal_social_edit_caption_is_note_only_reason_not_appended():
    store = _FakeStore([_row()])
    status, body = ps.handle_edit("eng", "uuid-1", "U1", note=NEW_CAP,
                                  sb_store=store, reason=REASON)
    assert status == 200
    # the caption written to the store is EXACTLY the note, byte for byte
    assert store.caption_patches == [("uuid-1", NEW_CAP)]
    # the response caption carries the note and NEVER the reason text
    assert body["caption"] == NEW_CAP
    assert REASON not in body["caption"]
    assert "youth video" not in body["caption"]
    # receipt signal for the UI
    assert body["reason_captured"] is True
    # the reason lives ONLY as the edit's teaching rule in the brain, not the caption
    assert REASON in " ".join(tenant_brain.style_rules("eng_ig"))


def test_portal_routes_legacy_edit_caption_is_note_only(monkeypatch):
    store = _FakeStore([_row()])
    monkeypatch.setattr(pr._pcs, "SupabaseCalendarStore", lambda *a, **k: store)
    status, body = pr.handle_portal_action("edit", "eng", "uuid-1", "U1",
                                           note=NEW_CAP, reason=REASON)
    assert status == 200
    assert store.caption_patches == [("uuid-1", NEW_CAP)]
    assert body["caption"] == NEW_CAP
    assert REASON not in body["caption"]
    assert body["reason_captured"] is True


def test_edit_with_no_reason_still_clean_caption():
    store = _FakeStore([_row()])
    status, body = ps.handle_edit("eng", "uuid-1", "U1", note=NEW_CAP, sb_store=store)
    assert status == 200
    assert body["caption"] == NEW_CAP
    assert body["reason_captured"] is False
