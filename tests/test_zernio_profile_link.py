"""
zernio_profile_link tests (agent/zernio_profile_link.py), fully offline.

Pierce 2026-08-24: a fully-connected Zernio profile with an empty gyms.zernio_profile_id
silently never published. This module backfills that column by matching the Zernio profile
name to the gym base. Covers: flag gate, links only empty gyms, never overwrites a set id,
handles no-profile, extracts the FB page id, one gym's error never blocks the rest, the
registry-sourced display-name/handle fallback, and the alert grace period.

Swift River 2026-08-31: db.gym_get(base) returns {} for every dynamically-registered client
gym (its display name only lives in the account registry), which silently no-op'd the whole
display-name fallback; and the ops alert fired on first sighting with no grace period,
indistinguishable from a genuinely stuck gym. Both covered below.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, zernio_profile_link as zpl


class _FakeDb:
    def __init__(self, rows, kv=None):
        self.rows = {k: dict(v) for k, v in rows.items()}
        self.upserts = []
        self.kv = dict(kv or {})

    def gym_get(self, key):
        return self.rows.get(key)

    def gym_upsert(self, account_key, **fields):
        self.upserts.append((account_key, fields))
        self.rows.setdefault(account_key, {}).update(fields)

    def kv_get(self, key, default=""):
        return self.kv.get(key, default)

    def kv_set(self, key, value):
        self.kv[key] = value


class _FakeZernio:
    def __init__(self, profiles, accounts=None, raise_for=None):
        self._profiles = profiles           # {name: profile_id}
        self._accounts = accounts or {}     # {profile_id: accounts_json}
        self._raise_for = raise_for or set()

    def find_profile_id(self, name):
        if name in self._raise_for:
            raise RuntimeError("boom")
        return self._profiles.get(name)

    def find_profile_id_any(self, *names):
        for n in names:
            if n in self._raise_for:
                raise RuntimeError("boom")
            pid = self._profiles.get(n)
            if pid:
                return pid
        return None

    def list_accounts(self, profile_id):
        return self._accounts.get(profile_id, {"accounts": []})


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_ZERNIO_PROFILE_LINK", "true")


def test_flag_off_is_noop(monkeypatch):
    monkeypatch.delenv("AGENT_ZERNIO_PROFILE_LINK", raising=False)
    out = zpl.link_client_profiles(bases=["pierce"], zernio=_FakeZernio({}), db=_FakeDb({}))
    assert out["ok"] is False


def test_links_empty_gym_with_fb_page(armed):
    db = _FakeDb({"piercefitness": {"zernio_profile_id": ""}})
    z = _FakeZernio(
        {"piercefitness": "PID1"},
        accounts={"PID1": {"accounts": [
            {"platform": "facebook",
             "metadata": {"availablePages": [{"id": "661535357048979"}]}},
            {"platform": "instagram"},
        ]}},
    )
    out = zpl.link_client_profiles(bases=["piercefitness"], zernio=z, db=db)
    assert out["linked"] == 1
    assert db.rows["piercefitness"]["zernio_profile_id"] == "PID1"
    assert db.rows["piercefitness"]["zernio_default_fb_page_id"] == "661535357048979"


def test_never_overwrites_a_set_id(armed):
    db = _FakeDb({"eng": {"zernio_profile_id": "ALREADY"}})
    z = _FakeZernio({"eng": "SHOULD_NOT_WIN"})
    out = zpl.link_client_profiles(bases=["eng"], zernio=z, db=db)
    assert out["already"] == 1 and out["linked"] == 0
    assert db.rows["eng"]["zernio_profile_id"] == "ALREADY"
    assert db.upserts == []


def test_no_profile_is_skipped_not_errored(armed):
    db = _FakeDb({"ghost": {"zernio_profile_id": ""}})
    out = zpl.link_client_profiles(bases=["ghost"], zernio=_FakeZernio({}), db=db)
    assert out["no_profile"] == 1 and out["linked"] == 0
    assert db.upserts == []


def test_one_gym_error_never_blocks_the_rest(armed):
    db = _FakeDb({"bad": {"zernio_profile_id": ""}, "good": {"zernio_profile_id": ""}})
    z = _FakeZernio({"good": "PIDG"}, raise_for={"bad"})
    out = zpl.link_client_profiles(bases=["bad", "good"], zernio=z, db=db)
    assert out["errors"] == 1 and out["linked"] == 1
    assert db.rows["good"]["zernio_profile_id"] == "PIDG"


def test_links_uuid_keyed_gym_by_display_name(armed):
    """A portal-onboarded gym is keyed by a UUID, so its base never matches the Zernio
    profile name. It must still link via its display name (everyone going forward)."""
    uuid = "8a668b95-da93-41ea-b28a-df3526c529fe"
    db = _FakeDb({uuid: {"zernio_profile_id": "", "display_name": "Top Fuel Fitness"}})
    z = _FakeZernio(
        {"topfuelfitness": "PIDT"},                 # profile named for the gym, not the uuid
        accounts={"PIDT": {"accounts": [{"platform": "instagram"}]}},
    )
    out = zpl.link_client_profiles(bases=[uuid], zernio=z, db=db)
    assert out["linked"] == 1
    assert db.rows[uuid]["zernio_profile_id"] == "PIDT"


def test_name_candidates_variants():
    assert zpl._name_candidates({"display_name": "Top Fuel Fitness"}) == [
        "Top Fuel Fitness", "topfuelfitness", "top_fuel_fitness", "top fuel fitness"]
    assert zpl._name_candidates({"display_name": ""}) == []
    assert zpl._name_candidates({"gym_name": "GritX"}) == ["GritX", "gritx"]


def test_fb_page_id_picks_single_available_page():
    assert zpl._fb_page_id({"accounts": [
        {"platform": "facebook", "metadata": {"availablePages": [{"id": "P1"}]}}]}) == "P1"
    # several unlabelled pages -> we do NOT guess
    assert zpl._fb_page_id({"accounts": [
        {"platform": "facebook",
         "metadata": {"availablePages": [{"id": "P1"}, {"id": "P2"}]}}]}) == ""
    # an explicit selected page wins
    assert zpl._fb_page_id({"accounts": [
        {"platform": "facebook", "metadata": {"selectedPageId": "SEL",
         "availablePages": [{"id": "P1"}, {"id": "P2"}]}}]}) == "SEL"


# ---- registry-sourced display name / handle (Swift River, 2026-08-31) --------------------

def test_links_via_registry_row_when_gym_get_has_no_row(armed, monkeypatch):
    """A dynamically-registered client gym has NO row in the gyms table (db.gym_get -> None),
    so its display name / handle must come from the account registry instead, or the
    fallback silently never runs (the exact Swift River bug)."""
    base = "swiftrivercrossfitd23567"
    monkeypatch.setattr(
        zpl, "_registry_row_for_base",
        lambda b: {"base": base, "name": "Swift River CrossFit",
                   "ig_handle": "swiftrivercrossfit"} if b == base else {},
    )
    db = _FakeDb({})  # no gyms-table row at all for this base
    z = _FakeZernio({"Swift River CrossFit": "PIDX"},
                     accounts={"PIDX": {"accounts": [{"platform": "instagram"}]}})
    out = zpl.link_client_profiles(bases=[base], zernio=z, db=db)
    assert out["linked"] == 1
    assert db.rows[base]["zernio_profile_id"] == "PIDX"


def test_links_via_registry_ig_handle(armed, monkeypatch):
    """Some Zernio profiles are named after the raw IG handle, not the brand name."""
    base = "swiftrivercrossfitd23567"
    monkeypatch.setattr(
        zpl, "_registry_row_for_base",
        lambda b: {"base": base, "name": "Swift River CrossFit",
                   "ig_handle": "swiftrivercrossfit"} if b == base else {},
    )
    db = _FakeDb({})
    z = _FakeZernio({"swiftrivercrossfit": "PIDH"})
    out = zpl.link_client_profiles(bases=[base], zernio=z, db=db)
    assert out["linked"] == 1
    assert db.rows[base]["zernio_profile_id"] == "PIDH"


def test_candidate_names_for_base_unions_gym_row_and_registry(monkeypatch):
    monkeypatch.setattr(
        zpl, "_registry_row_for_base",
        lambda b: {"name": "Swift River CrossFit", "ig_handle": "swiftrivercrossfit"},
    )
    out = zpl._candidate_names_for_base("swiftrivercrossfitd23567", {"display_name": "Swift River"})
    assert "Swift River" in out
    assert "Swift River CrossFit" in out
    assert "swiftrivercrossfit" in out


def test_registry_row_for_base_never_raises_when_registry_missing():
    # No registry file at config.gym_registry_path() in a clean checkout -> [] -> {}.
    assert zpl._registry_row_for_base("no-such-base-at-all") == {}


# ---- alert grace period (Swift River, 2026-08-31: fired ~37min after intake) --------------

def _with_media(monkeypatch, tmp_path, base):
    """Point config.LIBRARY_PATH at a tmp dir containing one media file for `base`."""
    lib_root = tmp_path / "content_library"
    (lib_root / base).mkdir(parents=True)
    (lib_root / base / "igfill_2026-09-01_hero.png").write_bytes(b"x")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(lib_root))


def test_no_alert_on_first_sighting_only_starts_the_clock(armed, monkeypatch, tmp_path):
    base = "ghost"
    _with_media(monkeypatch, tmp_path, base)
    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda msg: alerts.append(msg))
    db = _FakeDb({base: {"zernio_profile_id": ""}})
    zpl.link_client_profiles(bases=[base], zernio=_FakeZernio({}), db=db)
    assert alerts == []
    seen = db.kv_get(f"zernio_link_alerted_{base}")
    assert seen and seen != "alerted"          # a timestamp was recorded, not a fire


def test_alert_fires_once_grace_period_has_elapsed(armed, monkeypatch, tmp_path):
    base = "ghost"
    _with_media(monkeypatch, tmp_path, base)
    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda msg: alerts.append(msg))
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    db = _FakeDb({base: {"zernio_profile_id": ""}}, kv={f"zernio_link_alerted_{base}": stale})
    zpl.link_client_profiles(bases=[base], zernio=_FakeZernio({}), db=db)
    assert len(alerts) == 1
    assert base in alerts[0] and "24h" in alerts[0]
    assert db.kv_get(f"zernio_link_alerted_{base}") == "alerted"


def test_no_alert_before_grace_period_elapses(armed, monkeypatch, tmp_path):
    base = "ghost"
    _with_media(monkeypatch, tmp_path, base)
    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda msg: alerts.append(msg))
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=37)).isoformat()
    db = _FakeDb({base: {"zernio_profile_id": ""}}, kv={f"zernio_link_alerted_{base}": fresh})
    zpl.link_client_profiles(bases=[base], zernio=_FakeZernio({}), db=db)
    assert alerts == []
    # still just the timestamp — not flipped to "alerted"
    assert db.kv_get(f"zernio_link_alerted_{base}") == fresh


def test_never_re_alerts_once_alerted(armed, monkeypatch, tmp_path):
    base = "ghost"
    _with_media(monkeypatch, tmp_path, base)
    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda msg: alerts.append(msg))
    db = _FakeDb({base: {"zernio_profile_id": ""}}, kv={f"zernio_link_alerted_{base}": "alerted"})
    zpl.link_client_profiles(bases=[base], zernio=_FakeZernio({}), db=db)
    assert alerts == []
    assert db.kv_get(f"zernio_link_alerted_{base}") == "alerted"


def test_no_media_never_starts_the_clock(armed, monkeypatch, tmp_path):
    """An empty onboarding stub (no uploaded media) must never even start the grace clock."""
    base = "ghost"
    lib_root = tmp_path / "content_library"
    monkeypatch.setattr(config, "LIBRARY_PATH", str(lib_root))  # no dir created for base
    db = _FakeDb({base: {"zernio_profile_id": ""}})
    zpl.link_client_profiles(bases=[base], zernio=_FakeZernio({}), db=db)
    assert db.kv_get(f"zernio_link_alerted_{base}") == ""
