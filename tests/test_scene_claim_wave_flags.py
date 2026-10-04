"""scene_claim_wave: flag and fail-closed gate contracts for the claim-wave
redesign.

Pins the CURRENT repo behavior that must stay true while the claim-wave
redesign (docs/VISUAL_SCENE_GUARD_DRAFT.md items (a)-(e)) is being built:

* AGENT_VISUAL_SCENE_GUARD is tri-state and OFF by default; OFF/unset keeps
  byte-for-byte legacy behavior and never consults the scene ledger.
* An ambiguous flag value is armed-fail-closed (None), never a silent default.
* SCENE_GUARD_OPERATIONAL stays False: an ARMED guard fails closed with
  SceneLedgerUnavailable ("not operational") BEFORE any ledger scan, because
  the prep-time writer architecture was rejected and the claim-wave redesign
  has not landed.

None of this touches SQL or any provider; everything is DRAFT/OFF.
"""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent import gym_media_selector as gms  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, make_asset  # noqa: E402

GYM = "gymx"
NOW_DT = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
FLAG = "AGENT_VISUAL_SCENE_GUARD"


def _photo(asset_id):
    return make_asset(asset_id, gym_id=GYM, kind="photo")


def _read_bytes(asset):
    return b"bytes:" + str(asset["id"]).encode("utf-8")


def _creds(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-test")


# ---- tri-state flag ----------------------------------------------------------

def test_flag_unset_is_off(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    assert config.visual_scene_guard_enabled() is False
    assert config.visual_scene_guard_flag() is False
    assert gms.scene_guard_flag() is False


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", " Off "])
def test_flag_explicit_off(monkeypatch, value):
    monkeypatch.setenv(FLAG, value)
    assert config.visual_scene_guard_flag() is False
    assert config.visual_scene_guard_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", " On "])
def test_flag_explicit_on(monkeypatch, value):
    monkeypatch.setenv(FLAG, value)
    assert config.visual_scene_guard_flag() is True
    assert config.visual_scene_guard_enabled() is True


@pytest.mark.parametrize("value", ["2", "enable", "enabled", "Y", "nah", "[]"])
def test_flag_ambiguous_is_fail_closed_never_a_silent_default(monkeypatch, value):
    monkeypatch.setenv(FLAG, value)
    assert config.visual_scene_guard_flag() is None
    assert gms.scene_guard_flag() is None


# ---- AGENT_VISUAL_SCENE_CANDIDATE (candidate staging emission, wave item a) --

def test_candidate_flag_unset_is_off(monkeypatch):
    monkeypatch.delenv("AGENT_VISUAL_SCENE_CANDIDATE", raising=False)
    assert config.visual_scene_candidate_flag() is False


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("on", True),
    ("0", False), ("false", False), ("off", False),
    ("sometimes", None), ("2", None),
])
def test_candidate_flag_tri_state(monkeypatch, value, expected):
    monkeypatch.setenv("AGENT_VISUAL_SCENE_CANDIDATE", value)
    assert config.visual_scene_candidate_flag() is expected


# ---- gate state while the claim-wave redesign is unlanded --------------------

def test_scene_guard_is_pinned_not_operational():
    # Until redesign items (a)-(e) land, this stays False or an armed flag
    # stops meaning fail-closed and starts meaning enforcement.
    assert gms.SCENE_GUARD_OPERATIONAL is False


def test_flag_off_is_byte_for_byte_legacy(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    store = FakeMediaStore(assets=[_photo("p1")])

    def _boom(*a, **k):
        raise AssertionError("flag OFF must never touch the scene ledger")

    monkeypatch.setattr(gms, "cross_tenant_scene_phashes", _boom)
    picks = gms.pickable(GYM, store=store, now=NOW_DT)
    assert [a["id"] for a in picks] == ["p1"]


def test_armed_guard_fails_closed_not_operational(monkeypatch):
    """Armed but incomplete setup: strict raises SceneLedgerUnavailable with
    the not-operational reason; non-strict returns [] — and the ledger read
    machinery is never reached in either mode."""
    _creds(monkeypatch)
    monkeypatch.setenv(FLAG, "true")
    store = FakeMediaStore(assets=[_photo("p1")])

    def _boom(*a, **k):
        raise AssertionError("non-operational guard must never read the ledger")

    monkeypatch.setattr(gms, "cross_tenant_scene_phashes", _boom)
    assert gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=None,
                        scene_read_bytes=_read_bytes) == []
    with pytest.raises(gms.SceneLedgerUnavailable, match="not operational"):
        gms.pickable(GYM, store=store, now=NOW_DT, ledger_http=None,
                     scene_read_bytes=_read_bytes, strict_claims=True)


def test_ambiguous_flag_value_fails_closed(monkeypatch):
    monkeypatch.setenv(FLAG, "sometimes")
    store = FakeMediaStore(assets=[_photo("p1")])
    assert gms.pickable(GYM, store=store, now=NOW_DT) == []
    with pytest.raises(gms.SceneLedgerUnavailable):
        gms.pickable(GYM, store=store, now=NOW_DT, strict_claims=True)
