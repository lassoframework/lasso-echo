"""
Automatic social-intake forward (sync_unrouted): map EVERY un-routed
echo_social_intake row into Echo and mark it routed. This is the durable fix for
the CrossFit ENG miss (captured intake, never forwarded).

Fully OFFLINE: lister/reader/marker/onboard are all injectable. Asserts:
  - the flag defaults OFF;
  - a base WITH a registry account is onboarded and marked routed;
  - a base with NO account is skipped with a reason + one ops alert, never onboarded;
  - a base with no answers is skipped;
  - an onboarding exception is contained (one alert, loop continues);
  - SCALE: 100 un-routed gyms all process, with a bad/no-account gym mid-batch
    never blocking the rest.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, social_intake_reader as sir, ops_alerts  # noqa: E402


def _answers(name="Test Gym"):
    return {"gym": {"name": name}, "offers": {"front_door_offer": "No Sweat Intro",
            "services": "Group classes\nHYROX"}, "audience": {"ideal_member": "Busy parents"},
            "proof": {"verifiable_numbers": "100 five star reviews"},
            "voice": {"words_to_never_use": "Cheat\nEasy"}}


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_SOCIAL_INTAKE_SYNC", raising=False)
    assert config.social_intake_sync_enabled() is False


def test_maps_and_marks_a_base_with_an_account(monkeypatch):
    # eng_ig is a real registry account (ENG onboarded 2026-08-12)
    onboarded, marked = [], []

    def fake_onboard(account_key, answers, approve=True):
        onboarded.append((account_key, approve))
        return {"sources_created": 3, "base": "eng"}

    def fake_marker(base, account_key):
        marked.append((base, account_key))
        return True

    out = sir.sync_unrouted(lister=lambda: ["eng"],
                            reader=lambda b: _answers("CrossFit ENG"),
                            marker=fake_marker, onboard=fake_onboard)
    assert out == [{"base": "eng", "ok": True, "account": "eng_ig",
                    "sources_created": 3, "marked_routed": True}]
    # approve defaults FALSE: sources land PENDING for one human review
    assert onboarded == [("eng_ig", False)]
    assert marked == [("eng", "eng")]


def test_auto_provisions_account_when_dynamic_enabled(monkeypatch, tmp_path):
    """SCALE (zero-touch): AGENT_DYNAMIC_ACCOUNTS armed -> a base with no hardcoded
    account is auto-provisioned from its intake and onboarded, no accounts.py edit."""
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import accounts
    accounts._dynamic_cache = None
    onboarded = []

    def fake_onboard(account_key, answers, approve=False):
        onboarded.append(account_key)
        return {"sources_created": 2}

    out = sir.sync_unrouted(
        lister=lambda: ["newbox"],
        reader=lambda b: {"gym": {"name": "New Box", "ig_handle": "@nb"}},
        marker=lambda b, a: True, onboard=fake_onboard)
    assert out[0]["ok"] is True and out[0]["account"] == "newbox_ig"
    assert onboarded == ["newbox_ig"]
    assert accounts.get_account("newbox_ig") is not None   # now resolvable
    accounts._dynamic_cache = None


def test_no_account_and_dynamic_off_still_skips(monkeypatch):
    """Flag OFF: a base with no account is skipped with an alert (never fabricated)."""
    monkeypatch.delenv("AGENT_DYNAMIC_ACCOUNTS", raising=False)
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: None)
    out = sir.sync_unrouted(lister=lambda: ["ghostbox"],
                            reader=lambda b: _answers(),
                            marker=lambda b, a: True,
                            onboard=lambda *a, **k: {"sources_created": 0})
    assert out == [{"base": "ghostbox", "ok": False, "reason": "no account"}]


def test_base_with_no_account_is_skipped_with_alert(monkeypatch):
    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: alerts.append(m))
    onboarded = []
    out = sir.sync_unrouted(
        lister=lambda: ["ghostgym"],
        reader=lambda b: _answers(),
        marker=lambda b, a: True,
        onboard=lambda *a, **k: onboarded.append(a) or {"sources_created": 0})
    assert out == [{"base": "ghostgym", "ok": False, "reason": "no account"}]
    assert onboarded == []                         # never onboarded a ghost
    assert len(alerts) == 1 and "ghostgym" in alerts[0]


def test_base_with_no_answers_is_skipped(monkeypatch):
    out = sir.sync_unrouted(lister=lambda: ["eng"], reader=lambda b: None,
                            marker=lambda b, a: True,
                            onboard=lambda *a, **k: {"sources_created": 0})
    assert out == [{"base": "eng", "ok": False, "reason": "no answers"}]


def test_onboard_exception_is_contained(monkeypatch):
    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: alerts.append(m))

    def boom(account_key, answers, approve=True):
        raise RuntimeError("db down")

    out = sir.sync_unrouted(lister=lambda: ["eng"], reader=lambda b: _answers(),
                            marker=lambda b, a: True, onboard=boom)
    assert out[0]["ok"] is False and out[0]["reason"] == "RuntimeError"
    assert len(alerts) == 1 and "eng" in alerts[0]


def test_scale_100_gyms_one_bad_never_blocks_the_rest(monkeypatch):
    """SCALE: 100 un-routed gyms. 99 have accounts (eng), one has none. All 100
    process; the no-account gym is skipped, the other 99 map. No crash."""
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: None)
    bases = [f"eng" for _ in range(99)] + ["ghostgym"]
    # give each a unique label via index by wrapping the lister output order
    # (dedup is on real client_key in prod; here we exercise volume + isolation)
    calls = {"onboard": 0, "mark": 0}

    def fake_onboard(account_key, answers, approve=True):
        calls["onboard"] += 1
        return {"sources_created": 1}

    def fake_marker(base, account_key):
        calls["mark"] += 1
        return True

    out = sir.sync_unrouted(lister=lambda: bases, reader=lambda b: _answers(),
                            marker=fake_marker, onboard=fake_onboard)
    assert len(out) == 100
    ok = [r for r in out if r.get("ok")]
    bad = [r for r in out if not r.get("ok")]
    assert len(ok) == 99 and len(bad) == 1
    assert bad[0]["base"] == "ghostgym" and bad[0]["reason"] == "no account"
    assert calls["onboard"] == 99 and calls["mark"] == 99
