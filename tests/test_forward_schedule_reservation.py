"""Offline tests for the forward schedule reservation Python binding (DRAFT,
2026-10-08; AGENT_FORWARD_SCHEDULE_RESERVATION default OFF).

Contract: migrations/DRAFT_fixer_forward_schedule_reservation_20261008.sql and
docs/FORWARD_SCHEDULE_RESERVATION_20261008.md. A fake HTTP client stands in for
PostgREST; no network, no database. Everything fails closed: unknown outcomes,
unreadable gates and malformed responses never count as free or reserved.
"""

import os
import sys
import uuid
from copy import deepcopy

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_calendar_store as pcs
from agent import forward_media_visual_index as visual_index
from agent.forward_media_guard import ForwardMediaVerificationHold

SHA = "a" * 64
SHA_B = "b" * 64
ROW_ID = str(uuid.uuid4())
LPID = str(uuid.uuid4())
RID = str(uuid.uuid4())
ATT_IDS = [str(uuid.uuid4()) for _ in range(3)]
DAY = "2026-10-20"
PROOF = {"source_sha256": SHA, "phash_v1": 123456789,
         "source_media_asset_id": "asset-1"}


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text
        self.headers = {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _HTTP:
    """Routes by URL tail: rpc/<fn> names and the lineage table read."""

    def __init__(self, posts=None, gets=None, delete_payload=None):
        self.calls = []
        self.posts = posts or {}
        self.gets = gets or {}
        self.delete_payload = delete_payload

    def post(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append(("post", url, json))
        tail = url.rsplit("/", 1)[-1]
        handler = self.posts.get(tail)
        if handler is None:
            return _Resp(200, [])
        if callable(handler):
            return handler(json)
        return handler

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("get", url, params))
        if (params or {}).get("media_not_ready_reason") == "not.is.null":
            response = _Resp(200, [])
            response.headers = {"Content-Range": "*/0"}
            return response
        tail = url.rsplit("/", 1)[-1]
        handler = self.gets.get(tail)
        if handler is None:
            return _Resp(200, [])
        if callable(handler):
            return handler(params)
        return handler

    def delete(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("delete", url, params))
        return _Resp(200, self.delete_payload or [])


def _store(http):
    return pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                     service_key="svc-key-secret", http=http)


def _err(code, message="x"):
    return _Resp(409, {"code": code, "message": message})


# ---- flag tri-state -----------------------------------------------------------

def test_flag_tri_state(monkeypatch):
    monkeypatch.delenv(pcs.FORWARD_RESERVATION_FLAG_ENV, raising=False)
    assert pcs.forward_reservation_flag() is False
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    assert pcs.forward_reservation_flag() is True
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "off")
    assert pcs.forward_reservation_flag() is False
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "maybe")
    assert pcs.forward_reservation_flag() is None


# ---- screening proof validation ----------------------------------------------

def test_valid_reservation_proof():
    assert pcs.valid_reservation_proof(PROOF) is True
    assert pcs.valid_reservation_proof(None) is False
    assert pcs.valid_reservation_proof({}) is False
    assert pcs.valid_reservation_proof(
        {**PROOF, "source_sha256": "zz"}) is False
    assert pcs.valid_reservation_proof({**PROOF, "phash_v1": None}) is False
    assert pcs.valid_reservation_proof({**PROOF, "phash_v1": "123"}) is False
    assert pcs.valid_reservation_proof(
        {**PROOF, "source_media_asset_id": "  "}) is False


# ---- RPC wrappers --------------------------------------------------------------

def test_reserve_happy_path():
    http = _HTTP(posts={pcs._RESERVE_RPC: _Resp(200, RID)})
    rid = _store(http).reserve_forward_slot(ROW_ID, LPID, "rev-md5", ATT_IDS)
    assert rid == RID
    args = http.calls[0][2]
    assert args == {"p_calendar_row_id": ROW_ID, "p_logical_post_id": LPID,
                    "p_expected_revision": "rev-md5", "p_attestation_ids": ATT_IDS}


def test_reserve_rejects_bad_args_without_http():
    store = _store(_HTTP())
    with pytest.raises(pcs.ReservationArgumentError):
        store.reserve_forward_slot("not-a-uuid", LPID, "rev", ATT_IDS)
    with pytest.raises(pcs.ReservationArgumentError):
        store.reserve_forward_slot(ROW_ID, LPID, "", ATT_IDS)
    with pytest.raises(pcs.ReservationArgumentError):
        store.reserve_forward_slot(ROW_ID, LPID, "rev", ATT_IDS[:2])


@pytest.mark.parametrize("code,exc", [
    ("23514", pcs.ReservationHoldError),
    ("23505", pcs.ReservationHoldError),
    ("22023", pcs.ReservationArgumentError),
    ("55000", pcs.ReservationGateError),
    ("25000", pcs.ReservationStoreError),
    ("PGRST", pcs.ReservationStoreError),
])
def test_rpc_sqlstate_mapping(code, exc):
    http = _HTTP(posts={pcs._RESERVE_RPC: _err(code)})
    with pytest.raises(exc):
        _store(http).reserve_forward_slot(ROW_ID, LPID, "rev", ATT_IDS)


def test_reserve_malformed_response_is_unknown():
    http = _HTTP(posts={pcs._RESERVE_RPC: _Resp(200, {"not": "a-uuid"})})
    with pytest.raises(pcs.ReservationStoreError):
        _store(http).reserve_forward_slot(ROW_ID, LPID, "rev", ATT_IDS)


def test_release_literal_true_only():
    assert _store(_HTTP(posts={pcs._RELEASE_RPC: _Resp(200, True)})).release_forward_slot(
        RID, "superseded") is True
    with pytest.raises(pcs.ReservationStoreError):
        _store(_HTTP(posts={pcs._RELEASE_RPC: _Resp(200, "true")})).release_forward_slot(
            RID, "superseded")
    with pytest.raises(pcs.ReservationArgumentError):
        _store(_HTTP()).release_forward_slot(RID, "  ")


def test_revoke_returns_count():
    http = _HTTP(posts={pcs._REVOKE_RPC: _Resp(200, 2)})
    assert _store(http).revoke_source_reservations("tenant", SHA, "late negative") == 2
    assert http.calls[0][2] == {"p_tenant_id": "tenant", "p_source_sha256": SHA,
                                "p_reason": "late negative"}
    with pytest.raises(pcs.ReservationStoreError):
        _store(_HTTP(posts={pcs._REVOKE_RPC: _Resp(200, -1)})).revoke_source_reservations(
            "tenant", SHA, "x")


def test_check_conflicts_shape():
    body = {"allowed": False,
            "conflicts": [{"kind": "active_reservation", "reservation_id": RID}]}
    http = _HTTP(posts={pcs._CHECK_RPC: _Resp(200, body)})
    out = _store(http).check_reservation_conflicts(
        "tenant", post_date=DAY, logical_post_id=LPID,
        source_sha256=SHA, phash_v1=7)
    assert out == body
    with pytest.raises(pcs.ReservationStoreError):
        _store(_HTTP(posts={pcs._CHECK_RPC: _Resp(
            200, {"allowed": True, "conflicts": [{"kind": "bogus"}]})})
            ).check_reservation_conflicts(
                "tenant", post_date=DAY, logical_post_id=LPID,
                source_sha256=SHA, phash_v1=7)
    with pytest.raises(pcs.ReservationArgumentError):
        _store(_HTTP()).check_reservation_conflicts(
            "tenant", post_date=DAY, logical_post_id=LPID,
            source_sha256=SHA, phash_v1=None)


def test_forward_reservation_proof_strict():
    proof = {"reservation_id": RID, "tenant_id": "tenant", "post_date": DAY,
             "logical_post_id": LPID, "source_sha256": SHA,
             "row_revision": "rev", "attestation_ids": ATT_IDS}
    http = _HTTP(posts={pcs._PROOF_RPC: _Resp(200, proof)})
    assert _store(http).forward_reservation_proof(ROW_ID, SHA) == proof
    foreign = dict(proof, source_sha256=SHA_B)
    with pytest.raises(pcs.ReservationStoreError):
        _store(_HTTP(posts={pcs._PROOF_RPC: _Resp(200, foreign)})
               ).forward_reservation_proof(ROW_ID, SHA)
    with pytest.raises(pcs.ReservationHoldError):
        _store(_HTTP(posts={pcs._PROOF_RPC: _err("23514")})
               ).forward_reservation_proof(ROW_ID, SHA)


# ---- insert_rows gate + staging ------------------------------------------------

def _group_rows():
    base = {"post_date": DAY, "status": "pending", "image_url": "https://x/y.jpg",
            "logical_post_id": LPID, pcs.RESERVATION_PROOF: PROOF}
    return [dict(base, account="instagram", format="feed", caption="one"),
            dict(base, account="facebook", format="feed", caption="two"),
            dict(base, account="instagram", format="story", caption="")]


def _inserted(rows):
    return [dict(r, id=str(uuid.uuid4()), gym_id="lasso")
            for r in ({k: v for k, v in r.items()
                       if k != pcs.RESERVATION_PROOF} for r in rows)]


def test_insert_rows_ambiguous_flag_refuses_before_any_write(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "bogus")
    http = _HTTP()
    with pytest.raises(pcs.CalendarInsertNotStartedError):
        _store(http).insert_rows("lasso", _group_rows())
    assert not [c for c in http.calls if c[0] == "post"], "no POST may happen"


def test_insert_rows_armed_requires_logical_post_and_proof(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    http = _HTTP()
    rows = _group_rows()
    for row in rows:
        del row[pcs.RESERVATION_PROOF]
    with pytest.raises(pcs.CalendarInsertNotStartedError):
        _store(http).insert_rows("lasso", rows)
    assert not [c for c in http.calls if c[0] == "post"]
    rows = _group_rows()
    for row in rows:
        del row["logical_post_id"]
    # proof-without-logical-post fails even earlier (ValueError in the clean
    # loop); both are definite refusals before any write.
    with pytest.raises((ValueError, pcs.CalendarInsertNotStartedError)):
        _store(http).insert_rows("lasso", rows)
    assert not [c for c in http.calls if c[0] == "post"]


def test_insert_rows_invalid_proof_refuses_even_when_flag_off(monkeypatch):
    monkeypatch.delenv(pcs.FORWARD_RESERVATION_FLAG_ENV, raising=False)
    rows = _group_rows()
    for row in rows:
        row[pcs.RESERVATION_PROOF] = {"source_sha256": "zz"}
    with pytest.raises(ValueError):
        _store(_HTTP()).insert_rows("lasso", rows)


def _stage_members(args):
    import json
    request = json.loads(args["p_request"])
    return request["members"]


def _stage_old_rows(args):
    import json
    request = json.loads(args["p_request"])
    return request.get("old_rows", [])


def _stage_receipt(args):
    members = _stage_members(args)
    return _Resp(200, {
        "batch_id": args["p_batch_id"], "tenant_id": args["p_tenant_id"],
        "request_digest": args["p_request_digest"], "state": "staged",
        "member_row_ids": [m["row"]["id"] for m in members],
        "observation_row_ids": [m["row"]["id"] for m in members if m.get("observation")],
        "old_row_ids": [o["id"] for o in _stage_old_rows(args)],
        "finalize_receipt": None})


def _armed_http(rows, *, stage_resp=None):
    staged = {}
    def stage(args):
        staged.update(args)
        if stage_resp is not None:
            return stage_resp(args) if callable(stage_resp) else stage_resp
        return _stage_receipt(args)
    return _HTTP(posts={pcs._STAGE_RPC: stage}), staged


def _arm_attester(monkeypatch):
    monkeypatch.setattr(visual_index, "enabled", lambda: True)
    monkeypatch.setattr(visual_index, "attest", lambda *a, **k: {
        "attestation_ids": dict(zip(visual_index.ROLES, ATT_IDS))})


def test_insert_rows_armed_stages_one_atomic_preparing_batch(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    rows = _group_rows()
    http, staged = _armed_http(rows)
    store = _store(http)
    out = store.insert_rows("lasso", rows)
    assert len(out) == 3
    posts = [c for c in http.calls if c[0] == "post"]
    stage_calls = [c for c in posts if c[1].endswith(pcs._STAGE_RPC)]
    assert len(stage_calls) == 1, "one atomic stage RPC for the entire batch"
    assert not [c for c in posts if c[1].endswith("content_calendar")], \
        "the planner never writes content_calendar directly when armed"
    assert not [c for c in posts if c[1].endswith((pcs._RESERVE_RPC, pcs._FINALIZE_RPC))], \
        "staging never reserves or finalizes inline"
    args = stage_calls[0][2]
    assert args["p_tenant_id"] == "lasso"
    members = _stage_members(args)
    assert len(members) == 3
    import hashlib
    assert args["p_request_digest"] == hashlib.sha256(
        args["p_request"].encode("utf-8")).hexdigest(), \
        "the persisted digest covers the exact request bytes"
    for member in members:
        row = member["row"]
        assert row["logical_post_id"] == LPID
        # The planner sends UNMARKED rows; the SQL authority stamps the
        # inactive candidate identity itself.
        assert row.get("media_not_ready_reason") is None
        assert row.get("variant_status") in (None, "candidate")
        assert pcs.RESERVATION_PROOF not in row
        assert "observation" not in row
    # The returned rows are STAGED INACTIVE candidates, never an active
    # replacement, and the staged receipt is the only outcome handle.
    assert all(row["variant_status"] == "candidate" for row in out)
    assert all(row["media_not_ready_reason"] == "forward_reservation_staged" for row in out)
    receipt = store.last_forward_stage
    assert receipt["state"] == "staged"
    assert receipt["batch_id"] == args["p_batch_id"]
    assert receipt["member_row_ids"] == [m["row"]["id"] for m in members]
    assert receipt["finalize_receipt"] is None


def test_insert_rows_armed_retry_replays_same_batch_identity(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    rows = _group_rows()
    attempts = []
    def stage(args):
        attempts.append(args)
        return _stage_receipt(args)
    first = _store(_HTTP(posts={pcs._STAGE_RPC: stage})).insert_rows("lasso", _group_rows())
    second = _store(_HTTP(posts={pcs._STAGE_RPC: stage})).insert_rows("lasso", _group_rows())
    assert len(attempts) == 2
    assert attempts[0]["p_batch_id"] == attempts[1]["p_batch_id"], \
        "an identical retry after a lost response resumes the same batch"
    assert attempts[0]["p_request_digest"] == attempts[1]["p_request_digest"]
    assert attempts[0]["p_request"] == attempts[1]["p_request"], \
        "deterministic row identity -- no random ids on the armed lane"
    assert first == second


def test_insert_rows_armed_changed_request_is_a_different_batch(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    attempts = []
    def stage(args):
        attempts.append(args)
        return _stage_receipt(args)
    store = _store(_HTTP(posts={pcs._STAGE_RPC: stage}))
    store.insert_rows("lasso", _group_rows())
    changed = _group_rows()
    changed[0]["caption"] = "a different caption"
    store.insert_rows("lasso", changed)
    assert len(attempts) == 2
    assert attempts[0]["p_request_digest"] != attempts[1]["p_request_digest"]
    assert attempts[0]["p_batch_id"] != attempts[1]["p_batch_id"]


def test_insert_rows_armed_stage_hold_is_definite_and_writes_nothing(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    rows = _group_rows()
    http, _ = _armed_http(rows, stage_resp=_err("23514", "slot conflict"))
    with pytest.raises(pcs.ReservationHoldError):
        _store(http).insert_rows("lasso", rows)
    assert not [c for c in http.calls if c[0] == "delete"]
    assert not [c for c in http.calls
                if c[0] == "post" and c[1].endswith("content_calendar")]


def test_insert_rows_armed_changed_digest_same_batch_holds(monkeypatch):
    """The SQL authority refuses a reused batch id carrying a new digest."""
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    seen = {}
    def stage(args):
        prior = seen.setdefault(args["p_batch_id"], args["p_request_digest"])
        if prior != args["p_request_digest"]:
            return _err("23514", "stage batch request changed")
        return _stage_receipt(args)
    http = _HTTP(posts={pcs._STAGE_RPC: stage})
    _store(http).insert_rows("lasso", _group_rows())
    first_batch_id = [c for c in http.calls
                      if c[1].endswith(pcs._STAGE_RPC)][0][2]["p_batch_id"]
    changed = _group_rows()
    changed[1]["caption"] = "changed after the first attempt"
    # A changed request derives a new batch id; force the collision the SQL
    # guard exists for by pinning the identity helper to the first batch id.
    monkeypatch.setattr(pcs, "forward_batch_identity",
                        lambda tenant, digest: first_batch_id)
    with pytest.raises(pcs.ReservationHoldError):
        _store(http).insert_rows("lasso", changed)


def test_insert_rows_flag_off_legacy_path_unchanged(monkeypatch):
    monkeypatch.delenv(pcs.FORWARD_RESERVATION_FLAG_ENV, raising=False)
    rows = [dict(post_date=DAY, account="instagram", format="feed",
                 caption="legacy", status="pending", image_url="https://x/y.jpg")]
    http = _HTTP(posts={"content_calendar": _Resp(
        201, [dict(rows[0], id=str(uuid.uuid4()), gym_id="lasso")])})
    out = _store(http).insert_rows("lasso", rows)
    assert len(out) == 1
    assert not [c for c in http.calls
                if c[0] == "post" and "/rpc/" in c[1]], "no reservation RPCs"


# ---- cross-logical-post reuse denial (contract correction 2026-10-08) ---------

def test_insert_rows_armed_refuses_cross_lpid_same_date_same_source(monkeypatch):
    """Same tenant/date is NOT reuse authority: the same source bytes staged
    under two different logical posts on one date refuse before any write."""
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    other_lpid = str(uuid.uuid4())
    rows = _group_rows() + [
        dict(post_date=DAY, account="instagram", format="feed", caption="other",
             status="pending", image_url="https://x/y.jpg", time_slot="pm",
             logical_post_id=other_lpid, **{pcs.RESERVATION_PROOF: PROOF})]
    http = _HTTP()
    with pytest.raises(pcs.CalendarInsertNotStartedError):
        _store(http).insert_rows("lasso", rows)
    assert not [c for c in http.calls if c[0] == "post"], "no POST may happen"


def test_insert_rows_armed_receipt_mismatch_is_unknown_never_readback(monkeypatch):
    """A malformed stage receipt never justifies cleanup, calendar readback or
    success inference; the batch attempt is the only resolution handle."""
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    rows = _group_rows()
    bad = _Resp(200, {"batch_id": "x", "state": "staged"})
    http, _ = _armed_http(rows, stage_resp=bad)
    store = _store(http)
    with pytest.raises(pcs.ReservationStoreError):
        store.insert_rows("lasso", rows)
    assert not [c for c in http.calls if c[0] == "delete"]
    stage_at = next(i for i, c in enumerate(http.calls) if c[1].endswith(pcs._STAGE_RPC))
    assert not [c for c in http.calls[stage_at:] if c[0] == "get"], \
        "no calendar readback after the ambiguous response may infer the outcome"
    attempt = store.last_forward_stage_attempt
    assert attempt and attempt["batch_id"] and attempt["request_digest"]
    assert store.last_forward_stage is None


def test_insert_rows_armed_finalized_replay_is_not_claimed_as_staged(monkeypatch):
    """A replay that lands on an already-finalized batch is NOT this lane's
    outcome: never report staged rows from it; resolve via the status RPC."""
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    rows = _group_rows()
    def finalized(args):
        receipt = _stage_receipt(args)._payload
        receipt["state"] = "finalized"
        receipt["finalize_receipt"] = {
            "batch_id": args["p_batch_id"], "state": "finalized",
            "row_ids": receipt["member_row_ids"],
            "reservation_ids": [RID] * len(receipt["member_row_ids"]),
            "archived_old_row_ids": []}
        return _Resp(200, receipt)
    http, _ = _armed_http(rows, stage_resp=finalized)
    store = _store(http)
    with pytest.raises(pcs.ReservationStoreError):
        store.insert_rows("lasso", rows)
    assert store.last_forward_stage is None


def test_insert_rows_armed_siblings_share_one_batch(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    rows = _group_rows()
    http, _ = _armed_http(rows)
    out = _store(http).insert_rows("lasso", rows)
    assert len(out) == 3
    stage_calls = [c for c in http.calls
                   if c[0] == "post" and c[1].endswith(pcs._STAGE_RPC)]
    assert len(stage_calls) == 1
    members = _stage_members(stage_calls[0][2])
    assert {m["row"]["logical_post_id"] for m in members} == {LPID}


def _observation_for(row, tenant="lasso"):
    import hashlib
    import json
    obs = {"schema_version": 1, "provenance_status": "unverified", "tenant": tenant,
           "source_asset_id": row["source_media_asset_id"],
           "source_exact_url": row["source_media_url"],
           "delivered_exact_url": row["image_url"],
           "recipe": {"op": "copy"}, "hold_reasons": [],
           "source_sha256": SHA, "delivered_sha256": SHA_B,
           "source_byte_length": 100, "delivered_byte_length": 100}
    encoded = json.dumps(obs, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    obs["observation_digest"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return obs


def test_insert_rows_armed_stages_observations_atomically(monkeypatch):
    """Unverified observations ride the SAME atomic stage request; the row
    payload stays clean and the receipt reports exact observed membership."""
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    monkeypatch.setenv("AGENT_FORWARD_MEDIA_OBSERVATION_BRIDGE", "1")
    rows = _group_rows()
    for row in rows:
        row["source_media_asset_id"] = "asset-1"
        row["source_media_url"] = "https://x/source.jpg"
        row["_forward_media_observations"] = [_observation_for(row)]
    http, _ = _armed_http(rows)
    http.posts["fixer_forward_media_observation_bridge_ready_20261007"] = _Resp(200, True)
    store = _store(http)
    out = store.insert_rows("lasso", rows)
    assert len(out) == 3
    stage_calls = [c for c in http.calls if c[1].endswith(pcs._STAGE_RPC)]
    assert len(stage_calls) == 1
    members = _stage_members(stage_calls[0][2])
    assert all(member.get("observation") for member in members), \
        "every observed row's packet rides the same atomic request"
    for member in members:
        assert "observation" not in member["row"], \
            "the observation packet is never a calendar column"
        packet = member["observation"]
        assert packet["observation_json"] and packet["digest_input"]
    receipt = store.last_forward_stage
    assert receipt["observation_row_ids"] == receipt["member_row_ids"]
    # Retry stability: identical observations keep the same batch identity.
    attempts = []
    def stage(args):
        attempts.append(args)
        return _stage_receipt(args)
    http2 = _HTTP(posts={pcs._STAGE_RPC: stage,
                         "fixer_forward_media_observation_bridge_ready_20261007": _Resp(200, True)})
    _store(http2).insert_rows("lasso", [dict(r) for r in rows])
    assert attempts[0]["p_request_digest"] == stage_calls[0][2]["p_request_digest"]


# ---- batch status + preparation eligibility bindings ----------------------------

def test_forward_schedule_batch_status_strict():
    batch_id = str(uuid.uuid4())
    staged = {"batch_id": batch_id, "tenant_id": "lasso", "state": "staged",
              "request_digest": "c" * 64, "member_row_ids": [ROW_ID],
              "observation_row_ids": [ROW_ID], "old_row_ids": [RID],
              "finalize_receipt": None}
    http = _HTTP(posts={pcs._BATCH_STATUS_RPC: _Resp(200, staged)})
    assert _store(http).forward_schedule_batch_status(batch_id) == staged
    finalized = dict(staged, state="finalized", finalize_receipt={
        "batch_id": batch_id, "state": "finalized", "tenant_id": "lasso",
        "request_digest": "c" * 64,
        "row_ids": [ROW_ID], "reservation_ids": [RID],
        "archived_old_row_ids": [RID]})
    store = _store(_HTTP(posts={pcs._BATCH_STATUS_RPC: _Resp(200, finalized)}))
    assert store.forward_schedule_batch_status(batch_id)["state"] == "finalized"
    # Finalized without its complete terminal proof is malformed: unknown.
    for broken in (dict(staged, state="finalized", finalize_receipt=None),
                   dict(finalized, finalize_receipt={
                       **finalized["finalize_receipt"], "tenant_id": "other"}),
                   dict(finalized, finalize_receipt={
                       **finalized["finalize_receipt"], "request_digest": "d" * 64}),
                   dict(finalized, finalize_receipt={
                       k: v for k, v in finalized["finalize_receipt"].items()
                       if k != "archived_old_row_ids"}),
                   dict(finalized, finalize_receipt={
                       **finalized["finalize_receipt"],
                       "archived_old_row_ids": []}),
                   dict(finalized, finalize_receipt={
                       **finalized["finalize_receipt"],
                       "row_ids": [ROW_ID, ROW_ID],
                       "reservation_ids": [RID, str(uuid.uuid4())]}),
                   dict(finalized, finalize_receipt={
                       **finalized["finalize_receipt"],
                       "archived_old_row_ids": [RID, RID]}),
                   dict(staged, member_row_ids=[ROW_ID, ROW_ID]),
                   dict(staged, observation_row_ids=[ROW_ID, ROW_ID]),
                   dict(staged, old_row_ids=[RID, RID])):
        store = _store(_HTTP(posts={pcs._BATCH_STATUS_RPC: _Resp(200, broken)}))
        with pytest.raises(pcs.ReservationStoreError):
            store.forward_schedule_batch_status(batch_id)
    # An observation row outside the membership is malformed.
    bogus = dict(staged, observation_row_ids=[str(uuid.uuid4())])
    store = _store(_HTTP(posts={pcs._BATCH_STATUS_RPC: _Resp(200, bogus)}))
    with pytest.raises(pcs.ReservationStoreError):
        store.forward_schedule_batch_status(batch_id)
    # An old row overlapping the membership is malformed.
    bogus = dict(staged, old_row_ids=[ROW_ID])
    store = _store(_HTTP(posts={pcs._BATCH_STATUS_RPC: _Resp(200, bogus)}))
    with pytest.raises(pcs.ReservationStoreError):
        store.forward_schedule_batch_status(batch_id)
    # Missing tenant or digest binding is malformed.
    for missing in ("tenant_id", "request_digest", "old_row_ids"):
        bogus = {k: v for k, v in staged.items() if k != missing}
        store = _store(_HTTP(posts={pcs._BATCH_STATUS_RPC: _Resp(200, bogus)}))
        with pytest.raises(pcs.ReservationStoreError):
            store.forward_schedule_batch_status(batch_id)
    foreign = dict(staged, batch_id=str(uuid.uuid4()))
    store = _store(_HTTP(posts={pcs._BATCH_STATUS_RPC: _Resp(200, foreign)}))
    with pytest.raises(pcs.ReservationStoreError):
        store.forward_schedule_batch_status(batch_id)
    # A batch that does not exist is a definite SQL hold, never a success.
    store = _store(_HTTP(posts={pcs._BATCH_STATUS_RPC: _err("23514", "schedule batch unavailable")}))
    with pytest.raises(pcs.ReservationHoldError):
        store.forward_schedule_batch_status(batch_id)


def test_batch_status_bound_readback_rejects_mismatched_identity():
    """Ambiguous recovery resolves ONLY through a readback bound to the exact
    batch id, tenant, request digest, member set and old-row set."""
    batch_id = str(uuid.uuid4())
    old_id = str(uuid.uuid4())
    staged = {"batch_id": batch_id, "tenant_id": "lasso", "state": "staged",
              "request_digest": "c" * 64, "member_row_ids": [ROW_ID],
              "observation_row_ids": [], "old_row_ids": [old_id],
              "finalize_receipt": None}
    store = _store(_HTTP(posts={pcs._BATCH_STATUS_RPC: _Resp(200, staged)}))
    bound = dict(tenant_id="lasso", request_digest="c" * 64,
                 member_row_ids=[ROW_ID], old_row_ids=[old_id])
    assert store.forward_schedule_batch_status(batch_id, **bound) == staged
    for mismatch in (dict(bound, tenant_id="other"),
                     dict(bound, request_digest="d" * 64),
                     dict(bound, member_row_ids=[str(uuid.uuid4())]),
                     dict(bound, member_row_ids=[ROW_ID, str(uuid.uuid4())]),
                     dict(bound, old_row_ids=[]),
                     dict(bound, old_row_ids=[str(uuid.uuid4())])):
        with pytest.raises(pcs.ReservationStoreError):
            store.forward_schedule_batch_status(batch_id, **mismatch)


def test_resolve_forward_stage_attempt_uses_exact_bound_identity():
    batch_id = str(uuid.uuid4())
    old_id = str(uuid.uuid4())
    staged = {"batch_id": batch_id, "tenant_id": "lasso", "state": "staged",
              "request_digest": "c" * 64, "member_row_ids": [ROW_ID],
              "observation_row_ids": [], "old_row_ids": [old_id],
              "finalize_receipt": None}
    store = _store(_HTTP(posts={pcs._BATCH_STATUS_RPC: _Resp(200, staged)}))
    store.last_forward_stage_attempt = {
        "batch_id": batch_id, "tenant_id": "lasso", "request_digest": "c" * 64,
        "member_row_ids": [ROW_ID], "old_row_ids": [old_id]}
    assert store.resolve_forward_stage_attempt() == staged
    store.last_forward_stage_attempt["request_digest"] = "d" * 64
    with pytest.raises(pcs.ReservationStoreError):
        store.resolve_forward_stage_attempt()
    store.last_forward_stage_attempt = None
    with pytest.raises(pcs.ReservationStoreError):
        store.resolve_forward_stage_attempt()


def test_forward_preparation_eligible_strict():
    eligible = {"eligible": True, "mode": "staged", "tenant_id": "lasso",
                "batch_id": RID, "reason": None}
    http = _HTTP(posts={pcs._PREPARATION_ELIGIBLE_RPC: _Resp(200, eligible)})
    assert _store(http).forward_preparation_eligible(ROW_ID) == eligible
    assert http.calls[0][2] == {"p_calendar_row_id": ROW_ID}
    ineligible = {"eligible": False, "mode": None, "tenant_id": "lasso",
                  "batch_id": None, "reason": "unregistered staged row"}
    http = _HTTP(posts={pcs._PREPARATION_ELIGIBLE_RPC: _Resp(200, ineligible)})
    assert _store(http).forward_preparation_eligible(ROW_ID)["eligible"] is False
    # Anything incoherent fails closed.
    for bogus in (True, "true", None, [],
                  {"eligible": True, "mode": None},
                  {"eligible": False, "mode": "staged"},
                  {"eligible": "yes", "mode": "active"}):
        store = _store(_HTTP(posts={pcs._PREPARATION_ELIGIBLE_RPC: _Resp(200, bogus)}))
        with pytest.raises(pcs.ReservationStoreError):
            store.forward_preparation_eligible(ROW_ID)


# ---- planner advisory screen (gym_media_builder) -------------------------------

class _ScreenStore:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def check_reservation_conflicts(self, tenant, **kwargs):
        self.calls.append((tenant, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.result


def _screen_env(monkeypatch, store):
    from agent import gym_media_builder as gmb
    monkeypatch.setattr(visual_index, "phash_v1", lambda _bytes: 4242)
    monkeypatch.setattr(pcs, "SupabaseCalendarStore", lambda: store)
    return gmb


def test_screen_cross_lpid_same_date_conflict_tries_next_asset(monkeypatch):
    store = _ScreenStore(result={"allowed": False, "conflicts": [
        {"kind": "active_reservation", "reservation_id": RID,
         "tenant_id": "gritx", "post_date": DAY, "distance": 0}]})
    gmb = _screen_env(monkeypatch, store)
    assert gmb._screen_reservation_candidate(
        b"bytes", gym_base="gritx", day_key=DAY, asset_id="a1") == "conflict"
    # The screen passes a FRESH logical_post_id: the own-slot exclusion matches
    # nothing, so a different-lpid same-date reservation is a conflict.
    assert store.calls[0][1]["logical_post_id"] != LPID
    assert store.calls[0][1]["source_sha256"]


def test_screen_clear_candidate_returns_proof(monkeypatch):
    store = _ScreenStore(result={"allowed": True, "conflicts": []})
    gmb = _screen_env(monkeypatch, store)
    proof = gmb._screen_reservation_candidate(
        b"bytes", gym_base="gritx", day_key=DAY, asset_id="a1")
    import hashlib
    assert proof == {"source_sha256": hashlib.sha256(b"bytes").hexdigest(),
                     "phash_v1": 4242, "source_media_asset_id": "a1"}


def test_screen_unknown_outcome_holds(monkeypatch):
    store = _ScreenStore(exc=pcs.ReservationStoreError(0, "transport"))
    gmb = _screen_env(monkeypatch, store)
    assert gmb._screen_reservation_candidate(
        b"bytes", gym_base="gritx", day_key=DAY, asset_id="a1") is None


def test_screen_undecodable_bytes_hold(monkeypatch):
    from agent import gym_media_builder as gmb
    monkeypatch.setattr(visual_index, "phash_v1", lambda _bytes: None)
    assert gmb._screen_reservation_candidate(
        b"junk", gym_base="gritx", day_key=DAY, asset_id="a1") is None


# ---- client_month_run._apply pre-delete gate -----------------------------------

class _ApplyStore:
    def __init__(self):
        self.deleted = []
        self.inserted = []
        self.last_forward_stage = None
        self.last_forward_stage_attempt = None

    def list_month(self, base_key, month):
        return []

    def delete_month(self, base_key, month, preserve_dates=(), return_rows=False):
        self.deleted.append(month)
        return [] if return_rows else 0

    def insert_rows(self, base_key, rows, *, expected_old_rows=None, **kwargs):
        self.expected_old_rows = expected_old_rows
        self.inserted.extend(rows)
        batch_id = pcs.forward_batch_identity(base_key, "d" * 64)
        self.last_forward_stage_attempt = {"batch_id": batch_id,
                                           "tenant_id": base_key,
                                           "request_digest": "d" * 64,
                                           "member_row_ids": [],
                                           "old_row_ids": []}
        self.last_forward_stage = {
            "batch_id": batch_id, "tenant_id": base_key,
            "request_digest": "d" * 64, "state": "staged",
            "member_row_ids": [str(uuid.uuid4()) for _ in rows],
            "observation_row_ids": [], "old_row_ids": [],
            "finalize_receipt": None}
        return rows


def _apply_rows(*, with_proofs):
    rows = _group_rows()
    for row in rows:
        row["gym_id"] = "gritx"
        if not with_proofs:
            row.pop(pcs.RESERVATION_PROOF, None)
    return rows


def test_apply_ambiguous_flag_refuses_before_delete(monkeypatch):
    from datetime import date
    from agent import client_month_run as cmr
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "bogus")
    store = _ApplyStore()
    res = cmr._apply("gritx", _apply_rows(with_proofs=True),
                     date(2026, 10, 20), 1, store, lambda m: None)
    assert res["ok"] is False and "reservation" in res["reason"]
    assert store.deleted == [] and store.inserted == []


def test_apply_armed_refuses_unbound_rows_before_delete(monkeypatch):
    from datetime import date
    from agent import client_month_run as cmr
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    store = _ApplyStore()
    res = cmr._apply("gritx", _apply_rows(with_proofs=False),
                     date(2026, 10, 20), 1, store, lambda m: None)
    assert res["ok"] is False and "reservation" in res["reason"]
    assert res.get("unbound_rows") == 3
    assert store.deleted == [] and store.inserted == []


def test_apply_armed_bound_rows_proceed(monkeypatch):
    from datetime import date
    from agent import client_month_run as cmr
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    store = _ApplyStore()
    res = cmr._apply("gritx", _apply_rows(with_proofs=True),
                     date(2026, 10, 20), 1, store, lambda m: None)
    assert res["ok"] is True, res
    assert store.inserted, "bound rows proceed to the stage"
    assert store.deleted == [], "armed apply never calls delete_month"
    assert store.expected_old_rows == []
    # PREPARING, never an immediate replacement claim.
    assert res["state"] == "preparing" and res["preparing"] is True
    assert res["batch_id"] == store.last_forward_stage["batch_id"]
    assert res["staged"] == len(store.inserted)
    assert res["upserted"] == 0 and res["inserted"] == 0
    assert res["deleted"] == 0 and res["superseded"] == 0


def test_apply_armed_missing_stage_receipt_is_unknown(monkeypatch):
    from datetime import date
    from agent import client_month_run as cmr
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    store = _ApplyStore()
    store.last_forward_stage = None
    original = store.insert_rows
    def no_receipt(base_key, rows, *, expected_old_rows=None, **kwargs):
        out = original(base_key, rows, expected_old_rows=expected_old_rows, **kwargs)
        store.last_forward_stage = None
        return out
    store.insert_rows = no_receipt
    res = cmr._apply("gritx", _apply_rows(with_proofs=True),
                     date(2026, 10, 20), 1, store, lambda m: None)
    assert res["ok"] is False
    assert res["insert_outcome_unknown"] is True
    assert res["forward_batch_attempt"] == store.last_forward_stage_attempt


# ---- before_claim reservation consult ------------------------------------------

class _ClaimStore:
    def __init__(self, http):
        self._http = http

    def _client(self):
        return self._http

    def _rest(self, path):
        return "https://proj.supabase.co/rest/v1/" + path

    def _headers(self, extra=None):
        return dict(extra or {})


def _before_claim_env(monkeypatch, reservation_flag):
    monkeypatch.setenv("AGENT_FORWARD_MEDIA_VISUAL_INDEX", "1")
    if reservation_flag is None:
        monkeypatch.delenv(pcs.FORWARD_RESERVATION_FLAG_ENV, raising=False)
    else:
        monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, reservation_flag)


def test_before_claim_armed_requires_source_bytes(monkeypatch):
    _before_claim_env(monkeypatch, "1")
    visual_proof = {"attestation_ids": ATT_IDS}
    http = _HTTP(posts={"fixer_forward_visual_proof_20261008": _Resp(200, visual_proof)})
    with pytest.raises(ForwardMediaVerificationHold):
        visual_index.before_claim(ROW_ID, "rev", str(uuid.uuid4()),
                                  store=_ClaimStore(http), claim_token=str(uuid.uuid4()))


def test_before_claim_armed_consults_reservation_proof(monkeypatch):
    _before_claim_env(monkeypatch, "1")
    posts = {
        "fixer_forward_visual_proof_20261008": _Resp(200, {"attestation_ids": ATT_IDS, "source_sha256": SHA}),
        pcs._PROOF_RPC: _Resp(200, {
            "reservation_id": RID, "tenant_id": "tenant", "post_date": DAY,
            "logical_post_id": LPID, "source_sha256": SHA,
            "row_revision": "rev", "attestation_ids": ATT_IDS}),
    }
    result = visual_index.before_claim(
        ROW_ID, "rev", str(uuid.uuid4()), store=_ClaimStore(_HTTP(posts=posts)),
        claim_token=str(uuid.uuid4()), source_sha256=SHA)
    assert result["attestation_ids"] == ATT_IDS
    assert result["reservation"]["reservation_id"] == RID


def test_before_claim_armed_missing_reservation_holds(monkeypatch):
    _before_claim_env(monkeypatch, "1")
    posts = {
        "fixer_forward_visual_proof_20261008": _Resp(200, {"attestation_ids": ATT_IDS, "source_sha256": SHA}),
        pcs._PROOF_RPC: _err("23514", "schedule reservation proof unavailable"),
    }
    with pytest.raises(ForwardMediaVerificationHold):
        visual_index.before_claim(ROW_ID, "rev", str(uuid.uuid4()),
                                  store=_ClaimStore(_HTTP(posts=posts)),
                                  claim_token=str(uuid.uuid4()), source_sha256=SHA)


def test_before_claim_flag_off_never_consults_reservation(monkeypatch):
    _before_claim_env(monkeypatch, None)
    posts = {"fixer_forward_visual_proof_20261008": _Resp(200, {"attestation_ids": ATT_IDS})}
    http = _HTTP(posts=posts)
    result = visual_index.before_claim(ROW_ID, "rev", str(uuid.uuid4()),
                                       store=_ClaimStore(http),
                                       claim_token=str(uuid.uuid4()))
    assert result == {"attestation_ids": ATT_IDS}
    assert not [c for c in http.calls if pcs._PROOF_RPC in c[1]]


# ---- frozen old-row snapshot in the staged request (phase 3) -------------------

def _old_rows():
    return [{"id": str(uuid.uuid4()), "gym_id": "lasso", "post_date": DAY,
             "account": "instagram", "format": "feed", "status": "pending",
             "variant_status": "active", "caption": "old",
             "image_url": "https://x/old.jpg", "retained_null": None}]


def test_stage_request_binds_frozen_old_row_snapshot(monkeypatch):
    """The exact old-row snapshot (every column, NULLs included) is frozen ONCE
    at stage time, rides the request, and is covered by the immutable digest.
    The finalizer never refreezes it."""
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    old = _old_rows()
    http, staged = _armed_http(_group_rows())
    store = _store(http)
    store.insert_rows("lasso", _group_rows(), expected_old_rows=old)
    args = staged
    import hashlib
    import json
    request = json.loads(args["p_request"])
    assert request["old_rows"][0]["id"] == old[0]["id"]
    assert "retained_null" in request["old_rows"][0], "NULL columns survive"
    assert args["p_request_digest"] == hashlib.sha256(
        args["p_request"].encode("utf-8")).hexdigest()
    attempt = store.last_forward_stage_attempt
    assert attempt["old_row_ids"] == [old[0]["id"]]
    assert attempt["tenant_id"] == "lasso"
    assert attempt["member_row_ids"]
    # Freezing is a snapshot: mutating the caller's manifest afterwards can
    # never change what this batch is bound to.
    old[0]["caption"] = "mutated after staging"
    old.clear()
    assert json.loads(args["p_request"])["old_rows"][0]["caption"] == "old"
    # The receipt must echo the exact frozen old-row set.
    assert store.last_forward_stage["old_row_ids"] == [request["old_rows"][0]["id"]]


def test_stage_old_row_change_is_a_different_batch(monkeypatch):
    """A changed old-row manifest changes the digest and the batch identity --
    a retry can never substitute a different replacement set under one batch."""
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    attempts = []
    def stage(args):
        attempts.append(args)
        return _stage_receipt(args)
    store = _store(_HTTP(posts={pcs._STAGE_RPC: stage}))
    old = _old_rows()
    store.insert_rows("lasso", _group_rows(), expected_old_rows=deepcopy(old))
    changed = deepcopy(old)
    changed[0]["caption"] = "old row changed after staging"
    store.insert_rows("lasso", _group_rows(), expected_old_rows=changed)
    assert attempts[0]["p_request_digest"] != attempts[1]["p_request_digest"]
    assert attempts[0]["p_batch_id"] != attempts[1]["p_batch_id"]
    same = _store(_HTTP(posts={pcs._STAGE_RPC: stage}))
    same.insert_rows("lasso", _group_rows(), expected_old_rows=deepcopy(old))
    # An identical retry (same content AND same old snapshot) replays the batch.
    assert attempts[2]["p_batch_id"] == attempts[0]["p_batch_id"]


def test_stage_receipt_old_row_mismatch_is_unknown(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    def wrong_old(args):
        receipt = _stage_receipt(args)._payload
        receipt["old_row_ids"] = [str(uuid.uuid4())]
        return _Resp(200, receipt)
    old = _old_rows()
    http, _ = _armed_http(_group_rows(), stage_resp=wrong_old)
    store = _store(http)
    with pytest.raises(pcs.ReservationStoreError):
        store.insert_rows("lasso", _group_rows(), expected_old_rows=old)
    assert store.last_forward_stage is None
    assert store.last_forward_stage_attempt["old_row_ids"] == [old[0]["id"]]
    assert not [c for c in http.calls if c[0] == "delete"]


def test_stage_refuses_foreign_or_overlapping_old_rows(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    http, _ = _armed_http(_group_rows())
    foreign = dict(_old_rows()[0], gym_id="other-gym")
    with pytest.raises(pcs.CalendarInsertNotStartedError):
        _store(http).insert_rows("lasso", _group_rows(), expected_old_rows=[foreign])
    assert not [c for c in http.calls if c[1].endswith(pcs._STAGE_RPC)]
    dup = _old_rows()
    dup.append(dict(dup[0]))
    with pytest.raises(pcs.CalendarInsertNotStartedError):
        _store(http).insert_rows("lasso", _group_rows(), expected_old_rows=dup)
    assert not [c for c in http.calls if c[1].endswith(pcs._STAGE_RPC)]


def test_armed_lane_never_reaches_visual_prepare_or_owner_dsn(monkeypatch):
    """Credential isolation: even with the global visual writer flag ON, the
    armed planner lane must not run _prepare_visual_row (the trusted owner
    receipt DSN boundary). It packages unverified observations only."""
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    def forbidden(*args, **kwargs):
        raise AssertionError("planner reached the trusted visual writer boundary")
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_prepare_visual_row", forbidden)
    from agent import visual_writer_prepare
    monkeypatch.setattr(visual_writer_prepare, "prepare", forbidden)
    from agent import visual_owner_receipts
    monkeypatch.setattr(visual_owner_receipts, "default_writer", forbidden)
    monkeypatch.setattr(visual_owner_receipts, "default_same_object_writer", forbidden)
    http, _ = _armed_http(_group_rows())
    out = _store(http).insert_rows("lasso", _group_rows(), expected_old_rows=_old_rows())
    assert len(out) == 3
    assert all(r["variant_status"] == "candidate" for r in out)


def test_lost_finalize_response_resolves_only_via_bound_status_readback():
    """A lost finalize HTTP response is unknown; resolution requires the batch
    status readback bound to the exact batch id, tenant, digest, member set and
    old-row set, with complete terminal proof."""
    batch_id = str(uuid.uuid4())
    old_id = str(uuid.uuid4())
    candidates = [{"calendar_row_id": ROW_ID, "logical_post_id": LPID,
                   "expected_revision": "rev", "attestation_ids": ATT_IDS}]
    old = [{"id": old_id, "gym_id": "lasso", "status": "pending",
            "variant_status": "active", "caption": "old"}]
    def lost(args):
        raise TimeoutError("finalize receipt lost")
    store = _store(_HTTP(posts={pcs._FINALIZE_RPC: lost}))
    with pytest.raises(pcs.ReservationStoreError):
        store.finalize_forward_schedule_batch("lasso", batch_id, candidates, old)
    finalized = {"batch_id": batch_id, "tenant_id": "lasso", "state": "finalized",
                 "request_digest": "e" * 64, "member_row_ids": [ROW_ID],
                 "observation_row_ids": [], "old_row_ids": [old_id],
                 "finalize_receipt": {
                     "batch_id": batch_id, "state": "finalized",
                     "tenant_id": "lasso", "request_digest": "e" * 64,
                     "row_ids": [ROW_ID], "reservation_ids": [RID],
                     "archived_old_row_ids": [old_id]}}
    store._http = _HTTP(posts={pcs._BATCH_STATUS_RPC: _Resp(200, finalized)})
    resolved = store.forward_schedule_batch_status(
        batch_id, tenant_id="lasso", request_digest="e" * 64,
        member_row_ids=[ROW_ID], old_row_ids=[old_id])
    assert resolved["state"] == "finalized"
    # A mismatched binding (wrong tenant/digest/member/old set) never resolves.
    for binding in (dict(tenant_id="other"), dict(request_digest="f" * 64),
                    dict(member_row_ids=[str(uuid.uuid4())]),
                    dict(old_row_ids=[str(uuid.uuid4())])):
        kwargs = dict(tenant_id="lasso", request_digest="e" * 64,
                      member_row_ids=[ROW_ID], old_row_ids=[old_id])
        kwargs.update(binding)
        with pytest.raises(pcs.ReservationStoreError):
            store.forward_schedule_batch_status(batch_id, **kwargs)
