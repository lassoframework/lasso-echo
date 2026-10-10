"""Offline tests for agent.historical_gbp_original_recovery and its
media_guard.swap_original_identity integration. All stores and byte readers
are fakes; no network, no database. Generic fixtures only -- no real gym ids,
row ids, URLs or hashes."""
import hashlib
import io
import json
import uuid

import pytest
from PIL import Image

from agent import historical_gbp_original_recovery as hgr
from agent import media_guard

ORIGIN = "https://cdn.example.test"
GYM = "gym-test"
SLUG = "gym-test"
HOLD = "cross_date_media_repeat_needs_new_visual"


def _jpeg(width, height, color):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _url(slug, data, name):
    return f"{ORIGIN}/echo/{slug}/{hashlib.sha1(data).hexdigest()[:16]}/{name}"


@pytest.fixture
def case():
    raw = _jpeg(1600, 1200, (10, 120, 200))
    delivered = hgr.render_gbp_crop(raw)
    assert raw != delivered
    source_url = _url(SLUG, raw, "raw.jpg")
    delivered_url = _url(SLUG, delivered, "delivered.jpg")
    target_id, hist_id = str(uuid.uuid4()), str(uuid.uuid4())
    target = {
        "id": target_id, "gym_id": GYM, "account": "googlebusiness",
        "format": "update", "post_date": "2026-09-01", "status": "pending",
        "caption": "hello", "image_url": delivered_url, "thumbnail_url": None,
        "source_media_url": None, "source_media_asset_id": None,
        "drive_file_id": None, "variant_status": "active",
        "media_not_ready_reason": HOLD, "byte_hash": None, "r2_key": None,
        "logical_post_id": None, "published_at": None,
        "publish_claim_token": None, "late_post_id": None,
        "approval_kind": None, "approved_by": None, "approved_at": None,
        "approval_digest": None, "scheduled_at": None,
    }
    historical = dict(target, id=hist_id, account="instagram", format="feed",
                      image_url=source_url, source_media_url=source_url,
                      media_not_ready_reason=None)
    binding = {
        "gym_id": GYM, "row_id": target_id, "historical_row_id": hist_id,
        "tenant_slug": SLUG, "source_origin": ORIGIN, "source_url": source_url,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "delivered_sha256": hashlib.sha256(delivered).hexdigest(),
        "recipe": hgr.RECIPE,
        "expected_before": hgr.row_snapshot(target),
        "historical_snapshot": hgr.row_snapshot(historical),
    }
    return {"raw": raw, "delivered": delivered, "source_url": source_url,
            "delivered_url": delivered_url, "target": target,
            "historical": historical, "binding": binding}


class _Resp:
    def __init__(self, status_code, data):
        self.status_code, self._data = status_code, data

    def json(self):
        return self._data


class _Client:
    def __init__(self, store):
        self.store = store

    def get(self, url, params=None, headers=None, timeout=None):
        assert url.endswith("/content_calendar")
        rid = params["id"][3:]
        gym = params["gym_id"][3:]
        rows = [r for r in self.store.rows.values()
                if str(r["id"]) == rid and str(r["gym_id"]) == gym]
        return _Resp(200, rows)

    def post(self, url, headers=None, json=None, timeout=None):
        fn = url.rsplit("/rpc/", 1)[-1]
        handler = self.store.rpc.get(fn)
        assert handler, fn
        return handler(json)


class FakeStore:
    def __init__(self, rows=(), rpc=None):
        self.rows = {str(r["id"]): dict(r) for r in rows}
        self.rpc = rpc or {}

    def _client(self):
        return _Client(self)

    def _rest(self, name):
        return f"https://db.test/rest/v1/{name}"

    def _headers(self, extra=None):
        return dict(extra or {})


def _read_rpc(binding=None, receipt=None, fail=None):
    def handler(payload):
        if fail:
            return fail
        if binding is None:
            return _Resp(200, None)
        return _Resp(200, {"binding": dict(binding), "receipt": receipt})
    return handler


def _reader(mapping):
    def read(url):
        if url not in mapping:
            raise ValueError(f"unexpected fetch {url}")
        return mapping[url]
    return read


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv(hgr.FLAG_ENV, "true")
    monkeypatch.setenv(hgr.GYMS_ENV, f"other-gym,{GYM}")
    yield


def _store(case, receipt=None):
    return FakeStore(rows=[case["target"], case["historical"]],
                     rpc={"historical_gbp_original_recovery_read":
                          _read_rpc(case["binding"], receipt)})


def _bytes(case):
    return _reader({case["source_url"]: case["raw"],
                    case["delivered_url"]: case["delivered"]})


# --- flags / gates -----------------------------------------------------------

def test_disabled_by_default(case, monkeypatch):
    monkeypatch.delenv(hgr.FLAG_ENV)
    with pytest.raises(hgr.RecoveryUnavailable):
        hgr.observe(_store(case), GYM, case["target"], read_bytes=_bytes(case))


def test_gym_allowlist_required(case, monkeypatch):
    monkeypatch.setenv(hgr.GYMS_ENV, "someone-else")
    with pytest.raises(hgr.RecoveryUnavailable):
        hgr.observe(_store(case), GYM, case["target"], read_bytes=_bytes(case))


def test_no_binding_fails_closed(case):
    store = FakeStore(rows=[case["target"]],
                      rpc={"historical_gbp_original_recovery_read": _read_rpc(None)})
    with pytest.raises(hgr.RecoveryUnavailable):
        hgr.observe(store, GYM, case["target"], read_bytes=_bytes(case))


# --- URL shape ---------------------------------------------------------------

def test_hosted_url_shape(case):
    seg = hgr.validate_hosted_url(case["source_url"], ORIGIN, SLUG)
    assert seg == hashlib.sha1(case["raw"]).hexdigest()[:16]
    ig = _url(SLUG + "_ig", case["raw"], "raw.jpg")
    assert hgr.validate_hosted_url(ig, ORIGIN, SLUG) is not None


@pytest.mark.parametrize("url", [
    "http://cdn.example.test/echo/gym-test/0123456789abcdef/x.jpg",   # not https
    "https://evil.test/echo/gym-test/0123456789abcdef/x.jpg",         # wrong host
    "https://cdn.example.test.evil.test/echo/gym-test/0123456789abcdef/x.jpg",
    "https://user@cdn.example.test/echo/gym-test/0123456789abcdef/x.jpg",
    "https://cdn.example.test/echo/gym-test/0123456789abcdef/x.jpg?token=1",
    "https://cdn.example.test/echo/gym-test/0123456789abcdef/x.jpg#f",
    "https://cdn.example.test/echo/gym-test/0123456789abcdef/../x.jpg",
    "https://cdn.example.test/echo/gym-test/0123456789abcdef/%2e%2e/x.jpg",
    "https://cdn.example.test/echo/other-gym/0123456789abcdef/x.jpg",  # cross tenant
    "https://cdn.example.test/echo/gym-test-extra/0123456789abcdef/x.jpg",
    "https://cdn.example.test/echo/gym-test/xyz/x.jpg",                # bad segment
    "https://cdn.example.test/echo/gym-test/0123456789abcdef/",        # no name
    "https://cdn.example.test/echo/gym-test/0123456789abcdef/a/b.jpg",
])
def test_hosted_url_rejected(url):
    assert hgr.validate_hosted_url(url, ORIGIN, SLUG) is None


# --- observe verification ----------------------------------------------------

def test_observe_success_builds_proof(case):
    obs = hgr.observe(_store(case), GYM, case["target"], read_bytes=_bytes(case))
    assert obs.proof == hgr.proof_for(case["binding"])
    assert obs.receipt is None
    assert len(obs.fingerprint) == 64
    assert obs.before["media_not_ready_reason"] == HOLD


@pytest.mark.parametrize("mutate", [
    lambda c: c["target"].update(status="approved"),
    lambda c: c["target"].update(media_not_ready_reason=None),
    lambda c: c["target"].update(source_media_asset_id="asset-1"),
    lambda c: c["target"].update(published_at="2026-09-02T00:00:00+00:00"),
    lambda c: c["target"].update(account="instagram"),
    lambda c: c["target"].update(caption="edited"),
])
def test_observe_target_drift_refused(case, mutate):
    mutate(case)
    with pytest.raises(hgr.RecoveryError):
        hgr.observe(_store(case), GYM, case["target"], read_bytes=_bytes(case))


def test_observe_cross_tenant_historical_refused(case):
    case["historical"]["gym_id"] = "other-gym"
    case["binding"]["historical_snapshot"] = hgr.row_snapshot(case["historical"])
    with pytest.raises(hgr.RecoveryVerificationError):
        hgr.observe(_store(case), GYM, case["target"], read_bytes=_bytes(case))


def test_observe_historical_not_referencing_original_refused(case):
    case["historical"]["source_media_url"] = None
    case["historical"]["image_url"] = case["delivered_url"]
    case["binding"]["historical_snapshot"] = hgr.row_snapshot(case["historical"])
    with pytest.raises(hgr.RecoveryVerificationError):
        hgr.observe(_store(case), GYM, case["target"], read_bytes=_bytes(case))


def test_observe_wrong_raw_bytes_refused(case):
    forged = _jpeg(1600, 1200, (1, 2, 3))
    reader = _reader({case["source_url"]: forged,
                      case["delivered_url"]: case["delivered"]})
    with pytest.raises(hgr.RecoveryVerificationError):
        hgr.observe(_store(case), GYM, case["target"], read_bytes=reader)


def test_observe_truncated_raw_refused(case):
    reader = _reader({case["source_url"]: case["raw"][:-50],
                      case["delivered_url"]: case["delivered"]})
    with pytest.raises(hgr.RecoveryVerificationError):
        hgr.observe(_store(case), GYM, case["target"], read_bytes=reader)


def test_observe_rerender_mismatch_refused(case):
    other = hgr.render_gbp_crop(_jpeg(1600, 1200, (9, 9, 9)))
    case["binding"]["delivered_sha256"] = hashlib.sha256(other).hexdigest()
    reader = _reader({case["source_url"]: case["raw"],
                      case["delivered_url"]: other})
    with pytest.raises(hgr.RecoveryVerificationError):
        hgr.observe(_store(case), GYM, case["target"], read_bytes=reader)


# --- apply / receipt ----------------------------------------------------------

def _ok_apply(case):
    obs_holder = {}

    def handler(payload):
        receipt = {
            "id": 1, "gym_id": GYM, "row_id": case["target"]["id"],
            "request_id": payload["p_request_id"],
            "request_fingerprint": payload["p_request_fingerprint"],
            "status": "succeeded",
            "before_state": case["binding"]["expected_before"],
            "after_state": dict(case["binding"]["expected_before"],
                                source_media_url=case["source_url"]),
            "binding": {k: v for k, v in case["binding"].items()},
            "proof": payload["p_proof"],
        }
        obs_holder["receipt"] = receipt
        return _Resp(200, receipt)
    return handler, obs_holder


def test_apply_returns_persisted_receipt(case):
    handler, holder = _ok_apply(case)
    store = _store(case)
    def mutate(payload):
        response = handler(payload)
        store.rows[case["target"]["id"]] = dict(response._data["after_state"])
        return response
    store.rpc["historical_gbp_original_recovery_apply"] = mutate
    receipt = hgr.apply(store, GYM, case["target"], "req-1",
                        read_bytes=_bytes(case))
    assert receipt is holder["receipt"]


def test_apply_unknown_transport_reconciles_persisted_receipt(case):
    handler, holder = _ok_apply(case)
    store = _store(case)
    def lost(payload):
        response = handler(payload)
        store.rows[case["target"]["id"]] = dict(response._data["after_state"])
        raise TimeoutError("response lost after commit")
    store.rpc["historical_gbp_original_recovery_apply"] = lost
    store.rpc["historical_gbp_original_recovery_read"] = lambda payload: _Resp(
        200, {"binding": case["binding"], "receipt": holder.get("receipt")})
    got = hgr.apply(store, GYM, case["target"], "req-1", read_bytes=_bytes(case))
    assert got == holder["receipt"]


def test_apply_unknown_transport_without_receipt_raises(case):
    def boom(payload):
        raise TimeoutError()
    store = _store(case)
    store.rpc["historical_gbp_original_recovery_apply"] = \
        lambda payload: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(hgr.RecoveryStoreError):
        hgr.apply(store, GYM, case["target"], "req-1", read_bytes=_bytes(case))


def test_apply_conflicting_receipt_refused(case):
    handler, holder = _ok_apply(case)
    store = _store(case)
    store.rpc["historical_gbp_original_recovery_apply"] = \
        lambda payload: _Resp(409, {"code": "23514", "message": "conflicting reuse"})
    with pytest.raises(hgr.RecoveryVerificationError):
        hgr.apply(store, GYM, case["target"], "req-2", read_bytes=_bytes(case))


# --- media_guard integration ---------------------------------------------------

def _recovered_row(case):
    return dict(case["target"], source_media_url=case["source_url"])


def _receipt_for(case):
    return {
        "id": 1, "gym_id": GYM, "row_id": case["target"]["id"],
        "request_id": "req-1",
        "request_fingerprint": hgr.request_fingerprint(
            GYM, case["target"]["id"], case["binding"]),
        "status": "succeeded",
        "before_state": case["binding"]["expected_before"],
        "after_state": hgr.row_snapshot(_recovered_row(case)),
        "binding": dict(case["binding"]),
        "proof": hgr.proof_for(case["binding"]),
    }


def test_media_guard_accepts_verified_recovery_receipt(case):
    store = _store(case, receipt=_receipt_for(case))
    row = _recovered_row(case)
    identity = media_guard.swap_original_identity(
        GYM, row, store, read_bytes=_bytes(case))
    assert identity == {"sha256": hashlib.sha256(case["raw"]).hexdigest(),
                        "source_asset_id": None,
                        "source_url": case["source_url"]}


def test_media_guard_no_receipt_same_refusal(case):
    store = _store(case)  # binding only, no receipt
    row = _recovered_row(case)
    with pytest.raises(ValueError, match="original lineage missing"):
        media_guard.swap_original_identity(GYM, row, store,
                                           read_bytes=_bytes(case))


def test_media_guard_flag_off_same_refusal(case, monkeypatch):
    monkeypatch.delenv(hgr.FLAG_ENV)
    store = _store(case, receipt=_receipt_for(case))
    with pytest.raises(ValueError, match="original lineage missing"):
        media_guard.swap_original_identity(GYM, _recovered_row(case), store,
                                           read_bytes=_bytes(case))


def test_media_guard_forged_receipt_after_state_refused(case):
    receipt = _receipt_for(case)
    receipt["after_state"]["caption"] = "forged"
    store = _store(case, receipt=receipt)
    with pytest.raises(hgr.RecoveryVerificationError):
        media_guard.swap_original_identity(GYM, _recovered_row(case), store,
                                           read_bytes=_bytes(case))


def test_media_guard_cross_tenant_receipt_refused(case):
    receipt = _receipt_for(case)
    receipt["gym_id"] = "other-gym"
    store = _store(case, receipt=receipt)
    with pytest.raises(hgr.RecoveryVerificationError):
        media_guard.swap_original_identity(GYM, _recovered_row(case), store,
                                           read_bytes=_bytes(case))


def test_media_guard_wrong_row_receipt_refused(case):
    receipt = _receipt_for(case)
    receipt["row_id"] = str(uuid.uuid4())
    store = _store(case, receipt=receipt)
    with pytest.raises(hgr.RecoveryVerificationError):
        media_guard.swap_original_identity(GYM, _recovered_row(case), store,
                                           read_bytes=_bytes(case))


def test_media_guard_recovered_bytes_still_reverified(case):
    forged = _jpeg(1600, 1200, (50, 50, 50))
    store = _store(case, receipt=_receipt_for(case))
    reader = _reader({case["source_url"]: forged,
                      case["delivered_url"]: case["delivered"]})
    with pytest.raises(hgr.RecoveryVerificationError):
        media_guard.swap_original_identity(GYM, _recovered_row(case), store,
                                           read_bytes=reader)


def test_snapshot_preserves_future_columns_and_value_types():
    row = {"id": "x", "approval_kind": None, "scheduled_at": None,
           "future": {"bool": False, "count": 2}}
    assert hgr.row_snapshot(row) == row
    assert hgr.row_snapshot(row)["future"]["bool"] is False


@pytest.mark.parametrize("field", ["approval_kind", "approved_by", "approved_at", "approval_digest", "late_post_id"])
def test_approval_or_provider_state_refuses_even_when_binding_pins_it(case, field):
    case["target"][field] = "unproved"
    case["binding"]["expected_before"] = hgr.row_snapshot(case["target"])
    with pytest.raises(hgr.RecoveryUnavailable):
        hgr.observe(_store(case), GYM, case["target"], read_bytes=_bytes(case))


def test_supplied_observation_cannot_bypass_fresh_bytes(case):
    store = _store(case)
    obs = hgr.observe(store, GYM, case["target"], read_bytes=_bytes(case))
    store.rpc["historical_gbp_original_recovery_apply"] = lambda _: pytest.fail("write attempted")
    with pytest.raises(hgr.RecoveryVerificationError):
        hgr.apply(store, GYM, case["target"], "req-1", observation=obs,
                  read_bytes=lambda _: b"drifted")


def test_same_version_future_column_drift_refuses(case):
    case["target"]["future_column"] = "changed"
    with pytest.raises(hgr.RecoveryVerificationError):
        hgr.observe(_store(case), GYM, case["target"], read_bytes=_bytes(case))


def test_canonical_tenant_slug_refuses_operator_misconfiguration(case):
    case["binding"]["tenant_slug"] = "other"
    with pytest.raises(hgr.RecoveryVerificationError, match="slug"):
        hgr.observe(_store(case), GYM, case["target"], read_bytes=_bytes(case))


@pytest.mark.parametrize('field,value,reason', [
    ('byte_hash', 'sha256:' + 'f' * 64, 'byte identity conflicts'),
    ('r2_key', 'echo/other/0123456789abcdef/wrong.jpg', 'object key conflicts'),
])
def test_recovery_does_not_skip_existing_object_metadata_guards(case, field, value, reason):
    case['target'][field] = value
    case['binding']['expected_before'] = hgr.row_snapshot(case['target'])
    receipt = _receipt_for(case)
    with pytest.raises(ValueError, match=reason):
        media_guard.swap_original_identity(GYM, _recovered_row(case),
            _store(case, receipt=receipt), read_bytes=_bytes(case))


def test_operator_cli_observe_is_wired_and_does_not_apply(case, monkeypatch, capsys):
    from agent import portal_calendar_store as pcs
    store = _store(case)
    monkeypatch.setattr(pcs, 'SupabaseCalendarStore', lambda: store)
    monkeypatch.setattr(hgr, '_default_reader', _bytes(case))
    assert hgr.main(['--gym', GYM, '--row-id', case['target']['id'], '--observe']) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['proof']['source_sha256'] == case['binding']['source_sha256']


def test_read_only_reconcile_checks_exact_request_and_history(case):
    row = _recovered_row(case)
    store = _store(case, receipt=_receipt_for(case))
    store.rows[row['id']] = row
    assert hgr.reconcile(store, GYM, row['id'], 'req-1', read_bytes=_bytes(case))['status'] == 'succeeded'
    with pytest.raises(hgr.RecoveryVerificationError):
        hgr.reconcile(store, GYM, row['id'], 'different-request', read_bytes=_bytes(case))
    store.rows[case['historical']['id']]['caption'] = 'same row changed'
    with pytest.raises(hgr.RecoveryVerificationError):
        hgr.reconcile(store, GYM, row['id'], 'req-1', read_bytes=_bytes(case))
