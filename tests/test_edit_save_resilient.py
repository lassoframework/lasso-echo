"""
ISSUE 2 (Dale, CrossFit ENG, round 2, 2026-08-17): saving an edit took several attempts;
the system "timed out or booted me out" before he could submit the reason.

These tests pin the resilience contract: the DURABLE caption write happens FIRST, and the
best-effort learning/brain write can NEVER turn a persisted edit into an error the client
retries against. Even if _learn_from_edit blows up, the edit returns 200 with the saved
caption; and the durable patch_caption is what determines success, not learning.

Offline: an injected fake store; learning is monkeypatched to raise/hang.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_social as ps       # noqa: E402
from agent import portal_routes as pr        # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-secret")
    monkeypatch.setenv("AGENT_SOCIAL_BILLING_DELEGATED", "true")
    yield


class _FakeStore:
    def __init__(self):
        self._rows = {"uuid-1": {"id": "uuid-1", "gym_id": "eng", "status": "pending",
                                 "caption": "old", "format": "feed",
                                 "post_date": "2026-08-18", "image_url": "https://cdn/x.jpg"}}
        self.caption_patches = []

    def get_row(self, account_key, row_id):
        r = self._rows.get(row_id)
        if r is None or r.get("gym_id") != account_key:
            return None
        return dict(r)

    def patch_caption(self, account_key, row_id, new_caption):
        self.caption_patches.append((row_id, new_caption))
        r = self._rows.get(row_id)
        r["caption"] = new_caption
        r["status"] = "pending"
        return dict(r)


NEW = "A clean new caption."


def test_edit_persists_even_when_learning_raises(monkeypatch):
    store = _FakeStore()

    def _boom(*a, **k):
        raise RuntimeError("brain volume stalled")

    monkeypatch.setattr(ps, "_learn_from_edit", _boom)
    status, body = ps.handle_edit("eng", "uuid-1", "U1", note=NEW, sb_store=store,
                                  reason="why")
    # the caption is DURABLY written and the save reports success despite learning failing
    assert status == 200 and body["ok"] is True
    assert body["caption"] == NEW
    assert store.caption_patches == [("uuid-1", NEW)]
    assert store._rows["uuid-1"]["caption"] == NEW


def test_edit_durable_write_precedes_learning(monkeypatch):
    """Learning only runs AFTER the caption has already persisted (durable-first)."""
    store = _FakeStore()
    order = []

    orig_patch = store.patch_caption

    def _tracked_patch(ak, rid, cap):
        order.append("patch")
        return orig_patch(ak, rid, cap)

    store.patch_caption = _tracked_patch
    monkeypatch.setattr(ps, "_learn_from_edit",
                        lambda *a, **k: order.append("learn"))
    ps.handle_edit("eng", "uuid-1", "U1", note=NEW, sb_store=store)
    assert order == ["patch", "learn"]


def test_legacy_edit_persists_even_when_learning_raises(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(pr._pcs, "SupabaseCalendarStore", lambda *a, **k: store)

    import agent.portal_social as _ps

    def _boom(*a, **k):
        raise RuntimeError("brain stalled")

    monkeypatch.setattr(_ps, "_learn_from_edit", _boom)
    status, body = pr.handle_portal_action("edit", "eng", "uuid-1", "U1",
                                           note=NEW, reason="why")
    assert status == 200 and body["ok"] is True
    assert body["caption"] == NEW
    assert store._rows["uuid-1"]["caption"] == NEW
