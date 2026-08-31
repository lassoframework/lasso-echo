"""
Per-gym autonomy lane: exactly-once.

THE INCIDENT (LASSO IG triple-publish, 2026-08-27): a refired daily draw re-sent the
same drafts. The socialapi_claims table exists for exactly this, and auto-approve and
trust-autopublish were both wired to it via _claimed_meta_publish. The AUTONOMY lane
never was: it called approvals.handle_action with no claim at all. approvals' own
row-level claim cannot cover it either, because this lane approves a draft BEFORE its
first store.put(), so that claim grants by default for every caller.

Independent re-audit, 2026-08-31, flagged it as still open. These tests pin it shut.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import runner  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402


def _draft(draft_id="auto1"):
    return Draft(draft_id=draft_id, account_key="lasso_ig", platform="instagram",
                 caption="Leads go cold in minutes.", hashtags=[],
                 creative_path="card.png", creative_public_url="",
                 scheduled_for="2026-09-01T18:30:00+00:00",
                 status=DraftStatus.PENDING, day_key="2026-09-01",
                 draft_type="feed")


class _Store:
    def __init__(self):
        self.puts = []

    def put(self, d):
        self.puts.append(d)


class _Poster:
    def post_notice(self, msg):
        pass


@pytest.fixture(autouse=True)
def _autonomous(monkeypatch, tmp_path):
    """_autonomous_publish imports db / accounts / ops_alerts INSIDE the function, so
    the patches must land on the modules themselves, not on runner's namespace."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    import agent.db as _db
    import agent.accounts as _acc
    monkeypatch.setattr(_db, "is_autonomous", lambda k: True)
    monkeypatch.setattr(_db, "kv_is_durable", lambda: True)
    monkeypatch.setattr(_acc, "get_account", lambda k: Account(
        key="lasso_ig", display_name="lasso_ig", platform=Platform.INSTAGRAM,
        token_env="T", target_id_env="I"))
    yield


def _run(monkeypatch, calls, detail="published: media_id=M1", ok=True):
    """Run the lane with handle_action stubbed, counting how often it is reached."""
    import agent.approvals as _ap

    def _fake(action, draft, actor, **kw):
        calls.append(draft.draft_id)
        draft.status = DraftStatus.APPROVED
        return _ap.ActionResult(ok=ok, action="approve", draft_id=draft.draft_id,
                                detail=detail)

    monkeypatch.setattr(_ap, "handle_action", _fake)
    return runner._autonomous_publish(_draft(), _Store(), _Poster())


def test_a_refired_draw_publishes_once_not_twice(monkeypatch):
    """Draft ids are deterministic, so a refired daily draw maps to the SAME claim
    row. The second run must lose the claim rather than re-send the post."""
    calls = []
    assert _run(monkeypatch, calls) is True
    assert _run(monkeypatch, calls) is False, "the refired draw published again"
    assert calls == ["auto1"], "the post went out twice"


def test_a_dry_run_releases_the_claim_so_it_can_publish_when_armed(monkeypatch):
    """Publishing defaults OFF: a would_publish sent nothing, so holding the claim
    would mean the post can never go out once the flag is armed."""
    calls = []
    _run(monkeypatch, calls, detail="would_publish: media_id=-")
    assert _run(monkeypatch, calls, detail="published: media_id=M1") is True
    assert len(calls) == 2


def test_a_held_draft_releases_the_claim_for_a_clean_retry(monkeypatch):
    """MediaNotReady returns ok=False and publishes nothing, so a retry must work."""
    calls = []
    assert _run(monkeypatch, calls, ok=False, detail="Held: media not ready") is False
    assert _run(monkeypatch, calls) is True
    assert len(calls) == 2


def test_an_unverifiable_in_flight_claim_holds_and_never_republishes(monkeypatch):
    """An ambiguous mid-call failure keeps the claim in flight. The next run must
    VERIFY against the post log, and fail CLOSED when it cannot."""
    import agent.approvals as _ap
    import agent.ops_alerts as _oa
    calls, alerts = [], []
    monkeypatch.setattr(_oa, "alert", lambda m, **k: alerts.append(m))

    def _boom(action, draft, actor, **kw):
        calls.append(draft.draft_id)
        raise RuntimeError("connection reset mid-publish")

    monkeypatch.setattr(_ap, "handle_action", _boom)
    assert runner._autonomous_publish(_draft(), _Store(), _Poster()) is False
    monkeypatch.setattr(runner, "_verify_published_24h", lambda a, c: None)
    assert _run(monkeypatch, calls) is False, "it republished an ambiguous post"
    assert calls == ["auto1"], "the ambiguous post was sent a second time"
    assert any("HELD" in m or "cannot be verified" in m for m in alerts)


def test_a_verified_in_flight_claim_is_closed_out_not_resent(monkeypatch):
    import agent.approvals as _ap
    calls = []

    def _boom(action, draft, actor, **kw):
        calls.append(draft.draft_id)
        raise RuntimeError("connection reset mid-publish")

    monkeypatch.setattr(_ap, "handle_action", _boom)
    runner._autonomous_publish(_draft(), _Store(), _Poster())
    monkeypatch.setattr(runner, "_verify_published_24h", lambda a, c: "M1")
    assert _run(monkeypatch, calls) is False
    assert calls == ["auto1"], "a verified post was sent again"


def test_the_hold_alert_does_not_storm(monkeypatch):
    """A draft stuck on an unresolved claim would otherwise re-alert on EVERY daily
    run until a human released it. At 100 gyms that is the storm that teaches people
    to ignore the channel."""
    import agent.approvals as _ap
    import agent.ops_alerts as _oa
    alerts = []
    monkeypatch.setattr(_oa, "alert", lambda m, **k: alerts.append(m))
    monkeypatch.setattr(_ap, "handle_action",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mid-publish")))
    runner._autonomous_publish(_draft(), _Store(), _Poster())      # claim left in flight
    monkeypatch.setattr(runner, "_verify_published_24h", lambda a, c: None)
    for _ in range(5):                                             # five daily runs
        runner._autonomous_publish(_draft(), _Store(), _Poster())
    assert len(alerts) == 1, f"the hold alert stormed ({len(alerts)} alerts)"


# ---- INTEGRATION: the real handle_action, not a stub ------------------------------
class _CountingPublisher:
    """A fake PUBLISHER (the only thing stubbed): everything between the autonomy lane
    and the network is the real code path."""

    def __init__(self):
        self.calls = 0
        from agent import meta_publisher as _mp
        self.MediaNotReady = _mp.MediaNotReady

    def publish(self, draft, account):
        from agent.meta_publisher import PublishResult
        self.calls += 1
        return PublishResult(ok=True, mode="published", media_id="M1")


def test_refired_draw_through_the_REAL_approve_path_publishes_once(monkeypatch):
    """The other tests in this file stub approvals.handle_action, so they prove the
    runner's claim state machine but nothing about the integration. This one drives
    _autonomous_publish through the REAL handle_action (approver gate, per-row claim,
    postlog, the mode-string contract) with only the publisher faked, and re-runs the
    same deterministic draft id exactly as a refired daily draw would."""
    import agent.approvals as _ap
    import agent.postlog as _pl
    pub = _CountingPublisher()
    monkeypatch.setattr(_ap, "_publisher_for", lambda acct: pub)
    monkeypatch.setattr(_pl, "log_post", lambda **kw: None)

    assert runner._autonomous_publish(_draft(), _Store(), _Poster()) is True
    assert pub.calls == 1
    assert runner._autonomous_publish(_draft(), _Store(), _Poster()) is False
    assert pub.calls == 1, "the refired draw published a second time for real"
