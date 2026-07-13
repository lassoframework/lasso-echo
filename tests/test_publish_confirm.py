"""
Publish confirmation tests. Fully OFFLINE: recording HTTP + poster fakes, no
network. Asserts: the flag defaults OFF and OFF (or a would_publish result) is
fully dormant; ON, one Graph READ per confirm and one "LIVE: <permalink>" reply
into the card's thread; a failed verify (the post is already live) posts ONE
soft note into the thread and emits NO ops alert — never implying a live post
failed; the module can never re-publish (any POST explodes the test); and the
token never appears in any message.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, db, ops_alerts, publish_confirm  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft  # noqa: E402


def _kv_delete(key):
    try:
        with db._lock, db.connect() as conn:
            conn.execute("DELETE FROM kv WHERE key=?", (key,))
            conn.commit()
    except Exception:
        pass

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
    http = RecordingHTTP({"id": "M1", "permalink": "https://www.instagram.com/p/xyz/"})
    poster = RecordingPoster()
    out = publish_confirm.confirm_publish(_draft(), _acct(), Result(), http=http,
                                          poster=poster)
    assert out == {"verified": True, "permalink": "https://www.instagram.com/p/xyz/"}
    assert len(http.gets) == 1                     # exactly one READ
    url, params = http.gets[0]
    assert url == f"{config.GRAPH_API_BASE}/M1"
    assert params["fields"] == "id,permalink"      # minimal + the IG link
    assert poster.replies == [("C1", "123.456",
                               "LIVE: https://www.instagram.com/p/xyz/")]


def test_fb_page_reads_minimal_existence_fields(monkeypatch):
    """The lasso_fb false alarm, reproduced: a /photos publish can return the
    PHOTO node id, and Photo/PagePost nodes disagree on fields — asking for one
    the node lacks (or one needing a scope we do not have) 400s while the post is
    live. The fix reads only `id` (present on every node, no extra scope) and
    verifies."""
    _arm(monkeypatch)

    class PhotoNodeGraph(RecordingHTTP):
        def get(self, url, params=None, timeout=None):
            self.gets.append((url, dict(params or {})))
            fields = (params or {}).get("fields", "")
            # any field beyond bare id would 400 on this node type
            if fields != "id":
                payload, status = {"error": {"code": 100,
                    "message": f"Tried accessing nonexisting field ({fields}) "
                               "on node type (Photo)"}}, 400
            else:
                payload, status = {"id": "PHOTO123"}, 200

            class R:
                status_code = status

                def json(self):
                    return payload

            return R()

    http = PhotoNodeGraph()
    poster = RecordingPoster()
    out = publish_confirm.confirm_publish(
        _draft(), _acct(Platform.FACEBOOK_PAGE, "lasso_fb"),
        Result(media_id="PHOTO123"), http=http, poster=poster)
    assert out["verified"] is True                    # live, verified, no false alarm
    assert http.gets[0][1]["fields"] == "id"          # minimal, no-scope read
    assert "LIVE on lasso_fb" in poster.replies[0][2]
    assert "verified" in poster.replies[0][2]


# ---- 3. failed verify (post is live): SOFT note in thread, NO ops alert -------
def _wire_alerts(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)
    return rec


def test_verify_http_error_is_soft_note_not_alert(monkeypatch):
    """A 400/500 on the read-back means only the verify failed — the post is
    live. It must post a SOFT note into the thread and fire NO ECHO ALERT."""
    _arm(monkeypatch)
    alerts = _wire_alerts(monkeypatch)
    _kv_delete("verify_noted_d1")
    poster = RecordingPoster()
    out = publish_confirm.confirm_publish(
        _draft(), _acct(), Result(), http=RecordingHTTP(status=400), poster=poster)
    assert out == {"verified": False, "permalink": ""}
    channel, ts, text = poster.replies[0]          # the note lands IN THE THREAD
    assert (channel, ts) == ("C1", "123.456")
    # HONEST wording: the post IS live; only verification failed
    assert "post is live" in text and "d1" in text
    assert "WARNING" not in text
    assert "HTTP 400" in text                       # the reason is surfaced softly
    # the KEY fix: NO ECHO ALERT for a published-but-unverified post
    assert not any(n.startswith("ECHO ALERT: ") for n in alerts.notices)


def test_verify_empty_body_is_unconfirmed_not_alarmed(monkeypatch):
    _arm(monkeypatch)
    alerts = _wire_alerts(monkeypatch)
    _kv_delete("verify_noted_d1")
    poster = RecordingPoster()
    out = publish_confirm.confirm_publish(
        _draft(), _acct(), Result(), http=RecordingHTTP({}), poster=poster)
    assert out["verified"] is False                   # no id: existence unproven
    assert "post is live" in poster.replies[0][2]     # honest, softer note
    assert not any(n.startswith("ECHO ALERT: ") for n in alerts.notices)


def test_verified_without_permalink_when_id_present(monkeypatch):
    _arm(monkeypatch)
    poster = RecordingPoster()
    out = publish_confirm.confirm_publish(
        _draft(), _acct(), Result(), http=RecordingHTTP({"id": "M1"}), poster=poster)
    assert out["verified"] is True                    # existence proven by id
    assert "LIVE on lasso_ig" in poster.replies[0][2]


def test_verify_without_media_id_makes_no_network_call(monkeypatch):
    _arm(monkeypatch)
    _wire_alerts(monkeypatch)
    _kv_delete("verify_noted_d1")
    poster = RecordingPoster()
    out = publish_confirm.confirm_publish(
        _draft(), _acct(), Result(media_id=""), http=ExplodingHTTP(), poster=poster)
    assert out["verified"] is False
    assert "no media id" in poster.replies[0][2] and "post is live" in poster.replies[0][2]


# ---- 4. the token never appears in any surfaced text -----------------------------
def test_token_never_in_any_message(monkeypatch):
    _arm(monkeypatch)
    alerts = _wire_alerts(monkeypatch)
    _kv_delete("verify_noted_d1")
    poster = RecordingPoster()
    publish_confirm.confirm_publish(
        _draft(), _acct(), Result(), http=RecordingHTTP(status=500), poster=poster)
    surfaced = [t for _, _, t in poster.replies] + poster.notices + alerts.notices
    assert surfaced                               # something WAS surfaced
    assert all(TOKEN not in t for t in surfaced)


# ---- 5. the audit trail carries the distinction ------------------------------------
def test_audit_distinguishes_verified_vs_unconfirmed(monkeypatch):
    _arm(monkeypatch)
    _kv_delete("verify_noted_d1")
    poster = RecordingPoster()
    publish_confirm.confirm_publish(
        _draft(), _acct(), Result(media_id="OK1"),
        http=RecordingHTTP({"id": "OK1", "permalink": "https://ig/p/1"}), poster=poster)
    publish_confirm.confirm_publish(
        _draft(), _acct(), Result(media_id="BAD1"),
        http=RecordingHTTP(status=400), poster=poster)
    reasons = [r["reason"] for r in db.audit_rows() if r["kind"] == "publish_confirm"]
    assert any(r.startswith("verified live") for r in reasons)
    assert any(r.startswith("published, verify unconfirmed") for r in reasons)


# ---- 6. dedup: same draft called twice posts the soft note exactly once --------
def test_verify_soft_note_posts_only_once_per_draft(monkeypatch):
    """Slack can retry the tap webhook, calling confirm_publish twice for the
    same draft. The soft note must post exactly once (the lasso_fb draft
    1527038d4e double-post, reproduced here) and NEVER an ECHO ALERT."""
    _arm(monkeypatch)
    alerts = _wire_alerts(monkeypatch)
    draft_id = "d_dedup_verify_test_001"
    _kv_delete(f"verify_noted_{draft_id}")

    d = Draft(draft_id=draft_id, account_key="lasso_fb", platform="facebook",
              caption="x", hashtags=[], creative_path="a.png",
              creative_public_url="", scheduled_for="2026-07-01T18:30:00+00:00",
              slack_channel="C1", slack_ts="ts1")

    poster = RecordingPoster()
    # First call: soft note posts
    out1 = publish_confirm.confirm_publish(
        d, _acct(Platform.FACEBOOK_PAGE, "lasso_fb"),
        Result(media_id="P9"), http=RecordingHTTP(status=503), poster=poster)
    # Second call (Slack retry): soft note must NOT post again
    out2 = publish_confirm.confirm_publish(
        d, _acct(Platform.FACEBOOK_PAGE, "lasso_fb"),
        Result(media_id="P9"), http=RecordingHTTP(status=503), poster=poster)

    assert out1 == {"verified": False, "permalink": ""}
    assert out2 == {"verified": False, "permalink": ""}
    noted = [r for r in poster.replies if draft_id in r[2]]
    assert len(noted) == 1, f"expected 1 soft note, got {len(noted)}: {noted}"
    # never an ECHO ALERT, on either call
    assert not any(n.startswith("ECHO ALERT: ") for n in alerts.notices)


def test_real_publish_failure_still_alerts_loudly(monkeypatch):
    """The loud alert for an ACTUAL failed publish (approvals path) is unchanged
    and clearly distinct from the softer verify note."""
    from agent import approvals
    from agent.drafter import DraftStatus
    alerts = _wire_alerts(monkeypatch)
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")

    class ExplodingPublisher:
        def publish(self, draft, account):
            raise RuntimeError("Graph 500 on the publish WRITE")

    d = _draft()
    d.status = DraftStatus.PENDING
    import pytest
    with pytest.raises(RuntimeError):
        approvals.handle_action("approve", d, actor_slack_id="U06EPUUCL13",
                                publisher=ExplodingPublisher(), account=_acct())
    loud = [n for n in alerts.notices if "publish attempt failed" in n]
    assert len(loud) == 1                             # the REAL alarm, unchanged
    assert "post itself is live" not in loud[0]       # never the soft wording
