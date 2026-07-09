"""
Token watchdog tests. Fully OFFLINE: fake Graph clients only, no network.
Asserts: the flag defaults OFF and OFF means no check and no client touched;
armed, an alert fires when expiry is within AGENT_TOKEN_WARN_DAYS (default 7,
env-tunable) and stays silent otherwise; the token value never appears in any
result or alert; and a watchdog explosion can never take run_daily down.
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, token_watchdog  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.runner import run_daily  # noqa: E402

TOKEN = "tok_watchdog_secret_456"
NOW = 1_800_000_000  # fixed 'now' so day math is deterministic


class FakeGraph:
    def __init__(self, expires_at, status=200):
        self.calls = []
        self.expires_at = expires_at
        self.status = status

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        expires_at, status = self.expires_at, self.status

        class R:
            status_code = status

            def json(self):
                return {"data": {"expires_at": expires_at}}

        return R()


class ExplodingHTTP:
    def get(self, *a, **k):
        raise AssertionError("network was called; the gate failed")

    post = get


class RecordingPoster:
    def __init__(self):
        self.notices = []
        self.cards = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}

    def post_approval_card(self, draft):
        self.cards.append(draft)
        return {"ok": True}


def _acct():
    return Account(key="lasso_ig", display_name="LASSO IG", platform=Platform.INSTAGRAM,
                   token_env="WD_TEST_TOKEN", target_id_env="WD_TEST_TARGET")


def _arm(monkeypatch, warn_days=None):
    monkeypatch.setenv("AGENT_TOKEN_WATCHDOG_ENABLED", "true")
    monkeypatch.setenv("WD_TEST_TOKEN", TOKEN)
    # the ops-alerts flag stays OFF: the watchdog's own flag is the gate (force=True)
    monkeypatch.delenv("AGENT_OPS_ALERTS_ENABLED", raising=False)
    if warn_days is None:
        monkeypatch.delenv("AGENT_TOKEN_WARN_DAYS", raising=False)
    else:
        monkeypatch.setenv("AGENT_TOKEN_WARN_DAYS", str(warn_days))


# ---- 1. flag defaults OFF; OFF touches nothing ----------------------------------
def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_TOKEN_WATCHDOG_ENABLED", raising=False)
    assert config.token_watchdog_enabled() is False


def test_warn_days_defaults_to_7(monkeypatch):
    monkeypatch.delenv("AGENT_TOKEN_WARN_DAYS", raising=False)
    assert config.token_warn_days() == 7


def test_disabled_means_no_check_no_client(monkeypatch):
    monkeypatch.delenv("AGENT_TOKEN_WATCHDOG_ENABLED", raising=False)
    out = token_watchdog.check_tokens(http=ExplodingHTTP(), accounts=[_acct()])
    assert out == {"status": "disabled", "results": [], "tenant_results": []}


# ---- 2. expiry inside the warn window fires ONE alert ----------------------------
def test_alert_fires_within_warn_window(monkeypatch):
    _arm(monkeypatch)
    poster = RecordingPoster()
    graph = FakeGraph(expires_at=NOW + 3 * 86400 + 60)   # ~3 days out
    out = token_watchdog.check_tokens(http=graph, poster=poster,
                                      accounts=[_acct()], now=NOW)
    assert out["status"] == "checked"
    r = out["results"][0]
    assert r["status"] == "expiring_soon" and r["days_remaining"] == 3
    assert len(poster.notices) == 1
    assert poster.notices[0].startswith("ECHO ALERT: ")
    assert "lasso_ig" in poster.notices[0] and "3 day(s)" in poster.notices[0]


def test_silent_when_expiry_is_far_out(monkeypatch):
    _arm(monkeypatch)
    poster = RecordingPoster()
    graph = FakeGraph(expires_at=NOW + 30 * 86400)
    out = token_watchdog.check_tokens(http=graph, poster=poster,
                                      accounts=[_acct()], now=NOW)
    r = out["results"][0]
    assert r["status"] == "ok" and r["days_remaining"] == 30
    assert poster.notices == []


def test_warn_days_env_is_respected(monkeypatch):
    _arm(monkeypatch, warn_days=10)
    poster = RecordingPoster()
    graph = FakeGraph(expires_at=NOW + 8 * 86400 + 60)   # ~8 days: inside 10
    out = token_watchdog.check_tokens(http=graph, poster=poster,
                                      accounts=[_acct()], now=NOW)
    assert out["results"][0]["status"] == "expiring_soon"
    assert len(poster.notices) == 1


def test_never_expiring_token_is_quiet(monkeypatch):
    _arm(monkeypatch)
    poster = RecordingPoster()
    out = token_watchdog.check_tokens(http=FakeGraph(expires_at=0), poster=poster,
                                      accounts=[_acct()], now=NOW)
    assert out["results"][0]["status"] == "never_expires"
    assert poster.notices == []


def test_missing_token_recorded_without_network(monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN_WATCHDOG_ENABLED", "true")
    monkeypatch.delenv("WD_TEST_TOKEN", raising=False)
    out = token_watchdog.check_tokens(http=ExplodingHTTP(), accounts=[_acct()], now=NOW)
    assert out["results"][0]["status"] == "no_token"


# ---- 3. the token value never surfaces -------------------------------------------
def test_token_value_never_in_results_or_alerts(monkeypatch):
    _arm(monkeypatch)
    poster = RecordingPoster()
    out = token_watchdog.check_tokens(http=FakeGraph(expires_at=NOW + 86400),
                                      poster=poster, accounts=[_acct()], now=NOW)
    assert TOKEN not in str(out)
    assert all(TOKEN not in n for n in poster.notices)


def test_cli_disabled_path_prints_no_token(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_TOKEN_WATCHDOG_ENABLED", raising=False)
    monkeypatch.setenv("WD_TEST_TOKEN", TOKEN)
    main_mod = importlib.import_module("agent.__main__")
    main_mod._check_tokens()
    out = capsys.readouterr().out
    assert "token watchdog is OFF" in out
    assert TOKEN not in out


# ---- 4. a watchdog explosion can never take run_daily down ------------------------
def test_run_daily_survives_watchdog_exception(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_TOKEN_WATCHDOG_ENABLED", "true")
    for f in ("AGENT_PUBLISH_ENABLED", "AGENT_STORIES_ENABLED",
              "AGENT_IDEMPOTENT_DRAFTS_ENABLED"):
        monkeypatch.delenv(f, raising=False)

    def boom(*a, **k):
        raise RuntimeError("debug_token exploded")

    monkeypatch.setattr(token_watchdog, "check_tokens", boom)

    voice = tmp_path / "voice.md"
    voice.write_text("We help gym owners grow.\n\n#LASSOFramework", encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")

    class _Store:
        def put(self, draft):
            pass

    acct = Account(key="gym_ig", display_name="Gym", platform=Platform.INSTAGRAM,
                   token_env="WD_TEST_TOKEN", target_id_env="WD_TEST_TARGET")
    out = run_daily(poster=RecordingPoster(), voice_path=str(voice),
                    library_path=str(lib), scheduled_for="2027-07-07T18:30:00+00:00",
                    accounts=[acct], store=_Store())
    assert out["status"] == "drafted"              # the run completed anyway
    assert "[token-watchdog] check failed" in capsys.readouterr().out
