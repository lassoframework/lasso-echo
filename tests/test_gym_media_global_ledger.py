"""
DRAFT global visual ledger — photo-first depletion (PR235 package A), offline.

With AGENT_VISUAL_GLOBAL_LEDGER enabled, photos whose exact bytes (MD5) the
global visual ledger shows used by ANOTHER canonical tenant must leave the
pickable photo set BEFORE client_infographic_fill decides the infographic
fallback is allowed. Flag OFF = byte-for-byte legacy behavior; any ledger
uncertainty (read failure, unmapped tenant, ambiguous row, ambiguous flag
value, unkeyable bytes) fails closed.
"""

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent import gym_media_selector as gms  # noqa: E402
from agent import client_infographic_fill as cif  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, make_asset  # noqa: E402

GYM = "gymx"
TENANT = "11111111-2222-3333-4444-555555555555"
OTHER_TENANT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
NOW = "2026-10-03T12:00:00+00:00"
from datetime import datetime, timezone as _tz  # noqa: E402
NOW_DT = datetime(2026, 10, 3, 12, 0, 0, tzinfo=_tz.utc)


def _md5(seed):
    return hashlib.md5(seed.encode("utf-8")).hexdigest()


def _photo(asset_id, seed=None):
    return make_asset(asset_id, gym_id=GYM, kind="photo",
                      content_hash=_md5(seed or asset_id))


class _Resp:
    def __init__(self, rows, status=200, headers=None):
        self._rows = rows
        self.status_code = status
        self.headers = headers or {"Content-Range": f"*/{len(rows)}" if not rows
                                   else f"0-{len(rows) - 1}/{len(rows)}"}

    def json(self):
        return self._rows


class FakeLedgerHttp:
    """Scripted PostgREST for tenant_alias + visual_global_usage."""

    def __init__(self, tenant_rows, usage_rows, fail_on=None):
        self._tenant = tenant_rows
        self._usage = usage_rows
        self._fail_on = fail_on
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if self._fail_on and self._fail_on in url:
            return _Resp({"message": "boom"}, status=500)
        if "tenant_alias" in url:
            return _Resp(list(self._tenant))
        if "visual_global_usage" in url:
            requested = set(params["fingerprint"][4:-1].split(","))
            return _Resp([row for row in self._usage
                          if row["fingerprint"] in requested])
        if "visual_global_historical_incident" in url:
            # This legacy fake models only visual_global_usage; the incident
            # table it never populated reads back empty (proven via */0).
            return _Resp([])
        raise AssertionError(f"unexpected ledger URL: {url}")


def _creds(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-test")


def _usage(fp, tenant, state="published", ambiguous=False):
    return {"fingerprint": fp, "tenant_id": tenant, "state": state,
            "ambiguous": ambiguous}


# ---- unit: cross_client_used_fingerprints -----------------------------------

def test_cross_client_used_fingerprints_returns_every_prior_usage(monkeypatch):
    _creds(monkeypatch)
    fp_used, fp_free = f"md5:{_md5('used')}", f"md5:{_md5('free')}"
    http = FakeLedgerHttp(
        [{"alias_key": GYM, "tenant_id": TENANT}],
        [_usage(fp_used, OTHER_TENANT), _usage(f"md5:{_md5('mine')}", TENANT)])
    used = gms.cross_client_used_fingerprints(
        GYM, [fp_used, fp_free, f"md5:{_md5('mine')}"], http=http)
    assert used == {fp_used, f"md5:{_md5('mine')}"}


@pytest.mark.parametrize("state", ["reserved", "published", "released"])
def test_every_usage_state_counts_as_consumed(monkeypatch, state):
    _creds(monkeypatch)
    fp = f"md5:{_md5('x')}"
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}],
                          [_usage(fp, OTHER_TENANT, state=state)])
    assert gms.cross_client_used_fingerprints(GYM, [fp], http=http) == {fp}


def test_missing_creds_fail_closed(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.cross_client_used_fingerprints(GYM, [f"md5:{_md5('x')}"],
                                           http=FakeLedgerHttp([], []))


def test_read_failure_fails_closed(monkeypatch):
    _creds(monkeypatch)
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}], [],
                          fail_on="visual_global_usage")
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.cross_client_used_fingerprints(GYM, [f"md5:{_md5('x')}"], http=http)


def test_unmapped_tenant_fails_closed(monkeypatch):
    _creds(monkeypatch)
    http = FakeLedgerHttp([], [])
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.cross_client_used_fingerprints(GYM, [f"md5:{_md5('x')}"], http=http)


def test_ambiguous_row_fails_closed(monkeypatch):
    _creds(monkeypatch)
    fp = f"md5:{_md5('x')}"
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}],
                          [_usage(fp, TENANT, ambiguous=True)])
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.cross_client_used_fingerprints(GYM, [fp], http=http)


@pytest.mark.parametrize("tenant, ambiguous", [
    ("not-a-uuid", False), (TENANT, None), (TENANT, "false"), (TENANT, 0),
])
def test_malformed_tenant_or_ambiguous_field_fails_closed(monkeypatch, tenant, ambiguous):
    _creds(monkeypatch)
    fp = f"md5:{_md5('x')}"
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}],
                          [_usage(fp, tenant, ambiguous=ambiguous)])
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.cross_client_used_fingerprints(GYM, [fp], http=http)


def test_malformed_canonical_tenant_uuid_fails_closed(monkeypatch):
    _creds(monkeypatch)
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": "not-a-uuid"}], [])
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.cross_client_used_fingerprints(GYM, [f"md5:{_md5('x')}"], http=http)


def test_malformed_row_fails_closed(monkeypatch):
    _creds(monkeypatch)
    class RogueRowHttp(FakeLedgerHttp):
        def get(self, url, params=None, headers=None, timeout=None):
            response = super().get(url, params, headers, timeout)
            if "visual_global_usage" in url:
                response._rows = [{"fingerprint": "bogus", "tenant_id": TENANT,
                                   "state": "archived", "ambiguous": False}]
                response.headers = {"Content-Range": "0-0/1"}
            return response
    http = RogueRowHttp([{"alias_key": GYM, "tenant_id": TENANT}], [])
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.cross_client_used_fingerprints(GYM, ["md5:" + _md5("x")], http=http)


# ---- pickable integration ----------------------------------------------------

def test_flag_off_is_byte_for_byte_legacy(monkeypatch):
    """Flag unset: the ledger is never consulted and a cross-client-used photo
    stays pickable exactly as before."""
    monkeypatch.delenv(gms.GLOBAL_LEDGER_FLAG_ENV, raising=False)
    photo = _photo("p1")
    store = FakeMediaStore(assets=[photo])

    def _boom(*a, **k):
        raise AssertionError("flag OFF must never touch the global ledger")

    monkeypatch.setattr(gms, "cross_client_used_fingerprints", _boom)
    picks = gms.pickable(GYM, store=store, now=NOW_DT)
    assert [a["id"] for a in picks] == ["p1"]


def test_flag_on_excludes_cross_client_used_photo(monkeypatch):
    _creds(monkeypatch)
    monkeypatch.setenv(gms.GLOBAL_LEDGER_FLAG_ENV, "true")
    used_photo, free_photo = _photo("used", "used"), _photo("free", "free")
    store = FakeMediaStore(assets=[used_photo, free_photo])
    http = FakeLedgerHttp(
        [{"alias_key": GYM, "tenant_id": TENANT}],
        [_usage(f"md5:{_md5('used')}", OTHER_TENANT)])
    picks = gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=http)
    assert [a["id"] for a in picks] == ["free"]


def test_flag_on_same_tenant_history_excludes(monkeypatch):
    """Local counters cannot prove imported or alias history is reusable."""
    _creds(monkeypatch)
    monkeypatch.setenv(gms.GLOBAL_LEDGER_FLAG_ENV, "on")
    photo = _photo("p1")
    store = FakeMediaStore(assets=[photo])
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}],
                          [_usage(f"md5:{_md5('p1')}", TENANT)])
    picks = gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=http)
    assert picks == []


def test_flag_on_ledger_outage_fails_closed(monkeypatch):
    _creds(monkeypatch)
    monkeypatch.setenv(gms.GLOBAL_LEDGER_FLAG_ENV, "true")
    store = FakeMediaStore(assets=[_photo("p1")])
    http = FakeLedgerHttp([], [], fail_on="tenant_alias")
    assert gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=http) == []
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=http,
                     strict_claims=True)


def test_ambiguous_flag_value_fails_closed(monkeypatch):
    monkeypatch.setenv(gms.GLOBAL_LEDGER_FLAG_ENV, "sometimes")
    store = FakeMediaStore(assets=[_photo("p1")])
    assert gms.pickable(GYM, store=store, now=NOW_DT) == []
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.pickable(GYM, store=store, now=NOW_DT, strict_claims=True)


def test_flag_on_photo_without_md5_is_unproven_and_excluded(monkeypatch):
    """A SHA-256-only photo has no global-ledger key, so its cross-client status
    can never be proven: with the flag ON it leaves the pickable photo set."""
    _creds(monkeypatch)
    monkeypatch.setenv(gms.GLOBAL_LEDGER_FLAG_ENV, "true")
    photo = make_asset("sha-only", gym_id=GYM, kind="photo",
                       content_hash=hashlib.sha256(b"sha-only").hexdigest())
    store = FakeMediaStore(assets=[photo])
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}], [])
    assert gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=http) == []
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=http,
                     strict_claims=True)


def test_ledger_read_requires_content_range_proof(monkeypatch):
    _creds(monkeypatch)
    fp = f"md5:{_md5('x')}"
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}], [])
    original_get = http.get

    def missing_range(*args, **kwargs):
        response = original_get(*args, **kwargs)
        response.headers = {}
        return response

    http.get = missing_range
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.cross_client_used_fingerprints(GYM, [fp], http=http)


def test_ledger_reads_fingerprints_in_exact_capped_unique_batches(monkeypatch):
    _creds(monkeypatch)
    fingerprints = [f"md5:{_md5(str(index))}" for index in range(101)]
    usage = [_usage(fingerprints[0], OTHER_TENANT),
             _usage(fingerprints[-1], OTHER_TENANT)]
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}], usage)
    assert gms.cross_client_used_fingerprints(GYM, fingerprints, http=http) == set(usage_row["fingerprint"] for usage_row in usage)
    calls = [call for call in http.calls if "visual_global_usage" in call[0]]
    assert len(calls) == 2
    assert all(call[2]["Range"] == "0-99" and call[2]["Prefer"] == "count=exact"
               for call in calls)
    assert sorted(len(call[1]["fingerprint"][4:-1].split(",")) for call in calls) == [1, 100]


def test_ledger_rejects_incomplete_or_duplicate_batch_rows(monkeypatch):
    _creds(monkeypatch)
    fp = f"md5:{_md5('x')}"
    class BadHttp(FakeLedgerHttp):
        def get(self, url, params=None, headers=None, timeout=None):
            response = super().get(url, params, headers, timeout)
            if "visual_global_usage" in url:
                response._rows = [_usage(fp, OTHER_TENANT), _usage(fp, TENANT)]
                response.headers = {"Content-Range": "0-1/2"}
            return response
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.cross_client_used_fingerprints(
            GYM, [fp], http=BadHttp([{"alias_key": GYM, "tenant_id": TENANT}], []))


def test_missing_ambiguous_field_fails_closed(monkeypatch):
    _creds(monkeypatch)
    fp = f"md5:{_md5('x')}"
    row = _usage(fp, TENANT)
    del row["ambiguous"]
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}], [row])
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.cross_client_used_fingerprints(GYM, [fp], http=http)


def test_ledger_rejects_total_above_bounded_batch(monkeypatch):
    _creds(monkeypatch)
    fingerprints = [f"md5:{_md5(str(index))}" for index in range(100)]

    class OverflowHttp(FakeLedgerHttp):
        def get(self, url, params=None, headers=None, timeout=None):
            response = super().get(url, params, headers, timeout)
            if "visual_global_usage" in url:
                response.headers = {"Content-Range": "0-99/101"}
            return response

    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.cross_client_used_fingerprints(
            GYM, fingerprints,
            http=OverflowHttp([{"alias_key": GYM, "tenant_id": TENANT}], []))


# ---- the infographic fallback decision (photos first) ------------------------

class _ReadyStore(FakeMediaStore):
    def list_sources(self, _base, include_inactive=False):
        return [{"id": "src1", "gym_id": _base, "kind": "gym_drive", "active": True,
                 "revoked_externally": False, "sync_status": "ready",
                 "sync_finished_at": "2026-10-02T00:00:00Z"}]


def _arm_drive_index(monkeypatch, assets):
    from agent import gym_media_index
    monkeypatch.setattr(gym_media_index, "default_store",
                        lambda: _ReadyStore(assets=assets))


def test_depletion_gate_sees_cross_client_used_photo_as_gone(monkeypatch):
    """Flag ON: the gym's only photo is already used by another client, so the
    pool is genuinely depleted and the infographic fallback may proceed."""
    monkeypatch.setenv(gms.GLOBAL_LEDGER_FLAG_ENV, "true")
    _arm_drive_index(monkeypatch, [_photo("p1")])
    monkeypatch.setattr(
        gms, "cross_client_used_fingerprints",
        lambda base, fps, **k: {f"md5:{_md5('p1')}"} if f"md5:{_md5('p1')}" in fps else set())
    assert cif.real_media_depleted(GYM, now=NOW) is True


def test_depletion_gate_holds_when_ledger_evidence_unavailable(monkeypatch):
    """Flag ON + ledger unreadable: FAIL CLOSED. The fallback may NOT conclude
    depletion from an unverifiable pool."""
    monkeypatch.setenv(gms.GLOBAL_LEDGER_FLAG_ENV, "true")
    _arm_drive_index(monkeypatch, [_photo("p1")])

    def _down(*a, **k):
        raise gms.GlobalLedgerUnavailable("ledger down")

    monkeypatch.setattr(gms, "cross_client_used_fingerprints", _down)
    assert cif.real_media_depleted(GYM, now=NOW) is False


def test_depletion_gate_holds_on_ambiguous_flag(monkeypatch):
    monkeypatch.setenv(gms.GLOBAL_LEDGER_FLAG_ENV, "maybe")
    _arm_drive_index(monkeypatch, [_photo("p1")])
    assert cif.real_media_depleted(GYM, now=NOW) is False


def test_depletion_gate_flag_off_ignores_ledger(monkeypatch):
    """Flag OFF: legacy depletion answer, the ledger is never consulted."""
    monkeypatch.delenv(gms.GLOBAL_LEDGER_FLAG_ENV, raising=False)
    _arm_drive_index(monkeypatch, [_photo("p1")])

    def _boom(*a, **k):
        raise AssertionError("flag OFF must never touch the global ledger")

    monkeypatch.setattr(gms, "cross_client_used_fingerprints", _boom)
    assert cif.real_media_depleted(GYM, now=NOW) is False  # photo is usable
    _arm_drive_index(monkeypatch, [])
    assert cif.real_media_depleted(GYM, now=NOW) is True
