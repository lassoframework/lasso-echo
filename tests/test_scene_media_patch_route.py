"""Python contract for routing guarded media PATCH lanes through the DRAFT
atomic scene RPC (visual_scene_atomic_media_patch) when
AGENT_VISUAL_SCENE_REGISTER is explicitly on.

Everything here is mocked: no PostgreSQL, no network. The pg proof lives in
tests/test_scene_candidate_patch_pg.py; this file pins the Python routing
contract: armed lanes call the RPC and never fall back to REST, OFF behavior
is byte-for-byte the old REST PATCH, a missing candidate fails closed, and
held/stale/not_found outcomes are never reported as a patched row.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_calendar_store as pcs
from agent import visual_scene_register as vsr
from agent import visual_writer_prepare as vwp

GYM = "gym-scene-patch"
ROW_ID = str(uuid.uuid4())
CANDIDATE_ID = str(uuid.uuid4())
GROUP = "vg_patchgroup"
PHASH = "abcdef0123456789"
FP = "md5:" + "0123456789abcdef" * 2
NEW_IMAGE = "https://media.example/replacement.jpg"
HOLD_REASON = ("Photo-first hold: unverified infographic placeholder; "
               "approved gym photo required")
CAS = pcs._VISUAL_MEDIA_CAS_COLUMNS
assert len(CAS) == 23


def full_row(**over):
    row = {key: None for key in CAS}
    row.update({
        "id": ROW_ID,
        "gym_id": GYM,
        "status": "pending",
        "format": "feed",
        "variant_status": "active",
        "account": "acct",
        "post_date": "2026-10-10",
        "caption": "cap",
        "created_at": "2026-10-01T00:00:00+00:00",
    })
    row.update(over)
    return row


def candidate_for(url, role="delivered", group=GROUP):
    return {
        "kind": "visual_scene_candidate",
        "stage": "candidate",
        "tenant_id": GYM,
        "group_key": group,
        "usage_claimed": False,
        "counts_as_use": False,
        "excludes_candidates": False,
        "observed_by": "visual_writer_prepare",
        "evidence_ref": "visual_writer_prepare:candidate_scene_evidence",
        "objects": [{
            "role": role,
            "phash": PHASH,
            "scene_fingerprint": "scene:phash64:" + PHASH,
            "exact_url": url,
            "fingerprint": FP,
            "byte_length": 321,
            "stageable": True,
        }],
    }


class _Response:
    text = ""

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _patched_row(args):
    """Simulate the SQL post-trigger row: expected image plus patch values."""
    row = dict(args["p_expected"])
    row.update(args["p_patch"])
    row["id"] = str(args["p_row_id"])
    row["gym_id"] = args["p_gym_id"]
    return row


class _HTTP:
    def __init__(self, *, get_rows=(), outcome="patched", patch_rows=None):
        self.get_rows = list(get_rows)
        self.outcome = outcome
        self.patch_rows = patch_rows
        self.rpc_args = []
        self.rest_patches = []

    def get(self, url, *, params, headers, timeout):
        return _Response(self.get_rows)

    def post(self, url, *, headers, json, timeout):
        assert url.endswith("/rpc/visual_scene_atomic_media_patch"), url
        self.rpc_args.append(json)
        row = _patched_row(json)
        if self.outcome == "held":
            row.update({"status": "pending", "variant_status": "archived",
                        "media_not_ready_reason": "scene_review_hold",
                        "publish_claim_token": None,
                        "publish_reservation_day": None})
            return _Response({"outcome": "held", "candidate_id": CANDIDATE_ID,
                              "candidate_reused": False, "row": row})
        if self.outcome in ("stale", "not_found"):
            return _Response({"outcome": self.outcome,
                              "row_id": json["p_row_id"],
                              "gym_id": json["p_gym_id"]})
        return _Response({"outcome": "patched", "candidate_id": CANDIDATE_ID,
                          "candidate_reused": False, "row": row})

    def patch(self, url, *, params, headers, json, timeout):
        self.rest_patches.append({"params": params, "json": json})
        return _Response(self.patch_rows if self.patch_rows is not None else [])


def _store(http):
    return pcs.SupabaseCalendarStore(url="http://test.invalid",
                                     service_key="test-key", http=http)


@pytest.fixture
def armed(monkeypatch):
    """Scene register explicit-on with prerequisites satisfied."""
    monkeypatch.setattr(vsr, "enabled", lambda: True)
    monkeypatch.setattr(vsr, "require_prerequisites", lambda: None)
    monkeypatch.setattr(vwp, "enabled", lambda: True)

    def fake_prepare(store, account_key, row, **kwargs):
        assert row.get("image_url")
        return {"visual_group_key": GROUP,
                "byte_hash": "derived:" + FP,
                "scene_candidate": candidate_for(row["image_url"])}
    monkeypatch.setattr(vwp, "prepare", fake_prepare)
    return monkeypatch


def _assert_rpc_contract(args, *, patch_keys, image_url):
    assert set(args) == {"p_row_id", "p_gym_id", "p_expected", "p_patch",
                         "p_candidate"}
    assert args["p_row_id"] == ROW_ID
    assert args["p_gym_id"] == GYM
    expected = args["p_expected"]
    # Full 23-key CAS image, explicit JSON nulls included.
    assert set(expected) == set(CAS)
    assert "thumbnail_url" in expected and expected["thumbnail_url"] is None
    assert expected["published_at"] is None
    patch = args["p_patch"]
    assert set(patch) == set(patch_keys)
    assert "scene_candidate" not in patch
    assert not set(patch) - {"image_url", "thumbnail_url", "source_media_url",
                             "source_media_asset_id", "drive_file_id",
                             "byte_hash", "r2_key", "visual_group_key",
                             "media_not_ready_reason"}
    candidate = args["p_candidate"]
    assert candidate["kind"] == "visual_scene_candidate"
    assert candidate["stage"] == "candidate"
    assert candidate["usage_claimed"] is False
    assert candidate["counts_as_use"] is False
    assert candidate["group_key"] == patch["visual_group_key"] == GROUP
    assert candidate["object_role"] == "display"
    assert candidate["exact_url"] == image_url
    assert candidate["phash"] == PHASH
    assert candidate["fingerprint"] == FP
    assert candidate["evidence"]["verified_bytes"] == FP


# -- swap_media -----------------------------------------------------------


def test_swap_media_routes_rpc_when_armed(armed):
    current = full_row(image_url="https://media.example/dup.jpg")
    http = _HTTP(get_rows=[current])
    row = _store(http).swap_media(GYM, ROW_ID, NEW_IMAGE)
    assert len(http.rpc_args) == 1
    assert http.rest_patches == []
    _assert_rpc_contract(http.rpc_args[0],
                         patch_keys={"image_url", "media_not_ready_reason",
                                     "visual_group_key", "byte_hash"},
                         image_url=NEW_IMAGE)
    assert row["image_url"] == NEW_IMAGE
    assert row["gym_id"] == GYM and row["id"] == ROW_ID


def test_swap_media_held_is_never_reported_patched(armed):
    current = full_row(image_url="https://media.example/dup.jpg")
    http = _HTTP(get_rows=[current], outcome="held")
    assert _store(http).swap_media(GYM, ROW_ID, NEW_IMAGE) is None
    assert len(http.rpc_args) == 1


def test_swap_media_stale_and_not_found_return_none(armed):
    current = full_row(image_url="https://media.example/dup.jpg")
    for outcome in ("stale", "not_found"):
        http = _HTTP(get_rows=[current], outcome=outcome)
        assert _store(http).swap_media(GYM, ROW_ID, NEW_IMAGE) is None
        assert len(http.rpc_args) == 1


def test_swap_media_missing_candidate_fails_closed(armed, monkeypatch):
    monkeypatch.setattr(vwp, "prepare",
                        lambda *a, **k: {"visual_group_key": GROUP,
                                         "byte_hash": "derived:" + FP})
    current = full_row(image_url="https://media.example/dup.jpg")
    http = _HTTP(get_rows=[current])
    with pytest.raises(pcs.PortalStoreError):
        _store(http).swap_media(GYM, ROW_ID, NEW_IMAGE)
    assert http.rpc_args == [] and http.rest_patches == []


def test_armed_without_prerequisites_fails_closed_no_rest(monkeypatch):
    monkeypatch.setattr(vsr, "enabled", lambda: True)
    monkeypatch.setattr(vwp, "enabled", lambda: False)
    current = full_row(image_url="https://media.example/dup.jpg")
    http = _HTTP(get_rows=[current])
    with pytest.raises(pcs.PortalStoreError):
        _store(http).swap_media(GYM, ROW_ID, NEW_IMAGE)
    assert http.rpc_args == [] and http.rest_patches == []


def test_off_behavior_rest_patch_and_candidate_stripped(monkeypatch):
    monkeypatch.setattr(vsr, "enabled", lambda: False)
    monkeypatch.setattr(vwp, "enabled", lambda: True)

    def fake_prepare(store, account_key, row, **kwargs):
        return {"visual_group_key": GROUP,
                "byte_hash": "derived:" + FP,
                "scene_candidate": candidate_for(row["image_url"])}
    monkeypatch.setattr(vwp, "prepare", fake_prepare)
    current = full_row(image_url="https://media.example/dup.jpg")
    patched = dict(current)
    patched.update({"image_url": NEW_IMAGE, "media_not_ready_reason": None,
                    "visual_group_key": GROUP, "byte_hash": "derived:" + FP})
    http = _HTTP(get_rows=[current], patch_rows=[patched])
    row = _store(http).swap_media(GYM, ROW_ID, NEW_IMAGE)
    assert row == patched
    assert http.rpc_args == []
    assert len(http.rest_patches) == 1
    assert "scene_candidate" not in http.rest_patches[0]["json"]


# -- patch_image_url -------------------------------------------------------


def test_patch_image_url_pending_row_routes(armed):
    current = full_row(image_url="https://media.example/old.jpg",
                       status="pending")
    http = _HTTP(get_rows=[current])
    row = _store(http).patch_image_url(GYM, ROW_ID, NEW_IMAGE)
    assert len(http.rpc_args) == 1 and http.rest_patches == []
    _assert_rpc_contract(http.rpc_args[0],
                         patch_keys={"image_url", "media_not_ready_reason",
                                     "visual_group_key", "byte_hash"},
                         image_url=NEW_IMAGE)
    assert row["image_url"] == NEW_IMAGE


def test_patch_image_url_approved_story_reburn_routes(armed):
    source = "https://media.example/raw-story.jpg"
    expected_row = full_row(image_url="https://media.example/burned-old.jpg",
                            status="approved", format="story",
                            source_media_url=source)
    evidence = {"source_exact_url": source, "delivered_exact_url": NEW_IMAGE}
    http = _HTTP()
    row = _store(http).patch_image_url(GYM, ROW_ID, NEW_IMAGE,
                                       expected_row=expected_row,
                                       render_evidence=evidence)
    assert len(http.rpc_args) == 1 and http.rest_patches == []
    args = http.rpc_args[0]
    assert args["p_expected"]["status"] == "approved"
    assert args["p_expected"]["source_media_url"] == source
    assert set(args["p_expected"]) == set(CAS)
    assert row["image_url"] == NEW_IMAGE


def test_patch_image_url_incomplete_expected_row_fails_closed(armed):
    expected_row = {key: full_row()[key] for key in CAS
                    if key != "time_slot"}
    expected_row.update({"id": ROW_ID, "gym_id": GYM, "status": "approved",
                         "format": "feed",
                         "image_url": "https://media.example/old.jpg"})
    http = _HTTP()
    with pytest.raises(pcs.PortalStoreError):
        _store(http).patch_image_url(GYM, ROW_ID, NEW_IMAGE,
                                     expected_row=expected_row)
    assert http.rpc_args == [] and http.rest_patches == []


# -- patch_media (placeholder backfill) ------------------------------------


def test_patch_media_pending_placeholder_row_routes(armed):
    current = full_row(image_url="",
                       media_not_ready_reason="needs media")
    http = _HTTP(get_rows=[current])
    row = _store(http).patch_media(GYM, ROW_ID, NEW_IMAGE,
                                   source_media_asset_id="asset-1")
    assert len(http.rpc_args) == 1 and http.rest_patches == []
    args = http.rpc_args[0]
    assert args["p_patch"]["image_url"] == NEW_IMAGE
    assert args["p_patch"]["source_media_asset_id"] == "asset-1"
    assert args["p_patch"]["media_not_ready_reason"] is None
    assert row["image_url"] == NEW_IMAGE


# -- replace_future_infographic_media --------------------------------------


def _placeholder_current():
    return full_row(image_url="https://media.example/igfill_2026-10-10_a.png",
                    status="approved", media_not_ready_reason=HOLD_REASON)


def test_replace_future_infographic_media_routes(armed):
    http = _HTTP()
    row = _store(http).replace_future_infographic_media(
        GYM, _placeholder_current(), image_url=NEW_IMAGE,
        source_media_url="https://media.example/source.jpg",
        source_media_asset_id="asset-9", reason=HOLD_REASON)
    assert len(http.rpc_args) == 1 and http.rest_patches == []
    args = http.rpc_args[0]
    assert args["p_row_id"] == ROW_ID
    assert set(args["p_expected"]) == set(CAS)
    assert args["p_expected"]["media_not_ready_reason"] == HOLD_REASON
    assert args["p_patch"]["media_not_ready_reason"] is None
    assert row["image_url"] == NEW_IMAGE


def test_replace_future_infographic_media_held_returns_none(armed):
    http = _HTTP(outcome="held")
    assert _store(http).replace_future_infographic_media(
        GYM, _placeholder_current(), image_url=NEW_IMAGE,
        source_media_url="https://media.example/source.jpg",
        source_media_asset_id="asset-9", reason=HOLD_REASON) is None
    assert len(http.rpc_args) == 1


# -- RPC result verification (no false success) ----------------------------


class _DriftHTTP(_HTTP):
    def __init__(self, get_rows, mutate):
        super().__init__(get_rows=get_rows)
        self._mutate = mutate

    def post(self, url, *, headers, json, timeout):
        self.rpc_args.append(json)
        row = _patched_row(json)
        return _Response(self._mutate(row, json))


def _swap_with(http):
    return _store(http).swap_media(GYM, ROW_ID, NEW_IMAGE)


def test_patched_outcome_with_unpersisted_value_raises(armed):
    current = full_row(image_url="https://media.example/dup.jpg")

    def mutate(row, args):
        row["image_url"] = "https://media.example/other.jpg"
        return {"outcome": "patched", "candidate_id": CANDIDATE_ID,
                "candidate_reused": False, "row": row}
    with pytest.raises(pcs.PortalStoreError):
        _swap_with(_DriftHTTP([current], mutate))


def test_patched_outcome_with_immutable_drift_raises(armed):
    current = full_row(image_url="https://media.example/dup.jpg")

    def mutate(row, args):
        row["caption"] = "tampered"
        return {"outcome": "patched", "candidate_id": CANDIDATE_ID,
                "candidate_reused": False, "row": row}
    with pytest.raises(pcs.PortalStoreError):
        _swap_with(_DriftHTTP([current], mutate))


def test_held_row_labeled_patched_raises(armed):
    current = full_row(image_url="https://media.example/dup.jpg")

    def mutate(row, args):
        row.update({"status": "pending", "variant_status": "archived",
                    "media_not_ready_reason": "scene_review_hold"})
        return {"outcome": "patched", "candidate_id": CANDIDATE_ID,
                "candidate_reused": False, "row": row}
    with pytest.raises(pcs.PortalStoreError):
        _swap_with(_DriftHTTP([current], mutate))


def test_held_outcome_without_held_markers_raises(armed):
    current = full_row(image_url="https://media.example/dup.jpg")

    def mutate(row, args):
        return {"outcome": "held", "candidate_id": CANDIDATE_ID,
                "candidate_reused": False, "row": row}
    with pytest.raises(pcs.PortalStoreError):
        _swap_with(_DriftHTTP([current], mutate))


def test_unknown_outcome_raises(armed):
    current = full_row(image_url="https://media.example/dup.jpg")

    def mutate(row, args):
        return {"outcome": "mystery", "row": row}
    with pytest.raises(pcs.PortalStoreError):
        _swap_with(_DriftHTTP([current], mutate))


def test_cross_tenant_result_row_raises(armed):
    current = full_row(image_url="https://media.example/dup.jpg")

    def mutate(row, args):
        row["gym_id"] = "other-gym"
        return {"outcome": "patched", "candidate_id": CANDIDATE_ID,
                "candidate_reused": False, "row": row}
    with pytest.raises(pcs.PortalStoreError):
        _swap_with(_DriftHTTP([current], mutate))


# -- restage_held_media -----------------------------------------------------


HELD_RESTAGE_REASON = "cross_date_media_repeat_needs_new_visual"


def _held_restage_current():
    return full_row(image_url="https://media.example/old.jpg",
                    media_not_ready_reason=HELD_RESTAGE_REASON)


def _restage_prepared_row(current):
    row = dict(current)
    row.update({"image_url": NEW_IMAGE, "source_media_url": None,
                "visual_group_key": GROUP, "byte_hash": "derived:" + FP,
                "media_not_ready_reason": HELD_RESTAGE_REASON})
    return row


def test_restage_held_media_armed_fails_closed_no_rest(armed):
    # The archived held-media restage is SQL-ineligible for
    # visual_scene_atomic_media_patch by design; armed means no write at all,
    # never a REST bypass, and the hold evidence is untouched.
    http = _HTTP()
    result = _store(http).restage_held_media(
        GYM, _held_restage_current(), image_url=NEW_IMAGE,
        source_media_url=None)
    assert result is None
    assert http.rpc_args == [] and http.rest_patches == []


def test_restage_held_media_release_armed_fails_closed_no_rest(armed):
    http = _HTTP()
    assert _store(http).restage_held_media(
        GYM, _held_restage_current(), release=True) is None
    assert http.rpc_args == [] and http.rest_patches == []


def test_restage_held_media_armed_without_prerequisites_raises(monkeypatch):
    monkeypatch.setattr(vsr, "enabled", lambda: True)
    monkeypatch.setattr(vwp, "enabled", lambda: False)
    http = _HTTP()
    with pytest.raises(pcs.PortalStoreError):
        _store(http).restage_held_media(
            GYM, _held_restage_current(), image_url=NEW_IMAGE,
            source_media_url=None)
    assert http.rpc_args == [] and http.rest_patches == []


def test_restage_held_media_off_rest_strips_candidate(monkeypatch):
    # Partial configuration: preparation + candidate emission ON, atomic scene
    # register OFF. The sidecar candidate must never reach the REST payload.
    monkeypatch.setattr(vsr, "enabled", lambda: False)
    monkeypatch.setattr(vwp, "enabled", lambda: True)

    def fake_prepare(store, account_key, row, **kwargs):
        return {"visual_group_key": GROUP,
                "byte_hash": "derived:" + FP,
                "scene_candidate": candidate_for(row["image_url"])}
    monkeypatch.setattr(vwp, "prepare", fake_prepare)
    current = _held_restage_current()
    http = _HTTP(patch_rows=[_restage_prepared_row(current)])
    row = _store(http).restage_held_media(
        GYM, current, image_url=NEW_IMAGE, source_media_url=None)
    assert row is not None and row["image_url"] == NEW_IMAGE
    assert row["media_not_ready_reason"] == HELD_RESTAGE_REASON
    assert http.rpc_args == [] and len(http.rest_patches) == 1
    assert "scene_candidate" not in http.rest_patches[0]["json"]
    assert http.rest_patches[0]["json"]["visual_group_key"] == GROUP


# -- recover_story_media_hold ----------------------------------------------


def _story_hold_current():
    return full_row(format="story",
                    image_url="https://media.example/story_old.jpg",
                    media_not_ready_reason="story_media_missing")


def _story_proposed():
    return {"image_url": NEW_IMAGE, "format": "story", "status": "pending",
            "media_not_ready_reason": None, "account": "acct",
            "post_date": "2026-10-10"}


def test_recover_story_media_hold_routes_rpc_when_armed(armed):
    current = _story_hold_current()
    http = _HTTP()
    row = _store(http).recover_story_media_hold(
        GYM, current, _story_proposed())
    assert len(http.rpc_args) == 1
    assert http.rest_patches == []
    _assert_rpc_contract(http.rpc_args[0],
                         patch_keys={"image_url", "media_not_ready_reason",
                                     "visual_group_key", "byte_hash"},
                         image_url=NEW_IMAGE)
    assert http.rpc_args[0]["p_expected"]["media_not_ready_reason"] == \
        "story_media_missing"
    assert row["image_url"] == NEW_IMAGE
    assert row["gym_id"] == GYM and row["id"] == ROW_ID


def test_recover_story_media_hold_held_and_stale_return_none(armed):
    current = _story_hold_current()
    for outcome in ("held", "stale", "not_found"):
        http = _HTTP(outcome=outcome)
        assert _store(http).recover_story_media_hold(
            GYM, current, _story_proposed()) is None
        assert len(http.rpc_args) == 1
        assert http.rest_patches == []


def test_recover_story_media_hold_armed_without_prerequisites_raises(monkeypatch):
    monkeypatch.setattr(vsr, "enabled", lambda: True)
    monkeypatch.setattr(vwp, "enabled", lambda: False)
    http = _HTTP()
    with pytest.raises(pcs.PortalStoreError):
        _store(http).recover_story_media_hold(
            GYM, _story_hold_current(), _story_proposed())
    assert http.rpc_args == [] and http.rest_patches == []


def test_recover_story_media_hold_off_rest_strips_candidate(monkeypatch):
    # Partial configuration: preparation + candidate emission ON, atomic scene
    # register OFF. The sidecar candidate must never reach the REST payload.
    monkeypatch.setattr(vsr, "enabled", lambda: False)
    monkeypatch.setattr(vwp, "enabled", lambda: True)

    def fake_prepare(store, account_key, row, **kwargs):
        return {"visual_group_key": GROUP,
                "byte_hash": "derived:" + FP,
                "scene_candidate": candidate_for(row["image_url"])}
    monkeypatch.setattr(vwp, "prepare", fake_prepare)
    current = _story_hold_current()
    patched = dict(current)
    patched.update({"image_url": NEW_IMAGE, "media_not_ready_reason": None,
                    "visual_group_key": GROUP, "byte_hash": "derived:" + FP})
    http = _HTTP(patch_rows=[patched])
    row = _store(http).recover_story_media_hold(
        GYM, current, _story_proposed())
    assert row == patched
    assert http.rpc_args == [] and len(http.rest_patches) == 1
    assert "scene_candidate" not in http.rest_patches[0]["json"]
    assert http.rest_patches[0]["json"]["image_url"] == NEW_IMAGE
