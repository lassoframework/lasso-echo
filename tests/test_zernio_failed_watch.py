"""
zernio_failed_watch tests (agent/zernio_failed_watch.py), fully offline.

WHAT THIS PINS. The client IG/FB lane treats a 2xx from Zernio's POST /v1/posts as
publication. That is ACCEPTANCE, not delivery: Zernio can fail a post afterwards (a
revoked token, a dropped Facebook page grant, media the platform rejects) and until this
module nothing in Echo ever looked again. There was no failed-post read in the client
lane at all, and publish_confirm is advisory (a failed verify never reverts) and routes
Zernio verification only for LASSO — a CLIENT gym took the Meta Graph branch with a
Zernio post id, a guaranteed 4xx and a guaranteed unconfirmed nobody acted on.

So a client row could sit "Published" in the portal, carrying a real-looking post id,
while Zernio's own record said FAILED. That is published-but-not-posted in its purest
form, and it was completely invisible.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import zernio_failed_watch as zfw  # noqa: E402


def _post(pid, status="published", platform_status=None):
    p = {"_id": pid, "status": status}
    if platform_status is not None:
        p["platforms"] = [{"platform": "instagram", "status": platform_status}]
    return p


class _FakeDb:
    def __init__(self):
        self.kv = {}

    def kv_get(self, key, default=""):
        return self.kv.get(key, default)

    def kv_set(self, key, value):
        self.kv[key] = value


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_ZERNIO_FAILED_WATCH", "true")


# ---- the classifier ---------------------------------------------------------------

def test_a_row_marked_published_that_zernio_failed_is_the_loudest_finding():
    """THE CORE CASE. The portal tells the client the post is live; Zernio says it
    failed. Nothing else in Echo can see this."""
    out = zfw.build_findings(
        {"eng": [_post("zp_1", "failed"), _post("zp_2", "published")]},
        {"eng": {"zp_1", "zp_2"}})
    assert len(out) == 1
    f = out[0]
    assert f["reason"] == zfw.REASON_PUBLISHED_BUT_FAILED
    assert f["post_ids"] == ["zp_1"] and f["count"] == 1
    # The fix must warn against the blind retry that would double-post.
    assert "Do NOT blind-retry" in f["fix"]


def test_a_per_platform_failure_counts_even_when_the_post_status_does_not():
    """A post can succeed on one platform and fail on the other; the top-level status
    does not always demote. Reading only the top level would miss half of them."""
    out = zfw.build_findings(
        {"eng": [_post("zp_x", status="published", platform_status="failed")]},
        {"eng": {"zp_x"}})
    assert len(out) == 1 and out[0]["reason"] == zfw.REASON_PUBLISHED_BUT_FAILED


def test_a_clean_gym_yields_nothing():
    out = zfw.build_findings(
        {"eng": [_post("zp_1"), _post("zp_2")]}, {"eng": {"zp_1", "zp_2"}})
    assert out == []


def test_one_failed_post_alone_is_not_a_pileup():
    """A single failure is a hiccup the next post usually clears. Alerting on it trains
    everyone to ignore the channel, which is how the real pile-ups got missed."""
    out = zfw.build_findings({"eng": [_post("zp_1", "failed")]}, {"eng": set()})
    assert out == []


def test_two_or_more_failures_are_a_pileup():
    out = zfw.build_findings(
        {"eng": [_post("zp_1", "failed"), _post("zp_2", "failed"),
                 _post("zp_3", "published")]},
        {"eng": set()})
    assert len(out) == 1
    assert out[0]["reason"] == zfw.REASON_FAILED_PILEUP and out[0]["count"] == 2


def test_published_but_failed_is_reported_before_a_pileup():
    """Ordering is not cosmetic: the mis-reported row is the one the client can already
    see on their own feed, so it must lead."""
    out = zfw.build_findings(
        {"aaa": [_post("p1", "failed"), _post("p2", "failed")],
         "zzz": [_post("p3", "failed")]},
        {"zzz": {"p3"}})
    assert [f["reason"] for f in out] == [zfw.REASON_PUBLISHED_BUT_FAILED,
                                          zfw.REASON_FAILED_PILEUP]


def test_a_gyms_failure_is_never_attributed_to_another_gym():
    """Tenant isolation: findings are keyed per base, and one gym's failed post id must
    never satisfy another gym's published set."""
    out = zfw.build_findings(
        {"eng": [_post("zp_1", "failed"), _post("zp_2", "failed")]},
        {"gritx": {"zp_1"}})
    assert all(f["base"] == "eng" for f in out)
    assert all(f["reason"] != zfw.REASON_PUBLISHED_BUT_FAILED for f in out)


# ---- the sweep --------------------------------------------------------------------

def test_flag_off_is_a_total_noop(monkeypatch):
    monkeypatch.delenv("AGENT_ZERNIO_FAILED_WATCH", raising=False)
    alerts = []
    out = zfw.run(bases=["eng"], gym_posts={"eng": [_post("zp_1", "failed")]},
                  published_ids={"eng": {"zp_1"}}, alert=alerts.append, db=_FakeDb())
    assert out["enabled"] is False and out["findings"] == [] and alerts == []


def test_one_alert_per_gym_per_reason_per_day(armed):
    alerts = []
    db = _FakeDb()
    args = dict(bases=["eng"], gym_posts={"eng": [_post("zp_1", "failed")]},
                published_ids={"eng": {"zp_1"}}, alert=alerts.append, db=db,
                today="2026-08-31")
    zfw.run(**args)
    zfw.run(**args)
    assert len(alerts) == 1
    args["today"] = "2026-09-01"
    zfw.run(**args)
    assert len(alerts) == 2


def test_an_unreadable_calendar_can_only_suppress_never_invent(armed):
    """Empty published ids must not manufacture a published_but_failed finding. The
    honest failure mode of this watch is silence, never a false accusation."""
    alerts = []
    out = zfw.run(bases=["eng"], gym_posts={"eng": [_post("zp_1", "failed")]},
                  published_ids={}, alert=alerts.append, db=_FakeDb())
    assert all(f["reason"] != zfw.REASON_PUBLISHED_BUT_FAILED
               for f in out["findings"])


def test_the_watch_never_retries_or_republishes(armed):
    """HARD RAIL. A post Zernio failed on ONE platform may be live on the other, so an
    automatic retry double-posts to a real client feed. This module reports only."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(zfw))
    # Walk the CALLS only. Grepping raw source would trip over the module docstring,
    # which necessarily names the very operations this module refuses to perform.
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name:
                called.add(name)
    for forbidden in ("create_post", "create_post_raw", "publish_now", "retry",
                      "retry_all_failed", "mark_published", "mark_publishing",
                      "set_status", "patch", "post", "delete", "insert_rows"):
        assert forbidden not in called, \
            f"the failed watch must never write ({forbidden} is called)"
