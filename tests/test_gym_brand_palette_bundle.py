"""Ordinary infographic fallback palette: approved source-brand bundle only.

2026-10-09 architecture ruling: the ordinary scheduler NEVER uses dedicated
owner credentials (there is no ForwardMediaOwnerPersistence.from_environment);
the approved readback is the narrow scheduler read-only adapter
(astra_prompt.scheduler_source_brand_bundle) bound to the caller's existing
SupabaseCalendarStore service-role credentials: echo_intake_tokens maps the
exact echo_account_key to its unique gym_id (verified reciprocally), then the
existing production RPC echo_source_brand_active(p_gym) returns the active
bundle for exactly that gym. A missing/error/ambiguous/stale/cross-tenant
bundle readback at the ordinary fallback call sites fails CLOSED --
brand_colors.json is never consulted there. The legacy file loader survives
only for callers that pass no bundle_reader. Synthetic evidence only; no
production reads.
"""
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import astra_prompt as ap  # noqa: E402
from agent import generated_infographic_runtime as runtime  # noqa: E402
from test_generated_canonical_owner import frozen, identity, snapshot  # noqa: E402

BASE = "same-gym"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def make_active(base=BASE, *, minutes_ago=2):
    now = NOW - timedelta(minutes=minutes_ago)
    gym, bundle_id = identity(), identity()
    raw, sha = frozen(snapshot(gym, base, now))
    bundle = dict(id=bundle_id, gym_id=gym, echo_account_key=base,
                  schema_version=1, version=1,
                  capture_ids=[c["id"] for c in json.loads(raw)["captures"]],
                  snapshot_bytes=raw, content_sha256=sha,
                  source_revision="bundle-source-v1",
                  palette_revision="bundle-palette-v1",
                  created_at=now.isoformat())
    receipt = dict(id=1, gym_id=gym, bundle_id=bundle_id, bundle_version=1,
                   content_sha256=sha, actor_clerk_user_id="user_authenticated",
                   actor_authority="blake", action="approve",
                   purpose="echo_source_brand_configuration",
                   request_id=identity(), created_at=now.isoformat())
    observation = dict(id=1, gym_id=gym, bundle_id=bundle_id,
                       configuration_sha256=sha, snapshot_bytes=raw,
                       content_sha256=sha,
                       validator_revision="trusted-validator-v1",
                       validation_report=dict(
                           selected_facts_status="supported_uncontradicted",
                           identity_status="verified"),
                       created_at=now.isoformat())
    return dict(bundle=bundle, approval_receipt=receipt,
                observation=observation, fact_approval_mode="delegated_policy",
                fact_validation="supported_uncontradicted")


@pytest.fixture
def file_colors(monkeypatch, tmp_path):
    """A VALID brand_colors.json that must never rescue an invalid bundle."""
    path = tmp_path / "brand_colors.json"
    path.write_text(json.dumps({
        "colors": ["#ABC123", "#DEF456"],
        "source_url": "https://gym.example.test/brand",
    }))
    monkeypatch.setattr(ap, "_gym_brand_colors_path", lambda base: str(path))
    return str(path)


def make_active_with_fact(text, base=BASE):
    """A valid approved bundle whose single supported fact is `text`."""
    now = NOW - timedelta(minutes=2)
    gym, bundle_id = identity(), identity()
    import base64 as _b64, hashlib as _hl
    website = b"<style>#112233 #44AA77</style> " + text.encode()
    social = ('{"caption":"' + text + '"}').encode()
    captures = []
    for kind, data in [("website", website), ("social", social)]:
        captures.append(dict(
            id=identity(), gym_id=gym, echo_account_key=base, source_kind=kind,
            source_url=("https://gym.example.test/" if kind == "website"
                        else "https://api.apify.com/v2/datasets/test/items"),
            source_locator=(None if kind == "website"
                            else "https://www.instagram.com/verified_gym/"),
            capture_provider="direct" if kind == "website" else "apify",
            provider_response_id=None if kind == "website" else "dataset:test:1",
            provider_account_id=None if kind == "website" else "social-account:1",
            source_revision="capture-v1", mapping_revision="map-v1",
            mapping_evidence={"binding": "gym"},
            fetched_at=now.isoformat(),
            bytes_sha256=_hl.sha256(data).hexdigest(),
            bytes_base64=_b64.b64encode(data).decode()))
    capture = captures[0]
    snap = dict(schema_version=1, gym_id=gym, echo_account_key=base,
                fact_policy="delegated_supported_facts", captures=captures,
                palette=dict(capture_id=capture["id"],
                             bytes_sha256=capture["bytes_sha256"],
                             primary="#112233", secondary="#44AA77",
                             primary_byte_offset=7, secondary_byte_offset=15),
                selected_facts=[dict(key="hours", capture_id=capture["id"],
                                     bytes_sha256=capture["bytes_sha256"],
                                     source_locator=capture["source_url"],
                                     byte_offset=website.index(text.encode()),
                                     byte_length=len(text.encode()),
                                     text=text)])
    raw, sha = frozen(snap)
    bundle = dict(id=bundle_id, gym_id=gym, echo_account_key=base,
                  schema_version=1, version=1,
                  capture_ids=[c["id"] for c in captures],
                  snapshot_bytes=raw, content_sha256=sha,
                  source_revision="bundle-source-v1",
                  palette_revision="bundle-palette-v1",
                  created_at=now.isoformat())
    receipt = dict(id=1, gym_id=gym, bundle_id=bundle_id, bundle_version=1,
                   content_sha256=sha, actor_clerk_user_id="user_authenticated",
                   actor_authority="blake", action="approve",
                   purpose="echo_source_brand_configuration",
                   request_id=identity(), created_at=now.isoformat())
    observation = dict(id=1, gym_id=gym, bundle_id=bundle_id,
                       configuration_sha256=sha, snapshot_bytes=raw,
                       content_sha256=sha,
                       validator_revision="trusted-validator-v1",
                       validation_report=dict(
                           selected_facts_status="supported_uncontradicted",
                           identity_status="verified"),
                       created_at=now.isoformat())
    return dict(bundle=bundle, approval_receipt=receipt,
                observation=observation, fact_approval_mode="delegated_policy",
                fact_validation="supported_uncontradicted")


def test_punctuated_fact_palette_succeeds_copy_still_holds(file_colors):
    """A valid approved bundle with a SUPPORTED PUNCTUATED fact must yield the
    verified palette (no fact date-selection, no copy-style gate on the
    palette path) while delegated_copy still enforces copy style unchanged."""
    active = make_active_with_fact("Open daily: 5am-9pm")
    got = ap.load_gym_brand_palette(BASE, bundle_reader=lambda b: active,
                                    now=NOW)
    assert got is not None
    assert got["source"] == "source_brand_bundle"
    assert got["colors"] == ["#112233", "#44AA77"]
    assert got["path"].startswith("source-brand-observation:sha256:")
    with pytest.raises(runtime.RuntimeHold,
                       match="generated_copy_style_invalid"):
        runtime.delegated_copy(active, BASE,
                               local_date=NOW.date().isoformat(), now=NOW)


def test_bare_colors_file_rejected(monkeypatch, tmp_path):
    """A brand_colors.json with colors but NO source_url/owner_approved is
    unproven input, not a verified palette: legacy loader rejects it."""
    path = tmp_path / "brand_colors.json"
    path.write_text(json.dumps({"colors": ["#ABC123", "#DEF456"]}))
    monkeypatch.setattr(ap, "_gym_brand_colors_path", lambda base: str(path))
    assert ap.load_gym_brand_palette(BASE) is None


@pytest.mark.parametrize("field,value", [
    ("id", None), ("id", 0), ("id", "1"),
    ("validator_revision", None), ("validator_revision", " "),
])
def test_invalid_observation_identity_holds_palette(field, value, file_colors):
    active = make_active()
    active["observation"][field] = value
    assert ap.load_gym_brand_palette(
        BASE, bundle_reader=lambda b: active, now=NOW) is None


@pytest.mark.parametrize("now", ["invalid", 123, NOW.replace(tzinfo=None)])
def test_invalid_clock_holds_palette(now, file_colors):
    assert ap.load_gym_brand_palette(
        BASE, bundle_reader=lambda b: make_active(), now=now) is None


def test_date_only_clock_is_handled_without_exception(file_colors, monkeypatch):
    from agent import config
    from zoneinfo import ZoneInfo
    utc_now = datetime.now(timezone.utc)
    # Pick a timezone whose calendar date differs from UTC at test time.
    zone_name = ("Pacific/Kiritimati" if utc_now.hour >= 10 else
                 "Etc/GMT+12")
    monkeypatch.setattr(config, "posting_timezone_for", lambda base: zone_name)
    local_today = utc_now.astimezone(ZoneInfo(zone_name)).date()
    assert local_today != utc_now.date()
    active = make_active()
    got = ap.load_gym_brand_palette(
        BASE, bundle_reader=lambda b: active, now=local_today)
    assert got is not None and got["colors"] == ["#112233", "#44AA77"]


def test_valid_bundle_palette_wins(file_colors):
    active = make_active()
    got = ap.load_gym_brand_palette(BASE, bundle_reader=lambda b: active,
                                    now=NOW)
    assert got is not None
    assert got["source"] == "source_brand_bundle"
    assert got["colors"] == ["#112233", "#44AA77"]
    assert got["path"].startswith("source-brand-observation:sha256:")


def test_missing_bundle_fails_closed_no_file_rescue(file_colors):
    got = ap.load_gym_brand_palette(BASE, bundle_reader=lambda b: None,
                                    now=NOW)
    assert got is None


def test_readback_error_fails_closed_no_file_rescue(file_colors):
    def boom(base):
        raise runtime.RuntimeHold("generated_bundle_publish_bridge_unavailable")
    assert ap.load_gym_brand_palette(BASE, bundle_reader=boom, now=NOW) is None


def test_cross_tenant_bundle_fails_closed_no_file_rescue(file_colors):
    active = make_active()
    active["bundle"]["echo_account_key"] = "other-gym"
    got = ap.load_gym_brand_palette(BASE, bundle_reader=lambda b: active,
                                    now=NOW)
    assert got is None


def test_stale_observation_fails_closed_no_file_rescue(file_colors):
    active = make_active(minutes_ago=120)  # evidence older than 'now' window
    # Observation/receipt timestamps AFTER the supplied 'now' => stale/invalid.
    got = ap.load_gym_brand_palette(BASE, bundle_reader=lambda b: active,
                                    now=NOW - timedelta(hours=3))
    assert got is None


def test_tampered_bundle_fails_closed_no_file_rescue(file_colors):
    active = make_active()
    active["approval_receipt"]["actor_authority"] = "coach"
    got = ap.load_gym_brand_palette(BASE, bundle_reader=lambda b: active,
                                    now=NOW)
    assert got is None


def test_legacy_file_loader_only_without_bundle_reader(file_colors):
    got = ap.load_gym_brand_palette(BASE, now=NOW)
    assert got is not None
    assert got["colors"] == ["#ABC123", "#DEF456"]
    assert got["path"] == file_colors


def test_lasso_never_gets_a_palette():
    active = make_active(base="lasso")
    assert ap.load_gym_brand_palette(
        "lasso_ig", bundle_reader=lambda b: active, now=NOW) is None


# ---- narrow scheduler read-only adapter (offline fake PostgREST) ----------

GYM = "11111111-2222-3333-4444-555555555555"


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


class FakeHttp:
    """Offline PostgREST double: records calls, serves canned rows/rpc."""

    def __init__(self, *, by_key=(), by_gym=(), rpc=None, rpc_status=200,
                 get_status=200):
        self.by_key, self.by_gym = list(by_key), list(by_gym)
        self.rpc, self.rpc_status, self.get_status = rpc, rpc_status, get_status
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, dict(params or {})))
        if self.get_status >= 400:
            return _Resp([], status=self.get_status)
        rows = self.by_key if "echo_account_key" in params else self.by_gym
        return _Resp(list(rows))

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("POST", url, dict(json or {})))
        return _Resp(self.rpc, status=self.rpc_status)


def _store(http):
    from agent.portal_calendar_store import SupabaseCalendarStore
    return SupabaseCalendarStore(url="https://sb.test", service_key="k",
                                 http=http)


def _token(gym=GYM, key=BASE):
    return {"gym_id": gym, "echo_account_key": key}


def test_adapter_exact_mapping_round_trip():
    active = make_active()
    active["bundle"]["gym_id"] = GYM
    http = FakeHttp(by_key=[_token()], by_gym=[_token()],
                    rpc={"active": active})
    got = ap.scheduler_source_brand_bundle(_store(http), BASE)
    assert got is active
    methods = [c[0] for c in http.calls]
    assert methods == ["GET", "GET", "POST"]
    assert http.calls[0][2]["echo_account_key"] == f"eq.{BASE}"
    assert http.calls[0][2]["select"] == "gym_id,echo_account_key"
    assert http.calls[0][2]["limit"] == "2"
    assert http.calls[1][2]["gym_id"] == f"eq.{GYM}"
    assert http.calls[2][1].endswith("rpc/echo_source_brand_active")
    assert http.calls[2][2] == {"p_gym": GYM}


def test_adapter_active_proof_end_to_end(file_colors):
    """A valid adapter readback feeds delegated_copy and yields the palette."""
    active = make_active()
    gym = active["bundle"]["gym_id"]  # snapshot bytes pin this gym exactly
    http = FakeHttp(by_key=[_token(gym=gym)], by_gym=[_token(gym=gym)],
                    rpc={"active": active})
    got = ap.load_gym_brand_palette(
        BASE, bundle_reader=lambda b: ap.scheduler_source_brand_bundle(
            _store(http), b), now=NOW)
    assert got is not None and got["source"] == "source_brand_bundle"
    assert got["colors"] == ["#112233", "#44AA77"]


def test_adapter_direct_schema_v2_website_readback(file_colors):
    from test_generated_infographic_runtime import _v2_active, V2_WEBSITE_ONLY
    active = _v2_active(V2_WEBSITE_ONLY, connected=False, now=NOW, base=BASE)
    gym = active["bundle"]["gym_id"]
    http = FakeHttp(by_key=[_token(gym=gym)], by_gym=[_token(gym=gym)],
                    rpc=active)
    got = ap.load_gym_brand_palette(
        BASE, bundle_reader=lambda b: ap.scheduler_source_brand_bundle(
            _store(http), b), now=NOW)
    assert got is not None and got["source"] == "source_brand_bundle"
    assert got["colors"] == ["#112233", "#44AA77"]
    active["provider_status"] = None
    assert ap.load_gym_brand_palette(
        BASE, bundle_reader=lambda b: ap.scheduler_source_brand_bundle(
            _store(http), b), now=NOW) is None


def test_adapter_no_rows_holds():
    http = FakeHttp(by_key=[], by_gym=[_token()], rpc={"active": {}})
    with pytest.raises(runtime.RuntimeHold):
        ap.scheduler_source_brand_bundle(_store(http), BASE)


def test_adapter_ambiguous_key_rows_hold():
    http = FakeHttp(by_key=[_token(), _token(gym=identity())],
                    by_gym=[_token()], rpc={"active": {}})
    with pytest.raises(runtime.RuntimeHold):
        ap.scheduler_source_brand_bundle(_store(http), BASE)


def test_adapter_ambiguous_gym_rows_hold():
    http = FakeHttp(by_key=[_token()],
                    by_gym=[_token(), _token(key="other-gym")],
                    rpc={"active": {}})
    with pytest.raises(runtime.RuntimeHold):
        ap.scheduler_source_brand_bundle(_store(http), BASE)


def test_adapter_reciprocal_pair_mismatch_holds():
    http = FakeHttp(by_key=[_token()], by_gym=[_token(key="other-gym")],
                    rpc={"active": {}})
    with pytest.raises(runtime.RuntimeHold):
        ap.scheduler_source_brand_bundle(_store(http), BASE)


def test_adapter_wrong_tenant_bundle_holds():
    active = make_active()  # bundle gym_id != token gym_id
    http = FakeHttp(by_key=[_token()], by_gym=[_token()],
                    rpc={"active": active})
    with pytest.raises(runtime.RuntimeHold):
        ap.scheduler_source_brand_bundle(_store(http), BASE)
    active["bundle"]["gym_id"] = GYM
    active["bundle"]["echo_account_key"] = "other-gym"
    http = FakeHttp(by_key=[_token()], by_gym=[_token()],
                    rpc={"active": active})
    with pytest.raises(runtime.RuntimeHold):
        ap.scheduler_source_brand_bundle(_store(http), BASE)


def test_adapter_errors_hold():
    for http in (FakeHttp(by_key=[_token()], by_gym=[_token()], rpc_status=404),
                 FakeHttp(by_key=[_token()], by_gym=[_token()], get_status=500),
                 FakeHttp(by_key=[_token()], by_gym=[_token()], rpc=None)):
        with pytest.raises(runtime.RuntimeHold):
            ap.scheduler_source_brand_bundle(_store(http), BASE)
    with pytest.raises(runtime.RuntimeHold):
        ap.scheduler_source_brand_bundle(None, BASE)


def test_adapter_hold_means_no_file_fallback(file_colors):
    """An adapter HOLD at the ordinary call site never rescues via the file."""
    http = FakeHttp(by_key=[], by_gym=[], rpc=None)
    got = ap.load_gym_brand_palette(
        BASE, bundle_reader=lambda b: ap.scheduler_source_brand_bundle(
            _store(http), b), now=NOW)
    assert got is None


def test_ordinary_seed_caller_wires_store_reader_and_holds(monkeypatch, tmp_path):
    """no_media_astra_seed.seed_gaps (ordinary fallback caller) binds the
    store's service-role readback; with no reachable bundle the seed HELDS
    even when a valid brand_colors.json exists (no generic file fallback)."""
    from agent import config, gym_media_index
    from agent import no_media_astra_seed as nmas
    from agent.accounts import Account, Platform

    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(lib_dir), raising=False)
    monkeypatch.setenv("AGENT_NO_MEDIA_ASTRA_SEED", "true")
    monkeypatch.setenv("AGENT_GYM_DEEP_BRAIN", "true")

    class EmptyDriveIndex:
        def available(self):
            return True

        def list_sources(self, _base, include_inactive=False):
            return [{"id": "src1", "gym_id": _base, "kind": "gym_drive",
                     "active": True, "revoked_externally": False,
                     "sync_status": "ready",
                     "sync_finished_at": "2026-10-02T00:00:00Z"}]

        def list_assets(self, _base):
            return []

    monkeypatch.setattr(gym_media_index, "default_store",
                        lambda: EmptyDriveIndex())

    colors = tmp_path / "brand_colors.json"
    colors.write_text(json.dumps({"colors": ["#ABC123"]}))
    monkeypatch.setattr(ap, "_gym_brand_colors_path", lambda base: str(colors))

    class Store:  # no _client/_rest: the service-role RPC is unreachable
        def list_month(self, base, month):
            return []

        def insert_rows(self, base, rows):
            raise AssertionError("must never insert without a valid bundle")

    from agent import gym_deep_brain
    monkeypatch.setattr(gym_deep_brain, "build_deep_brain", lambda base, **kw: {
        "ok": True, "base": base,
        "facts": [type("F", (), {"text": "Real fact about Chateau",
                                 "source_url": "https://gym.example.test/",
                                 "category": "service"})()]})

    account = Account(key="chateau_ig", display_name="CrossFit Chateau",
                      platform=Platform.INSTAGRAM, token_env="T",
                      target_id_env="G")
    logs = []
    n = nmas.seed_gaps("chateau", account, Store(), log=logs.append,
                       max_rows=1, days_ahead=1)
    assert n == 0
    assert any("no verified brand colors" in line for line in logs)
