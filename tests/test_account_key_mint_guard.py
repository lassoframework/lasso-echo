"""
Tests for the account_key mint-guard hardening (topfuel / district_h stranding class):

  1. account_key_mint.derive_mint_key — canonical at mint is DETERMINISTIC, folds the portal
     uuid in, keeps the _ig/_fb suffix, and (the load-bearing rail) DOES NOT re-key a gym that
     already has a link (existing-row idempotency) nor fabricate a key when the uuid is
     unresolved, so already-minted links' resolution never changes.
  2. account_key_doctor.diagnose — flags a topfuel-style base that resolve_gym_uuid can't
     bridge (UNRESOLVED), an ambiguous base (AMBIGUOUS), an archived-only base (ARCHIVED_ONLY),
     and passes a clean base (OK); read-only.
  3. account_key_doctor.fire_alerts — fires ONCE per unresolved social gym and is throttled on
     the second pass; a clean summary fires nothing; the flag gates the automatic fire.

Fully OFFLINE: pure functions + injected resolvers / stores / alert+kv sinks. No network, no
creds. Existing signed tokens are exercised through intake_tokens.verify to prove the mint
change never touches their resolution.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import account_key as ak  # noqa: E402
from agent import account_key_mint as akm  # noqa: E402
from agent import account_key_doctor as doc  # noqa: E402
from agent import config, intake_tokens as it  # noqa: E402


# =============================================================================
# helpers: a fake store for the doctor + a fake resolver for the mint
# =============================================================================
class _FakeStore:
    """Read-only stand-in for SupabaseCalendarStore. `gyms` is a list of {id,slug,name}.
    resolve_gym_uuid mirrors the real resolver's contract (exact/normalised/unique-containment,
    archived skipped, ambiguous -> None). gym_zernio_profile_id reads `profiles` by uuid."""

    def __init__(self, gyms, profiles=None):
        self._gyms = gyms
        self._profiles = profiles or {}

    def available(self):
        return True

    def list_gyms_min(self):
        return list(self._gyms)

    @staticmethod
    def _norm(s):
        return "".join(c for c in (s or "").lower() if c.isalnum())

    @staticmethod
    def _archived(r):
        slug = (r.get("slug") or "").lower()
        name = (r.get("name") or "").lower()
        return ("archived" in slug or "-dup" in slug or "archived" in name
                or "do not use" in name)

    def resolve_gym_uuid(self, base):
        base = (base or "").strip()
        clean = [g for g in self._gyms if not self._archived(g) and g.get("id")]
        for g in clean:  # exact slug / id
            if g.get("slug") == base or g.get("id") == base:
                return g["id"]
        target = self._norm(base)
        if not target:
            return None
        exact = [g for g in clean if self._norm(g.get("slug")) == target]
        if len(exact) == 1:
            return exact[0]["id"]
        if len(exact) > 1:
            return None
        contain = [g for g in clean
                   if self._norm(g.get("slug")).startswith(target)
                   or target.startswith(self._norm(g.get("slug")))]
        if len(contain) == 1:
            return contain[0]["id"]
        return None

    def gym_zernio_profile_id(self, base):
        uuid = self.resolve_gym_uuid(base)
        return self._profiles.get(uuid)


@pytest.fixture(autouse=True)
def _mint_on(monkeypatch):
    # Default the canonical-mint flag ON (its production default) unless a test overrides it.
    monkeypatch.delenv("AGENT_CANONICAL_MINT", raising=False)
    yield


# =============================================================================
# 1. derive_mint_key
# =============================================================================
def test_canonical_mint_is_deterministic_and_folds_uuid():
    """A fresh gym (no existing row) with a resolvable uuid gets the canonical key, and the
    same inputs always yield the same key."""
    resolve = lambda base: "uuid-topfuel-123"          # noqa: E731
    exists = lambda key: False                          # noqa: E731
    k1, info1 = akm.derive_mint_key("topfuel", "Top Fuel", resolve_uuid=resolve,
                                    gym_exists=exists)
    k2, info2 = akm.derive_mint_key("topfuel", "Top Fuel", resolve_uuid=resolve,
                                    gym_exists=exists)
    assert k1 == k2                                     # deterministic
    assert k1 == ak.canonical_account_key("uuid-topfuel-123", "Top Fuel")
    assert info1["derived"] is True and info1["gym_uuid"] == "uuid-topfuel-123"


def test_canonical_mint_preserves_ig_fb_suffix():
    """A suffixed base keeps its lane suffix so downstream lane splitting still works."""
    resolve = lambda base: "uuid-x"                     # noqa: E731
    exists = lambda key: False                          # noqa: E731
    k, _ = akm.derive_mint_key("topfuel_ig", "Top Fuel", resolve_uuid=resolve,
                               gym_exists=exists)
    assert k.endswith("_ig")
    assert k[:-3] == ak.canonical_account_key("uuid-x", "Top Fuel")


def test_existing_gym_row_is_never_rekeyed():
    """THE RAIL: a gym that already has a local row under its key keeps that key untouched, so
    its already-signed link never strands. Even with a resolvable uuid, no re-key happens."""
    resolve = lambda base: "uuid-would-differ"          # noqa: E731
    exists = lambda key: key == "topfuel"               # noqa: E731  (row already exists)
    k, info = akm.derive_mint_key("topfuel", "Top Fuel", resolve_uuid=resolve,
                                  gym_exists=exists)
    assert k == "topfuel"
    assert info["derived"] is False
    assert "idempotent" in info["reason"]


def test_unresolved_uuid_keeps_passed_key_no_fabrication():
    """No portal uuid -> the passed key is kept verbatim; no gym_id is invented."""
    resolve = lambda base: None                         # noqa: E731
    exists = lambda key: False                          # noqa: E731
    k, info = akm.derive_mint_key("brandnew", "Brand New Gym", resolve_uuid=resolve,
                                  gym_exists=exists)
    assert k == "brandnew"
    assert info["derived"] is False and info["gym_uuid"] is None


def test_flag_off_keeps_passed_key(monkeypatch):
    monkeypatch.setenv("AGENT_CANONICAL_MINT", "false")
    resolve = lambda base: "uuid-x"                     # noqa: E731
    exists = lambda key: False                          # noqa: E731
    k, info = akm.derive_mint_key("topfuel", "Top Fuel", resolve_uuid=resolve,
                                  gym_exists=exists)
    assert k == "topfuel"
    assert "flag off" in info["reason"]


def test_existing_link_resolution_unchanged_by_mint_change():
    """A token minted BEFORE this change (under the raw ad-hoc key) still self-decodes to that
    same raw key via verify — the mint-guard never re-resolves or re-signs an existing link."""
    secret = b"unit-test-signing-secret-0123456789"
    old_token = it.mint("topfuel", secret=secret)       # a pre-existing signed link
    # Even though the doctor/mint would now prefer a canonical key, verify() is signature-only
    # and returns the ORIGINAL embedded key, proving old links are untouched.
    assert it.verify(old_token, secret=secret) == "topfuel"
    # And a token minted under the NEW canonical key verifies to the canonical key — i.e. new
    # and old links coexist, each resolving to its own embedded key.
    canonical = ak.canonical_account_key("uuid-topfuel-123", "Top Fuel")
    new_token = it.mint(canonical, secret=secret)
    assert it.verify(new_token, secret=secret) == canonical
    assert new_token != old_token


# =============================================================================
# 2. account_key_doctor.diagnose
# =============================================================================
_CLEAN_GYMS = [
    {"id": "uuid-topfuel", "slug": "top-fuel", "name": "Top Fuel Fitness"},
    {"id": "uuid-eng", "slug": "eng", "name": "Engine Gym"},
]


def test_doctor_flags_topfuel_style_unresolvable_base():
    """A base that NO gym slug can bridge (normalisation can't reach it) is UNRESOLVED."""
    store = _FakeStore(_CLEAN_GYMS, profiles={"uuid-topfuel": "prof-1", "uuid-eng": "prof-2"})
    summary = doc.diagnose(base="ghostgym", store=store)
    assert summary["rows"][0]["verdict"] == "UNRESOLVED"
    assert summary["stranded"] and summary["stranded"][0]["base"] == "ghostgym"


def test_doctor_passes_a_clean_base():
    """topfuel -> top-fuel resolves to exactly one live gym with a profile -> OK, not stranded."""
    store = _FakeStore(_CLEAN_GYMS, profiles={"uuid-topfuel": "prof-1"})
    summary = doc.diagnose(base="topfuel", store=store)
    row = summary["rows"][0]
    assert row["verdict"] == "OK"
    assert row["gym_uuid"] == "uuid-topfuel" and row["profile_id"] == "prof-1"
    assert summary["stranded"] == []


def test_doctor_flags_ambiguous_base():
    """A base whose normalisation matches TWO live gyms is AMBIGUOUS (refuse to guess)."""
    # Both slugs normalise to 'fitclub' -> resolve_gym_uuid's tier-2a sees >1 exact match and
    # returns None; the doctor's read-only probe then classifies it AMBIGUOUS.
    gyms = [
        {"id": "u1", "slug": "fit-club", "name": "Fit Club North"},
        {"id": "u2", "slug": "fit-club-", "name": "Fit Club South"},
    ]
    store = _FakeStore(gyms)
    summary = doc.diagnose(base="fitclub", store=store, expect_profile=False)
    assert summary["rows"][0]["verdict"] == "AMBIGUOUS"


def test_doctor_flags_archived_only_base():
    """When the only match is an archived / -dup ghost, the verdict is ARCHIVED_ONLY."""
    gyms = [{"id": "u1", "slug": "district-h-archived-dup", "name": "District H (archived)"}]
    store = _FakeStore(gyms)
    summary = doc.diagnose(base="districth", store=store, expect_profile=False)
    assert summary["rows"][0]["verdict"] == "ARCHIVED_ONLY"


def test_doctor_no_profile_is_soft_warning_not_stranding():
    """Resolves to a live gym but no Zernio profile yet -> NO_PROFILE (not a stranding verdict)."""
    store = _FakeStore(_CLEAN_GYMS, profiles={})  # no profiles bound
    summary = doc.diagnose(base="topfuel", store=store)
    assert summary["rows"][0]["verdict"] == "NO_PROFILE"
    assert summary["stranded"] == []  # NO_PROFILE is not a hard stranding verdict


# =============================================================================
# 3. account_key_doctor.fire_alerts
# =============================================================================
class _FakeKV:
    def __init__(self):
        self.store = {}

    def get(self, k, default=""):
        return self.store.get(k, default)

    def set(self, k, v):
        self.store[k] = v


def test_alert_fires_once_and_throttles(monkeypatch):
    monkeypatch.setenv("AGENT_ACCOUNT_KEY_DOCTOR_ALERTS", "true")
    store = _FakeStore(_CLEAN_GYMS)
    summary = doc.diagnose(base="ghostgym", store=store)
    assert summary["stranded"]

    fired = []
    kv = _FakeKV()
    alert = lambda m: fired.append(m)                    # noqa: E731

    # first pass at t=1000: fires once
    out1 = doc.fire_alerts(summary, alert=alert, kv=kv, now=1000.0)
    assert out1 == ["ghostgym"]
    assert len(fired) == 1 and "STRANDING RISK" in fired[0]

    # second pass shortly after: throttled, no new alert
    out2 = doc.fire_alerts(summary, alert=alert, kv=kv, now=1000.0 + 60)
    assert out2 == []
    assert len(fired) == 1

    # well past the throttle window: fires again
    out3 = doc.fire_alerts(summary, alert=alert, kv=kv,
                           now=1000.0 + doc.ALERT_THROTTLE_SECONDS + 1)
    assert out3 == ["ghostgym"]
    assert len(fired) == 2


def test_alert_flag_off_fires_nothing(monkeypatch):
    monkeypatch.setenv("AGENT_ACCOUNT_KEY_DOCTOR_ALERTS", "false")
    store = _FakeStore(_CLEAN_GYMS)
    summary = doc.diagnose(base="ghostgym", store=store)
    fired = []
    out = doc.fire_alerts(summary, alert=lambda m: fired.append(m), kv=_FakeKV(), now=1.0)
    assert out == [] and fired == []
    # force=True bypasses the flag (mirrors the token watchdog carrying its own gate)
    out2 = doc.fire_alerts(summary, alert=lambda m: fired.append(m), kv=_FakeKV(),
                           now=1.0, force=True)
    assert out2 == ["ghostgym"] and len(fired) == 1


def test_alert_clean_summary_fires_nothing(monkeypatch):
    monkeypatch.setenv("AGENT_ACCOUNT_KEY_DOCTOR_ALERTS", "true")
    store = _FakeStore(_CLEAN_GYMS, profiles={"uuid-topfuel": "prof-1"})
    summary = doc.diagnose(base="topfuel", store=store)
    fired = []
    out = doc.fire_alerts(summary, alert=lambda m: fired.append(m), kv=_FakeKV(), now=1.0)
    assert out == [] and fired == []
