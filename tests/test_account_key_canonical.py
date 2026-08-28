"""
Tests for the account_key collision fix (Bird Dog CrossFit / Bolton Club, live):
  1. canonical_account_key — pure derivation: tenant-unique, idempotent, disambiguating,
     never name-alone, never fabricates a name.
  2. account_key_guard.check_bind — refuses a cross-tenant rebind + alerts, dark by default.
  3. account_key_reconcile — dry-run plan for the live collisions WITHOUT applying;
     --apply gated + idempotent; never merges two gyms.

Fully OFFLINE: pure functions + injected readers/writers/alert sinks. No network, no creds.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import account_key as ak  # noqa: E402
from agent import account_key_guard as guard  # noqa: E402
from agent import account_key_reconcile as recon  # noqa: E402
from agent import config  # noqa: E402


# =============================================================================
# 1. canonical_account_key
# =============================================================================
def test_two_different_gyms_near_identical_names_get_different_keys():
    """The core bug: name-alike gyms must NOT collide. Different stable ids -> different keys."""
    k1 = ak.canonical_account_key("gym-uuid-birddog", "Bird Dog CrossFit")
    k2 = ak.canonical_account_key("gym-uuid-bolton", "Bird Dog Crossfit")  # near-identical name
    assert k1 != k2
    # both are clean slugs
    assert k1 == k1.lower() and k1.isalnum()
    assert k2 == k2.lower() and k2.isalnum()


def test_same_gym_re_derived_is_identical():
    """Determinism / idempotency: same (id, name) -> same key every time."""
    a = ak.canonical_account_key("gym-uuid-1", "Zanshin Fitness")
    b = ak.canonical_account_key("gym-uuid-1", "Zanshin Fitness")
    assert a == b


def test_issued_key_is_returned_verbatim_even_after_rename():
    """An already-issued key is authoritative: a rename never re-mints it."""
    issued = "zanshinfitness630e22"
    out = ak.canonical_account_key("gym-uuid-1", "Zanshin Wellness Collective",
                                   issued_key=issued)
    assert out == issued


def test_never_derives_from_name_alone_missing_id_raises():
    with pytest.raises(ValueError):
        ak.canonical_account_key("", "Bird Dog CrossFit")
    with pytest.raises(ValueError):
        ak.canonical_account_key(None, "Bird Dog CrossFit")


def test_blank_name_is_rejected_never_fabricated():
    with pytest.raises(ValueError):
        ak.canonical_account_key("gym-uuid-1", "")
    with pytest.raises(ValueError):
        ak.canonical_account_key("gym-uuid-1", "   !!!   ")  # no alphanumerics


def test_collision_on_taken_key_appends_deterministic_disambiguator():
    """If the computed base key is already TAKEN by another gym, walk 2,3,4... deterministically."""
    base = ak.canonical_account_key("gym-uuid-x", "Iron Gym")
    # is_taken says the base (and base+2) are taken -> must land on base+3, twice the same.
    taken = {base, base + "2"}
    d1 = ak.canonical_account_key("gym-uuid-x", "Iron Gym", is_taken=lambda k: k in taken)
    d2 = ak.canonical_account_key("gym-uuid-x", "Iron Gym", is_taken=lambda k: k in taken)
    assert d1 == d2 == base + "3"


def test_key_is_human_recognisable_name_prefix():
    k = ak.canonical_account_key("some-stable-id", "Bird Dog CrossFit")
    assert k.startswith("birddogcrossfit")


def test_disambiguator_exhaustion_raises_never_fabricates():
    with pytest.raises(RuntimeError):
        ak.canonical_account_key("gym-uuid-x", "Iron Gym",
                                 is_taken=lambda k: True, max_disambiguators=3)


# =============================================================================
# 2. account_key_guard.check_bind
# =============================================================================
def test_guard_dark_by_default_allows(monkeypatch):
    monkeypatch.delenv("AGENT_ACCOUNT_KEY_GUARD", raising=False)
    alerts = []
    d = guard.check_bind("gymA", "profile-1", alert=alerts.append)
    assert d.allowed is True
    assert d.code == "disabled"
    assert alerts == []


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_ACCOUNT_KEY_GUARD", "true")


def test_guard_blocks_rebind_of_key_to_different_profile_and_alerts(monkeypatch):
    _arm(monkeypatch)
    alerts = []
    d = guard.check_bind(
        "gymA", "profile-NEW",
        existing_profile_for=lambda k: "profile-OLD",
        key_for_profile=lambda p: None,
        alert=alerts.append)
    assert d.allowed is False
    assert d.code == "rebind_key"
    assert len(alerts) == 1 and "REFUSED account_key rebind" in alerts[0]


def test_guard_blocks_binding_profile_owned_by_a_different_gym_and_alerts(monkeypatch):
    _arm(monkeypatch)
    alerts = []
    # gymB tries to bind a profile already owned by gymA — the cross-tenant leak.
    d = guard.check_bind(
        "gymB", "profile-1",
        existing_profile_for=lambda k: None,
        key_for_profile=lambda p: "gymA",
        alert=alerts.append)
    assert d.allowed is False
    assert d.code == "steal_profile"
    assert len(alerts) == 1 and "REFUSED cross-tenant bind" in alerts[0]


def test_guard_allows_idempotent_same_binding(monkeypatch):
    _arm(monkeypatch)
    alerts = []
    # Re-binding the SAME key to the SAME profile it already owns is a no-op, allowed.
    d = guard.check_bind(
        "gymA", "profile-1",
        existing_profile_for=lambda k: "profile-1",
        key_for_profile=lambda p: "gymA",
        alert=alerts.append)
    assert d.allowed is True
    assert d.code == "ok"
    assert alerts == []


def test_guard_allows_fresh_binding(monkeypatch):
    _arm(monkeypatch)
    alerts = []
    d = guard.check_bind(
        "gymA", "profile-1",
        existing_profile_for=lambda k: None,
        key_for_profile=lambda p: None,
        alert=alerts.append)
    assert d.allowed is True and alerts == []


def test_guard_empty_inputs_are_noop_allow(monkeypatch):
    _arm(monkeypatch)
    assert guard.check_bind("", "profile-1").allowed is True
    assert guard.check_bind("gymA", "").allowed is True


# =============================================================================
# 3. account_key_reconcile — the LIVE collisions, dry-run
# =============================================================================
# The live collision: Bird Dog CrossFit and Bolton Club both landed on ONE key.
_LIVE_COLLISION = [
    {"gym_id": "uuid-birddog", "name": "Bird Dog CrossFit",
     "account_key": "birddog", "has_social_product": True},
    {"gym_id": "uuid-bolton", "name": "Bolton Club",
     "account_key": "birddog", "has_social_product": True},  # collided onto the SAME key
    {"gym_id": "uuid-zanshin", "name": "Zanshin Fitness",
     "account_key": "", "has_social_product": True},          # STRANDED (missing key)
    {"gym_id": "uuid-noproduct", "name": "No Social Gym",
     "account_key": "", "has_social_product": False},         # excluded (no product)
]


def test_dry_run_plan_for_live_collision_separates_both_gyms_without_merging():
    plan = recon.build_plan(_LIVE_COLLISION)
    by_gym = {r["gym_id"]: r for r in plan}

    # the no-product gym is not in the plan at all
    assert "uuid-noproduct" not in by_gym
    assert set(by_gym) == {"uuid-birddog", "uuid-bolton", "uuid-zanshin"}

    bd = by_gym["uuid-birddog"]
    bo = by_gym["uuid-bolton"]
    za = by_gym["uuid-zanshin"]

    # both collided gyms are flagged COLLIDED and get DIFFERENT canonical keys (never merged)
    assert bd["status"] == "COLLIDED"
    assert bo["status"] == "COLLIDED"
    assert bd["canonical"] != bo["canonical"]
    # each canonical key folds in its own stable id -> tenant-unique
    assert bd["canonical"].startswith("birddogcrossfit")
    assert bo["canonical"].startswith("boltonclub")
    # the stranded gym is MISSING and now gets a canonical key
    assert za["status"] == "MISSING"
    assert za["canonical"].startswith("zanshinfitness")
    assert za["change"] is True


def test_reconcile_is_idempotent_second_pass_is_all_ok():
    """After canonicalisation, re-running plans everything OK with no change."""
    plan1 = recon.build_plan(_LIVE_COLLISION)
    # simulate applying: each gym now carries its canonical key
    applied = []
    for r in plan1:
        src = next(x for x in _LIVE_COLLISION if x["gym_id"] == r["gym_id"])
        applied.append({**src, "account_key": r["canonical"]})
    plan2 = recon.build_plan(applied)
    assert all(r["status"] == "OK" for r in plan2), [r for r in plan2 if r["status"] != "OK"]
    assert all(r["change"] is False for r in plan2)


def test_reconcile_dry_run_default_never_writes(monkeypatch):
    writes = []
    summary = recon.reconcile(reader=lambda: list(_LIVE_COLLISION), apply=False,
                              writer=lambda row: writes.append(row) or (True, "ok"))
    assert summary["apply"] is False
    assert summary["applied"] == []
    assert writes == []  # dry-run never touched the writer


def test_apply_writer_is_flag_gated_off_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_ACCOUNT_KEY_RECONCILE", raising=False)
    ok, detail = recon._default_writer(
        {"gym_id": "uuid-birddog", "canonical": "birddogcrossfitabc123"})
    assert ok is False
    assert "disabled" in detail


def test_apply_only_writes_changed_rows(monkeypatch):
    writes = []
    summary = recon.reconcile(
        reader=lambda: list(_LIVE_COLLISION), apply=True,
        writer=lambda row: (writes.append(row["gym_id"]) or (True, "updated")))
    # zanshin (missing) + both collided gyms change; none is skipped, none merged
    assert set(writes) == {"uuid-birddog", "uuid-bolton", "uuid-zanshin"}
    assert all(a["ok"] for a in summary["applied"])


def test_reconcile_single_gym_filter():
    summary = recon.reconcile(gym_id="uuid-zanshin",
                              reader=lambda: list(_LIVE_COLLISION), apply=False)
    assert len(summary["plan"]) == 1
    assert summary["plan"][0]["gym_id"] == "uuid-zanshin"


def test_print_plan_renders_without_crashing(capsys):
    summary = recon.reconcile(reader=lambda: list(_LIVE_COLLISION), apply=False)
    recon.print_plan(summary)
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "Bird Dog CrossFit" in out
    assert "Bolton Club" in out


# =============================================================================
# 4. Guard wired into the REAL bind choke point (_persist_profile_id)
# =============================================================================
def test_persist_profile_id_blocks_cross_tenant_bind_end_to_end(tmp_path, monkeypatch):
    """The guard, armed, must stop _persist_profile_id from wiring one gym's key to a
    profile another gym already owns — and must NOT write either plane."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_ACCOUNT_KEY_GUARD", "true")
    from agent import zernio_routes as zr
    from agent import db as _db
    from agent import account_key_guard as _akg

    # gymA legitimately owns profile-1.
    _db.gym_upsert("gymA", display_name="Gym A", zernio_profile_id="profile-1")

    fired = []
    monkeypatch.setattr(_akg.ops_alerts, "alert", lambda m, **k: fired.append(m))
    # No shared plane in this test (creds absent) — the local write is what we assert on.
    monkeypatch.setattr(zr, "_shared_store", lambda: None)

    # gymB tries to grab profile-1 -> BLOCKED, gymB's row never gets the profile id.
    zr._persist_profile_id("gymB", "profile-1")
    assert (_db.gym_get("gymB") or {}).get("zernio_profile_id") in (None, "")
    assert len(fired) == 1 and "REFUSED cross-tenant bind" in fired[0]
    # gymA's binding is untouched.
    assert _db.gym_get("gymA")["zernio_profile_id"] == "profile-1"


def test_persist_profile_id_allows_normal_bind_when_armed(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_ACCOUNT_KEY_GUARD", "true")
    from agent import zernio_routes as zr
    from agent import db as _db
    monkeypatch.setattr(zr, "_shared_store", lambda: None)

    zr._persist_profile_id("gymA", "profile-1")
    assert _db.gym_get("gymA")["zernio_profile_id"] == "profile-1"


def test_persist_profile_id_dark_guard_preserves_old_behaviour(tmp_path, monkeypatch):
    """With the guard OFF (default), a cross-tenant bind is NOT blocked — behaviour is
    exactly today's. (This is why the flag must be armed by hand.)"""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.delenv("AGENT_ACCOUNT_KEY_GUARD", raising=False)
    from agent import zernio_routes as zr
    from agent import db as _db
    monkeypatch.setattr(zr, "_shared_store", lambda: None)

    _db.gym_upsert("gymA", display_name="Gym A", zernio_profile_id="profile-1")
    zr._persist_profile_id("gymB", "profile-1")  # dark -> allowed (unchanged behaviour)
    assert _db.gym_get("gymB")["zernio_profile_id"] == "profile-1"


def test_db_key_for_zernio_profile_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    from agent import db as _db
    _db.gym_upsert("gymA", display_name="A", zernio_profile_id="p-9")
    assert _db.gym_key_for_zernio_profile("p-9") == "gymA"
    assert _db.gym_key_for_zernio_profile("nope") is None
    assert _db.gym_key_for_zernio_profile("") is None


# =============================================================================
# 5. resolve_gym_uuid — base != gyms.slug (topfuel/district_h/hillcountry, live)
# =============================================================================
# Live gyms rows the coordinator supplied, INCLUDING the archived dup that must never win.
_LIVE_GYMS = [
    {"id": "uuid-topfuel", "slug": "top-fuel", "name": "Top Fuel"},
    {"id": "uuid-districth", "slug": "district-h-strength-fitness",
     "name": "District H Strength & Fitness"},
    {"id": "uuid-districth-dup", "slug": "district-h-archived-dup",
     "name": "District H (archived, use district-h-strength-fitness)"},
    {"id": "uuid-hillcountry", "slug": "hill-country", "name": "Hill Country MVMT"},
    {"id": "uuid-eng", "slug": "eng", "name": "Engine CrossFit"},
    {"id": "uuid-gritx", "slug": "gritx", "name": "GritX"},
    {"id": "uuid-birddog", "slug": "bird-dog-crossfit", "name": "Bird Dog CrossFit"},
    {"id": "uuid-bolton", "slug": "bolton-club", "name": "Bolton Club"},
]


class _FakeResp:
    def __init__(self, data):
        self.status_code = 200
        self._d = data
        self.text = ""

    def json(self):
        return self._d


class _FakeGymsHttp:
    """Serves the gyms table for resolve_gym_uuid: slug/id exact + a bare-select full list."""

    def get(self, url, params=None, headers=None, timeout=None):
        p = params or {}
        if "slug" in p:
            v = p["slug"].replace("eq.", "")
            return _FakeResp([g for g in _LIVE_GYMS if g["slug"] == v])
        if "id" in p:
            v = p["id"].replace("eq.", "")
            return _FakeResp([g for g in _LIVE_GYMS if g["id"] == v])
        return _FakeResp(list(_LIVE_GYMS))


def _store():
    from agent.portal_calendar_store import SupabaseCalendarStore
    return SupabaseCalendarStore(url="http://x", service_key="k", http=_FakeGymsHttp())


def test_resolve_gym_uuid_exact_slug_when_base_equals_slug():
    s = _store()
    assert s.resolve_gym_uuid("eng") == "uuid-eng"
    assert s.resolve_gym_uuid("gritx") == "uuid-gritx"


def test_resolve_gym_uuid_normalised_match_topfuel_and_hillcountry():
    s = _store()
    assert s.resolve_gym_uuid("topfuel") == "uuid-topfuel"        # top-fuel
    assert s.resolve_gym_uuid("hillcountry") == "uuid-hillcountry"  # hill-country


def test_resolve_gym_uuid_containment_district_h_never_the_archived_dup():
    s = _store()
    # district_h must map to the LIVE row, never the -archived-dup ghost.
    assert s.resolve_gym_uuid("district_h") == "uuid-districth"


def test_resolve_gym_uuid_wrong_registry_string_still_maps_to_same_gym():
    s = _store()
    # the WRONG registry string hillcountrymvmt still resolves to the ONE hill-country gym,
    # so the reconciler can migrate the mismatched key onto the canonical one.
    assert s.resolve_gym_uuid("hillcountrymvmt") == "uuid-hillcountry"


def test_resolve_gym_uuid_unknown_base_is_none_never_a_guess():
    s = _store()
    assert s.resolve_gym_uuid("totally-unknown-gym") is None
    assert s.resolve_gym_uuid("") is None


def test_canonical_key_is_stable_across_the_disagreeing_identifiers():
    """The whole point: hillcountry and hillcountrymvmt (two registry strings, one gym)
    resolve to the SAME uuid and therefore the SAME canonical key."""
    s = _store()
    u1 = s.resolve_gym_uuid("hillcountry")
    u2 = s.resolve_gym_uuid("hillcountrymvmt")
    assert u1 == u2
    k1 = ak.canonical_account_key(u1, "Hill Country MVMT")
    k2 = ak.canonical_account_key(u2, "Hill Country MVMT")
    assert k1 == k2
