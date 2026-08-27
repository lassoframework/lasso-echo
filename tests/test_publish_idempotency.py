"""Exactly-once on the meta-direct autopublish lane (LASSO IG triple-publish,
2026-08-27), all offline.

The live bug: mid-draw deploys refired the daily draw (last_run_date was
stamped AFTER the full draw) and the auto-approve lane published with NO claim,
so identical posts re-sent ('Honest numbers or no numbers' x3, Pierce x2).

Coverage:
  * runner._claimed_meta_publish claims BEFORE the external call; a second call
    for the same (draft, account) never re-publishes;
  * an IN-FLIGHT claim from a dead run is VERIFIED against the post log
    (caption-hash, 24h) — verified => marked done, not re-sent; unverifiable =>
    HELD with ONE deduped alert (fail closed);
  * meta_publisher's 24h content-hash dedup: the same (account, caption, media)
    is never sent twice; release_dedup is the explicit human override;
  * listener stamp order: the day is claimed BEFORE the draw fires, and an
    interrupted draw alerts once instead of blind-refiring.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, listener, runner
from agent.accounts import Platform
from agent.drafter import Draft, DraftStatus
from agent.meta_publisher import PublishResult


def _draft(draft_id="d1", caption="Honest numbers or no numbers. Book a call."):
    return Draft(
        draft_id=draft_id, account_key="lasso_ig", platform="instagram",
        caption=caption, hashtags=[], creative_path="x.png",
        creative_public_url="https://cdn/x.jpg", scheduled_for="2026-08-27",
        status=DraftStatus.PENDING, day_key="2026-08-27", draft_type="feed")


class _Acct:
    key = "lasso_ig"
    platform = Platform.INSTAGRAM


# ---- the claim wrapper --------------------------------------------------------

def test_claim_prevents_second_publish(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "agent.meta_publisher.publish",
        lambda d, a: calls.append(d) or PublishResult(ok=True, mode="published",
                                                      media_id="M1"))
    d = _draft()
    out1 = runner._claimed_meta_publish(d, _Acct())
    out2 = runner._claimed_meta_publish(d, _Acct())     # the refired draw
    assert out1[0] == "published" and out1[1].media_id == "M1"
    assert out2 == ("already", None)
    assert len(calls) == 1                              # exactly once


def test_would_publish_releases_claim(monkeypatch):
    """Flag-off (no network) never burns the claim: an armed later run publishes."""
    modes = iter(["would_publish", "published"])
    calls = []
    monkeypatch.setattr(
        "agent.meta_publisher.publish",
        lambda d, a: calls.append(d) or PublishResult(ok=True, mode=next(modes)))
    d = _draft("d2")
    assert runner._claimed_meta_publish(d, _Acct())[0] == "published"
    assert runner._claimed_meta_publish(d, _Acct())[0] == "published"
    assert len(calls) == 2                              # both attempts ran


def test_inflight_claim_verified_against_post_log(monkeypatch):
    """A dead run left the claim in-flight but the post log PROVES it landed:
    marked done, never re-sent."""
    from agent import postlog
    d = _draft("d3")
    state, _ = db.socialapi_claim(d.draft_id, "lasso_ig")   # simulate the dead run
    assert state == "won"
    postlog.log_post(account_key="lasso_ig", platform="instagram",
                     caption=d.caption, media_id="M-live", mode="published",
                     draft_id=d.draft_id, path=os.devnull)
    calls = []
    monkeypatch.setattr("agent.meta_publisher.publish",
                        lambda dd, a: calls.append(dd))
    assert runner._claimed_meta_publish(d, _Acct()) == ("already", None)
    assert calls == []
    assert db.socialapi_claim(d.draft_id, "lasso_ig")[0] == "done"


def test_inflight_claim_unverifiable_holds_with_one_alert(monkeypatch):
    """No post-log evidence => HOLD (fail closed), one deduped alert, never a
    blind re-send."""
    from agent import ops_alerts
    sent = []
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: sent.append(m))
    d = _draft("d4")
    db.socialapi_claim(d.draft_id, "lasso_ig")              # dead run's claim
    calls = []
    monkeypatch.setattr("agent.meta_publisher.publish",
                        lambda dd, a: calls.append(dd))
    assert runner._claimed_meta_publish(d, _Acct()) == ("held", None)
    assert runner._claimed_meta_publish(d, _Acct()) == ("held", None)
    assert calls == []
    assert len([m for m in sent if "IN-FLIGHT" in m]) == 1  # deduped


def test_media_not_ready_releases_claim(monkeypatch):
    """MediaNotReady = nothing published (known retryable): the claim releases
    so the retry can actually retry."""
    from agent.meta_publisher import MediaNotReady
    d = _draft("d5")

    def boom(dd, a):
        raise MediaNotReady("container never finished")
    monkeypatch.setattr("agent.meta_publisher.publish", boom)
    with pytest.raises(MediaNotReady):
        runner._claimed_meta_publish(d, _Acct())
    assert db.socialapi_claim(d.draft_id, "lasso_ig")[0] == "won"  # released


def test_ambiguous_failure_keeps_claim_inflight(monkeypatch):
    """An mid-call exception may have reached Meta: the claim stays in-flight so
    the next attempt VERIFIES instead of resending."""
    d = _draft("d6")

    def boom(dd, a):
        raise RuntimeError("connection reset mid-request")
    monkeypatch.setattr("agent.meta_publisher.publish", boom)
    with pytest.raises(RuntimeError):
        runner._claimed_meta_publish(d, _Acct())
    assert db.socialapi_claim(d.draft_id, "lasso_ig")[0] == "in_flight"


# ---- the 24h content-hash dedup at the Meta boundary ----------------------------

class _FbAcct:
    key = "lasso_fb"
    platform = Platform.FACEBOOK_PAGE

    def get_token(self):
        return "tok"

    def get_target_id(self):
        return "page1"


class _Resp:
    status_code = 200
    text = "ok"

    def json(self):
        return {"post_id": "FB1"}


class _Http:
    def __init__(self):
        self.posts = []

    def post(self, url, data=None, timeout=None):
        self.posts.append(url)
        return _Resp()


def test_meta_dedup_never_sends_same_content_twice_in_24h(monkeypatch):
    from agent import meta_publisher
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    http = _Http()
    d = _draft("d7")
    r1 = meta_publisher.publish(d, _FbAcct(), http=http)
    assert r1.mode == "published" and r1.media_id == "FB1"
    assert len(http.posts) == 1
    # the 5-second-retry / refire shape: an identical send moments later
    d_again = _draft("d7-refire")                       # different draft id, same content
    r2 = meta_publisher.publish(d_again, _FbAcct(), http=http)
    assert r2.mode == "published" and r2.media_id == "FB1"
    assert "dedup" in r2.detail
    assert len(http.posts) == 1                         # NO second network call
    # a DIFFERENT account is not deduped (IG + FB cross-post stays legal)
    class _Fb2(_FbAcct):
        key = "lasso_fb2"
    r3 = meta_publisher.publish(_draft("d8"), _Fb2(), http=http)
    assert len(http.posts) == 2


def test_meta_dedup_explicit_human_release(monkeypatch):
    from agent import meta_publisher
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    http = _Http()
    d = _draft("d9")
    meta_publisher.publish(d, _FbAcct(), http=http)
    meta_publisher.release_dedup("lasso_fb", d)         # the human override
    meta_publisher.publish(d, _FbAcct(), http=http)
    assert len(http.posts) == 2


def test_would_publish_never_stamps_dedup(monkeypatch):
    from agent import meta_publisher
    d = _draft("d10")
    r = meta_publisher.publish(d, _FbAcct(), http=_Http())   # publish flag OFF
    assert r.mode == "would_publish"
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    http = _Http()
    r2 = meta_publisher.publish(d, _FbAcct(), http=http)
    assert r2.mode == "published" and len(http.posts) == 1   # not falsely deduped


# ---- listener: claim the day BEFORE the draw; interrupted draw alerts once ------

def test_interrupted_draw_alerts_once_and_never_refires(tmp_path, monkeypatch):
    from agent import ops_alerts
    monkeypatch.setenv("AGENT_SCHEDULER_STATE_DIR", str(tmp_path))
    sent = []
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: sent.append(m))
    # simulate: the day was claimed + started, the worker died mid-draw
    listener._write_last_run_date("2026-08-27")
    listener._mark_draw_started("2026-08-27")
    assert listener.alert_interrupted_draw() is True
    assert listener.alert_interrupted_draw() is False        # deduped
    assert len(sent) == 1 and "NOT auto-refired" in sent[0]
    # the day stays claimed: a restart does NOT refire the draw
    assert listener._read_last_run_date() == "2026-08-27"
    # a finished draw is quiet
    listener._mark_draw_finished("2026-08-27")
    assert listener.alert_interrupted_draw() is False
