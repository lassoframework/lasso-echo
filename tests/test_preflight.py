"""
Onboarding preflight + the channel ownership guard.

An account missing slack_channel reads NOT READY and, at run time, NEVER
silently posts its cards to the shared internal channel — it skips with one
ops alert. A fully configured account reads READY. The preflight CLI exits
nonzero on FAIL so it can gate scripts.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.accounts import Account, Platform
from agent.preflight import check_account

_VOICE = """# Voice
We help gym owners grow.
## CTAs
- Save this post.
## Hashtags
#LASSOFramework
"""


def _stocked_library(tmp_path, n=30):
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    for i in range(n):
        (lib / f"asset_{i:02d}.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    return lib


def _voice_doc(tmp_path):
    v = tmp_path / "voice.md"
    v.write_text(_VOICE, encoding="utf-8")
    return v


def _client(tmp_path, **kw):
    defaults = dict(key="gym_alpha_ig", display_name="Gym Alpha",
                    platform=Platform.INSTAGRAM,
                    token_env="TOK_GYM_ALPHA", target_id_env="TGT_GYM_ALPHA",
                    slack_channel="C_GYM_ALPHA",
                    approvers=["U_GYM_ALPHA_OWNER"],
                    library_prefix=str(_stocked_library(tmp_path)),
                    voice_doc=str(_voice_doc(tmp_path)))
    defaults.update(kw)
    return Account(**defaults)


def _arm_env(monkeypatch):
    monkeypatch.setenv("TOK_GYM_ALPHA", "tok")
    monkeypatch.setenv("TGT_GYM_ALPHA", "12345")


# ---------------------------------------------------------------------------
# check_account verdicts
# ---------------------------------------------------------------------------

def test_fully_configured_account_is_ready(monkeypatch, tmp_path):
    _arm_env(monkeypatch)
    report = check_account(_client(tmp_path))
    assert report["verdict"] == "READY", report["blocking"]
    assert report["blocking"] == []


def test_missing_slack_channel_is_not_ready(monkeypatch, tmp_path):
    _arm_env(monkeypatch)
    report = check_account(_client(tmp_path, slack_channel=""))
    assert report["verdict"] == "NOT READY"
    assert "slack_channel" in report["blocking"]


def test_missing_token_is_not_ready(monkeypatch, tmp_path):
    monkeypatch.delenv("TOK_GYM_ALPHA", raising=False)
    monkeypatch.setenv("TGT_GYM_ALPHA", "12345")
    report = check_account(_client(tmp_path))
    assert report["verdict"] == "NOT READY"
    assert "meta_token" in report["blocking"]


def test_thin_library_fails_under_minimum(monkeypatch, tmp_path):
    _arm_env(monkeypatch)
    thin = tmp_path / "thin_lib"
    thin.mkdir()
    for i in range(5):
        (thin / f"a{i}.png").write_bytes(b"x")
    report = check_account(_client(tmp_path, library_prefix=str(thin)))
    assert "library" in report["blocking"]


def test_library_warn_band(monkeypatch, tmp_path):
    """Between min (15) and warn (30): WARN, not blocking."""
    _arm_env(monkeypatch)
    mid = tmp_path / "mid_lib"
    mid.mkdir()
    for i in range(20):
        (mid / f"a{i}.png").write_bytes(b"x")
    report = check_account(_client(tmp_path, library_prefix=str(mid)))
    lib = next(c for c in report["checks"] if c["name"] == "library")
    assert lib["status"] == "WARN"
    assert "library" not in report["blocking"]


def test_missing_voice_doc_is_not_ready(monkeypatch, tmp_path):
    _arm_env(monkeypatch)
    report = check_account(
        _client(tmp_path, voice_doc=str(tmp_path / "missing.md")))
    assert "voice_doc" in report["blocking"]


def test_lasso_account_may_use_default_channel(monkeypatch, tmp_path):
    """Client zero owns the shared channel by design: no slack_channel is
    PASS for lasso accounts."""
    monkeypatch.setenv("TOK_LASSO", "tok")
    monkeypatch.setenv("TGT_LASSO", "1")
    report = check_account(_client(
        tmp_path, key="lasso_ig", token_env="TOK_LASSO",
        target_id_env="TGT_LASSO", slack_channel="", approvers=[]))
    ch = next(c for c in report["checks"] if c["name"] == "slack_channel")
    assert ch["status"] == "PASS"


def test_cli_exits_nonzero_on_fail(monkeypatch, tmp_path, capsys):
    import agent.__main__ as mm
    from agent import accounts as _accounts
    monkeypatch.delenv("TOK_GYM_ALPHA", raising=False)
    acct = _client(tmp_path, slack_channel="")
    monkeypatch.setattr(_accounts, "ACCOUNTS", [acct])
    with pytest.raises(SystemExit) as e:
        mm.main(["preflight", "--account", "gym_alpha_ig"])
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "NOT READY" in out
    assert "slack_channel" in out


def test_cli_exits_zero_on_ready(monkeypatch, tmp_path, capsys):
    import agent.__main__ as mm
    from agent import accounts as _accounts
    _arm_env(monkeypatch)
    monkeypatch.setattr(_accounts, "ACCOUNTS", [_client(tmp_path)])
    with pytest.raises(SystemExit) as e:
        mm.main(["preflight", "--account", "gym_alpha_ig"])
    assert e.value.code == 0
    assert "READY" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# run-daily channel ownership guard — the exact silent-routing scenario
# ---------------------------------------------------------------------------

def test_run_daily_skips_client_without_channel(monkeypatch, tmp_path):
    """A client account with no slack_channel must NOT card to the shared
    default channel. It skips with one ops alert; other accounts draft."""
    from agent import config as _config
    from agent.runner import run_daily
    from agent.store import PendingStore

    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.delenv("AGENT_PUBLISH_ENABLED", raising=False)
    # production shape: a shared internal channel EXISTS to leak into
    monkeypatch.setattr(_config, "SLACK_CHANNEL_ID", "C_LASSO_INTERNAL")
    _arm_env(monkeypatch)

    voice = _voice_doc(tmp_path)
    lib = _stocked_library(tmp_path, n=3)
    (lib / "asset_00.txt").write_text("An approved note.", encoding="utf-8")

    no_channel = _client(tmp_path, key="gym_nochan", slack_channel="",
                         token_env="TOK_GYM_ALPHA",
                         target_id_env="TGT_GYM_ALPHA",
                         library_prefix=str(lib))
    healthy = _client(tmp_path, key="gym_ok", slack_channel="C_GYM_OK",
                      token_env="TOK_GYM_ALPHA",
                      target_id_env="TGT_GYM_ALPHA",
                      library_prefix=str(lib))

    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert",
                        lambda m, **kw: alerts.append(m))

    posted = []

    class _Poster:
        def post_approval_card(self, draft):
            posted.append(draft.account_key)
            return {"channel": "C1", "ts": "t"}

        def post_notice(self, text):
            return {"ok": True}

        def mark_superseded(self, draft):
            pass

        def mark_expired(self, draft):
            pass

    out = run_daily(poster=_Poster(), voice_path=str(voice),
                    library_path=str(lib),
                    scheduled_for="2026-07-08T14:00:00+00:00",
                    accounts=[no_channel, healthy],
                    store=PendingStore(path=db_path))

    assert out["status"] == "drafted"
    assert "gym_nochan" not in posted, (
        "the channel-less client posted a card anyway — the silent "
        "wrong-channel routing this guard exists to stop")
    assert "gym_ok" in posted, "the healthy client must still draft"
    assert any("gym_nochan" in a and "slack_channel" in a for a in alerts), (
        "the skip must fire one ops alert naming the account and the fix")


def test_run_daily_lasso_default_channel_unaffected(monkeypatch, tmp_path):
    """LASSO accounts keep drafting on the shared channel exactly as before,
    even with the shared channel configured (client zero owns it)."""
    from agent import config as _config
    from agent.runner import run_daily
    from agent.store import PendingStore

    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setattr(_config, "SLACK_CHANNEL_ID", "C_LASSO_INTERNAL")

    voice = _voice_doc(tmp_path)
    lib = _stocked_library(tmp_path, n=3)
    (lib / "asset_00.txt").write_text("An approved note.", encoding="utf-8")

    lasso = Account(key="lasso_ig", display_name="LASSO IG",
                    platform=Platform.INSTAGRAM,
                    token_env="DUMMY_TOK", target_id_env="DUMMY_TGT")

    posted = []

    class _Poster:
        def post_approval_card(self, draft):
            posted.append(draft.account_key)
            return {"channel": "C1", "ts": "t"}

        def post_notice(self, text):
            return {"ok": True}

        def mark_superseded(self, draft):
            pass

        def mark_expired(self, draft):
            pass

    out = run_daily(poster=_Poster(), voice_path=str(voice),
                    library_path=str(lib),
                    scheduled_for="2026-07-08T14:00:00+00:00",
                    accounts=[lasso], store=PendingStore(path=db_path))
    assert out["status"] == "drafted"
    assert "lasso_ig" in posted
