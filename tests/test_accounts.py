"""
Account registry: blake_personal is an INACTIVE record (Meta ended personal-profile
publishing in 2018). It must not be drafted for, but stays discoverable for history.
"""

import os
import sys

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
