"""
Connect-token resolution tests. Asserts: flag OFF means kv tokens are ignored
entirely (env-only, byte-identical behavior); flag ON resolves the kv token by
page id; an env token ALWAYS wins when both exist; the kv token never appears
in any log line or audit row.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402

KV_TOKEN = "EAAkv-page-token-abc123"
ENV_TOKEN = "EAAenv-token-xyz789"


def _acct():
    return Account(key="gym_fb", display_name="Gym FB", platform=Platform.FACEBOOK_PAGE,
                   token_env="CT_TEST_TOKEN", target_id_env="CT_TEST_PAGE")


def _seed_kv(monkeypatch, page_id="PAGE77"):
    monkeypatch.setenv("CT_TEST_PAGE", page_id)
    db.kv_set(f"connect_page_token_{page_id}", KV_TOKEN)


def test_flag_off_ignores_kv(monkeypatch):
    monkeypatch.delenv("AGENT_CONNECT_TOKENS_ENABLED", raising=False)
    monkeypatch.delenv("CT_TEST_TOKEN", raising=False)
    _seed_kv(monkeypatch)
    assert _acct().get_token() is None            # kv exists but is never consulted


def test_flag_on_resolves_kv_token(monkeypatch):
    monkeypatch.setenv("AGENT_CONNECT_TOKENS_ENABLED", "true")
    monkeypatch.delenv("CT_TEST_TOKEN", raising=False)
    _seed_kv(monkeypatch)
    assert _acct().get_token() == KV_TOKEN


def test_env_always_wins_over_kv(monkeypatch):
    monkeypatch.setenv("AGENT_CONNECT_TOKENS_ENABLED", "true")
    monkeypatch.setenv("CT_TEST_TOKEN", ENV_TOKEN)
    _seed_kv(monkeypatch)
    assert _acct().get_token() == ENV_TOKEN       # the hand-set env token wins


def test_no_page_id_falls_back_to_env_behavior(monkeypatch):
    monkeypatch.setenv("AGENT_CONNECT_TOKENS_ENABLED", "true")
    monkeypatch.delenv("CT_TEST_TOKEN", raising=False)
    monkeypatch.delenv("CT_TEST_PAGE", raising=False)
    assert _acct().get_token() is None


def test_kv_token_never_in_logs_or_audit(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_CONNECT_TOKENS_ENABLED", "true")
    monkeypatch.delenv("CT_TEST_TOKEN", raising=False)
    _seed_kv(monkeypatch)
    token = _acct().get_token()
    assert token == KV_TOKEN
    db.audit("test", "token resolution", "account gym_fb resolved a connect token")
    printed = capsys.readouterr().out
    assert KV_TOKEN not in printed                 # never logged
    assert KV_TOKEN not in json.dumps(db.audit_rows())   # never in the trail
