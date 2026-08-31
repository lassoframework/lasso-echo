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
