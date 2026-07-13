"""
Slack 429 backoff (launch hardening Part 4). One transport carries every send
(post, thread reply, card edit): a 429 retries with backoff honoring Retry-After
and then succeeds; a rate limit past every retry returns a failed send; the
runner turns a hard send failure into ONE ops alert for THAT account only while
the rest of the fan-out posts normally. Fully OFFLINE (fake HTTP, injected sleep).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import slack_surface  # noqa: E402
from agent.slack_surface import SlackPoster  # noqa: E402


class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {"ok": True, "ts": "1.1",
                                                    "channel": "C1"}
        self.headers = headers or {}

    def json(self):
        return self._body


class ScriptedHTTP:
    """Returns the scripted responses in order; repeats the last one after."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, data=None, timeout=None):
        self.calls.append(url)
        return self._responses.pop(0) if len(self._responses) > 1 \
            else self._responses[0]


def _poster(http, sleeps):
    return SlackPoster(http=http, token="xoxb-test", channel="C1",
                       sleep=lambda s: sleeps.append(s))


# ---- 1. a 429 retries (honoring Retry-After) and succeeds ----------------------
def test_429_retries_and_succeeds():
    http = ScriptedHTTP([
        _Resp(status=429, body={"ok": False, "error": "ratelimited"},
              headers={"Retry-After": "7"}),
        _Resp(),                                     # then success
    ])
    sleeps = []
    out = _poster(http, sleeps).post_notice("hello")
    assert out["ok"] is True
    assert len(http.calls) == 2                      # one retry
    assert sleeps == [7.0]                           # Retry-After honored


def test_body_ratelimited_also_retries():
    """Slack can signal the limit in the body with HTTP 200."""
    http = ScriptedHTTP([
        _Resp(status=200, body={"ok": False, "error": "ratelimited"}),
        _Resp(),
    ])
    sleeps = []
    out = _poster(http, sleeps).post_notice("hello")
    assert out["ok"] is True
    assert sleeps == [slack_surface.SLACK_BACKOFF_BASE_SEC]  # exponential base


def test_backoff_grows_exponentially_without_retry_after():
    http = ScriptedHTTP([
        _Resp(status=429, body={}),
        _Resp(status=429, body={}),
        _Resp(status=429, body={}),
        _Resp(),
    ])
    sleeps = []
    out = _poster(http, sleeps).post_notice("hello")
    assert out["ok"] is True
    assert sleeps == [1.0, 2.0, 4.0]                 # 1s, 2s, 4s


# ---- 2. rate limited past every retry -> failed send, never an exception -------
def test_hard_rate_limit_returns_failed_send():
    http = ScriptedHTTP([_Resp(status=429, body={})])   # 429 forever
    sleeps = []
    out = _poster(http, sleeps).post_notice("hello")
    assert out == {"ok": False, "error": "ratelimited"}
    assert len(sleeps) == slack_surface.SLACK_MAX_RETRIES


# ---- 3. card edits share the same transport ------------------------------------
def test_update_card_retries_the_same_way():
    http = ScriptedHTTP([
        _Resp(status=429, body={}, headers={"Retry-After": "2"}),
        _Resp(),
    ])
    sleeps = []
    out = _poster(http, sleeps).update_card("C1", "1.1", "text", None)
    assert out["ok"] is True
    assert sleeps == [2.0]


# ---- 4. the runner: a hard failure alerts and skips THAT account only ----------
def test_hard_failure_alerts_and_isolates_account(monkeypatch, tmp_path):
    from agent import config as _config, ops_alerts
    from agent.runner import run_daily
    from agent.store import PendingStore
    from agent.accounts import Account, Platform

    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setattr(_config, "SLACK_CHANNEL_ID", "C_LASSO_INTERNAL")

    voice = tmp_path / "voice.md"
    voice.write_text("# Voice\nWe help gyms grow.\n#Tag\n", encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    for i in range(3):
        (lib / f"a{i}.png").write_bytes(b"\x89PNGFAKE")
        (lib / f"a{i}.txt").write_text("An approved note.", encoding="utf-8")

    down = Account(key="gym_down_ig", display_name="Down", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="TID", slack_channel="C_DOWN")
    fine = Account(key="gym_fine_ig", display_name="Fine", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="TID", slack_channel="C_FINE")

    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: alerts.append(m))

    posted = []

    class _Poster:
        def post_approval_card(self, draft):
            if draft.account_key == "gym_down_ig":       # Slack hard-fails for one
                return {"ok": False, "error": "ratelimited"}
            posted.append(draft.account_key)
            return {"ok": True, "channel": "C_FINE", "ts": "t"}

        def post_notice(self, text):
            return {"ok": True}

        def mark_superseded(self, draft):
            pass

        def mark_expired(self, draft):
            pass

    out = run_daily(poster=_Poster(), voice_path=str(voice), library_path=str(lib),
                    scheduled_for="2026-07-14T14:00:00+00:00",
                    accounts=[down, fine], store=PendingStore(path=str(tmp_path / "s.db")))

    assert out["status"] == "drafted"
    assert posted == ["gym_fine_ig"]                    # the healthy account posted
    hard = [a for a in alerts if "gym_down_ig" in a and "did not post" in a]
    assert len(hard) == 1                                # one loud alert, that account
    assert "other accounts are unaffected" in hard[0]
    assert not any("gym_fine_ig" in a and "did not post" in a for a in alerts)
