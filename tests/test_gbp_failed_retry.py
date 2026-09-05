"""
AUD-003 regression tests: a failed Google Business row is retried when that is provably
safe, and said again when it is not, so a stuck row cannot go quiet for sixteen days.

The live rows this was built from (2026-09-05):
    b11ba9c0  lasso                2026-08-20  "connection routing"
    333f90a3  crossfitnine7f7dadc  2026-09-03  "photo upload: ZernioError"
"""

import datetime

from agent import gbp_failed_retry as gfr
from agent import zernio as z

TODAY = datetime.date(2026, 9, 5)

LIVE_ROUTING = {"id": "b11ba9c0-d6cc-4b7a-a5b1-37191fe9eeaa", "gym_id": "lasso",
                "account": "googlebusiness", "post_date": "2026-08-20",
                "status": "failed", "reject_reason": "connection routing",
                "late_post_id": None}

LIVE_BARE = {"id": "333f90a3-429b-46a1-b9f8-a91af1166cc8",
             "gym_id": "crossfitnine7f7dadc", "account": "googlebusiness",
             "post_date": "2026-09-03", "status": "failed",
             "reject_reason": "photo upload: ZernioError", "late_post_id": None}


def _plan(rows, **kw):
    kw.setdefault("today", TODAY)
    return gfr.plan(rows, **kw)


# ---- the double post rail ---------------------------------------------------

def test_a_row_carrying_a_post_id_is_never_requeued():
    """It may already be in front of Google. Republishing would post it twice."""
    row = dict(LIVE_ROUTING, late_post_id="gmb-123")
    p = _plan([row])
    assert p["requeue"] == []
    assert p["realert"][0]["blocked"] == "carries a post id, so it may already be live"


def test_no_other_lane_is_ever_touched():
    for acct in ("instagram", "facebook"):
        p = _plan([dict(LIVE_ROUTING, account=acct)])
        assert p["requeue"] == []


def test_a_row_that_is_not_failed_is_never_requeued():
    p = _plan([dict(LIVE_ROUTING, status="published")])
    assert p["requeue"] == []


def test_a_row_is_left_alone_after_the_attempt_cap():
    rid = LIVE_ROUTING["id"]
    p = _plan([LIVE_ROUTING], attempts={rid: gfr.MAX_ATTEMPTS})
    assert p["requeue"] == []
    assert p["realert"][0]["blocked"].startswith("already retried")


# ---- classification ---------------------------------------------------------

def test_the_live_routing_failure_is_retryable():
    """Nothing reached Google, and all 13 connections read healthy, so this is exactly
    the row a retry exists for."""
    assert gfr._classify(LIVE_ROUTING)[0] == gfr.CLASS_RETRYABLE
    assert _plan([LIVE_ROUTING])["requeue"][0]["id"] == LIVE_ROUTING["id"]


def test_a_bare_exception_class_is_treated_as_permanent_not_guessed_into_a_retry():
    """The pre C14 shape carries no evidence either way. Guessing toward a retry is the
    direction that spends money and can repeat a post."""
    assert gfr._classify(LIVE_BARE)[0] == gfr.CLASS_PERMANENT
    p = _plan([LIVE_BARE])
    assert p["requeue"] == []
    assert p["realert"][0]["id"] == LIVE_BARE["id"]


def test_a_structured_error_drives_the_decision_when_present():
    retry = dict(LIVE_BARE, error={"retryable": True, "needs_reconnect": False})
    perm = dict(LIVE_BARE, error={"retryable": False, "needs_reconnect": False})
    recon = dict(LIVE_BARE, error={"retryable": True, "needs_reconnect": True})
    assert gfr._classify(retry)[0] == gfr.CLASS_RETRYABLE
    assert gfr._classify(perm)[0] == gfr.CLASS_PERMANENT
    assert gfr._classify(recon)[0] == gfr.CLASS_RECONNECT


def test_a_reconnect_failure_is_never_requeued_and_says_so():
    row = dict(LIVE_BARE, error={"retryable": True, "needs_reconnect": True})
    p = _plan([row])
    assert p["requeue"] == []
    line = gfr._describe(p["realert"][0])
    assert "needs reconnecting" in line


# ---- the re-alert cadence (the actual AUD-003 symptom) ----------------------

def test_a_stuck_row_is_said_again():
    p = _plan([LIVE_BARE])
    assert len(p["realert"]) == 1


def test_a_row_alerted_today_stays_quiet():
    p = _plan([LIVE_BARE], last_alert={LIVE_BARE["id"]: "2026-09-05"})
    assert p["realert"] == [] and len(p["skip"]) == 1


def test_a_row_alerted_long_enough_ago_is_said_again():
    p = _plan([LIVE_BARE], last_alert={LIVE_BARE["id"]: "2026-09-01"})
    assert len(p["realert"]) == 1


def test_a_corrupt_alert_stamp_re_alerts_rather_than_going_silent():
    p = _plan([LIVE_BARE], last_alert={LIVE_BARE["id"]: "not a date"})
    assert len(p["realert"]) == 1


# ---- copy rules -------------------------------------------------------------

def test_alert_copy_carries_no_dashes_and_never_the_supplier_word():
    for row in (LIVE_ROUTING, LIVE_BARE):
        for item in _plan([row])["realert"] + _plan([row])["requeue"]:
            line = gfr._describe(item)
            assert "—" not in line and "–" not in line
            assert "vendor" not in line.lower()


# ---- the sweep is off by default -------------------------------------------

def test_run_is_off_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_GBP_FAILED_RETRY", raising=False)
    out = gfr.run()
    assert out["ok"] is False and out["requeued"] == 0


# ---- C14: the structured unwrap --------------------------------------------

def test_describe_error_unwraps_status_endpoint_account_and_retryability():
    err = z.ZernioError(422, "image too small")
    d = z.describe_error(err, endpoint="/v1/gmb-media", account_id="acct-1")
    assert d["status"] == 422 and d["endpoint"] == "/v1/gmb-media"
    assert d["account_id"] == "acct-1"
    assert d["retryable"] is False and d["needs_reconnect"] is False
    assert "image too small" in d["detail"]


def test_describe_error_marks_a_dead_grant_as_needing_a_reconnect():
    d = z.describe_error(z.ZernioError(401, "token revoked"))
    assert d["needs_reconnect"] is True and d["retryable"] is False


def test_describe_error_marks_a_5xx_and_a_429_retryable():
    for status in (429, 500, 503):
        assert z.describe_error(z.ZernioError(status, "x"))["retryable"] is True


def test_describe_error_marks_a_transport_failure_retryable():
    class ConnectionTimeout(Exception):
        pass
    assert z.describe_error(ConnectionTimeout("no route"))["retryable"] is True


def test_describe_error_never_raises_on_a_plain_exception():
    d = z.describe_error(ValueError("boom"))
    assert d["error"] == "ValueError" and d["status"] is None


def test_error_summary_replaces_the_bare_class_name():
    """The whole of C14: 'ZernioError' told nobody anything."""
    summary = z.error_summary(z.describe_error(
        z.ZernioError(422, "image rejected"), endpoint="/v1/gmb-media",
        account_id="acct-1"))
    assert "422" in summary and "/v1/gmb-media" in summary
    assert "acct-1" in summary and "[permanent]" in summary
    assert "—" not in summary and "–" not in summary
