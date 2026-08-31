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


# ---- GAP 8: token-row resolution before provisioning ------------------------------
import pytest


@pytest.fixture(autouse=True)
def _no_live_supabase(monkeypatch):
    """The default resolver/marker read SUPABASE_* lazily; scrub them so every
    test in this file stays fully offline no matter the host env."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)


def test_uuid_client_key_resolves_to_existing_account(monkeypatch, tmp_path):
    """A self-serve row carries a portal gym UUID as client_key. The token row
    maps it to the name-slug account portal onboarding minted; sync must use
    THAT account and never provision a UUID-named tenant beside it."""
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")   # provisioning armed...
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import accounts
    accounts._dynamic_cache = None
    uuid_key = "3f2b8a9e-1111-2222-3333-444455556666"
    resolved_calls, onboarded, marked = [], [], []

    def resolver(k):
        resolved_calls.append(k)
        return "eng"                                       # the minted slug account

    out = sir.sync_unrouted(
        lister=lambda: [uuid_key],
        reader=lambda b: _answers("CrossFit ENG"),
        marker=lambda b, a: marked.append((b, a)) or True,
        onboard=lambda ak, ans, approve=False: onboarded.append(ak) or {"sources_created": 2},
        resolver=resolver)

    assert resolved_calls == [uuid_key]
    assert out[0]["ok"] is True
    assert out[0]["base"] == uuid_key                      # the row is reported by its raw key
    assert out[0]["account"] == "eng_ig"                   # ...but routed to the EXISTING account
    assert out[0]["resolved"] == "eng"
    assert onboarded == ["eng_ig"]
    # the RAW row is marked, recording the resolved account
    assert marked == [(uuid_key, "eng")]
    # ...and NO UUID-named account was provisioned, even with dynamic accounts armed
    assert accounts.get_account(f"{uuid_key}_ig") is None
    assert accounts.get_account(uuid_key) is None
    accounts._dynamic_cache = None


def test_no_token_row_falls_back_to_raw_key_and_provisions(monkeypatch, tmp_path):
    """No token row (resolver -> None): current behavior is preserved -- the raw
    key stands and dynamic provisioning creates the fresh account."""
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import accounts
    accounts._dynamic_cache = None
    marked = []

    out = sir.sync_unrouted(
        lister=lambda: ["freshbox"],
        reader=lambda b: {"gym": {"name": "Fresh Box"}},
        marker=lambda b, a: marked.append((b, a)) or True,
        onboard=lambda ak, ans, approve=False: {"sources_created": 1},
        resolver=lambda k: None)

    assert out[0]["ok"] is True and out[0]["account"] == "freshbox_ig"
    assert "resolved" not in out[0]
    assert marked == [("freshbox", "freshbox")]
    assert accounts.get_account("freshbox_ig") is not None
    accounts._dynamic_cache = None


def test_default_resolver_without_creds_is_none():
    assert sir._default_token_resolver("3f2b8a9e-1111-2222-3333-444455556666") is None
    assert sir._default_token_resolver("") is None


def test_default_resolver_reads_token_row(monkeypatch):
    """Offline: fake requests. gym_id lookup returns the minted account key."""
    calls = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return [{"echo_account_key": "eng"}]

    class _FakeRequests:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            calls.append((url, dict(params or {})))
            return _Resp()

    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    monkeypatch.setitem(sys.modules, "requests", _FakeRequests)
    assert sir._default_token_resolver("uuid-123") == "eng"
    assert calls[0][0].endswith("/rest/v1/echo_intake_tokens")
    assert calls[0][1]["gym_id"] == "eq.uuid-123"


def test_default_marker_records_resolved_account(monkeypatch):
    """The mark PATCHes the RAW client_key rows but records the RESOLVED
    echo_account_key (previously it wrote the raw key as the account)."""
    calls = {}

    class _Resp:
        status_code = 204

    class _FakeRequests:
        @staticmethod
        def patch(url, params=None, headers=None, json=None, timeout=None):
            calls["url"] = url
            calls["params"] = dict(params or {})
            calls["body"] = dict(json or {})
            return _Resp()

    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    monkeypatch.setitem(sys.modules, "requests", _FakeRequests)
    assert sir._default_marker("uuid-123", "eng") is True
    assert calls["params"] == {"client_key": "eq.uuid-123"}
    assert calls["body"]["echo_account_key"] == "eng"
    assert calls["body"]["echo_forwarded"] is True


# ---- the UUID-keyed registry corruption (live 2026-08-30, repaired by hand) ----
def test_uuid_key_with_no_token_row_mints_canonical_not_a_uuid_account(monkeypatch, tmp_path):
    """The hole the token-row guard did not cover. With a token row, sync already
    refuses to provision a UUID-named tenant. With NO token row there was nothing to
    resolve against, so register_gym took the portal gym UUID verbatim and the gym was
    registered under a UUID key: exactly what was found in /data/gym_accounts.json.
    It must now mint the SAME canonical key portal onboarding would."""
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import accounts
    from agent.account_key import canonical_account_key
    accounts._dynamic_cache = None
    uuid_key = "7c1d2e3f-4444-5555-6666-777788889999"
    expected = canonical_account_key(uuid_key, "Fresh Box")
    onboarded, marked = [], []

    out = sir.sync_unrouted(
        lister=lambda: [uuid_key],
        reader=lambda b: _answers("Fresh Box"),
        marker=lambda b, a: marked.append((b, a)) or True,
        onboard=lambda ak, ans, approve=False: onboarded.append(ak) or {"sources_created": 1},
        resolver=lambda k: None)

    assert out[0]["ok"] is True
    assert out[0]["account"] == f"{expected}_ig"
    assert onboarded == [f"{expected}_ig"]
    assert marked == [(uuid_key, expected)]
    # the UUID must NOT become an account key
    assert accounts.get_account(f"{uuid_key}_ig") is None
    assert accounts.get_account(uuid_key) is None
    assert accounts.get_account(f"{expected}_ig") is not None
    accounts._dynamic_cache = None


def test_a_slug_client_key_is_never_re_minted(monkeypatch, tmp_path):
    """Only the UUID case is re-keyed. A slug-shaped client_key is already a real
    account key, and re-minting it would hand an existing gym a brand new key: the
    very stranding this guard exists to prevent."""
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import accounts
    accounts._dynamic_cache = None
    out = sir.sync_unrouted(
        lister=lambda: ["freshbox"],
        reader=lambda b: _answers("Fresh Box"),
        marker=lambda b, a: True,
        onboard=lambda ak, ans, approve=False: {"sources_created": 1},
        resolver=lambda k: None)
    assert out[0]["account"] == "freshbox_ig"
    accounts._dynamic_cache = None


def test_uuid_key_with_no_gym_name_is_left_unrouted_never_uuid_keyed(monkeypatch, tmp_path):
    """No name means no honest canonical key. We never fabricate one, and a UUID key
    is worse than waiting, so the row is left for a human with one alert."""
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", str(tmp_path / "reg.json"))
    from agent import accounts
    accounts._dynamic_cache = None
    uuid_key = "8d2e3f4a-5555-6666-7777-888899990000"
    alerts = []
    monkeypatch.setattr(sir.ops_alerts, "alert", lambda m: alerts.append(m))

    out = sir.sync_unrouted(
        lister=lambda: [uuid_key],
        reader=lambda b: {"gym": {}},
        marker=lambda b, a: True,
        onboard=lambda ak, ans, approve=False: {"sources_created": 1},
        resolver=lambda k: None)

    assert out[0]["ok"] is False and out[0]["reason"] == "no canonical key"
    assert len(alerts) == 1 and "canonical account key" in alerts[0]
    assert accounts.get_account(f"{uuid_key}_ig") is None
    accounts._dynamic_cache = None


# ---- a failed read must never look like a clean pass -------------------------
def _fake_requests(status=500, boom=False):
    class _R:
        status_code = status

        @staticmethod
        def json():
            return []

    class _Req:
        @staticmethod
        def get(*a, **k):
            if boom:
                raise OSError("connection reset")
            return _R()
    return _Req


def test_first_page_read_failure_alerts_instead_of_looking_clean(monkeypatch):
    """A non-2xx used to break out and return [], which is indistinguishable from
    'nothing un-routed'. Every stranded gym stayed stranded with no Slack signal."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    monkeypatch.setitem(sys.modules, "requests", _fake_requests(status=500))
    alerts = []
    monkeypatch.setattr(sir.ops_alerts, "alert", lambda m: alerts.append(m))
    assert sir._default_lister() == []
    assert len(alerts) == 1
    assert "500" in alerts[0] and "looks clean but is not" in alerts[0]


def test_network_error_alerts_and_does_not_escape(monkeypatch):
    """A network exception used to escape the whole sweep to the listener's bare
    print, aborting every gym's sync with no Slack signal."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    monkeypatch.setitem(sys.modules, "requests", _fake_requests(boom=True))
    alerts = []
    monkeypatch.setattr(sir.ops_alerts, "alert", lambda m: alerts.append(m))
    assert sir._default_lister() == []
    assert len(alerts) == 1 and "OSError" in alerts[0]


def test_map_answers_caps_field_length(monkeypatch):
    """This third intake door had no size cap, so an unbounded answer flowed into
    the drafted bible, into client_sources, and from there into a caption."""
    from agent import intake_web
    huge = "x" * (intake_web._FIELD_MAX + 5000)
    out = sir.map_answers({"gym": {"name": "Cap Gym", "about": huge},
                           "voice": {"vibe": huge},
                           # faq/promo are read straight off the RAW answers, so the
                           # first cap missed them entirely and they flowed uncapped
                           # into client_sources and from there into a caption.
                           "faq": huge, "promo": huge})
    blob = repr(out)
    # the uncapped run would carry the full 9000-char answer straight through
    assert "x" * (intake_web._FIELD_MAX + 1) not in blob, "field cap not applied"
    assert "x" * 100 in blob, "the answer should be present, just truncated"
