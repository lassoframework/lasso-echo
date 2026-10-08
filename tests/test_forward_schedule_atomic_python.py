"""Offline failure regressions for the inactive prepare / atomic finalize lane."""
from copy import deepcopy
from datetime import date
import uuid

import pytest

from agent import client_month_run as cmr
from agent import forward_media_guard as guard
from agent import forward_media_visual_index as visual
from agent import portal_calendar_store as pcs
from test_forward_schedule_reservation import (
    ATT_IDS, DAY, LPID, RID, ROW_ID, SHA, SHA_B, _ApplyStore, _ClaimStore,
    _HTTP, _Resp, _apply_rows, _arm_attester, _armed_http, _before_claim_env,
    _err, _group_rows, _store,
)


def test_partial_attestation_failure_never_reserves_or_finalizes(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    _arm_attester(monkeypatch)
    attested = []
    def attest(row_id, *args, **kwargs):
        attested.append(row_id)
        if len(attested) == 2:
            raise guard.ForwardMediaVerificationHold("unreadable second sibling")
        return {"attestation_ids": dict(zip(visual.ROLES, ATT_IDS))}
    monkeypatch.setattr(visual, "attest", attest)
    http, inserted = _armed_http(_group_rows())
    old = [{"id": str(uuid.uuid4()), "gym_id": "lasso", "status": "pending",
            "variant_status": "active", "caption": "old"}]
    before = deepcopy(old)
    with pytest.raises(pcs.ReservationStagingError):
        _store(http).insert_rows("lasso", _group_rows(), expected_old_rows=old)
    assert old == before
    assert len(attested) == 2
    assert len(inserted) == 3
    assert all(r["variant_status"] == "candidate" for r in inserted)
    assert all(r["media_not_ready_reason"] == "forward_reservation_staged" for r in inserted)
    assert not [c for c in http.calls if c[0] == "delete"]
    assert not [c for c in http.calls if c[1].endswith((pcs._RESERVE_RPC, pcs._FINALIZE_RPC))]


def test_short_insert_response_keeps_old_and_never_finalizes(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    _arm_attester(monkeypatch)
    http, _ = _armed_http(_group_rows())
    http.posts["content_calendar"] = lambda payload: _Resp(201, payload[:1])
    with pytest.raises(pcs.ReservationStagingError):
        _store(http).insert_rows("lasso", _group_rows())
    assert not [c for c in http.calls if c[0] == "delete"]
    assert not [c for c in http.calls if c[1].endswith(pcs._FINALIZE_RPC)]


@pytest.mark.parametrize("response", [
    _Resp(200, {}), _Resp(200, ValueError("bad JSON")),
    _Resp(200, {"row_ids": [ROW_ID], "reservation_ids": [RID]}),
])
def test_ambiguous_finalize_response_never_cleans_up_or_uses_readback(monkeypatch, response):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    _arm_attester(monkeypatch)
    http, inserted = _armed_http(_group_rows(), reserve_resp=response)
    # A mutable active readback is deliberately available; it is no terminal
    # receipt and cannot settle whether this exact finalization succeeded.
    http.gets["content_calendar"] = _Resp(200, [dict(r, variant_status="active") for r in inserted])
    with pytest.raises(pcs.ReservationStoreError):
        _store(http).insert_rows("lasso", _group_rows())
    assert not [c for c in http.calls if c[0] == "delete"]
    assert not [c for c in http.calls if c[0] == "get" and c[1].endswith("content_calendar")
                and "id" in (c[2] or {})]
    assert len([c for c in http.calls if c[1].endswith(pcs._FINALIZE_RPC)]) == 1


def test_lost_finalize_response_never_retries_or_deletes_activated_rows(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    _arm_attester(monkeypatch)
    http, inserted = _armed_http(_group_rows())
    def lost_response(args):
        # Model a committed transaction whose HTTP receipt was lost.
        for row in inserted:
            row["variant_status"] = "active"
            row["media_not_ready_reason"] = None
        raise TimeoutError("receipt lost")
    http.posts[pcs._FINALIZE_RPC] = lost_response
    with pytest.raises(pcs.ReservationStoreError):
        _store(http).insert_rows("lasso", _group_rows())
    assert all(r["variant_status"] == "active" for r in inserted)
    assert not [c for c in http.calls if c[0] == "delete"]
    assert len([c for c in http.calls if c[1].endswith(pcs._FINALIZE_RPC)]) == 1


def test_changed_sibling_holds_entire_batch_without_per_row_reservations(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    _arm_attester(monkeypatch)
    http, inserted = _armed_http(_group_rows())
    def changed(args):
        inserted[-1]["image_url"] = "https://x/changed-sibling.jpg"
        return _err("23514", "candidate revision changed")
    http.posts[pcs._FINALIZE_RPC] = changed
    with pytest.raises(pcs.ReservationHoldError):
        _store(http).insert_rows("lasso", _group_rows())
    assert not [c for c in http.calls if c[0] == "delete"]
    assert not [c for c in http.calls if c[1].endswith(pcs._RESERVE_RPC)]
    assert all(r["variant_status"] == "candidate" for r in inserted)


class _AtomicApplyStore(_ApplyStore):
    def __init__(self, failure=None):
        super().__init__()
        self.failure = failure
        self.old = [dict(id=str(uuid.uuid4()), gym_id="gritx", post_date=DAY,
                         account="instagram", format="feed", status="pending",
                         variant_status="active", caption="original", image_url="https://x/old.jpg",
                         logical_post_id=str(uuid.uuid4()), retained_null=None)]
        self.old += [dict(self.old[0], id=str(uuid.uuid4()), status=status,
                          post_date="2026-10-21") for status in ("approved", "published")]
        self.before = deepcopy(self.old)
        self.expected_old_rows = None

    def list_month(self, base_key, month):
        return deepcopy(self.old)

    def insert_rows(self, base_key, rows, *, expected_old_rows=None, **kwargs):
        self.expected_old_rows = deepcopy(expected_old_rows)
        if self.failure:
            raise self.failure
        return rows


@pytest.mark.parametrize("failure,unknown", [
    (pcs.ReservationHoldError(409, "sibling changed"), False),
    (pcs.ReservationStagingError(502, "second attestation unavailable"), False),
    (pcs.ReservationStoreError(0, "receipt lost"), True),
])
def test_apply_failure_preserves_old_and_does_not_claim_unknown_cleanup(monkeypatch, failure, unknown):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    store = _AtomicApplyStore(failure)
    result = cmr._apply("gritx", _apply_rows(with_proofs=True),
                        date(2026, 10, 20), 1, store, lambda msg: None)
    assert result["ok"] is False
    assert store.old == store.before  # includes approved + published siblings
    assert store.deleted == []
    assert store.expected_old_rows == store.before[:1]
    assert "retained_null" in store.expected_old_rows[0]
    assert result["insert_outcome_unknown"] is unknown
    assert result["old_calendar_preserved"] is (not unknown)
    assert result["deleted"] == (None if unknown else 0)


def test_apply_flag_off_preserves_legacy_delete_insert_lane(monkeypatch):
    monkeypatch.delenv(pcs.FORWARD_RESERVATION_FLAG_ENV, raising=False)
    store = _ApplyStore()
    result = cmr._apply("gritx", _apply_rows(with_proofs=False),
                        date(2026, 10, 20), 1, store, lambda msg: None)
    assert result["ok"] is True
    assert store.deleted == ["2026-10"]
    assert store.expected_old_rows is None


def test_atomic_old_manifest_rejects_partial_reads(monkeypatch):
    store = _ApplyStore()
    monkeypatch.setattr(store, "list_month", lambda *args: [{}] * 1000)
    with pytest.raises(pcs.CalendarInsertNotStartedError):
        cmr._forward_replacement_rows(store, "gritx", ["2026-10"], DAY, DAY, ())


def test_atomic_old_manifest_preserves_unknown_claimed_and_held_generations(monkeypatch):
    store = _AtomicApplyStore()
    original = store.old[0]
    unknowns = [dict(original, id=str(uuid.uuid4()), **fields) for fields in (
        {"status": None}, {"variant_status": None}, {"status": "queued"},
        {"publish_claim_token": ROW_ID}, {"scheduled_at": "2026-10-20T10:00:00Z"},
        {"media_not_ready_reason": "history_unknown"},
    )]
    store.old += unknowns
    rows = cmr._forward_replacement_rows(store, "gritx", ["2026-10"], DAY, DAY, ())
    assert rows == [original]


def test_candidate_rows_hidden_from_month_and_alternate_lists():
    staged = dict(id=ROW_ID, variant_status="candidate",
                  media_not_ready_reason="forward_reservation_staged")
    alternate = dict(id=str(uuid.uuid4()), variant_status="candidate", media_not_ready_reason=None)
    http = _HTTP(gets={"content_calendar": _Resp(200, [staged, alternate])})
    store = _store(http)
    assert store.list_variant_candidates("lasso", "2026-10") == [alternate]
    assert "forward_reservation_staged" in http.calls[-1][2]["or"]
    store.list_month("lasso", "2026-10")
    assert http.calls[-1][2]["variant_status"] == "eq.active"


@pytest.mark.parametrize("changed", [
    {"row_revision": "sibling-revision-changed"},
    {"attestation_ids": [str(uuid.uuid4()) for _ in range(3)]},
    {"source_sha256": SHA_B},
])
def test_publisher_holds_changed_reservation_proof(monkeypatch, changed):
    _before_claim_env(monkeypatch, "1")
    proof = {"reservation_id": RID, "tenant_id": "lasso", "post_date": DAY,
             "logical_post_id": LPID, "source_sha256": SHA,
             "row_revision": "rev", "attestation_ids": ATT_IDS}
    proof.update(changed)
    http = _HTTP(posts={
        "fixer_forward_visual_proof_20261008": _Resp(200, {"attestation_ids": ATT_IDS, "source_sha256": SHA}),
        pcs._PROOF_RPC: _Resp(200, proof),
    })
    with pytest.raises(guard.ForwardMediaVerificationHold):
        visual.before_claim(ROW_ID, "rev", str(uuid.uuid4()), store=_ClaimStore(http),
                            claim_token=str(uuid.uuid4()))


def test_final_publisher_derives_sha_and_passes_current_row_proof(monkeypatch):
    _before_claim_env(monkeypatch, "1")
    monkeypatch.setenv("AGENT_FORWARD_MEDIA_GUARD", "1")
    proof = {"reservation_id": RID, "tenant_id": "lasso", "post_date": DAY,
             "logical_post_id": LPID, "source_sha256": SHA,
             "row_revision": "rev", "attestation_ids": ATT_IDS}
    http = _HTTP(posts={
        "fixer_forward_visual_proof_20261008": _Resp(200, {"attestation_ids": ATT_IDS, "source_sha256": SHA}),
        pcs._PROOF_RPC: _Resp(200, proof),
        "fixer_forward_visual_index_claim_20261008": _Resp(200, True),
    })
    assert guard.claim(_ClaimStore(http), ROW_ID, str(uuid.uuid4()), str(uuid.uuid4()), "rev") is True
    reservation = [c for c in http.calls if c[1].endswith(pcs._PROOF_RPC)]
    assert reservation[0][2] == {"p_calendar_row_id": ROW_ID, "p_source_sha256": SHA}
    final = http.calls[-1]
    assert final[1].endswith("fixer_forward_visual_index_claim_20261008")
    assert final[2]["p_calendar_row_id"] == ROW_ID
    assert final[2]["p_expected_revision"] == "rev"
    assert final[2]["p_attestation_ids"] == ATT_IDS


def test_reservation_flag_alone_cannot_bypass_publisher_guard(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    monkeypatch.delenv("AGENT_FORWARD_MEDIA_GUARD", raising=False)
    monkeypatch.delenv("AGENT_FORWARD_MEDIA_VISUAL_INDEX", raising=False)
    assert guard.enabled() is True
    http = _HTTP()
    with pytest.raises(guard.ForwardMediaVerificationHold):
        guard.claim(_ClaimStore(http), ROW_ID, str(uuid.uuid4()), str(uuid.uuid4()), "rev")
    assert http.calls == []


def test_staff_variant_group_excludes_internal_staged_candidates(monkeypatch):
    anchor = dict(id=ROW_ID, gym_id="lasso", variant_status="active")
    staged = dict(id=str(uuid.uuid4()), gym_id="lasso", variant_of=ROW_ID,
                  variant_status="candidate", media_not_ready_reason="forward_reservation_staged")
    alternate = dict(staged, id=str(uuid.uuid4()), media_not_ready_reason=None)
    http = _HTTP(gets={"content_calendar": _Resp(200, [anchor, staged, alternate])})
    store = _store(http)
    monkeypatch.setattr(store, "get_row", lambda *args: anchor)
    assert store.get_variant_group("lasso", ROW_ID) == [anchor, alternate]
    assert "forward_reservation_staged" in http.calls[-1][2]["and"]
    monkeypatch.setattr(store, "get_row", lambda *args: staged)
    before = len(http.calls)
    assert store.get_variant_group("lasso", staged["id"]) == []
    assert len(http.calls) == before
