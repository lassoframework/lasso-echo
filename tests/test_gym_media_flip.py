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
    def __init__(self, status=204, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self):
        return self._payload


class _FakeRequests:
    """Captures the single PATCH the flip issues so the test can assert its shape.
    `status` is settable: the original fake always returned 204, which is exactly why
    nobody noticed Postgres was rejecting every one of these PATCHes with a 400."""

    def __init__(self, status=200, payload=None):
        self.calls = []
        self.status = status
        self.payload = payload if payload is not None else [{"id": "row1"}]

    def patch(self, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "json": json})
        return _FakeResp(self.status, self.payload, text="constraint violation")


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
    flipped, err = gm._flip_pending_using_asset("gritx", "drivepic1")
    assert len(_sb.calls) == 1
    call = _sb.calls[0]
    assert call["url"].endswith("/rest/v1/content_calendar")
    # Filters the gym's PENDING row that references THIS drive asset id.
    assert call["params"]["gym_id"] == "eq.gritx"
    assert call["params"]["status"] == "eq.pending"
    assert call["params"]["source_media_asset_id"] == "eq.drivepic1"
    # DENIED, not 'needs_media': that value is not in the content_calendar status
    # CHECK constraint, so every one of these PATCHes was rejected 400 and hiding a
    # photo silently did nothing. 'denied' is real AND is what deny-backfill watches.
    assert call["json"]["status"] == "denied"
    assert call["json"]["reject_reason"] == _idx.REJECT_HIDDEN
    assert call["json"]["media_not_ready_reason"] == _idx.REJECT_HIDDEN
    assert flipped == 1 and err is None


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
    assert call["json"]["status"] == "denied"
    assert call["json"]["reject_reason"] == _idx.REJECT_REMOVED
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
    assert gm._flip_pending_using_asset("gritx", "x") == (0, "the calendar store is not configured")
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
                        lambda path, gym=None, alert=None: {"version": 2,
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


# ---- a REJECTED flip must be reported, never reported as success ------------------
def test_hide_flip_reports_a_rejected_patch(monkeypatch):
    """The bug this whole file missed for months: Postgres rejected the PATCH with a
    400 (status 'needs_media' is not in the CHECK constraint), the response was never
    inspected, and the client was told the photo was hidden while the post stayed
    scheduled. A 4xx must now surface as an error."""
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
    fake = _FakeRequests(status=400)
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(patch=fake.patch))
    from agent import gym_media_routes as gm
    flipped, err = gm._flip_pending_using_asset("gritx", "drivepic1")
    assert flipped == 0
    assert err and "400" in err


def test_hide_route_tells_the_client_when_the_post_could_not_be_pulled(monkeypatch):
    """The route must NOT answer 200 ok when a scheduled post still uses the photo."""
    from agent import gym_media_routes as gm

    class _Store:
        def available(self):
            return True

        def get_asset(self, aid):
            return {"id": aid, "gym_id": "gritx"}

        def update_asset(self, aid, patch):
            return True

    monkeypatch.setattr(gm, "_armed", lambda k: True)
    monkeypatch.setattr(gm, "_flip_pending_using_asset",
                        lambda g, a: (0, "calendar store returned 400"))
    monkeypatch.setattr(gm._sel, "rollback_asset", lambda a, store=None: True)
    status, body = gm.handle_hide_asset("gritx", "a1", hide=True, store=_Store())
    assert status == 502
    assert body.get("hidden") is True and body.get("pulled") == 0
    assert "still using it" in body.get("error", "")


def test_removed_from_drive_logs_a_rejected_patch(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
    fake = _FakeRequests(status=400)
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(patch=fake.patch))
    from agent.jobs import sync_gym_media as sgm
    logs = []
    assert sgm._flip_pending_for_missing("gritx", ["p_gone"], logs.append) == 0
    assert any("REJECTED 400" in m for m in logs), "a 4xx flip must be logged, not silent"
