import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from agent import portal_social as ps


def _ordinary_row(**overrides):
    row = {"id": "r1", "gym_id": "gymx", "status": "pending",
           "image_url": "https://cdn.example/photo.jpg",
           "media_not_ready_reason": None}
    row.update(overrides)
    return row


@pytest.mark.parametrize("flag", [None, "AGENT_APPROVAL_CAPTURE",
                                   "AGENT_APPROVAL_PROOF"])
@pytest.mark.parametrize("identity", [
    {"creative_origin": "generated",
     "generated_artifact_version_id": "123e4567-e89b-12d3-a456-426614174000",
     "generated_artifact_sha256": "a" * 64},
    {"creative_origin": "generated"},
    {"generated_artifact_sha256": "a" * 64},
    {"creative_origin": "unknown"},
    {"source_media_asset_id": "generated-astra:job-123"},
])
@pytest.mark.parametrize("status", ["pending", "approved"])
def test_generated_identity_is_held_before_any_approval_branch(
        monkeypatch, flag, identity, status):
    monkeypatch.delenv("AGENT_APPROVAL_CAPTURE", raising=False)
    monkeypatch.delenv("AGENT_APPROVAL_PROOF", raising=False)
    if flag:
        monkeypatch.setenv(flag, "true")
    row = _ordinary_row(status=status, **identity)

    class Store:
        def __init__(self):
            self.calls = []

        def approve_ready(self, *args, **kwargs):
            self.calls.append(("approve_ready", args, kwargs))
            return {**row, "status": "approved"}

        def set_status(self, *args):
            self.calls.append(("set_status", args))
            return {**row, "status": "approved"}

        def recover_unproved_approval(self, *args):
            self.calls.append(("recover", args))
            return {"approval_digest": "unexpected"}

    store = Store()
    monkeypatch.setattr(ps, "_action_gates", lambda *a, **k: None)
    monkeypatch.setattr(ps, "_sb_load_owned_row", lambda *a, **k: (row, None))
    status_code, body = ps._handle_approve_supabase(
        "gymx", "r1", "actor", None, store)

    assert status_code == 409
    assert "generated" in body["error"]
    assert store.calls == []


@pytest.mark.parametrize("flag", [None, "AGENT_APPROVAL_CAPTURE",
                                   "AGENT_APPROVAL_PROOF"])
def test_ordinary_photo_approval_keeps_existing_behavior(monkeypatch, flag):
    monkeypatch.delenv("AGENT_APPROVAL_CAPTURE", raising=False)
    monkeypatch.delenv("AGENT_APPROVAL_PROOF", raising=False)
    if flag:
        monkeypatch.setenv(flag, "true")
    row = _ordinary_row()

    class Store:
        def __init__(self):
            self.calls = []

        def approve_ready(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {**row, "status": "approved", "approval_digest": "digest"}

    store = Store()
    monkeypatch.setattr(ps, "_action_gates", lambda *a, **k: None)
    monkeypatch.setattr(ps, "_sb_load_owned_row", lambda *a, **k: (row, None))
    expected = ({"caption": None, "media_url": row["image_url"],
                 "day_key": "2026-08-10", "format": "feed",
                 "platform": "instagram"} if flag else None)
    status_code, body = ps._handle_approve_supabase(
        "gymx", "r1", "actor", None, store,
        expected_creative=expected)

    assert status_code == 200
    assert body["approval_digest"] == "digest"
    assert len(store.calls) == 1
