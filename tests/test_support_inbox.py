"""
Support inbox poster tests. Fully OFFLINE: recording posters only, no Slack, no
network, no Supabase. Asserts:

  * submit_support_message posts ONE message to the CONFIGURED channel, with the
    gym's display name + account_key in the header (fake poster records the call);
  * an unknown gym name falls back to the account_key in the header;
  * an empty channel id is a no-op {ok:false, reason:"no_channel"} — Slack never touched;
  * an empty message is {ok:false, reason:"empty"} before anything else;
  * the per-gym rate limit trips to {ok:false, reason:"rate_limited"};
  * a Slack failure (exploding poster OR {ok:false} response) returns {ok:false,
    reason:"slack_failed"} and never raises;
  * the message is length-capped and scrubbed of secret env values.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, support_inbox  # noqa: E402


class RecordingPoster:
    def __init__(self, ok=True):
        self.calls = []
        self._ok = ok

    def _chat_post(self, text, blocks=None, channel=None, thread_ts=None):
        self.calls.append({"text": text, "channel": channel, "blocks": blocks})
        return {"ok": self._ok}


class ExplodingPoster:
    def _chat_post(self, text, blocks=None, channel=None, thread_ts=None):
        raise RuntimeError("slack is down")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # A configured channel by default; each test overrides as needed. Never resolve
    # the shared plane in unit tests (no creds).
    monkeypatch.setenv("AGENT_SUPPORT_CHANNEL_ID", "C0BTDAE1GLW")
    monkeypatch.setattr(support_inbox, "resolve_gym_identity",
                        lambda ak: ("Gritx Strength", ""))
    support_inbox._support_hits.clear()


# ---- happy path: posts to the configured channel with the header ---------------
def test_posts_to_configured_channel_with_header(monkeypatch):
    rec = RecordingPoster()
    out = support_inbox.submit_support_message("gritx", "My calendar looks empty",
                                               poster=rec)
    assert out == {"ok": True, "delivered": True, "reason": ""}
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["channel"] == "C0BTDAE1GLW"
    assert "New support request" in call["text"]
    assert "Gritx Strength" in call["text"]
    assert "(gritx)" in call["text"]
    assert "My calendar looks empty" in call["text"]


def test_unknown_name_falls_back_to_account_key(monkeypatch):
    monkeypatch.setattr(support_inbox, "resolve_gym_identity",
                        lambda ak: (ak, ""))          # resolver returned the key
    rec = RecordingPoster()
    out = support_inbox.submit_support_message("mysterygym", "hello", poster=rec)
    assert out["ok"] is True
    assert "mysterygym" in rec.calls[0]["text"]


def test_owner_line_included_when_known(monkeypatch):
    monkeypatch.setattr(support_inbox, "resolve_gym_identity",
                        lambda ak: ("Gritx Strength", "Coach Dana"))
    rec = RecordingPoster()
    support_inbox.submit_support_message("gritx", "hi", poster=rec)
    assert "Owner: Coach Dana" in rec.calls[0]["text"]


# ---- inert / empty guards ------------------------------------------------------
def test_empty_channel_is_noop(monkeypatch):
    monkeypatch.setenv("AGENT_SUPPORT_CHANNEL_ID", "")
    rec = RecordingPoster()
    out = support_inbox.submit_support_message("gritx", "hi", poster=rec)
    assert out == {"ok": False, "delivered": False, "reason": "no_channel"}
    assert rec.calls == []                            # Slack never touched


def test_empty_message_is_rejected(monkeypatch):
    rec = RecordingPoster()
    out = support_inbox.submit_support_message("gritx", "   ", poster=rec)
    assert out == {"ok": False, "delivered": False, "reason": "empty"}
    assert rec.calls == []


def test_channel_default_empty_when_env_unset(monkeypatch):
    monkeypatch.delenv("AGENT_SUPPORT_CHANNEL_ID", raising=False)
    assert config.support_channel_id() == ""


# ---- rate limit ----------------------------------------------------------------
def test_per_gym_rate_limit_trips(monkeypatch):
    rec = RecordingPoster()
    for _ in range(support_inbox.SUPPORT_RATE_PER_MINUTE):
        assert support_inbox.submit_support_message("gritx", "hi", poster=rec)["ok"] is True
    out = support_inbox.submit_support_message("gritx", "one too many", poster=rec)
    assert out == {"ok": False, "delivered": False, "reason": "rate_limited"}
    # A DIFFERENT gym has its own budget.
    other = support_inbox.submit_support_message("otherbox", "hi", poster=rec)
    assert other["ok"] is True


# ---- Slack failure never raises ------------------------------------------------
def test_exploding_poster_returns_false(monkeypatch, capsys):
    out = support_inbox.submit_support_message("gritx", "hi", poster=ExplodingPoster())
    assert out == {"ok": False, "delivered": False, "reason": "slack_failed"}
    assert "[support-inbox]" in capsys.readouterr().out


def test_slack_not_ok_response_returns_false(monkeypatch):
    rec = RecordingPoster(ok=False)                   # Slack responded {ok:false}
    out = support_inbox.submit_support_message("gritx", "hi", poster=rec)
    assert out == {"ok": False, "delivered": False, "reason": "slack_failed"}


# ---- length cap + scrub --------------------------------------------------------
def test_message_length_capped(monkeypatch):
    rec = RecordingPoster()
    huge = "x" * (support_inbox.SUPPORT_MSG_MAX + 500)
    support_inbox.submit_support_message("gritx", huge, poster=rec)
    text = rec.calls[0]["text"]
    assert "…(truncated)" in text
    # The run of 'x' (the body) is capped; the header/footer add only a handful more.
    longest_run = max(_runs_of(text, "x"), default=0)
    assert longest_run <= support_inbox.SUPPORT_MSG_MAX


def _runs_of(text, ch):
    """Lengths of each maximal run of `ch` in `text`."""
    runs, n = [], 0
    for c in text:
        if c == ch:
            n += 1
        elif n:
            runs.append(n)
            n = 0
    if n:
        runs.append(n)
    return runs


def test_secret_env_value_scrubbed_from_message(monkeypatch):
    monkeypatch.setenv("LEAKY_FAKE_TOKEN", "tok_super_secret_1")
    rec = RecordingPoster()
    support_inbox.submit_support_message(
        "gritx", "here is a token tok_super_secret_1 by accident", poster=rec)
    assert "tok_super_secret_1" not in rec.calls[0]["text"]
    assert "[REDACTED]" in rec.calls[0]["text"]


# ---- dedicated support token (Scout) -------------------------------------------
def test_default_poster_uses_dedicated_token_when_set(monkeypatch):
    monkeypatch.setenv("AGENT_SUPPORT_SLACK_BOT_TOKEN", "xoxb-scout-support")
    monkeypatch.setenv("AGENT_SLACK_BOT_TOKEN", "xoxb-default-echo")
    poster = support_inbox._default_poster()
    assert poster._token == "xoxb-scout-support"       # support post rides Scout


def test_default_poster_falls_back_to_default_token(monkeypatch):
    monkeypatch.delenv("AGENT_SUPPORT_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_SLACK_BOT_TOKEN", "xoxb-default-echo")
    poster = support_inbox._default_poster()
    assert poster._token == "xoxb-default-echo"        # single-bot fallback


def test_neither_token_is_logged(monkeypatch, capsys):
    # A Slack failure logs a line; assert neither token value appears in it.
    monkeypatch.setenv("AGENT_SUPPORT_SLACK_BOT_TOKEN", "xoxb-scout-support")
    monkeypatch.setenv("AGENT_SLACK_BOT_TOKEN", "xoxb-default-echo")
    support_inbox.submit_support_message("gritx", "hi", poster=ExplodingPoster())
    out = capsys.readouterr().out
    assert "xoxb-scout-support" not in out
    assert "xoxb-default-echo" not in out


# ---- an UNDELIVERED support message must never just evaporate -------------------
def test_slack_failure_escalates_the_message_to_ops(monkeypatch):
    """A gym's support message is the one thing we cannot afford to drop. When the
    support channel post fails, the words must still reach a human."""
    seen = []
    monkeypatch.setattr("agent.ops_alerts.alert",
                        lambda msg, poster=None, force=False: seen.append(msg))
    out = support_inbox.submit_support_message(
        "gritx_ig", "my instagram is not posting", poster=ExplodingPoster())
    assert out["ok"] is False and out["reason"] == "slack_failed"
    assert len(seen) == 1
    assert "UNDELIVERED" in seen[0]
    assert "my instagram is not posting" in seen[0], "the client's words must survive"
    assert "Gritx Strength" in seen[0]


def test_no_channel_configured_still_escalates(monkeypatch):
    """The likeliest silent-drop mode in production: the support channel is unset, so
    the client retries forever and nobody at LASSO ever learns they tried."""
    monkeypatch.delenv("AGENT_SUPPORT_CHANNEL_ID", raising=False)
    seen = []
    monkeypatch.setattr("agent.ops_alerts.alert",
                        lambda msg, poster=None, force=False: seen.append(msg))
    out = support_inbox.submit_support_message("gritx_ig", "help please")
    assert out["reason"] == "no_channel"
    assert len(seen) == 1 and "help please" in seen[0]


def test_failed_sends_do_not_burn_the_rate_limit(monkeypatch):
    """Five Slack failures used to eat the whole per-minute budget, turning an outage
    into a lockout on the one surface a stuck client uses to reach us."""
    monkeypatch.setattr("agent.ops_alerts.alert",
                        lambda msg, poster=None, force=False: None)
    for _ in range(support_inbox.SUPPORT_RATE_PER_MINUTE):
        out = support_inbox.submit_support_message("gritx_ig", "x",
                                                   poster=ExplodingPoster())
        assert out["reason"] == "slack_failed"
    # Slack recovers: the gym must still be able to get through.
    good = RecordingPoster(ok=True)
    out = support_inbox.submit_support_message("gritx_ig", "now it works", poster=good)
    assert out["ok"] is True, "a recovered Slack must not be blocked by earlier failures"
    assert len(good.calls) == 1
