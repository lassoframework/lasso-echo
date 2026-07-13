"""
IG "media not ready" (subcode 2207027) retry.

On an Instagram single-image publish we now poll the media container's
status_code until FINISHED before calling media_publish, so we never publish a
container Meta has not finished processing. Fully OFFLINE (fake Graph client,
injected no-op sleep). Asserts: the happy path polls then publishes; a container
that never finishes (or reports ERROR) raises MediaNotReady and NEVER publishes;
and the approval path turns MediaNotReady into a HELD card with a clear,
non-alarming note, not a crash and not the loud publish-failure alarm.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import approvals, meta_publisher, ops_alerts  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402


class FakeGraph:
    """Fake Graph client. GET returns the next status_code (repeats the last one
    once the script is exhausted); POST records the call and returns an id."""

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self.posts = []
        self.gets = []

    def post(self, url, data=None, timeout=None, **k):
        self.posts.append((url, dict(data or {})))

        class R:
            status_code = 200

            def json(self_inner):
                return {"id": "CID", "post_id": "PID"}

        return R()

    def get(self, url, params=None, timeout=None, **k):
        self.gets.append((url, dict(params or {})))
        status = self._statuses.pop(0) if self._statuses else self._last
        self._last = status

        class R:
            status_code = 200

            def json(self_inner):
                return {"status_code": status}

        return R()


def _acct():
    return Account(key="lasso_ig", display_name="lasso_ig",
                   platform=Platform.INSTAGRAM,
                   token_env="MEDIA_READY_TOKEN", target_id_env="MEDIA_READY_TARGET")


def _draft():
    return Draft(draft_id="mr1", account_key="lasso_ig", platform="instagram",
                 caption="Leads go cold in minutes.", hashtags=["#LASSOFramework"],
                 creative_path="card.png",
                 creative_public_url="https://cdn.echo.test/echo/lasso_ig/card.png",
                 scheduled_for="2026-07-13T18:30:00+00:00", status=DraftStatus.PENDING,
                 slack_channel="C1", slack_ts="1.1")


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("MEDIA_READY_TOKEN", "tok_media_ready")
    monkeypatch.setenv("MEDIA_READY_TARGET", "1789")
    # never actually sleep between polls
    monkeypatch.setattr(meta_publisher.time, "sleep", lambda *_a, **_k: None)


# ---- 1. happy path: poll until FINISHED, THEN publish -------------------------
def test_ig_image_polls_then_publishes(monkeypatch):
    _arm(monkeypatch)
    http = FakeGraph(["IN_PROGRESS", "FINISHED"])   # not ready once, then ready
    res = meta_publisher.publish(_draft(), _acct(), http=http)
    assert res.mode == "published"
    assert res.media_id == "CID"
    # the container was polled before publishing (>=1 status GET happened)
    assert len(http.gets) >= 1
    assert all("status_code" == g[1].get("fields") for g in http.gets)
    # exactly two POSTs: create container, then media_publish (in that order)
    assert len(http.posts) == 2
    assert http.posts[0][0].endswith("/1789/media")
    assert http.posts[1][0].endswith("/1789/media_publish")


# ---- 2. timeout: never FINISHED -> MediaNotReady, and NEVER publishes ---------
def test_ig_image_times_out_holds_without_publishing(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(meta_publisher, "IMG_POLL_MAX_TRIES", 3)
    monkeypatch.setattr(meta_publisher, "IMG_POLL_INTERVAL_SEC", 1)
    http = FakeGraph(["IN_PROGRESS"])               # forever in progress
    with pytest.raises(meta_publisher.MediaNotReady):
        meta_publisher.publish(_draft(), _acct(), http=http)
    # the container was created but media_publish was NEVER called
    assert len(http.posts) == 1
    assert http.posts[0][0].endswith("/1789/media")
    assert not any(u.endswith("/media_publish") for u, _ in http.posts)


# ---- 3. container ERROR -> MediaNotReady immediately, NEVER publishes ---------
def test_ig_image_error_status_holds_without_publishing(monkeypatch):
    _arm(monkeypatch)
    http = FakeGraph(["ERROR"])
    with pytest.raises(meta_publisher.MediaNotReady):
        meta_publisher.publish(_draft(), _acct(), http=http)
    assert len(http.posts) == 1                      # only the create, no publish
    assert not any(u.endswith("/media_publish") for u, _ in http.posts)


# ---- 4. MediaNotReady is a PublishError (existing catchers still catch it) ----
def test_media_not_ready_is_publish_error():
    assert issubclass(meta_publisher.MediaNotReady, meta_publisher.PublishError)


# ---- 5. approval path: MediaNotReady HOLDS the card, does not crash -----------
def _wire_alerts(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")

    class RecordingPoster:
        def __init__(self):
            self.notices = []

        def post_notice(self, text):
            self.notices.append(text)
            return {"ok": True}

    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)
    return rec


def test_approve_holds_card_when_media_not_ready(monkeypatch):
    alerts = _wire_alerts(monkeypatch)

    class HeldPublisher:
        def publish(self, draft, account):
            raise meta_publisher.MediaNotReady(
                "media container CID not FINISHED after 30 tries (~60s).")

    d = _draft()
    res = approvals.handle_action("approve", d, actor_slack_id="U06EPUUCL13",
                                  publisher=HeldPublisher(), account=_acct())
    # HELD, not a crash: ok False, clear retry wording, draft stays PENDING
    assert res.ok is False
    assert "Held" in res.detail and "retry" in res.detail.lower()
    assert d.status == DraftStatus.PENDING           # never marked APPROVED
    # one non-alarming ops alert; NEVER the loud publish-failure wording
    held = [n for n in alerts.notices if "media not ready" in n]
    assert len(held) == 1
    assert "Nothing published" in held[0]
    assert not any("publish attempt failed" in n for n in alerts.notices)
