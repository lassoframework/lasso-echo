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


def _armed_http(rows, *, reserve_resp=None):
    inserted = []
    def stage(payload):
        inserted[:] = [dict(row) for row in payload]
        return _Resp(201, inserted)
    def finalize(args):
        if reserve_resp is not None:
            return reserve_resp(args) if callable(reserve_resp) else reserve_resp
        return _Resp(200, {"row_ids": [c["calendar_row_id"] for c in args["p_candidates"]],
                           "reservation_ids": [RID] * len(args["p_candidates"])})
    posts = {"content_calendar": stage,
             pcs._SNAPSHOT_RPC: _Resp(200, {"revision": "rev-1"}),
             pcs._FINALIZE_RPC: finalize}
    lineage = [{"evidence_id": str(uuid.uuid4())}]
    return http_with(posts, lineage, inserted), inserted


class http_with(_HTTP):
    def __init__(self, posts, lineage, deleted):
        super().__init__(posts=posts,
                         gets={pcs._LINEAGE_TABLE: _Resp(200, lineage)},
                         delete_payload=deleted)


def _arm_attester(monkeypatch):
    monkeypatch.setattr(visual_index, "enabled", lambda: True)
    monkeypatch.setattr(visual_index, "attest", lambda *a, **k: {
        "attestation_ids": dict(zip(visual_index.ROLES, ATT_IDS))})


def test_insert_rows_armed_stages_reservation(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    _arm_attester(monkeypatch)
    rows = _group_rows()
    http, inserted = _armed_http(rows)
    out = _store(http).insert_rows("lasso", rows)
    assert len(out) == 3
    posts = [c for c in http.calls if c[0] == "post"]
    finalizations = [c for c in posts if c[1].endswith(pcs._FINALIZE_RPC)]
    assert len(finalizations) == 1, "one atomic finalization for the entire batch"
    assert not [c for c in posts if c[1].endswith(pcs._RESERVE_RPC)]
    candidates = finalizations[0][2]["p_candidates"]
    assert len(candidates) == 3
    for candidate in candidates:
        assert candidate["logical_post_id"] == LPID
        assert candidate["expected_revision"] == "rev-1"
        assert len(candidate["attestation_ids"]) == 3
    assert all(row["variant_status"] == "active" for row in out)
    # The proof metadata never reaches content_calendar.
    calendar_post = [c for c in posts if c[1].endswith("content_calendar")][0]
    assert all(pcs.RESERVATION_PROOF not in row for row in calendar_post[2])


def test_insert_rows_armed_finalize_hold_preserves_old_and_inactive_rows(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    _arm_attester(monkeypatch)
    rows = _group_rows()
    http, inserted = _armed_http(rows, reserve_resp=_err("23514", "slot conflict"))
    with pytest.raises(pcs.ReservationHoldError):
        _store(http).insert_rows("lasso", rows)
    assert not [c for c in http.calls if c[0] == "delete"]
    assert all(row["variant_status"] == "candidate" for row in inserted)
    assert all(row["media_not_ready_reason"] == "forward_reservation_staged" for row in inserted)


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


def test_insert_rows_armed_sibling_reservation_mismatch_is_unknown(monkeypatch):
    """An invalid receipt never justifies deleting possibly activated rows."""
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    _arm_attester(monkeypatch)
    rows = _group_rows()
    def mismatch(args):
        return _Resp(200, {"row_ids": [c["calendar_row_id"] for c in args["p_candidates"]],
                           "reservation_ids": [RID, str(uuid.uuid4()), RID]})
    http, inserted = _armed_http(rows, reserve_resp=mismatch)
    with pytest.raises(pcs.ReservationStoreError):
        _store(http).insert_rows("lasso", rows)
    assert not [c for c in http.calls if c[0] == "delete"]


def test_insert_rows_armed_siblings_share_one_atomic_reservation(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, "1")
    _arm_attester(monkeypatch)
    rows = _group_rows()
    http, _inserted_rows = _armed_http(rows)
    out = _store(http).insert_rows("lasso", rows)
    assert len(out) == 3
    finalizations = [c for c in http.calls
                    if c[0] == "post" and c[1].endswith(pcs._FINALIZE_RPC)]
    assert len(finalizations) == 1
    assert {c["logical_post_id"] for c in finalizations[0][2]["p_candidates"]} == {LPID}


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

    def list_month(self, base_key, month):
        return []

    def delete_month(self, base_key, month, preserve_dates=(), return_rows=False):
        self.deleted.append(month)
        return [] if return_rows else 0

    def insert_rows(self, base_key, rows, *, expected_old_rows=None, **kwargs):
        self.expected_old_rows = expected_old_rows
        self.inserted.extend(rows)
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
    assert store.inserted, "bound rows proceed to the insert"
    assert store.deleted == [], "armed apply never calls delete_month"
    assert store.expected_old_rows == []


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
