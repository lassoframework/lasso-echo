"""
Echo half of the portal ECHO_VERIFIED_APPROVAL_PROOF_CONTRACT.md snapshot
compare (2026-10-05): when AGENT_APPROVAL_PROOF is ON, Echo's approve
endpoint REQUIRES the portal's visible-card expected_creative snapshot and
compares it atomically against the locked row (same UPDATE as status+digest).
Absent/malformed snapshot or ANY compare mismatch -> 409
review_refresh_required, no status change, no digest. Flag OFF keeps the
legacy behavior byte-for-byte (snapshot ignored, legacy wire + error).

Snapshot contract: caption (string or null, null/empty normalized the same
as display), media_url (nonempty final image URL), day_key (YYYY-MM-DD
visible post_date), format (feed/story), platform (instagram/facebook/
googlebusiness). scheduled_for/scheduled_at are NOT required.

No database, no network. The SQL compare itself lives in
migrations/calendar_approval_provenance_20261005.sql and is exercised
against a disposable Postgres by tests/sql/approval_provenance_check.sh.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config
from agent import portal_calendar_store as pcs
from agent import portal_routes


def _snapshot(**over):
    snap = {"caption": "Workout Wednesday!",
            "media_url": "https://cdn.example/x.jpg",
            "day_key": "2026-08-10",
            "format": "feed",
            "platform": "instagram"}
    snap.update(over)
    return snap


def _pending_row():
    return {"id": "r1", "gym_id": "gymx", "status": "pending",
            "image_url": "https://cdn.example/x.jpg",
            "media_not_ready_reason": None}


class _SB:
    """Offline store: approve_ready enforces the SQL compare semantics
    (caption normalized like display, final image_url, post_date, effective
    format, canonical account platform) and returns None on zero rows."""

    def __init__(self, row):
        self.row = dict(row)
        self.calls = []

    def approve_ready(self, account_key, row_id, expected_creative=None):
        self.calls.append(expected_creative)
        r = self.row
        if expected_creative is not None:
            e = expected_creative
            caption_matches = (
                (r.get("caption") or "").strip() or None
            ) == ((e.get("caption") or "").strip() or None)
            fmt = (r.get("format") or "").strip().lower() or "feed"
            ok = (caption_matches
                  and (r.get("image_url") or "").strip() == e["media_url"]
                  and str(r.get("post_date") or "") == e["day_key"]
                  and fmt == e["format"]
                  and (r.get("account") or "").strip().lower()
                      == e["platform"])
            if not ok:
                return None
        out = dict(r)
        out["status"] = "approved"
        out["approval_digest"] = "digest-abc"
        return out


def _run(monkeypatch, sb, expected_creative=None):
    import agent.portal_social as ps
    monkeypatch.setattr(ps, "_action_gates", lambda *a, **k: None)
    monkeypatch.setattr(ps, "_sb_load_owned_row",
                        lambda *a, **k: (sb.row, None))
    return ps._handle_approve_supabase(
        "gymx", "r1", "spoofed-browser-actor", None, sb,
        expected_creative=expected_creative)


def test_approved_unproved_retry_requires_atomic_recovery(monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")

    class _RetryStore:
        row = {**_pending_row(), "status": "approved"}
        calls = []

        def recover_unproved_approval(self, gym, row_id, expected):
            self.calls.append((gym, row_id, expected))
            return {"gym_id": gym, "approval_digest": "digest-abc"}

    sb = _RetryStore()
    status, body = _run(monkeypatch, sb, _snapshot())
    assert status == 200
    assert body["approval_state"] == "approved_unproved_retry"
    assert body["idempotent"] is True
    assert body["approval_digest"] == "digest-abc"
    assert sb.calls == [("gymx", "r1", _snapshot())]

    status, body = _run(monkeypatch, sb, None)
    assert status == 409 and body["error"] == "review_refresh_required"


def test_capture_only_recovers_legacy_approved_row_without_actor(monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_CAPTURE", "true")
    monkeypatch.delenv("AGENT_APPROVAL_PROOF", raising=False)
    assert config.approval_capture_enabled() is True
    assert config.approval_proof_enabled() is False

    class _LegacyStore:
        row = {**_full_row(), "status": "approved", "approval_digest": None,
               "approval_kind": None, "approved_by": None, "approved_at": None}

        def recover_unproved_approval(self, gym, row_id, expected):
            assert expected == _snapshot()
            return {**self.row, "approval_digest": "legacy-digest"}

    status, body = _run(monkeypatch, _LegacyStore(), _snapshot())
    assert status == 200
    assert body["approval_state"] == "approved_unproved_retry"
    assert body["approval_digest"] == "legacy-digest"
    assert body["idempotent"] is True


def test_capture_only_requires_snapshot_and_rejects_stale_pending_card(monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_CAPTURE", "true")
    monkeypatch.delenv("AGENT_APPROVAL_PROOF", raising=False)
    sb = _SB(_full_row())
    status, body = _run(monkeypatch, sb)
    assert status == 409 and body["error"] == "review_refresh_required"
    assert sb.calls == []

    status, body = _run(monkeypatch, sb, _snapshot(caption="stale"))
    assert status == 409 and body["error"] == "review_refresh_required"
    assert sb.calls == [_snapshot(caption="stale")]

    status, body = _run(monkeypatch, sb, _snapshot())
    assert status == 200 and body["approval_digest"] == "digest-abc"


@pytest.mark.parametrize("flag", ["AGENT_APPROVAL_CAPTURE", "AGENT_APPROVAL_PROOF"])
def test_guarded_approve_never_falls_back_to_generic_status_write(monkeypatch, flag):
    monkeypatch.delenv("AGENT_APPROVAL_CAPTURE", raising=False)
    monkeypatch.delenv("AGENT_APPROVAL_PROOF", raising=False)
    monkeypatch.setenv(flag, "true")

    class _LegacyStore:
        row = _full_row()
        writes = 0

        def set_status(self, *args):
            self.writes += 1
            return {**self.row, "status": "approved"}

    sb = _LegacyStore()
    status, body = _run(monkeypatch, sb, _snapshot())
    assert status == 409 and body["error"] == "review_refresh_required"
    assert "approval_digest" not in body
    assert sb.writes == 0


def test_capture_only_recovery_failure_does_not_return_digest(monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_CAPTURE", "true")
    monkeypatch.delenv("AGENT_APPROVAL_PROOF", raising=False)

    class _ProvedStore:
        row = {**_full_row(), "status": "approved", "approval_digest": "old",
               "approval_kind": "human", "approved_by": "clerk-user"}

        def recover_unproved_approval(self, *args):
            return None

    status, body = _run(monkeypatch, _ProvedStore(), _snapshot())
    assert status == 409 and body["error"] == "review_refresh_required"
    assert "approval_digest" not in body


def test_generic_approved_replay_cannot_mint_recovery_digest(monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")

    class _RetryStore:
        row = {**_pending_row(), "status": "approved"}

        def recover_unproved_approval(self, *args):
            return None

    status, body = _run(monkeypatch, _RetryStore(), _snapshot())
    assert status == 409 and body["error"] == "review_refresh_required"
    assert "approval_digest" not in body


# ---- flag OFF: legacy behavior preserved ------------------------------------

def test_flag_off_ignores_snapshot_and_keeps_legacy_wire(monkeypatch):
    monkeypatch.delenv("AGENT_APPROVAL_PROOF", raising=False)
    monkeypatch.delenv("AGENT_APPROVAL_CAPTURE", raising=False)
    assert config.approval_proof_enabled() is False
    assert config.approval_capture_enabled() is False
    sb = _SB(_pending_row())
    # A snapshot that WOULD mismatch is completely ignored when OFF.
    status, resp = _run(monkeypatch, sb, expected_creative=_snapshot(
        caption="a totally different caption"))
    assert status == 200
    assert resp["approval_state"] == "approved"
    # Legacy 2-arg call: no p_expected anywhere.
    assert sb.calls == [None]


def test_flag_off_missing_snapshot_still_approves(monkeypatch):
    monkeypatch.delenv("AGENT_APPROVAL_PROOF", raising=False)
    sb = _SB(_pending_row())
    status, _ = _run(monkeypatch, sb, expected_creative=None)
    assert status == 200
    assert sb.calls == [None]


def test_flag_off_legacy_error_message_preserved(monkeypatch):
    monkeypatch.delenv("AGENT_APPROVAL_PROOF", raising=False)

    class _EmptySB:
        row = _pending_row()

        def approve_ready(self, *a, **k):
            return None

    status, resp = _run(monkeypatch, _EmptySB())
    assert status == 409
    assert resp["error"] == "post changed before approval; refresh and review it again"


# ---- flag ON: snapshot required ---------------------------------------------

def test_flag_on_missing_snapshot_rejected(monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")
    sb = _SB(_pending_row())
    status, resp = _run(monkeypatch, sb, expected_creative=None)
    assert status == 409
    assert resp["error"] == "review_refresh_required"
    # Nothing was written: no status change, no digest stamp.
    assert sb.row["status"] == "pending"
    assert sb.calls == []


@pytest.mark.parametrize("bad", [
    "not-an-object", ["a", "list"], 42,
])
def test_flag_on_malformed_snapshot_rejected(monkeypatch, bad):
    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")
    sb = _SB(_pending_row())
    status, resp = _run(monkeypatch, sb, expected_creative=bad)
    assert status == 409
    assert resp["error"] == "review_refresh_required"
    assert sb.calls == []


@pytest.mark.parametrize("field,value", [
    ("media_url", ""), ("media_url", None),
    ("day_key", "08/10/2026"), ("day_key", "2026-8-1"),
    ("format", "reel"), ("format", ""),
    ("platform", "tiktok"), ("platform", ""),
])
def test_flag_on_field_validation(monkeypatch, field, value):
    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")
    sb = _SB(_pending_row())
    status, resp = _run(monkeypatch, sb,
                        expected_creative=_snapshot(**{field: value}))
    assert status == 409
    assert resp["error"] == "review_refresh_required"
    assert "detail" in resp
    assert sb.calls == []


def test_flag_on_nonstring_caption_rejected(monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")
    sb = _SB(_pending_row())
    status, resp = _run(monkeypatch, sb,
                        expected_creative=_snapshot(caption={"obj": 1}))
    assert status == 409
    assert resp["error"] == "review_refresh_required"


# ---- flag ON: compare --------------------------------------------------------

def _full_row():
    row = _pending_row()
    row.update({"caption": "Workout Wednesday!", "post_date": "2026-08-10",
                "format": "feed", "account": "instagram"})
    return row


def test_flag_on_matching_snapshot_approves_with_digest(monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")
    sb = _SB(_full_row())
    status, resp = _run(monkeypatch, sb, expected_creative=_snapshot())
    assert status == 200
    assert resp["approval_state"] == "approved"
    assert resp["approval_digest"] == "digest-abc"
    # The normalized snapshot rode the RPC as p_expected.
    assert sb.calls == [_snapshot()]


def test_flag_on_null_and_empty_caption_both_normalize(monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")
    row = _full_row()
    row["caption"] = None
    sb = _SB(row)
    status, _ = _run(monkeypatch, sb,
                     expected_creative=_snapshot(caption=""))
    assert status == 200
    row2 = _full_row()
    row2["caption"] = "   "
    sb2 = _SB(row2)
    status2, _ = _run(monkeypatch, sb2,
                      expected_creative=_snapshot(caption=None))
    assert status2 == 200


@pytest.mark.parametrize("edit", [
    {"caption": "edited AFTER the tap"},
    {"image_url": "https://cdn.example/NEW.jpg"},
    {"post_date": "2026-08-11"},
    {"account": "facebook"},
    {"format": "story"},
])
def test_flag_on_stale_compare_fails_closed(monkeypatch, edit):
    """Any drift between the tapped card and the locked row -> 409
    review_refresh_required, no status change, no digest."""
    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")
    row = _full_row()
    row.update(edit)
    sb = _SB(row)
    status, resp = _run(monkeypatch, sb, expected_creative=_snapshot())
    assert status == 409
    assert resp["error"] == "review_refresh_required"
    assert sb.row["status"] == "pending"
    # The RPC WAS invoked with the snapshot; the compare inside it matched
    # zero rows and stamped nothing.
    assert sb.calls == [_snapshot()]


def test_flag_on_case_insensitive_format_and_platform(monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")
    row = _full_row()
    row["format"] = "Feed"
    row["account"] = "Instagram"
    sb = _SB(row)
    status, _ = _run(monkeypatch, sb,
                     expected_creative=_snapshot(format="FEED",
                                                 platform="INSTAGRAM"))
    assert status == 200


@pytest.mark.parametrize("flag", ["AGENT_APPROVAL_PROOF", "AGENT_APPROVAL_CAPTURE"])
def test_legacy_approve_guard_keeps_legacy_access_gates(monkeypatch, flag):
    """Either flag adds a snapshot guard, never the Part-B product gate.

    The legacy route is already behind portal-approval token handling.  Calling
    the common Supabase approval handler must retain its row ownership and
    proof checks while skipping only the portal-social feature and billing
    gates that legacy customers do not have.
    """
    monkeypatch.delenv("AGENT_APPROVAL_PROOF", raising=False)
    monkeypatch.delenv("AGENT_APPROVAL_CAPTURE", raising=False)
    monkeypatch.setenv(flag, "true")
    monkeypatch.setattr(portal_routes.config, "portal_approvals_enabled",
                        lambda: True)
    monkeypatch.setattr(portal_routes.config, "portal_calendar_supabase_enabled",
                        lambda: True)

    calls = {}

    class _LegacyStore:
        def get_row(self, account_key, row_id):
            calls["loaded"] = (account_key, row_id)
            return _full_row()

        def approve_ready(self, account_key, row_id, expected_creative=None):
            calls["approved"] = (account_key, row_id, expected_creative)
            return {**_full_row(), "status": "approved",
                    "approval_digest": "digest-abc"}

    store = _LegacyStore()
    monkeypatch.setattr(portal_routes._pcs, "SupabaseCalendarStore",
                        lambda: store)
    # These are deliberately false. The legacy approval route must still run.
    monkeypatch.setattr("agent.portal_social.config.portal_social_enabled",
                        lambda: False)
    monkeypatch.setattr("agent.portal_social.is_social_active",
                        lambda *args, **kwargs: False)

    status, body = portal_routes.handle_portal_action(
        "approve", "gymx", "r1", "legacy-actor",
        expected_creative=_snapshot())

    assert status == 200
    assert body["approval_digest"] == "digest-abc"
    assert calls["loaded"] == ("gymx", "r1")
    assert calls["approved"] == ("gymx", "r1", _snapshot())

    calls.clear()
    status, body = portal_routes.handle_portal_action(
        "approve", "gymx", "r1", "legacy-actor", expected_creative=None)
    assert status == 409 and body["error"] == "review_refresh_required"
    assert calls == {"loaded": ("gymx", "r1")}


def test_flag_on_row_changed_under_tap_fails_closed(monkeypatch):
    """approve_ready returning zero rows (RPC compare mismatch) -> 409
    review_refresh_required, never the flag-OFF message."""
    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")

    class _EmptySB:
        row = _full_row()

        def approve_ready(self, *a, **k):
            return None

    status, resp = _run(monkeypatch, _EmptySB(),
                        expected_creative=_snapshot())
    assert status == 409
    assert resp["error"] == "review_refresh_required"


# ---- store wire shape --------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = ""

    def json(self):
        return self._payload


class _FakeHTTP:
    def __init__(self, post_resp):
        self.calls = []
        self._post_resp = post_resp

    def post(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append((url, json))
        return self._post_resp


def test_approve_ready_sends_expected_only_when_present(monkeypatch):
    http = _FakeHTTP(_Resp(200, [{"id": "r1", "gym_id": "gymx"}]))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    pcs.SupabaseCalendarStore().approve_ready("gymx", "r1")
    _, payload = http.calls[0]
    assert payload == {"p_row_id": "r1", "p_gym_id": "gymx"}
    assert "p_expected" not in payload

    http2 = _FakeHTTP(_Resp(200, [{"id": "r1", "gym_id": "gymx"}]))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http2)
    snap = _snapshot()
    pcs.SupabaseCalendarStore().approve_ready("gymx", "r1",
                                              expected_creative=snap)
    _, payload2 = http2.calls[0]
    assert payload2["p_expected"] == snap


# ---- validation helper -------------------------------------------------------

def test_validate_expected_creative_accepts_null_caption():
    import agent.portal_social as ps
    norm, err = ps._validate_expected_creative(_snapshot(caption=None))
    assert err is None
    assert norm["caption"] is None


def test_validate_expected_creative_requires_known_platform():
    import agent.portal_social as ps
    _, err = ps._validate_expected_creative(_snapshot(platform="twitter"))
    assert err is not None


def test_validate_expected_creative_allows_googlebusiness_photo_only():
    import agent.portal_social as ps
    norm, err = ps._validate_expected_creative(
        _snapshot(format="photo", platform="googlebusiness"))
    assert err is None
    assert norm["format"] == "photo"
    assert norm["platform"] == "googlebusiness"

    # Feed/story remain valid for GBP and IG/FB, while GBP's photo format
    # cannot accidentally broaden the Instagram/Facebook snapshot contract.
    for platform in ("instagram", "facebook", "googlebusiness"):
        for fmt in ("feed", "story"):
            assert ps._validate_expected_creative(
                _snapshot(format=fmt, platform=platform))[1] is None
    for platform in ("instagram", "facebook"):
        assert ps._validate_expected_creative(
            _snapshot(format="photo", platform=platform))[1] is not None
