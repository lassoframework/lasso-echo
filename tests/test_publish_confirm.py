"""
Publish confirmation tests. Fully OFFLINE: recording HTTP + poster fakes, no
network. Asserts: the flag defaults OFF and OFF (or a would_publish result) is
fully dormant; ON, one Graph READ per confirm and one "LIVE: <permalink>" reply
into the card's thread; a failed verify warns in-thread and emits one ops alert;
the module can never re-publish (any POST explodes the test); and the token never
appears in any message.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, ops_alerts, publish_confirm  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft  # noqa: E402

TOKEN = "tok_confirm_secret_123"


class Result:
    def __init__(self, mode="published", media_id="M1"):
        self.mode = mode
        self.media_id = media_id


class RecordingHTTP:
    """READ-only fake Graph client. Any write attempt fails the test."""

    def __init__(self, payload=None, status=200):
        self.gets = []
        self.payload = payload if payload is not None else {}
        self.status = status

    def get(self, url, params=None, timeout=None):
        self.gets.append((url, dict(params or {})))
        payload, status = self.payload, self.status

        class R:
            status_code = status

            def json(self):
                return payload

        return R()

    def post(self, *a, **k):
        raise AssertionError("a WRITE was attempted; confirm must never publish")


class ExplodingHTTP:
    def get(self, *a, **k):
        raise AssertionError("network was called; the gate failed")

    post = get


class RecordingPoster:
    def __init__(self):
        self.replies = []
        self.notices = []

    def post_thread_reply(self, channel, ts, text):
        self.replies.append((channel, ts, text))
        return {"ok": True}

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


def _acct(platform=Platform.INSTAGRAM, key="lasso_ig"):
    return Account(key=key, display_name=key, platform=platform,
                   token_env="CONFIRM_TEST_TOKEN", target_id_env="CONFIRM_TEST_TARGET")


def _draft():
    return Draft(draft_id="d1", account_key="lasso_ig", platform="instagram",
                 caption="x", hashtags=[], creative_path="a.png",
                 creative_public_url="", scheduled_for="2026-07-01T18:30:00+00:00",
                 slack_channel="C1", slack_ts="123.456")


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_CONFIRM_ENABLED", "true")
    monkeypatch.setenv("CONFIRM_TEST_TOKEN", TOKEN)


# ---- 1. flag defaults OFF, and OFF / not-a-real-publish is fully dormant -------
def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_PUBLISH_CONFIRM_ENABLED", raising=False)
    assert config.publish_confirm_enabled() is False


def test_dormant_when_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_PUBLISH_CONFIRM_ENABLED", raising=False)
    out = publish_confirm.confirm_publish(
        _draft(), _acct(), Result("published"), http=ExplodingHTTP(),
        poster=RecordingPoster())
    assert out is None


def test_dormant_when_publish_was_draft_only(monkeypatch):
    _arm(monkeypatch)
    poster = RecordingPoster()
    out = publish_confirm.confirm_publish(
        _draft(), _acct(), Result("would_publish"), http=ExplodingHTTP(), poster=poster)
    assert out is None
    assert poster.replies == [] and poster.notices == []


# ---- 2. success: one READ, one LIVE reply in the card's thread ------------------
def test_success_reads_back_and_replies_permalink(monkeypatch):
    _arm(monkeypatch)
    http = RecordingHTTP({"permalink": "https://www.instagram.com/p/xyz/"})
    poster = RecordingPoster()
    out = publish_confirm.confirm_publish(_draft(), _acct(), Result(), http=http,
                                          poster=poster)
    assert out == {"verified": True, "permalink": "https://www.instagram.com/p/xyz/"}
    assert len(http.gets) == 1                     # exactly one READ
    url, params = http.gets[0]
    assert url == f"{config.GRAPH_API_BASE}/M1"
    assert params["fields"] == "permalink"         # IG media field
    assert poster.replies == [("C1", "123.456",
                               "LIVE: https://www.instagram.com/p/xyz/")]


def test_fb_page_reads_permalink_url_field(monkeypatch):
    _arm(monkeypatch)
    http = RecordingHTTP({"permalink_url": "https://www.facebook.com/123/posts/9"})
    poster = RecordingPoster()
    out = publish_confirm.confirm_publish(
        _draft(), _acct(Platform.FACEBOOK_PAGE, "lasso_fb"), Result(), http=http,
        poster=poster)
    assert out["verified"] is True
    assert http.gets[0][1]["fields"] == "permalink_url"


# ---- 3. failed verify: warn in thread + one ops alert, never re-publish ---------
def _wire_alerts(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)
    return rec


def test_verify_http_error_warns_and_alerts(monkeypatch):
    _arm(monkeypatch)
    alerts = _wire_alerts(monkeypatch)
    poster = RecordingPoster()
    out = publish_confirm.confirm_publish(
        _draft(), _acct(), Result(), http=RecordingHTTP(status=500), poster=poster)
    assert out == {"verified": False, "permalink": ""}
    channel, ts, text = poster.replies[0]          # the warn lands IN THE THREAD
    assert (channel, ts) == ("C1", "123.456")
    assert "WARNING" in text and "d1" in text
    lines = [n for n in alerts.notices if n.startswith("ECHO ALERT: ")]
    assert len(lines) == 1
    assert "publish verify failed" in lines[0]


def test_verify_missing_permalink_fails_safe(monkeypatch):
    _arm(monkeypatch)
    _wire_alerts(monkeypatch)
    poster = RecordingPoster()
    out = publish_confirm.confirm_publish(
        _draft(), _acct(), Result(), http=RecordingHTTP({}), poster=poster)
    assert out["verified"] is False
    assert "WARNING" in poster.replies[0][2]


def test_verify_without_media_id_makes_no_network_call(monkeypatch):
    _arm(monkeypatch)
    _wire_alerts(monkeypatch)
    poster = RecordingPoster()
    out = publish_confirm.confirm_publish(
        _draft(), _acct(), Result(media_id=""), http=ExplodingHTTP(), poster=poster)
    assert out["verified"] is False
    assert "no media id" in poster.replies[0][2]


# ---- 4. the token never appears in any surfaced text -----------------------------
def test_token_never_in_any_message(monkeypatch):
    _arm(monkeypatch)
    alerts = _wire_alerts(monkeypatch)
    poster = RecordingPoster()
    publish_confirm.confirm_publish(
        _draft(), _acct(), Result(), http=RecordingHTTP(status=500), poster=poster)
    surfaced = [t for _, _, t in poster.replies] + poster.notices + alerts.notices
    assert surfaced                               # something WAS surfaced
    assert all(TOKEN not in t for t in surfaced)
