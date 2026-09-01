"""
Account registry: blake_personal is an INACTIVE record (Meta ended personal-profile
publishing in 2018). It must not be drafted for, but stays discoverable for history.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import accounts  # noqa: E402


def test_active_accounts_excludes_blake_personal():
    keys = [a.key for a in accounts.active_accounts()]
    assert keys == ["lasso_ig", "lasso_fb"]
    assert "blake_personal" not in keys


def test_blake_personal_kept_as_inactive_record():
    a = accounts.get_account("blake_personal")     # still discoverable (history kept)
    assert a is not None
    assert a.active is False


def test_lasso_accounts_untouched_and_active():
    for key in ("lasso_ig", "lasso_fb"):
        a = accounts.get_account(key)
        assert a is not None and a.active is True


def test_dynamic_accounts_off_is_unchanged(monkeypatch):
    """Flag OFF: no registry is read; only hardcoded accounts exist."""
    monkeypatch.delenv("AGENT_DYNAMIC_ACCOUNTS", raising=False)
    accounts._dynamic_cache = None
    assert accounts.get_account("zzz_ig") is None
    assert accounts.register_gym("zzz", name="Z") == []      # no-op when OFF


def test_dynamic_account_auto_provision_and_isolation(monkeypatch, tmp_path):
    """Flag ON: register_gym persists an inactive Account pair that resolves via
    get_account, never enters active_accounts, and vanishes when the flag flips OFF."""
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    accounts._dynamic_cache = None
    keys = accounts.register_gym("boxfit", name="Box Fit", ig_handle="@boxfit")
    assert keys == ["boxfit_ig", "boxfit_fb"]
    a = accounts.get_account("boxfit_ig")
    assert a is not None and a.active is False
    assert a.token_env == "AGENT_BOXFIT_IG_TOKEN"
    assert a.voice_doc == "brand_voice/boxfit/lasso_voice.md"
    assert a.library_prefix == "content_library/boxfit"
    # inactive -> never in the daily run
    assert "boxfit_ig" not in [x.key for x in accounts.active_accounts()]
    # hardcoded LASSO still wins / present
    assert accounts.get_account("lasso_ig").active is True
    monkeypatch.delenv("AGENT_DYNAMIC_ACCOUNTS", raising=False)
    accounts._dynamic_cache = None
    assert accounts.get_account("boxfit_ig") is None         # gone when OFF


def test_dynamic_accounts_picks_up_an_external_edit_without_restart(monkeypatch, tmp_path):
    """LIVE INCIDENT (2026-09-01): the daemon's cache was keyed on the registry PATH
    only, which never changes for the process's whole life. An ops fix applied from
    a SEPARATE process (railway ssh, exactly how every registry repair was applied
    tonight) edited the file on disk correctly, but the long-running daemon kept
    serving the OLD list for hours — a dead gym key resurrected a stale sample book
    under it well after every other store was clean. register_gym's own
    `_dynamic_cache = None` only helps a caller in the SAME process; this simulates
    the cross-process case directly by writing the file with plain I/O, the way an
    external script does, never touching this process's cache variable."""
    import json
    import time

    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    reg = tmp_path / "reg.json"
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(reg))
    accounts._dynamic_cache = None

    reg.write_text(json.dumps([{"base": "deadgym", "name": "Dead Gym"}]))
    assert accounts.get_account("deadgym_ig") is not None   # cache primed, sees it

    # An EXTERNAL process repairs the file — this process's cache is never told.
    time.sleep(0.01)   # ensure a distinct mtime on every filesystem's clock resolution
    reg.write_text(json.dumps([{"base": "realgym", "name": "Real Gym"}]))

    # No accounts._dynamic_cache = None here — proving the mtime check alone
    # is what makes the next read see the repaired file.
    assert accounts.get_account("deadgym_ig") is None, \
        "the dead gym must not resolve once the file was repaired, even without " \
        "an in-process cache invalidation"
    assert accounts.get_account("realgym_ig") is not None


def test_register_gym_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    accounts._dynamic_cache = None
    accounts.register_gym("boxfit", name="Box Fit")
    accounts.register_gym("boxfit", name="Box Fit Renamed")  # update in place
    import json
    rows = json.load(open(tmp_path / "reg.json"))
    assert len([r for r in rows if r["base"] == "boxfit"]) == 1
    assert accounts.get_account("boxfit_ig").display_name == "Box Fit Renamed IG"
    accounts._dynamic_cache = None


def test_eng_registered_as_client_gym():
    """CrossFit ENG onboarded into Echo 2026-08-12 (its social intake was captured
    but never routed). Registered like other client gyms: inactive (posts via the
    client/draft-on-upload path, not LASSO's daily run), with its own voice doc."""
    for key in ("eng_ig", "eng_fb"):
        a = accounts.get_account(key)
        assert a is not None, f"{key} missing from the registry"
        assert a.active is False                 # client gyms are not in the daily run
        assert a.voice_doc == "brand_voice/eng/lasso_voice.md"
        assert a.library_prefix == "content_library/eng"


# ---- registry durability (scale audit 2026-08-30) --------------------------------
def _reg_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    path = tmp_path / "gym_accounts.json"
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(path))
    from agent import accounts
    accounts._dynamic_cache = None
    return accounts, path


def test_a_corrupt_registry_is_never_saved_over(monkeypatch, tmp_path):
    """THE FLEET-ERASER. register_gym opened the real path 'w', which truncates
    first, so a crash or container restart mid-write left a partial file.
    _load_registry_rows read that as 'no gyms', and the NEXT registration then
    wrote a one-row registry, making that loss permanent. This is how gyms go
    invisible to Echo all at once. The writer must refuse rather than save over
    something it could not read."""
    accounts, path = _reg_env(monkeypatch, tmp_path)
    path.write_text('[{"base": "eng"}, {"base": "gri')      # truncated mid-write
    monkeypatch.setattr("agent.ops_alerts.alert", lambda m: None)
    with pytest.raises(accounts.RegistryUnreadable):
        accounts.register_gym("newgym", name="New Gym")
    assert path.read_text().startswith('[{"base": "eng"}'), "the corrupt file was overwritten"


def test_a_corrupt_registry_alerts_rather_than_reading_as_empty(monkeypatch, tmp_path):
    accounts, path = _reg_env(monkeypatch, tmp_path)
    path.write_text("{not json")
    seen = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda m: seen.append(m))
    assert accounts._load_registry_rows() == []          # readers stay lenient
    assert len(seen) == 1 and "invisible" in seen[0]


def test_a_missing_registry_is_simply_no_gyms_and_never_alerts(monkeypatch, tmp_path):
    """A gym-less registry is the normal first-run state, not an incident."""
    accounts, _path = _reg_env(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda m: seen.append(m))
    assert accounts._load_registry_rows() == []
    assert seen == []


def test_a_failed_write_leaves_the_previous_registry_intact(monkeypatch, tmp_path):
    """Atomicity: the registry is only ever the old complete file or the new
    complete one, never half of either."""
    accounts, path = _reg_env(monkeypatch, tmp_path)
    accounts.register_gym("eng", name="ENG")
    before = path.read_text()
    assert "eng" in before

    import json as _json
    def _boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(_json, "dump", _boom)
    with pytest.raises(OSError):
        accounts.register_gym("newgym", name="New Gym")
    assert path.read_text() == before, "a failed write damaged the registry"


def test_registering_a_second_gym_never_drops_the_first(monkeypatch, tmp_path):
    accounts, path = _reg_env(monkeypatch, tmp_path)
    accounts.register_gym("eng", name="ENG")
    accounts.register_gym("gritx", name="GritX")
    bases = {r["base"] for r in accounts._load_registry_rows()}
    assert bases == {"eng", "gritx"}
    assert not list(tmp_path.glob("*.tmp.*")), "a temp file was left behind"


# ---- register_gym gym_id write-path guard (Sunnyside/Swift River split-key class,
# 2026-08-31: the same real gym registered under TWO different base strings, so
# all_accounts() served both keys forever). register_gym is the ONE place a registry
# row is created; the guard lives there so a second live row for one gym_id is
# structurally impossible, not merely detected after the fact. -----------------------

def test_same_gym_id_twice_never_produces_two_live_registry_rows(monkeypatch, tmp_path):
    """THE PIN. Registering the same gym_id twice under two different candidate bases
    must never leave two rows in the registry: the second call is refused and returns
    the FIRST base's keys instead of forking a second row."""
    accounts, path = _reg_env(monkeypatch, tmp_path)
    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda m: alerts.append(m))

    first_keys = accounts.register_gym(
        "swiftrivercrossfitd23567", name="Swift River CrossFit", gym_id="gym-uuid-1")
    assert first_keys == ["swiftrivercrossfitd23567_ig", "swiftrivercrossfitd23567_fb"]

    # Second onboarding path for the SAME gym_id, a DIFFERENT candidate base (the
    # legacy/manual-key or re-derived-canonical-key shape).
    second_keys = accounts.register_gym(
        "swiftrivercrossfite5c9db", name="Swift River CrossFit", gym_id="gym-uuid-1")

    # No fork: the second call is handed back the FIRST base's keys.
    assert second_keys == first_keys

    rows = accounts._load_registry_rows()
    matching = [r for r in rows if r.get("gym_id") == "gym-uuid-1"]
    assert len(matching) == 1, f"gym_id gym-uuid-1 has {len(matching)} live registry rows"
    assert matching[0]["base"] == "swiftrivercrossfitd23567"
    assert accounts.get_account("swiftrivercrossfite5c9db_ig") is None, \
        "the refused second base must never resolve to an Account"
    assert len(alerts) == 1 and "refused to register" in alerts[0]


def test_register_gym_stamps_and_preserves_gym_id(monkeypatch, tmp_path):
    """A re-registration that omits gym_id (a caller with no signal, e.g. the legacy
    call sites before this fix) must never erase a gym_id already stamped."""
    accounts, path = _reg_env(monkeypatch, tmp_path)
    accounts.register_gym("boxfit", name="Box Fit", gym_id="gym-uuid-2")
    accounts.register_gym("boxfit", name="Box Fit Renamed")   # no gym_id this time
    rows = accounts._load_registry_rows()
    row = next(r for r in rows if r["base"] == "boxfit")
    assert row["gym_id"] == "gym-uuid-2"
    assert row["name"] == "Box Fit Renamed"


def test_find_base_for_gym_id(monkeypatch, tmp_path):
    accounts, path = _reg_env(monkeypatch, tmp_path)
    assert accounts.find_base_for_gym_id("gym-uuid-3") is None
    accounts.register_gym("topfuel", name="Top Fuel", gym_id="gym-uuid-3")
    assert accounts.find_base_for_gym_id("gym-uuid-3") == "topfuel"
    assert accounts.find_base_for_gym_id("") is None


def test_different_gym_ids_register_independently(monkeypatch, tmp_path):
    """The guard must never block two genuinely DIFFERENT gyms, even with similar bases."""
    accounts, path = _reg_env(monkeypatch, tmp_path)
    monkeypatch.setattr("agent.ops_alerts.alert", lambda m: None)
    a = accounts.register_gym("crossfitlocal", name="CrossFit Local", gym_id="gym-a")
    b = accounts.register_gym("crossfitlocal2", name="CrossFit Local Two", gym_id="gym-b")
    assert a == ["crossfitlocal_ig", "crossfitlocal_fb"]
    assert b == ["crossfitlocal2_ig", "crossfitlocal2_fb"]
    bases = {r["base"] for r in accounts._load_registry_rows()}
    assert bases == {"crossfitlocal", "crossfitlocal2"}


def test_no_gym_id_signal_is_unchanged_behavior(monkeypatch, tmp_path):
    """gym_id omitted entirely (no caller signal) -> today's behavior, byte for byte:
    the guard cannot fire without a gym_id, by design (documented limitation)."""
    accounts, path = _reg_env(monkeypatch, tmp_path)
    keys = accounts.register_gym("boxfit", name="Box Fit", ig_handle="@boxfit")
    assert keys == ["boxfit_ig", "boxfit_fb"]
    row = next(r for r in accounts._load_registry_rows() if r["base"] == "boxfit")
    assert row["gym_id"] == ""
