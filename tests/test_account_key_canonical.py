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


class _FakeCadenceHttp(_FakeGymsHttp):
    """gyms + echo_gym_settings, so the cadence accessors can be exercised end to end.
    Top Fuel (base 'topfuel', slug 'top-fuel') is the base != slug case."""

    def __init__(self):
        self.settings = {"uuid-topfuel": 2, "uuid-eng": 1}
        self.written = []

    def get(self, url, params=None, headers=None, timeout=None):
        if "echo_gym_settings" in url:
            uuid = (params or {}).get("gym_id", "").replace("eq.", "")
            select = (params or {}).get("select", "")
            if select == "autonomous":
                auto = getattr(self, "autonomy", {})
                if uuid in auto:
                    return _FakeResp([{"autonomous": auto[uuid]}])
                return _FakeResp([])
            if uuid in self.settings:
                return _FakeResp([{"posts_per_day": self.settings[uuid]}])
            return _FakeResp([])
        return super().get(url, params=params, headers=headers, timeout=timeout)

    def post(self, url, params=None, headers=None, json=None, timeout=None):
        self.written.append(json)
        return _FakeResp(json or [])


def _cadence_store(http):
    from agent.portal_calendar_store import SupabaseCalendarStore
    return SupabaseCalendarStore(url="http://x", service_key="k", http=http)


def test_gym_posts_per_day_resolves_when_base_differs_from_slug():
    """Dale/ENG 2026-08-30 regression: the cadence reader used a raw slug=eq.<base>
    match, so every gym whose account-registry base differs from its gyms.slug read
    None and silently built at 1x no matter what its owner had toggled."""
    s = _cadence_store(_FakeCadenceHttp())
    assert s.gym_posts_per_day("topfuel") == 2   # base 'topfuel' -> slug 'top-fuel'
    assert s.gym_posts_per_day("eng") == 1       # base == slug still works
    assert s.gym_posts_per_day("nosuchgym") is None


def test_set_gym_posts_per_day_writes_when_base_differs_from_slug():
    """The writer had the same miss, and handle_cadence turns a False into a 503 —
    so those owners could not save a cadence at all."""
    http = _FakeCadenceHttp()
    s = _cadence_store(http)
    assert s.set_gym_posts_per_day("topfuel", 2, actor="client") is True
    assert http.written and http.written[0][0]["gym_id"] == "uuid-topfuel"
    assert http.written[0][0]["posts_per_day"] == 2
    assert s.set_gym_posts_per_day("nosuchgym", 2) is False
    assert s.set_gym_posts_per_day("topfuel", 3) is False   # only 1 or 2 is valid


def test_gym_autonomy_resolves_when_base_differs_from_slug():
    """Same base != slug defect as the cadence pair: the Autonomous toggle was silently
    a no-op for every gym whose base differs from its gyms.slug. Verified before the
    change that only lasso is autonomous=true (and it reads from local kv first), so a
    False gym still reads False — approval required stays the safe default."""
    http = _FakeCadenceHttp()
    http.autonomy = {"uuid-topfuel": False, "uuid-eng": False}
    s = _cadence_store(http)
    assert s.gym_autonomy("topfuel") is False   # base 'topfuel' -> slug 'top-fuel'
    assert s.gym_autonomy("eng") is False
    assert s.gym_autonomy("nosuchgym") is None  # unknown gym -> None, never True


def test_set_gym_autonomy_writes_when_base_differs_from_slug():
    http = _FakeCadenceHttp()
    http.autonomy = {}
    s = _cadence_store(http)
    assert s.set_gym_autonomy("topfuel", True, actor="owner") is True
    assert http.written and http.written[0][0]["gym_id"] == "uuid-topfuel"
    assert http.written[0][0]["autonomous"] is True
    assert s.set_gym_autonomy("nosuchgym", True) is False


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


def test_resolve_gym_uuid_never_resolves_an_unrelated_gym_onto_a_short_slug():
    """THE CROSS-TENANT RESOLVER (found 2026-08-31).

    The containment tier used a bare startswith in BOTH directions. Against this very
    fixture, the gym slugged 'eng' swallowed every base that merely began with those
    three letters, so a brand new gym called Engage Fitness Denver would silently read
    ENG's uuid — and therefore ENG's Zernio profile, settings, GBP connection and
    calendar. No error, no alert, wrong tenant. These bases share a prefix with a real
    gym and are NOT that gym; each must resolve to None rather than a guess."""
    s = _store()
    for stranger in ("engagefitnessdenver", "england", "engineering",
                     "gritxtreme", "birddogwalking", "boltonclubhouse"):
        assert s.resolve_gym_uuid(stranger) is None, \
            f"{stranger!r} must never resolve onto an unrelated gym"


def test_resolve_gym_uuid_still_maps_a_canonical_key_back_to_its_gym():
    """The reverse direction exists for exactly one shape: a canonically minted key is
    its name-slug PLUS a 6-hex fingerprint (account_key.py). That must keep resolving,
    or every canonically keyed gym goes dark — which is the opposite failure."""
    s = _store()
    for gym_id, name, expected in (
        ("uuid-topfuel", "Top Fuel", "uuid-topfuel"),
        ("uuid-birddog", "Bird Dog CrossFit", "uuid-birddog"),
        ("uuid-hillcountry", "Hill Country MVMT", "uuid-hillcountry"),
    ):
        key = ak.canonical_account_key(gym_id, name)
        assert s.resolve_gym_uuid(key) == expected, \
            f"canonical key {key!r} must resolve back to {expected}"


def test_containment_match_is_boundary_and_shape_precise():
    """Unit-pins the two narrow rules directly, so deleting either one fails here even
    if the resolver's fixture happens to hide it."""
    import agent.portal_calendar_store as pcs
    cm = pcs._containment_match
    # forward: word-boundary prefix of the slug is fine, mid-word is not
    assert cm("districth", "districthstrengthfitness", "district-h-strength-fitness", "")
    assert not cm("distric", "districthstrengthfitness", "district-h-strength-fitness", "")
    assert not cm("eng", "engagefitnessdenver", "engage-fitness-denver", "")
    # reverse: only the canonical <slug><6 hex> tail
    assert cm("topfuela1b2c3", "topfuel", "top-fuel", "Top Fuel")
    assert not cm("topfuelinjection", "topfuel", "top-fuel", "Top Fuel")
    # the name is a legitimate second source (hillcountrymvmt -> 'Hill Country MVMT')
    assert cm("hillcountrymvmt", "hillcountry", "hill-country", "Hill Country MVMT")
    assert not cm("hillcountryclub", "hillcountry", "hill-country", "Hill Country MVMT")


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


# 6. the resolver CACHE (scale audit 2026-08-30) ------------------------------------
class _CountingGymsHttp(_FakeGymsHttp):
    """_FakeGymsHttp that counts every gyms read, so the cache can be MEASURED."""

    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        return super().get(url, params=params, headers=headers, timeout=timeout)


def _counting_store(http):
    from agent.portal_calendar_store import SupabaseCalendarStore
    return SupabaseCalendarStore(url="http://x", service_key="k", http=http)


def test_repeat_resolves_of_the_same_base_cost_one_read_not_one_per_tick():
    """publish_client_gyms resolves EVERY gym on a ~1 minute tick. For a base != slug
    gym that was three gyms reads a tick, the third pulling the whole table unfiltered.
    At 100 gyms that is ~24k Supabase calls an hour, issued serially inside one tick."""
    http = _CountingGymsHttp()
    s = _counting_store(http)
    assert s.resolve_gym_uuid("topfuel") == "uuid-topfuel"
    first = http.calls
    assert first >= 2, "topfuel is the base != slug case, so it costs several reads"
    for _ in range(60):                       # an hour of ticks
        assert s.resolve_gym_uuid("topfuel") == "uuid-topfuel"
    assert http.calls == first, "a repeat resolve must cost NOTHING"


def test_a_miss_is_never_cached_so_a_new_gym_resolves_immediately():
    """Caching a miss would reintroduce the exact stranding this resolver exists to
    kill: a gym registering a moment from now must resolve on its very next tick,
    not after a six hour TTL."""
    http = _CountingGymsHttp()
    s = _counting_store(http)
    assert s.resolve_gym_uuid("nosuchgym") is None
    after_miss = http.calls
    assert s.resolve_gym_uuid("nosuchgym") is None
    assert http.calls > after_miss, "a miss must be re-read, never served from cache"


def test_the_cache_never_confuses_two_gyms():
    http = _CountingGymsHttp()
    s = _counting_store(http)
    assert s.resolve_gym_uuid("topfuel") == "uuid-topfuel"
    assert s.resolve_gym_uuid("eng") == "uuid-eng"
    assert s.resolve_gym_uuid("topfuel") == "uuid-topfuel"


def test_an_expired_entry_is_re_read():
    import agent.portal_calendar_store as pcs
    http = _CountingGymsHttp()
    s = _counting_store(http)
    assert s.resolve_gym_uuid("topfuel") == "uuid-topfuel"
    before = http.calls
    # age the entry past its TTL
    uuid, _exp = pcs._UUID_CACHE["topfuel"]
    pcs._UUID_CACHE["topfuel"] = (uuid, 0)
    assert s.resolve_gym_uuid("topfuel") == "uuid-topfuel"
    assert http.calls > before, "a stale entry must be re-read"


# 7. --apply must never SPLIT A LIVE GYM IN TWO (Blake's ruling, 2026-08-31) -----------
from agent import account_key_reconcile as akr  # noqa: E402


def _rec(gym_id, name, key):
    return {"gym_id": gym_id, "name": name, "account_key": key,
            "has_social_product": True}


def _counter(**kw):
    """A data probe for ONE key: everything else reads as an unused key."""
    def _c(account_key):
        return kw.get(account_key,
                      {"sources": 0, "calendar": 0, "voice": False, "library": False})
    return _c


def test_a_collided_gym_with_data_is_blocked_not_repointed():
    """THE SPLIT. --apply rewrites exactly ONE field, echo_intake_tokens.
    echo_account_key: it moves the POINTER and moves no DATA, so every source, calendar
    row, voice doc and media folder stays under the OLD key and the gym is handed an
    empty one. Echo then reads zero approved sources, the no-fabrication gate refuses to
    draft, and the gym goes quiet with its scheduled rows orphaned.

    NOTE the real blast radius, measured 2026-08-31: a NON-collided gym is never
    re-pointed at all (build_plan treats its current key as ISSUED, so canonical ==
    current and change is False). Pierce Fitness, with 155 calendar rows and 17 sources,
    plans OK and is never touched. The hazard is real only for a COLLIDED or
    MISSING-key gym, which is exactly what this test builds: two gyms sharing one key,
    where the one holding the data must still be refused."""
    wrote = []
    out = akr.reconcile(
        reader=lambda: [_rec("g-a", "Bird Dog CrossFit", "shared"),
                        _rec("g-b", "Bolton Club", "shared")],
        writer=lambda row: wrote.append(row["gym_id"]) or (True, "updated"),
        logger=lambda m: None, apply=True,
        data_counter=_counter(shared={"sources": 17, "calendar": 155,
                                      "voice": False, "library": True}))
    assert wrote == [], "a collided gym holding data was split from it"
    for row in out["plan"]:
        assert row["status"] == "BLOCKED"
        assert "strand" in row["error"] and "Migrate first" in row["error"]
    assert all(a["ok"] is False for a in out["applied"])


def test_a_non_collided_live_gym_is_never_repointed_at_all():
    """Pierce's real shape: a unique current key is treated as ISSUED, so there is no
    change to make and the guard never even has to fire."""
    plan = akr.build_plan([_rec("g-pierce", "Pierce Fitness", "piercefitness")])
    assert plan[0]["status"] == "OK" and plan[0]["change"] is False


def test_an_unused_key_is_still_reconciled():
    """The guard must not disable the tool. A key holding NOTHING is exactly the case
    reconcile exists for, and it must still be re-pointed."""
    wrote = []
    akr.reconcile(
        reader=lambda: [_rec("g-fresh", "Fresh Box", "")],   # MISSING key: nothing to strand
        writer=lambda row: wrote.append(row) or (True, "updated"),
        logger=lambda m: None, apply=True, data_counter=_counter())
    assert len(wrote) == 1, "a gym with no key at all should still be reconciled"


def test_each_kind_of_data_blocks_on_its_own():
    for label, probe in (
            ("sources", {"sources": 3, "calendar": 0, "voice": False, "library": False}),
            ("calendar", {"sources": 0, "calendar": 9, "voice": False, "library": False}),
            ("voice", {"sources": 0, "calendar": 0, "voice": True, "library": False}),
            ("library", {"sources": 0, "calendar": 0, "voice": False, "library": True})):
        assert akr.blocking_data("k", counter=_counter(k=probe)), f"{label} did not block"


def test_an_unreadable_probe_blocks_rather_than_assuming_empty():
    """If we cannot tell whether a gym has data, the safe answer is that it does.
    Assuming empty is how a healthy gym gets silently re-pointed."""
    probe = {"sources": -1, "calendar": -1, "voice": False, "library": False}
    reason = akr.blocking_data("k", counter=_counter(k=probe))
    assert "unreadable" in reason


def test_the_writer_itself_refuses_a_key_with_data(monkeypatch):
    """Defence in depth: reconcile() skips a blocked row, but a script or a hand-run
    that calls the writer directly must be refused too."""
    monkeypatch.setenv("AGENT_ACCOUNT_KEY_RECONCILE", "true")
    monkeypatch.setattr(akr, "blocking_data", lambda k, **kw: "has stuff")
    ok, detail = akr._default_writer({"gym_id": "g1", "canonical": "newkey",
                                      "current": "oldkey"})
    assert ok is False and detail.startswith("BLOCKED:")
