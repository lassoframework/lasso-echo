"""
PR385 — historical-incident global ledger gate, offline (Worker B tests).

With AGENT_VISUAL_GLOBAL_LEDGER armed, the selector must consult BOTH
public.visual_global_usage AND public.visual_global_historical_incident inside
cross_client_used_fingerprints: bytes consumed only by the incident-first
import (immutable rows keyed by fingerprint with source_state +
byte_evidence) must never be selected. Any ledger state counts as consumed;
any read failure / unreadable row fails closed; usage-table ambiguity still
fails closed; own-tenant same-day incident bytes stay consumed unless a later
calendar claim proves the same logical post; flag OFF is
untouched legacy behavior.
"""

import hashlib
import os
import sys
from datetime import datetime, timezone as _tz

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent import gym_media_selector as gms  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, make_asset  # noqa: E402

GYM = "gymh"
TENANT = "11111111-2222-3333-4444-555555555555"
OTHER_TENANT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
DAY = "2026-10-03"
NOW_DT = datetime(2026, 10, 3, 12, 0, 0, tzinfo=_tz.utc)


def _md5(seed):
    return hashlib.md5(seed.encode("utf-8")).hexdigest()


def _photo(asset_id, seed=None, **over):
    asset = make_asset(asset_id, gym_id=GYM, kind="photo",
                       content_hash=_md5(seed or asset_id))
    asset.update(over)
    return asset


class _Resp:
    def __init__(self, rows, status=200, headers=None):
        self._rows = rows
        self.status_code = status
        self.headers = headers or {
            "Content-Range": f"*/{len(rows)}" if not rows
            else f"0-{len(rows) - 1}/{len(rows)}"}

    def json(self):
        return self._rows


class FakeLedgerHttp:
    """Scripted PostgREST for tenant_alias + visual_global_usage +
    visual_global_historical_incident. Incident rows are filtered by the
    fingerprint `in.(...)` param when present; usage rows likewise."""

    def __init__(self, tenant_rows, usage_rows, incident_rows, fail_on=None):
        self._tenant = tenant_rows
        self._usage = usage_rows
        self._incident = incident_rows
        self._fail_on = fail_on
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if self._fail_on and self._fail_on in url:
            return _Resp({"message": "boom"}, status=500)
        if "tenant_alias" in url:
            return _Resp(list(self._tenant))
        if "visual_global_historical_incident" in url:
            return _Resp(self._filter(self._incident, params))
        if "visual_global_usage" in url:
            return _Resp(self._filter(self._usage, params))
        raise AssertionError(f"unexpected ledger URL: {url}")

    @staticmethod
    def _filter(rows, params):
        fp_param = (params or {}).get("fingerprint")
        if not fp_param:
            return list(rows)
        requested = set(fp_param[4:-1].split(","))
        return [r for r in rows if r["fingerprint"] in requested]


def _creds(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-test")


def _usage(fp, tenant, state="published", ambiguous=False):
    return {"fingerprint": fp, "tenant_id": tenant, "state": state,
            "ambiguous": ambiguous}


def _incident(fp, tenant, source_state="published", used_date=DAY,
              group_key="grp1", source_kind="drive", source_key="file1"):
    return {"fingerprint": fp, "tenant_id": tenant, "group_key": group_key,
            "source_kind": source_kind, "source_key": source_key,
            "source_state": source_state, "used_date": used_date,
            "byte_evidence": [{"url": "https://example/x.jpg", "bytes": 10}]}


def _armed(monkeypatch, extra_env="true"):
    _creds(monkeypatch)
    monkeypatch.setenv(gms.GLOBAL_LEDGER_FLAG_ENV, extra_env)


# ---- unit: cross_client_used_fingerprints reads the incident table -----------

def test_incident_only_consumed_bytes_are_used(monkeypatch):
    """The PR385 defect: bytes recorded ONLY in visual_global_historical_incident
    (no usage row) must come back as globally used."""
    _armed(monkeypatch)
    fp = f"md5:{_md5('used')}"
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}],
                          [], [_incident(fp, OTHER_TENANT)])
    assert gms.cross_client_used_fingerprints(GYM, [fp], http=http) == {fp}
    assert any("visual_global_historical_incident" in c[0] for c in http.calls)


@pytest.mark.parametrize("state", ["reserved", "published", "released"])
def test_every_incident_source_state_counts_as_consumed(monkeypatch, state):
    _armed(monkeypatch)
    fp = f"md5:{_md5('x')}"
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}],
                          [], [_incident(fp, OTHER_TENANT, source_state=state)])
    assert gms.cross_client_used_fingerprints(GYM, [fp], http=http) == {fp}


def test_incident_read_failure_fails_closed(monkeypatch):
    _armed(monkeypatch)
    fp = f"md5:{_md5('x')}"
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}], [],
                          [_incident(fp, OTHER_TENANT)],
                          fail_on="visual_global_historical_incident")
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.cross_client_used_fingerprints(GYM, [fp], http=http)


def test_usage_read_failure_still_fails_closed_with_incident_read(monkeypatch):
    _armed(monkeypatch)
    fp = f"md5:{_md5('x')}"
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}], [],
                          [], fail_on="visual_global_usage")
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.cross_client_used_fingerprints(GYM, [fp], http=http)


@pytest.mark.parametrize("bad", [
    {},                        # row missing the primary-key fingerprint
    {"fingerprint": "bogus"},  # fingerprint outside the requested batch
])
def test_malformed_incident_row_fails_closed(monkeypatch, bad):
    _armed(monkeypatch)
    fp = f"md5:{_md5('x')}"

    class RogueHttp(FakeLedgerHttp):
        def get(self, url, params=None, headers=None, timeout=None):
            response = super().get(url, params, headers, timeout)
            if "visual_global_historical_incident" in url:
                response._rows = [bad]
                response.headers = {"Content-Range": "0-0/1"}
            return response

    http = RogueHttp([{"alias_key": GYM, "tenant_id": TENANT}], [], [])
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.cross_client_used_fingerprints(GYM, [fp], http=http)


def test_incident_read_proves_page_completeness(monkeypatch):
    _armed(monkeypatch)
    fp = f"md5:{_md5('x')}"
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}], [],
                          [_incident(fp, OTHER_TENANT)])
    original_get = http.get

    def missing_range(*args, **kwargs):
        response = original_get(*args, **kwargs)
        if "visual_global_historical_incident" in args[0]:
            response.headers = {}
        return response

    http.get = missing_range
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.cross_client_used_fingerprints(GYM, [fp], http=http)


def test_incident_rows_checked_in_bounded_batches(monkeypatch):
    _armed(monkeypatch)
    fingerprints = [f"md5:{_md5(str(i))}" for i in range(101)]
    incidents = [_incident(fingerprints[0], OTHER_TENANT),
                 _incident(fingerprints[-1], OTHER_TENANT)]
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}],
                          [], incidents)
    assert gms.cross_client_used_fingerprints(GYM, fingerprints, http=http) == \
        {incidents[0]["fingerprint"], incidents[1]["fingerprint"]}
    calls = [c for c in http.calls if "visual_global_historical_incident" in c[0]]
    assert len(calls) == 2
    assert all(c[2]["Range"] == "0-99" and c[2]["Prefer"] == "count=exact"
               for c in calls)


def test_duplicate_incident_rows_same_fingerprint_process_cleanly(monkeypatch):
    """Multiple incident rows per fingerprint (different tenants/dates/states)
    are legitimate history, not corruption: they must not raise, and the
    fingerprint is consumed."""
    _armed(monkeypatch)
    fp = f"md5:{_md5('x')}"
    http = FakeLedgerHttp(
        [{"alias_key": GYM, "tenant_id": TENANT}], [],
        [_incident(fp, OTHER_TENANT, source_state="published", used_date="2026-08-01"),
         _incident(fp, TENANT, source_state="released", used_date="2026-09-01")])
    assert gms.cross_client_used_fingerprints(GYM, [fp], http=http) == {fp}


def test_incident_same_tenant_date_and_scene_still_consumes(monkeypatch):
    """A scene group is not a logical post, so it cannot exempt same-day bytes."""
    _armed(monkeypatch)
    fp = f"md5:{_md5('x')}"
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}], [],
                          [_incident(fp, TENANT, group_key="grp1")])
    assert gms.cross_client_used_fingerprints(GYM, [fp], http=http) == {fp}


def test_incident_sibling_group_mismatch_date_still_consumes(monkeypatch):
    """A prior date is consumed even for this tenant's visual group."""
    _armed(monkeypatch)
    fp = f"md5:{_md5('x')}"
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}], [],
                          [_incident(fp, TENANT, group_key="grp1",
                                     used_date="2026-09-01")])
    assert gms.cross_client_used_fingerprints(GYM, [fp], http=http) == {fp}


def test_incident_sibling_group_cross_tenant_still_consumes(monkeypatch):
    """The same scene label in another tenant is consumed."""
    _armed(monkeypatch)
    fp = f"md5:{_md5('x')}"
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}], [],
                          [_incident(fp, OTHER_TENANT, group_key="grp1")])
    assert gms.cross_client_used_fingerprints(GYM, [fp], http=http) == {fp}


# ---- pickable integration -----------------------------------------------------

def _store(*assets):
    return FakeMediaStore(assets=list(assets))


def test_incident_consumed_photo_leaves_pickable_set(monkeypatch):
    _armed(monkeypatch)
    consumed, free = _photo("consumed"), _photo("free")
    http = FakeLedgerHttp(
        [{"alias_key": GYM, "tenant_id": TENANT}], [],
        [_incident(f"md5:{_md5('consumed')}", OTHER_TENANT)])
    picks = gms.pickable(GYM, store=_store(consumed, free), now=NOW_DT,
                         ledger_http=http)
    assert [a["id"] for a in picks] == ["free"]


def test_incident_own_tenant_same_day_without_group_proof_consumes(monkeypatch):
    """The picker cannot prove a logical sibling and excludes the photo."""
    _armed(monkeypatch)
    photo = _photo("p1")
    http = FakeLedgerHttp(
        [{"alias_key": GYM, "tenant_id": TENANT}], [],
        [_incident(f"md5:{_md5('p1')}", TENANT, used_date=DAY)])
    picks = gms.pickable(GYM, store=_store(photo), now=NOW_DT,
                         ledger_http=http, post_date=DAY)
    assert picks == []


def test_incident_own_tenant_other_day_excludes(monkeypatch):
    _armed(monkeypatch)
    photo = _photo("p1")
    http = FakeLedgerHttp(
        [{"alias_key": GYM, "tenant_id": TENANT}], [],
        [_incident(f"md5:{_md5('p1')}", TENANT, used_date="2026-09-01")])
    picks = gms.pickable(GYM, store=_store(photo), now=NOW_DT,
                         ledger_http=http, post_date=DAY)
    assert picks == []


def test_incident_cross_tenant_same_day_excludes(monkeypatch):
    _armed(monkeypatch)
    photo = _photo("p1")
    http = FakeLedgerHttp(
        [{"alias_key": GYM, "tenant_id": TENANT}], [],
        [_incident(f"md5:{_md5('p1')}", OTHER_TENANT, used_date=DAY)])
    picks = gms.pickable(GYM, store=_store(photo), now=NOW_DT,
                         ledger_http=http, post_date=DAY)
    assert picks == []


def test_incident_outage_fails_closed_strict_and_non_strict(monkeypatch):
    _armed(monkeypatch)
    store = _store(_photo("p1"))
    http = FakeLedgerHttp([{"alias_key": GYM, "tenant_id": TENANT}], [], [],
                          fail_on="visual_global_historical_incident")
    assert gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=http) == []
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=http,
                     strict_claims=True)


def test_usage_ambiguity_still_fails_closed_when_incident_clean(monkeypatch):
    _armed(monkeypatch)
    fp = f"md5:{_md5('p1')}"
    http = FakeLedgerHttp(
        [{"alias_key": GYM, "tenant_id": TENANT}],
        [_usage(fp, TENANT, ambiguous=True)],
        [_incident(fp, OTHER_TENANT)])
    store = _store(_photo("p1"))
    assert gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=http) == []
    with pytest.raises(gms.GlobalLedgerUnavailable):
        gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=http,
                     strict_claims=True)


def test_incident_gate_keeps_photo_first_ordering(monkeypatch):
    """Incident exclusion removes consumed bytes; the remaining never-used
    photos keep pick order (id tiebreak, all counters at the floor)."""
    _armed(monkeypatch)
    b_photo = _photo("b_photo")
    a_photo = _photo("a_photo")
    consumed = _photo("consumed")
    http = FakeLedgerHttp(
        [{"alias_key": GYM, "tenant_id": TENANT}], [],
        [_incident(f"md5:{_md5('consumed')}", OTHER_TENANT)])
    picks = gms.pickable(GYM, store=_store(consumed, b_photo, a_photo),
                         now=NOW_DT, ledger_http=http)
    assert [a["id"] for a in picks] == ["a_photo", "b_photo"]


def test_incident_gate_does_not_exclude_videos(monkeypatch):
    """The exact-byte ledger gate is photo-only; a video whose bytes appear in
    the incident table stays pickable."""
    _armed(monkeypatch)
    clip = make_asset("v1", gym_id=GYM, kind="video", mime="video/mp4",
                      content_hash=_md5("v1"))
    http = FakeLedgerHttp(
        [{"alias_key": GYM, "tenant_id": TENANT}], [],
        [_incident(f"md5:{_md5('v1')}", OTHER_TENANT)])
    picks = gms.pickable(GYM, store=_store(clip), now=NOW_DT, ledger_http=http)
    assert [a["id"] for a in picks] == ["v1"]


def test_flag_off_never_reads_incident_table(monkeypatch):
    monkeypatch.delenv(gms.GLOBAL_LEDGER_FLAG_ENV, raising=False)
    store = _store(_photo("p1"))

    def _boom(*a, **k):
        raise AssertionError("flag OFF must never touch the global ledger")

    monkeypatch.setattr(gms, "cross_client_used_fingerprints", _boom)
    picks = gms.pickable(GYM, store=store, now=NOW_DT)
    assert [a["id"] for a in picks] == ["p1"]


# ---- armed remote stamp keeps legacy KV fields (PR385 contract item 4) --------

def test_armed_remote_stamp_keeps_legacy_kv_fields(tmp_path, monkeypatch):
    import json as _json
    import sqlite3
    import uuid
    from copy import deepcopy
    from agent import remote_drive_use as remote
    from tests.gym_media_fakes import make_source

    gym_dir = tmp_path / "library" / "pierce"
    gym_dir.mkdir(parents=True)
    db_file = tmp_path / "echo.db"
    sqlite3.connect(db_file).close()
    use_id = str(uuid.uuid4())
    stamped = "2026-10-10T09:00:00+00:00"
    monkeypatch.setenv("AGENT_DB_PATH", str(db_file))
    monkeypatch.setenv("AGENT_REMOTE_DRIVE_USE_CAS_ENABLED", "true")
    monkeypatch.setenv("LOCAL_INVENTORY_MUTATION_EPOCH", str(uuid.uuid4()))
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path / "library"))
    kv = {}
    monkeypatch.setattr("agent.db.kv_get", lambda k, d="": kv.get(k, d))
    monkeypatch.setattr("agent.db.kv_set", lambda k, v: kv.__setitem__(k, v))

    class _Authority:
        def apply_use(self, request):
            after = dict(request["asset_before"], used_count=1,
                         last_used_at=stamped,
                         drive_use_version=request["asset_before"]["drive_use_version"] + 1)
            return dict(state="applied", request=deepcopy(request),
                        asset_after=after,
                        source_after=deepcopy(request["source_before"]))

        def use_receipt(self, request):
            raise OSError("no receipt")

        def close(self):
            pass

    monkeypatch.setattr(remote.DriveUseAuthority, "from_environment",
                        classmethod(lambda cls: _Authority()))
    asset = make_asset("a1", gym_id="pierce", source_id="src1")
    asset["drive_use_version"] = 1
    source = make_source("src1", gym_id="pierce")
    source["drive_use_version"] = 2
    store = FakeMediaStore(sources=[source], assets=[asset])
    gms.stamp_use({"id": "a1"}, "pierce", "2026-10-10", store=store,
                  use_id=use_id, asset_row=asset, source_row=source)
    records = _json.loads(kv["gym_media_use:pierce:2026-10-10"])
    assert records == [{"asset_id": "a1", "gym_id": "pierce", "use_id": use_id,
                        "prev_used_count": 0, "prev_last_used_at": None,
                        "staged_at": stamped, "rolled_back": False}]
