"""Offline failure regressions for the two-phase stage / prepare boundary."""
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
    _HTTP, _Resp, _apply_rows, _armed_http, _before_claim_env, _err,
    _group_rows, _stage_members, _stage_receipt, _store,
)


def test_stage_hold_never_reserves_finalizes_or_touches_old_rows(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    http, _ = _armed_http(_group_rows(),
                          stage_resp=_err("23514", "staged membership refused"))
    old = [{"id": str(uuid.uuid4()), "gym_id": "lasso", "status": "pending",
            "variant_status": "active", "caption": "old"}]
    before = deepcopy(old)
    with pytest.raises(pcs.ReservationHoldError):
        _store(http).insert_rows("lasso", _group_rows(), expected_old_rows=old)
    assert old == before
    assert not [c for c in http.calls if c[0] == "delete"]
    assert not [c for c in http.calls
                if c[0] == "post" and c[1].endswith("content_calendar")]
    assert not [c for c in http.calls if c[1].endswith((pcs._RESERVE_RPC, pcs._FINALIZE_RPC))]


def test_planner_never_calls_attester_or_trusted_dsn(monkeypatch):
    """The immediate planner-to-attester path is removed: no inline attest,
    no snapshot/lineage reads, no trusted DSN from the store lane."""
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    def forbidden(*args, **kwargs):
        raise AssertionError("planner must never call visual_index.attest")
    monkeypatch.setattr(visual, "attest", forbidden)
    http, _ = _armed_http(_group_rows())
    out = _store(http).insert_rows("lasso", _group_rows())
    assert len(out) == 3
    assert not [c for c in http.calls if pcs._SNAPSHOT_RPC in c[1] or pcs._LINEAGE_TABLE in c[1]]


@pytest.mark.parametrize("response", [
    _Resp(200, {}), _Resp(200, ValueError("bad JSON")),
    _Resp(200, {"batch_id": str(uuid.uuid4()), "state": "preparing",
                "staged_row_ids": []}),
])
def test_ambiguous_stage_response_never_cleans_up_or_uses_readback(monkeypatch, response):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    http, _ = _armed_http(_group_rows(), stage_resp=response)
    # A mutable active readback is deliberately available; it is no terminal
    # receipt and cannot settle whether this exact stage succeeded.
    http.gets["content_calendar"] = _Resp(200, [dict(r, id=str(uuid.uuid4()),
                                                     variant_status="active")
                                                for r in _group_rows()])
    store = _store(http)
    with pytest.raises(pcs.ReservationStoreError):
        store.insert_rows("lasso", _group_rows())
    assert not [c for c in http.calls if c[0] == "delete"]
    assert not [c for c in http.calls if c[0] == "get" and c[1].endswith("content_calendar")
                and "id" in (c[2] or {})]
    assert len([c for c in http.calls if c[1].endswith(pcs._STAGE_RPC)]) == 1
    assert store.last_forward_stage is None
    assert store.last_forward_stage_attempt["batch_id"]


def test_lost_stage_response_never_retries_or_infers_from_rows(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    staged_rows = []
    def lost_response(args):
        # Model a committed stage whose HTTP receipt was lost.
        staged_rows.extend(m["row"] for m in _stage_members(args))
        raise TimeoutError("receipt lost")
    http, _ = _armed_http(_group_rows(), stage_resp=lost_response)
    store = _store(http)
    with pytest.raises(pcs.ReservationStoreError):
        store.insert_rows("lasso", _group_rows())
    assert len(staged_rows) == 3, "the attempt reached the RPC exactly once"
    assert not [c for c in http.calls if c[0] == "delete"]
    assert len([c for c in http.calls if c[1].endswith(pcs._STAGE_RPC)]) == 1
    attempt = store.last_forward_stage_attempt
    # The ONLY resolution handle: the batch status RPC with this exact id.
    status = {"batch_id": attempt["batch_id"], "tenant_id": "lasso",
              "state": "staged", "request_digest": attempt["request_digest"],
              "member_row_ids": [r["id"] for r in staged_rows],
              "observation_row_ids": [], "old_row_ids": [],
              "finalize_receipt": None}
    store._http = _HTTP(posts={pcs._BATCH_STATUS_RPC: _Resp(200, status)})
    resolved = store.resolve_forward_stage_attempt()
    assert resolved["state"] == "staged"


def test_changed_request_holds_same_batch_without_per_row_work(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    http, _ = _armed_http(_group_rows(),
                          stage_resp=_err("23514", "batch request digest changed"))
    with pytest.raises(pcs.ReservationHoldError):
        _store(http).insert_rows("lasso", _group_rows())
    assert not [c for c in http.calls if c[0] == "delete"]
    assert not [c for c in http.calls if c[1].endswith(pcs._RESERVE_RPC)]
    assert not [c for c in http.calls if c[1].endswith(pcs._FINALIZE_RPC)]


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
        return super().insert_rows(base_key, rows, expected_old_rows=expected_old_rows, **kwargs)


@pytest.mark.parametrize("failure,unknown", [
    (pcs.ReservationHoldError(409, "request digest changed"), False),
    (pcs.ReservationStagingError(502, "stage preparation refused"), False),
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


def test_apply_armed_reports_preparing_batch_never_replacement(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    store = _AtomicApplyStore()
    result = cmr._apply("gritx", _apply_rows(with_proofs=True),
                        date(2026, 10, 20), 1, store, lambda msg: None)
    assert result["ok"] is True
    assert result["state"] == "preparing"
    assert result["batch_id"] == store.last_forward_stage["batch_id"]
    assert store.old == store.before, "old approved/published rows untouched"
    assert store.deleted == []
    assert result["upserted"] == 0 and result["deleted"] == 0
    assert result["staged"] == 3


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


def test_apply_preparing_receipt_keeps_drive_media_landed(monkeypatch):
    """inserted=0 with a durable staged/preparing receipt is LANDED for Drive
    media ownership: the outer cleanup must not roll this build's picks back."""
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    store = _AtomicApplyStore()
    result = cmr._apply("gritx", _apply_rows(with_proofs=True),
                        date(2026, 10, 20), 1, store, lambda msg: None)
    assert result["inserted"] == 0
    assert cmr._forward_staged_batch_landed(result) is True
    assert result["forward_batch_attempt"]["batch_id"] == result["batch_id"]
    assert result["forward_batch_attempt"]["tenant_id"] == "gritx"
    # Failure/unknown/no-op results are never "landed".
    assert cmr._forward_staged_batch_landed({"ok": False, "inserted": 0}) is False
    assert cmr._forward_staged_batch_landed(
        {"ok": True, "inserted": 0, "state": "preparing"}) is False
    assert cmr._forward_staged_batch_landed(None) is False


def test_staged_batch_media_release_requires_guarded_terminal_proof():
    """Drive media owned by a staged batch is released ONLY after a bound
    readback proves a guarded terminal cancel/failure. Staged, finalized,
    mismatched and unreadable readbacks all fail closed."""
    attempt = {"batch_id": str(uuid.uuid4()), "tenant_id": "gritx",
               "request_digest": "d" * 64, "member_row_ids": [ROW_ID],
               "old_row_ids": []}

    class _StatusStore:
        def __init__(self, payload=None, exc=None):
            self.payload = payload
            self.exc = exc
            self.calls = []

        def forward_schedule_batch_status(self, batch_id, **bindings):
            self.calls.append((batch_id, bindings))
            if self.exc is not None:
                raise self.exc
            return self.payload

    staged = {"batch_id": attempt["batch_id"], "tenant_id": "gritx",
              "state": "staged", "request_digest": "d" * 64,
              "member_row_ids": [ROW_ID], "observation_row_ids": [],
              "old_row_ids": [], "finalize_receipt": None}
    assert cmr.staged_batch_media_release_allowed(
        _StatusStore(payload=staged), attempt) is False
    finalized = dict(staged, state="finalized", finalize_receipt={
        "batch_id": attempt["batch_id"], "state": "finalized",
        "tenant_id": "gritx", "request_digest": "d" * 64,
        "row_ids": [ROW_ID], "reservation_ids": [RID],
        "archived_old_row_ids": []})
    assert cmr.staged_batch_media_release_allowed(
        _StatusStore(payload=finalized), attempt) is False
    cancelled = dict(staged, state="cancelled")
    assert cmr.staged_batch_media_release_allowed(
        _StatusStore(payload=cancelled), attempt) is True
    # Transport failure / unknown outcome: never a release.
    assert cmr.staged_batch_media_release_allowed(
        _StatusStore(exc=pcs.ReservationStoreError(0, "transport")), attempt) is False
    # No attempt identity: never a release.
    assert cmr.staged_batch_media_release_allowed(_StatusStore(payload=cancelled), {}) is False
    # The release check always binds the readback to the exact attempt identity.
    store = _StatusStore(payload=cancelled)
    cmr.staged_batch_media_release_allowed(store, attempt)
    _, bindings = store.calls[0]
    assert bindings == {"tenant_id": "gritx", "request_digest": "d" * 64,
                        "member_row_ids": [ROW_ID], "old_row_ids": []}


def test_apply_unknown_outcome_carries_full_attempt_identity(monkeypatch):
    """After a lost stage response the apply result carries the complete
    attempt handle (batch id + tenant + digest) for the bound readback, and
    no Drive rollback is authorized (insert_outcome_unknown)."""
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    store = _AtomicApplyStore(pcs.ReservationStoreError(0, "receipt lost"))
    store.last_forward_stage_attempt = None
    result = cmr._apply("gritx", _apply_rows(with_proofs=True),
                        date(2026, 10, 20), 1, store, lambda msg: None)
    assert result["ok"] is False
    assert result["insert_outcome_unknown"] is True
    assert cmr._forward_staged_batch_landed(result) is False
    assert store.old == store.before, "staged cleanup never touches old visible rows"


def test_apply_log_failure_after_stage_attempt_still_reports_unknown_outcome(monkeypatch):
    """Reproduced P1: model a committed stage whose HTTP response was lost,
    then make log() raise OSError inside _apply's exception handler. The
    logger must not be able to lose the attempt/unknown-outcome state:
    _apply still returns the guarded result and marks the armed stage attempt
    on apply_state, so the outer cleanup can never read this as prewrite."""
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    store = _AtomicApplyStore(pcs.ReservationStoreError(0, "receipt lost"))

    def broken_log(msg):
        raise OSError("log sink unavailable")

    apply_state = {"result": None}
    result = cmr._apply("gritx", _apply_rows(with_proofs=True),
                        date(2026, 10, 20), 1, store, broken_log,
                        apply_state=apply_state)
    assert result["ok"] is False
    assert result["insert_outcome_unknown"] is True
    assert cmr._forward_staged_batch_landed(result) is False
    assert apply_state["stage_rpc_attempted"] is True
    assert store.old == store.before
    assert store.deleted == []


class _FinallyStore:
    """Minimal store for build_client_month's outer finally ownership tests."""

    def __init__(self):
        self.last_forward_stage_attempt = None

    def list_month(self, base_key, month):
        return []


class _Draft:
    source_media_asset_id = "asset-1"
    account_key = "gritx_ig"
    day_key = "2026-10-20"
    is_story = False


def _drive_to_finally(monkeypatch, body):
    monkeypatch.setenv("AGENT_CLIENT_MONTH", "true")
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    monkeypatch.setattr(cmr, "_client_media_count", lambda path: 1)
    monkeypatch.setattr(cmr, "_build_client_month_body", body)
    calls = {"rollback": [], "released": [], "restored": 0}
    monkeypatch.setattr(cmr, "_rollback_new_drive_drafts",
                        lambda drafts, log: calls["rollback"].append(list(drafts)))
    monkeypatch.setattr(cmr, "_release_unlanded_reservations",
                        lambda drafts, retained=(): calls["released"].append(list(drafts)))
    monkeypatch.setattr(cmr, "_restore_released_drive_assets",
                        lambda *a, **k: calls.__setitem__("restored", calls["restored"] + 1))
    from types import SimpleNamespace
    account = SimpleNamespace(key="gritx_ig", platform="instagram")
    voice = SimpleNamespace(ctas=[])
    return account, voice, calls


def test_build_finally_preserves_drive_assets_when_stage_attempt_escapes(monkeypatch):
    """Outer finally ownership decision: an armed stage RPC was attempted and
    _apply escaped without a result (the reproduced log-failure shape). The
    cleanup must NOT release Drive assets or reservations -- terminal
    nonexistence/cancellation was never proven by a bound readback."""
    def body(*args, **kwargs):
        kwargs["apply_state"]["stage_rpc_attempted"] = True
        kwargs["drafts"].append(_Draft())
        raise RuntimeError("escaped after the stage attempt")

    account, voice, calls = _drive_to_finally(monkeypatch, body)
    with pytest.raises(RuntimeError):
        cmr.build_client_month(account, "gritx", "2026-10-20", days=1,
                               voice=voice, library_path="/nope",
                               store=_FinallyStore())
    assert calls["rollback"] == [], "no release on an unknown stage outcome"
    assert calls["released"] == []
    assert calls["restored"] == 0


def test_build_finally_prewrite_failure_still_rolls_back(monkeypatch):
    """Legacy behavior preserved: with NO armed stage attempt, an escaping
    build body is still a prewrite planning failure and the unlanded Drive
    picks are returned to the pool exactly as before."""
    def body(*args, **kwargs):
        kwargs["drafts"].append(_Draft())
        raise RuntimeError("prewrite planning failure")

    account, voice, calls = _drive_to_finally(monkeypatch, body)
    with pytest.raises(RuntimeError):
        cmr.build_client_month(account, "gritx", "2026-10-20", days=1,
                               voice=voice, library_path="/nope",
                               store=_FinallyStore())
    assert len(calls["rollback"]) == 1
    assert len(calls["released"]) == 1
    assert calls["restored"] == 1


@pytest.mark.parametrize("gbp_formats", [(), ("update",)])
@pytest.mark.parametrize("retained_ordinal", [None, 0, 1])
def test_atomic_old_manifest_matches_partial_day_slot_and_gbp_preservation(
        gbp_formats, retained_ordinal):
    store = _AtomicApplyStore()
    original = store.old[0]
    retained_slot = retained_ordinal or 0
    open_slot = 1 - retained_slot
    replaceable = [dict(original, id=str(uuid.uuid4()), account=account, format=fmt,
                        slot_index=open_slot)
                   for account, fmt in (("instagram", "feed"), ("facebook", "feed"))]
    protected = [dict(row, id=str(uuid.uuid4()), slot_index=retained_ordinal)
                 for row in replaceable]
    gbp = [dict(original, id=str(uuid.uuid4()), account="googlebusiness", format=fmt,
                slot_index=None) for fmt in ("update", "photo")]
    whole_locked = dict(original, id=str(uuid.uuid4()), post_date="2026-10-21")
    store.old = replaceable + protected + gbp + [whole_locked]
    rows = cmr._forward_replacement_rows(
        store, "gritx", ["2026-10"], DAY, "2026-10-21", {"2026-10-21"},
        preserve_slots={(DAY, retained_slot)}, preserve_gbp={DAY: gbp_formats})
    assert rows == replaceable + [row for row in gbp if row["format"] in gbp_formats]


def test_armed_apply_freezes_open_partial_day_slot_without_deleting(monkeypatch):
    from agent import cadence
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    monkeypatch.setattr(cadence, 'resolve_posts_per_day', lambda *a, **k: 2)
    store = _AtomicApplyStore()
    stale = dict(store.old[0], slot_index=1)
    retained = dict(stale, id=str(uuid.uuid4()), slot_index=0)
    store.old = [stale, retained]
    store.before = deepcopy(store.old)
    def delete_month(*args, preserve_slots=(), preserve_gbp=None, **kwargs):
        pytest.fail("armed staging must not delete")
    store.delete_month = delete_month
    incoming = [dict(row, slot_index=1) for row in _apply_rows(with_proofs=True)]
    result = cmr._apply("gritx", incoming, date(2026, 10, 20), 1, store,
                        lambda msg: None, locked_days={DAY})
    assert result["ok"] is True and result["state"] == "preparing"
    assert store.expected_old_rows == [stale]
    assert store.old == store.before
    assert store.deleted == []
