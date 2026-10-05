"""Typed SupabaseCalendarStore wrappers for the v3 receipt RPCs: service-role
scoped arguments, strict response/error parsing, fail closed on anything
unexpected. Offline: an injectable http stub stands in for PostgREST."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_calendar_store as pcs  # noqa: E402

GYM = "zanshin"
AID = "act-1"
FP = "a" * 64
ROW = "11111111-1111-4111-8111-111111111111"


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _HTTP:
    def __init__(self, resp):
        self.resp = resp
        self.posts = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append((url, json))
        return self.resp


def _store(resp):
    return pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                     service_key="svc-key-secret",
                                     http=_HTTP(resp))


def _receipt(**over):
    base = {"id": 1, "gym_id": GYM, "action_id": AID, "action": "swap-media",
            "row_id": ROW, "actor_id": "actor-1", "request_fingerprint": FP,
            "status": "started", "selected_asset": None,
            "planned_siblings": None, "member_manifest": None,
            "before_state": {"image_url": "https://cdn/old.jpg"},
            "after_state": None, "sibling_outcomes": None, "error": None,
            "response_status": None}
    base.update(over)
    return base


def _begin(store):
    return store.action_receipt_begin(GYM, AID, "swap-media", ROW, "actor-1", FP)


def test_begin_sends_tenant_scoped_args_and_never_a_before_state():
    http_store = _store(_Resp(200, _receipt()))
    out = _begin(http_store)
    url, payload = http_store._http.posts[0]
    assert url.endswith("/rest/v1/rpc/portal_action_receipt_begin")
    assert payload == {"p_gym_id": GYM, "p_action_id": AID,
                       "p_action": "swap-media", "p_row_id": ROW,
                       "p_actor_id": "actor-1", "p_request_fingerprint": FP}
    assert "before_state" not in payload and "p_before_state" not in payload
    assert out["status"] == "started"


def test_conflicting_reuse_maps_to_conflict():
    err = {"code": "23514",
           "message": "conflicting reuse of action_id with a different request binding"}
    with pytest.raises(pcs.ReceiptConflictError):
        _begin(_store(_Resp(409, err)))


def test_null_logical_or_missing_row_maps_to_manual_review_hold():
    err = {"code": "23514",
           "message": "primary row has no logical_post_id; conflicting request "
                      "held for manual review"}
    with pytest.raises(pcs.ReceiptHoldError):
        _begin(_store(_Resp(409, err)))


def test_malformed_selection_maps_to_selection_error():
    err = {"code": "22023",
           "message": "selected asset must be an allowlisted public object identity"}
    store = _store(_Resp(400, err))
    with pytest.raises(pcs.ReceiptSelectionError):
        store.action_receipt_claim(GYM, AID, FP, {"asset_id": "x"}, {})


@pytest.mark.parametrize("payload", [
    _receipt(gym_id="othergym"),            # foreign tenant row
    _receipt(action_id="act-2"),            # different action
    _receipt(request_fingerprint="b" * 64),  # mismatched binding
    _receipt(status="bogus"),               # unknown status
    ["not", "a", "dict"],                   # wrong shape
    ValueError("not json"),                 # unparseable body
])
def test_unexpected_success_payload_fails_closed(payload):
    with pytest.raises(pcs.ReceiptStoreError):
        _begin(_store(_Resp(200, payload)))


def test_http_error_without_sqlstate_fails_closed():
    with pytest.raises(pcs.ReceiptStoreError):
        _begin(_store(_Resp(500, {"code": "XX000", "message": "boom"})))


def test_transport_failure_fails_closed():
    class _Down:
        def post(self, *a, **k):
            raise ConnectionError("socket gone")

    store = pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                      service_key="svc-key-secret", http=_Down())
    with pytest.raises(pcs.ReceiptStoreError):
        _begin(store)


def test_apply_sends_prepared_and_parses_terminal_receipt():
    prepared = {"rows": [{"calendar_row_id": ROW,
                          "media": {"image_url": "https://cdn/new.jpg"}}]}
    store = _store(_Resp(200, _receipt(status="succeeded",
                                       after_state={"image_url": "https://cdn/new.jpg"},
                                       response_status=200)))
    out = store.action_receipt_apply(GYM, AID, FP, prepared)
    url, payload = store._http.posts[0]
    assert url.endswith("/rest/v1/rpc/portal_action_receipt_apply")
    assert payload["p_prepared"] == prepared
    assert payload["p_gym_id"] == GYM
    assert out["status"] == "succeeded"


def test_member_listing_is_tenant_scoped_and_rejects_non_uuid():
    store = _store(_Resp(200, []))
    with pytest.raises(pcs.PortalStoreError):
        store.list_active_logical_post_rows(GYM, "not-a-uuid; drop table")
    assert store._http.posts == []


def test_member_listing_refuses_a_foreign_row():
    store = pcs.SupabaseCalendarStore(url="https://proj.supabase.co",
                                      service_key="svc-key-secret",
                                      http=None)
    rows = _Resp(200, [{"id": ROW, "gym_id": "othergym"}])

    class _GetHTTP:
        def get(self, *a, **k):
            return rows

    store._http = _GetHTTP()
    with pytest.raises(pcs.PortalStoreError):
        store.list_active_logical_post_rows(GYM, ROW)
