"""gym_media_drive §4/§8 MEDIA-NOT-READY FLIP (audit CRITICAL #2).

The builder stamps content_calendar.source_media_asset_id when it stages a Drive
asset. Two paths flip a PENDING row that uses an asset back to needs_media:
  * the portal HIDE action  -> gym_media_routes._flip_pending_using_asset
  * the removed-from-Drive sweep -> jobs.sync_gym_media._flip_pending_for_missing
Both PATCH content_calendar filtering source_media_asset_id=eq.<id> and set
media_not_ready_reason. These tests exercise the REAL flip functions (not the stubs
the other suites use) with a fake `requests`, proving the PATCH targets the columns
the content_calendar_media_not_ready migration adds and sets the right reason.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gym_media_index as _idx  # noqa: E402


class _FakeResp:
    def __init__(self, status=204):
        self.status_code = status


class _FakeRequests:
    """Captures the single PATCH the flip issues so the test can assert its shape."""

    def __init__(self):
        self.calls = []

    def patch(self, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "json": json})
        return _FakeResp(204)


@pytest.fixture
def _sb(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
    fake = _FakeRequests()
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(patch=fake.patch))
    return fake


# ---- HIDE flips a pending row back to needs_media ---------------------------------
def test_hide_flips_pending_row(_sb, monkeypatch):
    from agent import gym_media_routes as gm
    gm._flip_pending_using_asset("gritx", "drivepic1")
    assert len(_sb.calls) == 1
    call = _sb.calls[0]
    assert call["url"].endswith("/rest/v1/content_calendar")
    # Filters the gym's PENDING row that references THIS drive asset id.
    assert call["params"]["gym_id"] == "eq.gritx"
    assert call["params"]["status"] == "eq.pending"
    assert call["params"]["source_media_asset_id"] == "eq.drivepic1"
    # Flips it to needs_media with the media_hidden reason.
    assert call["json"]["status"] == "needs_media"
    assert call["json"]["media_not_ready_reason"] == _idx.REJECT_HIDDEN


# ---- removed-from-Drive flips a pending row back to needs_media --------------------
def test_removed_from_drive_flips_pending_row(_sb):
    from agent.jobs import sync_gym_media as sgm
    flipped = sgm._flip_pending_for_missing("gritx", ["p_gone"], lambda m: None)
    assert flipped == 1
    assert len(_sb.calls) == 1
    call = _sb.calls[0]
    assert call["url"].endswith("/rest/v1/content_calendar")
    assert call["params"]["gym_id"] == "eq.gritx"
    assert call["params"]["status"] == "eq.pending"
    assert call["params"]["source_media_asset_id"] == "eq.p_gone"
    assert call["json"]["status"] == "needs_media"
    assert call["json"]["media_not_ready_reason"] == _idx.REJECT_REMOVED


# ---- no creds -> no-op (never a crash, never a stray PATCH) ------------------------
def test_flip_noop_without_creds(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    called = []
    monkeypatch.setitem(
        sys.modules, "requests",
        types.SimpleNamespace(patch=lambda *a, **k: called.append(1)))
    from agent import gym_media_routes as gm
    from agent.jobs import sync_gym_media as sgm
    gm._flip_pending_using_asset("gritx", "x")
    assert sgm._flip_pending_for_missing("gritx", ["y"], lambda m: None) == 0
    assert called == []


# ---- the builder stamps source_media_asset_id, closing the loop -------------------
def test_builder_stamps_asset_id_end_to_end(monkeypatch, tmp_path):
    """A row staged by the builder carries the exact asset id the flip filters on, so
    hiding/removing that asset actually finds the row (the loop closes)."""
    from agent import gym_media_builder as builder
    from agent import real_calendar_mirror as mirror
    from tests.gym_media_fakes import FakeMediaStore, FakeDrive, make_asset

    monkeypatch.setattr("agent.vision.analyze_and_store",
                        lambda path, gym=None: {"version": 2,
                                                "quality": {"usable": True},
                                                "safety_flags": [],
                                                "one_line": "a class"})
    monkeypatch.setattr("agent.vision.auto_plannable", lambda a: (True, []))
    monkeypatch.setattr("agent.vision.crop_verify",
                        lambda b, a, **k: {"ok": True, "bucket": "small_group",
                                           "verified_details": []})
    monkeypatch.setattr("agent.client_content.make_caption",
                        lambda *a, **k: ("A grounded caption", []))
    monkeypatch.setattr("agent.media_host.host_media",
                        lambda path, gym: "https://cdn.fake/served.jpg")

    class _Acct:
        key = "gritx_ig"
        platform = "instagram"

    store = FakeMediaStore(assets=[make_asset("A1", gym_id="gritx", kind="photo")])
    drive = FakeDrive(blobs={"A1": b"jpg"})
    draft = builder.build_gym_media_draft(
        _Acct(), "2026-08-05", "faces", voice=object(), source=object(),
        store=store, drive=drive, library_dir=str(tmp_path))
    assert draft is not None
    assert draft.source_media_asset_id == "A1"
    # And the row mapper carries it onto the content_calendar row the store inserts.
    row = mirror._real_row("gritx", draft)
    assert row["source_media_asset_id"] == "A1"
