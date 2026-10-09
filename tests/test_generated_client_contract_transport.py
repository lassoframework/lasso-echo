"""GENERATED-CLIENT CONTRACT transport (2026-10-09 frozen package), all offline.

Under test (agent/drafter.py, agent/store.py, agent/real_calendar_mirror.py,
agent/portal_social.py):
  * Ordinary photo drafts keep empty generated fields and never hold.
  * generated_hold_reason: fail-closed on unsupported origin, bad UUID, bad SHA,
    missing receipt AND on a fully well-formed producer-supplied receipt. There
    is NO trusted hosted-byte receipt authority, so EVERY generated card holds;
    None only for a draft with no generated identity at all.
  * PendingStore reload (FRESH object) retains creative_origin / version / SHA /
    receipt exactly (unconditional round-trip).
  * real_calendar_mirror._real_row: every generated card lands on the media
    hold with NO generated columns, including one carrying a well-formed
    fabricated receipt; wrong gym/version/URL/SHA holds; a cross-tenant
    destination (gym_b row for a gym_a draft) can never emit generated columns.
  * portal_social._generated_from_row maps only complete, well-formed generated
    rows; a partial/malformed generated row sets the media hold instead of
    silently downgrading to an ordinary photo;
    _validate_expected_creative accepts an exact generated snapshot, rejects
    missing/malformed marker, SHA and version, and leaves legacy snapshots
    byte-for-byte unchanged.
  * No coach_review state is introduced anywhere in this transport.
"""

import hashlib
import os
import sys
import uuid as _uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_social as ps
from agent import real_calendar_mirror as rcm
from agent.drafter import (CREATIVE_ORIGIN_GENERATED, Draft, DraftStatus,
                           generated_hold_reason)
from agent.store import PendingStore
from agent.store import _from_dict

SHA_A = hashlib.sha256(b" rendition A ").hexdigest()
SHA_B = hashlib.sha256(b" rendition B ").hexdigest()
VER_A = str(_uuid.uuid4())
URL_A = "https://cdn.example/gen-a.png"
GYM = "northside_ig"


def _draft(**kw):
    kw.setdefault("caption", "hi")
    return Draft(
        draft_id="d1", account_key=GYM, platform="instagram",
        hashtags=[], creative_path="x.png", creative_public_url=URL_A,
        scheduled_for="", status=DraftStatus.PENDING, day_key="2026-08-06",
        draft_type="feed", **kw)


def _receipt(**over):
    receipt = {
        "receipt_id": "rcpt_1",
        "gym_id": GYM,
        "artifact_version_id": VER_A,
        "hosted_url": URL_A,
        "delivered_sha256": SHA_A,
        "render_manifest": {"engine": "astra", "model": "gpt-image-2.5"},
    }
    receipt.update(over)
    return receipt


def _generated_draft(**over):
    fields = {
        "creative_origin": CREATIVE_ORIGIN_GENERATED,
        "generated_artifact_version_id": VER_A,
        "generated_artifact_sha256": SHA_A,
    }
    fields.update(over)
    return _draft(**fields)


# ---- drafter: hold predicate ----------------------------------------------

def test_photo_draft_untouched_and_never_held():
    d = _draft()
    assert d.creative_origin == ""
    assert d.generated_artifact_version_id == ""
    assert d.generated_artifact_sha256 == ""
    assert d.generated_receipt == {}
    assert generated_hold_reason(d) is None


def test_unsupported_origin_holds():
    assert "unsupported creative_origin" in generated_hold_reason(
        _generated_draft(creative_origin="uploaded"))


def test_generated_without_receipt_holds_and_reports_dependency():
    reason = generated_hold_reason(_generated_draft())
    assert reason and "receipt" in reason


@pytest.mark.parametrize("over", [
    {"generated_artifact_version_id": "not-a-uuid"},
    {"generated_artifact_sha256": "ABCD" * 16},          # uppercase: not lowercase
    {"generated_artifact_sha256": "abc"},                # wrong length
])
def test_malformed_identity_holds(over):
    assert generated_hold_reason(
        _generated_draft(generated_receipt=_receipt(), **over))


@pytest.mark.parametrize("over", [
    {},                                                     # missing receipt
    {"receipt_id": ""},                                     # no receipt id
    {"gym_id": "other_gym"},                                # tenant mismatch
    {"artifact_version_id": str(_uuid.uuid4())},            # version mismatch
    {"hosted_url": "https://cdn.example/other.png"},        # URL mismatch
    {"delivered_sha256": SHA_B},                            # delivered-byte mismatch
    {"delivered_sha256": "G" * 64},                         # malformed receipt SHA
    {"render_manifest": {}},                                # empty manifest
    {"extra_key": True},                                    # unknown receipt key
])
def test_receipt_mismatch_or_incomplete_holds(over):
    receipt = _receipt(**over) if over else None
    d = _generated_draft()
    if receipt is not None:
        d.generated_receipt = receipt
    assert generated_hold_reason(d)


def test_fabricated_exact_receipt_still_holds():
    """P1-1 regression: a fully well-formed but FABRICATED receipt (invented
    receipt id, arbitrary manifest, unverifiable hosted URL) must NOT lift the
    hold -- no trusted hosted-byte receipt authority exists to prove it."""
    d = _generated_draft(generated_receipt=_receipt(
        receipt_id="fabricated", render_manifest={"engine": "invented"}))
    reason = generated_hold_reason(d)
    assert reason and "receipt" in reason


@pytest.mark.parametrize("clear", ["creative_origin"])
def test_origin_cleared_with_generated_fields_holds(clear):
    """P1-2 regression: clearing the marker while leaving version/SHA/receipt
    is a partial generated identity, not an ordinary photo."""
    d = _generated_draft(generated_receipt=_receipt())
    setattr(d, clear, "")
    assert generated_hold_reason(d)


def test_unknown_marker_with_generated_fields_holds():
    d = _generated_draft(creative_origin="mystery", generated_receipt=_receipt())
    assert "unsupported creative_origin" in generated_hold_reason(d)


def test_source_only_generated_asset_marker_holds_draft():
    d = _draft(source_media_asset_id="generated-astra:job123")
    assert "partial generated identity" in generated_hold_reason(d)


def test_same_url_changed_sha_holds():
    # A same-hosted-URL replacement with a different digest must NOT reuse
    # the old receipt: human proof never transfers across renditions.
    d = _generated_draft(generated_artifact_sha256=SHA_B,
                         generated_receipt=_receipt())
    assert generated_hold_reason(d)


# ---- store: unconditional reload round-trip --------------------------------

@pytest.fixture
def store(tmp_path):
    return PendingStore(str(tmp_path / "p.db")), str(tmp_path / "p.db")


def test_store_reload_retains_generated_fields(store):
    st, path = store
    receipt = _receipt()
    st.put(_generated_draft(generated_receipt=receipt))
    fresh = PendingStore(path)  # FRESH object: SQLite reload, not same-object stash
    d = fresh.get("d1")
    assert d.creative_origin == "generated"
    assert d.generated_artifact_version_id == VER_A
    assert d.generated_artifact_sha256 == SHA_A
    assert d.generated_receipt == receipt


def test_store_reload_photo_draft_unchanged(store):
    st, path = store
    st.put(_draft())
    d = PendingStore(path).get("d1")
    assert d.creative_origin == ""
    assert d.generated_receipt == {}


def test_store_reload_malformed_generated_identity_fails_closed_at_mirror(store):
    st, path = store
    st.put(_generated_draft(generated_artifact_sha256="zz"))
    d = PendingStore(path).get("d1")
    assert generated_hold_reason(d)


def test_store_reload_preserves_source_only_generated_hold(store):
    st, path = store
    draft = _draft(source_media_asset_id="generated-astra:job123")
    assert generated_hold_reason(draft)
    st.put(draft)

    reloaded = PendingStore(path).get("d1")
    assert reloaded.source_media_asset_id == "generated-astra:job123"
    assert generated_hold_reason(reloaded)
    row = _row_for(reloaded)
    assert "partial generated identity" in row["media_not_ready_reason"]
    displayed = ps._content_calendar_post(row)
    assert displayed["needs_media"] is True
    assert "partial generated identity" in displayed["media_not_ready_reason"]


def test_restore_ordinary_source_id_without_observations_stays_omitted():
    restored = _from_dict({"draft_id": "ordinary", "status": "pending",
                           "source_media_asset_id": "drive-photo-123"})
    assert getattr(restored, "source_media_asset_id", "") == ""


# ---- mirror: transport vs hold ----------------------------------------------

def _row_for(draft):
    return rcm._real_row(GYM, draft)


def test_mirror_holds_generated_without_receipt():
    row = _row_for(_generated_draft())
    assert row["media_not_ready_reason"]
    assert "creative_origin" not in row
    assert "generated_artifact_version_id" not in row
    assert "generated_artifact_sha256" not in row
    # the creative itself stays visible for preview; the hold blocks approve/claim
    assert row["image_url"] == URL_A


def test_mirror_holds_source_only_generated_asset_marker():
    row = _row_for(_draft(source_media_asset_id="generated-astra:job123"))
    assert "partial generated identity" in row["media_not_ready_reason"]
    assert row["source_media_asset_id"] == "generated-astra:job123"
    for key in ("creative_origin", "generated_artifact_version_id",
                "generated_artifact_sha256"):
        assert key not in row


def test_mirror_holds_receipt_with_wrong_gym():
    row = _row_for(_generated_draft(generated_receipt=_receipt(gym_id="other_gym")))
    assert row["media_not_ready_reason"]


def test_mirror_holds_even_with_wellformed_fabricated_receipt():
    """No trusted receipt authority exists: a well-formed producer-supplied
    receipt never transports generated columns."""
    row = _row_for(_generated_draft(generated_receipt=_receipt()))
    assert row["media_not_ready_reason"]
    for key in ("creative_origin", "generated_artifact_version_id",
                "generated_artifact_sha256"):
        assert key not in row


def test_mirror_cross_tenant_destination_can_never_emit_generated_columns():
    """P1-4 regression: building a gym_b row from a gym_a draft holding a
    gym_a-bound receipt must never emit generated columns for gym_b. The
    unconditional hold makes this impossible today; the destination tenant
    binding guard in _real_row documents the check the future trusted path
    must keep."""
    draft_a = _generated_draft(generated_receipt=_receipt())  # bound to GYM
    row = rcm._real_row("gym_b", draft_a)
    assert row["gym_id"] == "gym_b"
    assert row["media_not_ready_reason"]
    for key in ("creative_origin", "generated_artifact_version_id",
                "generated_artifact_sha256"):
        assert key not in row


def test_mirror_photo_row_has_no_generated_keys():
    row = _row_for(_draft())
    for key in ("creative_origin", "generated_artifact_version_id",
                "generated_artifact_sha256", "media_not_ready_reason"):
        assert key not in row


# ---- portal social: visible card + expected snapshot -------------------------

def test_visible_post_carries_generated_binding():
    row = {"gym_id": GYM, "account": "instagram", "post_date": "2026-08-06",
           "status": "pending", "format": "feed", "caption": "hi",
           "image_url": URL_A, "creative_origin": "generated",
           "generated_artifact_version_id": VER_A,
           "generated_artifact_sha256": SHA_A}
    post = ps._content_calendar_post(row)
    assert post["creative_origin"] == "generated"
    assert post["generated_artifact_version_id"] == VER_A
    assert post["generated_artifact_sha256"] == SHA_A


def test_partial_or_spoofed_generated_row_fails_closed_with_media_hold():
    """P1-3 regression: a partial/malformed generated row must NOT silently
    drop its marker and report needs_media=False like an ordinary photo. It
    keeps no generated marker AND carries the media hold for display/approval."""
    base = {"gym_id": GYM, "account": "instagram", "post_date": "2026-08-06",
            "status": "pending", "format": "feed", "caption": "hi",
            "image_url": URL_A}
    plain = ps._content_calendar_post(base)
    assert "creative_origin" not in plain and plain["needs_media"] is False
    partial = dict(base, creative_origin="generated")
    post = ps._content_calendar_post(partial)
    assert "creative_origin" not in post
    assert post["needs_media"] is True and "generated" in post["media_not_ready_reason"]
    bad = dict(base, creative_origin="generated",
               generated_artifact_version_id="nope",
               generated_artifact_sha256=SHA_A)
    post = ps._content_calendar_post(bad)
    assert "creative_origin" not in post
    assert post["needs_media"] is True and post["media_not_ready_reason"]
    marker_cleared = dict(base, creative_origin="",
                          generated_artifact_version_id=VER_A,
                          generated_artifact_sha256=SHA_A)
    post = ps._content_calendar_post(marker_cleared)
    assert "creative_origin" not in post
    assert post["needs_media"] is True and post["media_not_ready_reason"]
    unknown = dict(base, creative_origin="uploaded")
    post = ps._content_calendar_post(unknown)
    assert post["needs_media"] is True and "unsupported creative_origin" in post["media_not_ready_reason"]


def test_source_only_generated_asset_marker_displays_media_hold():
    row = {"gym_id": GYM, "account": "instagram", "post_date": "2026-08-06",
           "status": "pending", "format": "feed", "caption": "hi",
           "image_url": URL_A,
           "source_media_asset_id": "generated-astra:job123"}
    post = ps._content_calendar_post(row)
    assert post["needs_media"] is True
    assert "partial generated identity" in post["media_not_ready_reason"]
    assert "creative_origin" not in post


def _expected(**over):
    e = {"caption": "hi", "media_url": URL_A, "day_key": "2026-08-06",
         "format": "feed", "platform": "instagram"}
    e.update(over)
    return e


def test_expected_snapshot_accepts_exact_generated_binding():
    normalized, why = ps._validate_expected_creative(_expected(
        creative_origin="generated",
        generated_artifact_version_id=VER_A,
        generated_artifact_sha256=SHA_A))
    assert why is None
    assert normalized["creative_origin"] == "generated"
    assert normalized["generated_artifact_version_id"] == VER_A
    assert normalized["generated_artifact_sha256"] == SHA_A


def test_expected_snapshot_generated_fail_closed():
    good = {"creative_origin": "generated",
            "generated_artifact_version_id": VER_A,
            "generated_artifact_sha256": SHA_A}
    # missing marker companions
    for drop in list(good):
        bad = dict(good)
        del bad[drop]
        normalized, why = ps._validate_expected_creative(_expected(**bad))
        assert normalized is None and why
    # malformed values
    normalized, why = ps._validate_expected_creative(_expected(
        creative_origin="generated",
        generated_artifact_version_id="nope",
        generated_artifact_sha256=SHA_A))
    assert normalized is None
    normalized, why = ps._validate_expected_creative(_expected(
        creative_origin="generated",
        generated_artifact_version_id=VER_A,
        generated_artifact_sha256=SHA_B.upper()))
    assert normalized is None
    normalized, why = ps._validate_expected_creative(_expected(
        creative_origin="photo"))
    assert normalized is None


def test_expected_snapshot_legacy_rows_unchanged():
    normalized, why = ps._validate_expected_creative(_expected())
    assert why is None
    assert set(normalized) == {"caption", "media_url", "day_key",
                               "format", "platform"}


def test_no_coach_review_state_introduced():
    # The transport must never mint the coach_review status anywhere: mirror
    # rows stay PENDING-with-hold, not coach_review.
    held = _row_for(_generated_draft())
    assert held["status"] != "coach_review"
    held_receipt = _row_for(_generated_draft(generated_receipt=_receipt()))
    assert held_receipt["status"] != "coach_review"
