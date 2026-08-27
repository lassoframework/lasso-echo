"""
LASSO-via-Zernio COMPLETE cutover (Blake 2026-08-27: "want everything under
zernio"): the RESIDUAL LASSO publish lanes that still went Meta-direct even under
AGENT_LASSO_VIA_ZERNIO — the book_queue / welcome / demo / trust / auto-approve
draft lanes (all via runner._claimed_meta_publish), the Slack Approve -> publish
path (approvals._publisher_for), and the approve-in-chat path
(chat_publish._real_publish_fn) — now route through the SAME Zernio client lane.

WHY: metrics_sync ingests Zernio analytics; a LASSO post that went out Meta-direct
reads there as a second publisher and taints LASSO's own months. One publish path.

Coverage, all offline:
  - flag OFF is byte-for-byte today: every lane calls meta_publisher, never Zernio.
  - flag ON + LASSO: every lane publishes through zernio_publisher, never Meta-direct.
  - exactly-once holds under the flag: the runner claim + Zernio's 24h dedup mean a
    refired draw never double-publishes.
  - missing setup HOLDS every lane with ONE deduped alert, no Meta-direct fallback.
  - a client gym is untouched by the flag (still its own client route).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, runner, approvals, chat_publish, config
from agent import lasso_zernio_route as lzr
from agent.accounts import Platform
from agent.drafter import Draft, DraftStatus
from agent.meta_publisher import PublishResult
from agent.zernio_publisher import ZernioPublishError


PROFILE_ID = "6a74a3b977a9ae3719f5c0c0"
PAGE_ID = "fbpage77"


def _draft(draft_id="d1", account_key="lasso_ig",
           caption="Honest numbers or no numbers. Book a call.", is_story=False):
    return Draft(
        draft_id=draft_id, account_key=account_key, platform="instagram",
        caption=caption, hashtags=[], creative_path="x.png",
        creative_public_url="https://cdn/x.jpg", scheduled_for="2026-08-27",
        status=DraftStatus.PENDING, day_key="2026-08-27",
        draft_type=("story" if is_story else "feed"), is_story=is_story)


class _Acct:
    def __init__(self, key="lasso_ig", platform=Platform.INSTAGRAM):
        self.key = key
        self.platform = platform


def _stamp_lasso_setup():
    db.gym_upsert("lasso", zernio_profile_id=PROFILE_ID,
                  zernio_default_fb_page_id=PAGE_ID)


@pytest.fixture
def lasso_flag(monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_VIA_ZERNIO", "true")


@pytest.fixture
def zern_armed(monkeypatch):
    # publish gates the Zernio publisher self-checks (kept armed so it would send).
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_ZERNIO_PUBLISH", "true")


@pytest.fixture
def alerts(monkeypatch):
    from agent import ops_alerts
    captured = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: captured.append(msg))
    return captured


def _patch_zernio(monkeypatch, sent, result=None):
    """Patch zernio_publisher.publish (the module the shared route calls)."""
    from agent import zernio_publisher

    def _pub(draft, account, scheduled_for=None):
        sent.append((draft.draft_id, account.key, scheduled_for,
                     bool(getattr(draft, "is_story", False))))
        return result or PublishResult(ok=True, mode="published", media_id="Z1")
    monkeypatch.setattr(zernio_publisher, "publish", _pub)


def _bomb_meta(monkeypatch):
    def _boom(d, a):
        raise AssertionError(f"Meta-direct publisher called for {a.key} under flag")
    monkeypatch.setattr("agent.meta_publisher.publish", _boom)


# ============================================================================
# LANE 1: runner._claimed_meta_publish (book/welcome/demo/trust/auto-approve)
# ============================================================================

def test_claimed_lane_flag_off_meta_direct_zernio_never_touched(monkeypatch):
    assert config.lasso_via_zernio_enabled() is False
    calls = []
    monkeypatch.setattr("agent.meta_publisher.publish",
                        lambda d, a: calls.append((d.draft_id, a.key))
                        or PublishResult(ok=True, mode="published", media_id="M1"))
    from agent import zernio_publisher
    monkeypatch.setattr(zernio_publisher, "publish",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("zernio touched with flag OFF")))
    out = runner._claimed_meta_publish(_draft("d1"), _Acct())
    assert out[0] == "published" and out[1].media_id == "M1"
    assert calls == [("d1", "lasso_ig")]


def test_claimed_lane_flag_on_routes_via_zernio(monkeypatch, lasso_flag, zern_armed):
    _stamp_lasso_setup()
    _bomb_meta(monkeypatch)
    sent = []
    _patch_zernio(monkeypatch, sent)
    out = runner._claimed_meta_publish(_draft("d1"), _Acct())
    assert out[0] == "published" and out[1].media_id == "Z1"
    assert sent == [("d1", "lasso_ig", None, False)]


def test_claimed_lane_flag_on_story_travels_as_story(monkeypatch, lasso_flag, zern_armed):
    _stamp_lasso_setup()
    _bomb_meta(monkeypatch)
    sent = []
    _patch_zernio(monkeypatch, sent)
    runner._claimed_meta_publish(_draft("s1", is_story=True), _Acct())
    assert sent == [("s1", "lasso_ig", None, True)]      # is_story preserved


def test_claimed_lane_flag_on_exactly_once_refired_draw(monkeypatch, lasso_flag, zern_armed):
    """The claim guards the Zernio lane too: a refired daily draw never re-sends."""
    _stamp_lasso_setup()
    _bomb_meta(monkeypatch)
    sent = []
    _patch_zernio(monkeypatch, sent)
    d = _draft("dup1")
    out1 = runner._claimed_meta_publish(d, _Acct())
    out2 = runner._claimed_meta_publish(d, _Acct())      # refired
    assert out1[0] == "published"
    assert out2 == ("already", None)
    assert len(sent) == 1                                # exactly once


def test_claimed_lane_flag_on_missing_setup_holds_one_alert_no_claim(
        monkeypatch, lasso_flag, zern_armed, alerts):
    """No profile/page stamped: HOLD before any claim; ONE deduped alert; the
    Zernio publisher is never called and there is NO Meta-direct fallback."""
    _bomb_meta(monkeypatch)
    from agent import zernio_publisher
    monkeypatch.setattr(zernio_publisher, "publish",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("published while setup missing")))
    d = _draft("h1")
    assert runner._claimed_meta_publish(d, _Acct()) == ("held", None)
    assert runner._claimed_meta_publish(d, _Acct()) == ("held", None)
    assert len(alerts) == 1                              # deduped
    # no claim was taken -> a later armed+set-up run can still send it
    assert db.socialapi_claim(d.draft_id, "lasso_ig")[0] == "won"


def test_claimed_lane_flag_on_zernio_setup_error_releases_claim(
        monkeypatch, lasso_flag, zern_armed):
    """Row-level setup passed but the deeper resolver fails (no connected account):
    ZernioPublishError raises BEFORE any POST, so nothing published -> claim released
    for a clean retry (not held in-flight)."""
    _stamp_lasso_setup()
    _bomb_meta(monkeypatch)
    from agent import zernio_publisher

    def _boom(draft, account, scheduled_for=None):
        raise ZernioPublishError("no connected account under profile")
    monkeypatch.setattr(zernio_publisher, "publish", _boom)
    d = _draft("e1")
    with pytest.raises(ZernioPublishError):
        runner._claimed_meta_publish(d, _Acct())
    assert db.socialapi_claim(d.draft_id, "lasso_ig")[0] == "won"   # released


def test_claimed_lane_client_gym_unaffected_by_flag(monkeypatch, lasso_flag, zern_armed):
    """A non-LASSO account is never routed by this LASSO flag: it keeps its Meta path
    (the client-lane routing lives elsewhere; here the flag simply must not fire)."""
    calls = []
    monkeypatch.setattr("agent.meta_publisher.publish",
                        lambda d, a: calls.append(a.key)
                        or PublishResult(ok=True, mode="published", media_id="M9"))
    out = runner._claimed_meta_publish(_draft("c1", account_key="gritx_ig"),
                                       _Acct(key="gritx_ig"))
    assert out[0] == "published"
    assert calls == ["gritx_ig"]


# ============================================================================
# LANE 2: approvals._publisher_for / handle_action (Slack Approve -> publish)
# ============================================================================

def test_approvals_flag_off_publisher_is_meta(monkeypatch):
    assert config.lasso_via_zernio_enabled() is False
    pub = approvals._publisher_for(_Acct())
    from agent import meta_publisher
    assert pub is meta_publisher


def test_approvals_flag_on_lasso_publisher_routes_zernio(
        monkeypatch, lasso_flag, zern_armed):
    _stamp_lasso_setup()
    sent = []
    _patch_zernio(monkeypatch, sent)
    _bomb_meta(monkeypatch)
    pub = approvals._publisher_for(_Acct())
    res = pub.publish(_draft("a1"), _Acct())
    assert res.mode == "published" and res.media_id == "Z1"
    assert sent == [("a1", "lasso_ig", None, False)]


def test_approvals_flag_on_client_gym_not_zernio_routed(monkeypatch, lasso_flag):
    """The LASSO flag must not hijack a client gym's approval publisher."""
    pub = approvals._publisher_for(_Acct(key="gritx_ig"))
    from agent import meta_publisher
    assert pub is meta_publisher


def test_approvals_flag_on_missing_setup_holds_via_medianotready(
        monkeypatch, lasso_flag, zern_armed, alerts):
    """Incomplete setup: the shim raises MediaNotReady (handle_action's retryable
    hold), fires ONE alert, and never falls back to Meta-direct."""
    from agent import meta_publisher
    monkeypatch.setattr(meta_publisher, "publish",
                        lambda d, a: (_ for _ in ()).throw(
                            AssertionError("Meta-direct fallback on hold")))
    pub = approvals._publisher_for(_Acct())
    with pytest.raises(meta_publisher.MediaNotReady):
        pub.publish(_draft("a2"), _Acct())
    assert len(alerts) == 1


def test_approvals_handle_action_approve_routes_lasso_via_zernio(
        monkeypatch, lasso_flag, zern_armed):
    """End-to-end handle_action('approve') on a LASSO draft goes through Zernio."""
    _stamp_lasso_setup()
    sent = []
    _patch_zernio(monkeypatch, sent)
    _bomb_meta(monkeypatch)
    monkeypatch.setattr(config, "APPROVER_SLACK_ID", "U_APPROVER")
    d = _draft("ha1")
    res = approvals.handle_action("approve", d, "U_APPROVER",
                                  account=_Acct(),
                                  confirmer=lambda *a, **k: None,
                                  logger=type("L", (), {
                                      "log_post": staticmethod(lambda **k: None)})())
    assert res.ok is True
    assert sent == [("ha1", "lasso_ig", None, False)]


# ============================================================================
# LANE 3: chat_publish._real_publish_fn (approve-in-chat -> publish)
# ============================================================================

def test_chat_publish_flag_off_meta_direct(monkeypatch):
    assert config.lasso_via_zernio_enabled() is False
    calls = []
    monkeypatch.setattr("agent.meta_publisher.publish",
                        lambda d, a: calls.append(a.key)
                        or PublishResult(ok=True, mode="published", media_id="M1"))
    fn = chat_publish._real_publish_fn()
    out = fn("lasso_ig", _draft("cp1"), ["ig"])
    assert out.get("mode") == "published"
    assert calls == ["lasso_ig"]


def test_chat_publish_flag_on_routes_zernio(monkeypatch, lasso_flag, zern_armed):
    _stamp_lasso_setup()
    sent = []
    _patch_zernio(monkeypatch, sent)
    _bomb_meta(monkeypatch)
    monkeypatch.setattr("agent.publish_confirm.confirm_publish",
                        lambda *a, **k: "https://permalink/z")
    fn = chat_publish._real_publish_fn()
    out = fn("lasso_ig", _draft("cp2"), ["ig"])
    assert out.get("mode") == "published"
    assert sent == [("cp2", "lasso_ig", None, False)]


def test_chat_publish_flag_on_missing_setup_holds_no_meta(
        monkeypatch, lasso_flag, zern_armed, alerts):
    from agent import zernio_publisher
    monkeypatch.setattr(zernio_publisher, "publish",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("published while setup missing")))
    monkeypatch.setattr("agent.meta_publisher.publish",
                        lambda d, a: (_ for _ in ()).throw(
                            AssertionError("Meta-direct fallback on hold")))
    fn = chat_publish._real_publish_fn()
    out = fn("lasso_ig", _draft("cp3"), ["ig"])
    assert "held" in out.get("error", "").lower() or "incomplete" in out.get("error", "").lower()
    assert len(alerts) == 1


# ============================================================================
# SHARED MODULE: the choke point contract
# ============================================================================

def test_shared_should_route_lasso_only_and_flag_gated(monkeypatch):
    assert lzr.should_route("lasso_ig") is False          # flag OFF
    monkeypatch.setenv("AGENT_LASSO_VIA_ZERNIO", "true")
    assert lzr.should_route("lasso_ig") is True
    assert lzr.should_route("lasso_fb") is True
    assert lzr.should_route("gritx_ig") is False          # client gym
    assert lzr.should_route("") is False


def test_shared_held_dedups_alert_and_rearms(monkeypatch, lasso_flag, alerts):
    # missing setup -> held returns the pieces + ONE alert, repeated calls dedupe
    assert lzr.held("lasso_ig")                            # truthy: missing pieces
    assert lzr.held("lasso_ig")
    assert len(alerts) == 1
    # complete the setup -> held is [] and the alert re-arms for a future regression
    _stamp_lasso_setup()
    assert lzr.held("lasso_ig") == []
    assert db.kv_get(lzr.HOLD_KEY) in ("", None)
