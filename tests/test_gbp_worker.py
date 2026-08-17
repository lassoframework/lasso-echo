"""
GBP publish worker (agent/gbp_worker.py): row->payload, send in DRAFT mode (nothing
live), send-time rail re-validation, and the §7.2 reconcile classifier. Offline; the
Zernio client is a fake that records the body and never hits the network.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gbp_worker as gw  # noqa: E402


_GOOD_CAPTION = ("Carmel strength for busy parents: a plan that fits real life and "
                 "actually sticks. Come see the floor and meet the coaches this week.")


def _conn():
    return {"zernio_account_id": "acc_gbp_1", "gbp_location_id": "locations/123"}


def _row(**over):
    r = {"caption": _GOOD_CAPTION, "image_url": "https://r2/x.jpg",
         "gbp_topic_type": "STANDARD", "gbp_cta_type": "LEARN_MORE",
         "gbp_cta_url": "https://gym.com/start", "pillar": "Local Update",
         "gbp_event": None, "gbp_offer": None}
    r.update(over)
    return r


class _FakeClient:
    def __init__(self):
        self.calls = []

    def create_post_raw(self, payload, *, draft=False, publish_now=True):
        self.calls.append({"payload": payload, "draft": draft})
        return {"_id": "zpost_gbp_1"}


# ---- row -> payload --------------------------------------------------------

def test_standard_row_builds_payload_and_sends_draft():
    c = _FakeClient()
    out = gw.publish_gbp_row(_row(), _conn(), client=c, draft=True)
    assert out["ok"] and out["status"] == "published"
    assert out["late_post_id"] == "zpost_gbp_1" and out["mode"] == "draft"
    assert c.calls[0]["draft"] is True, "autonomous build must send isDraft, never live"
    psd = c.calls[0]["payload"]["platforms"][0]["platformSpecificData"]
    assert psd["topicType"] == "STANDARD"
    assert "utm_campaign=echo_local_update" in psd["callToAction"]["url"]


def test_offer_row_omits_call_to_action():
    c = _FakeClient()
    row = _row(gbp_topic_type="OFFER", gbp_cta_type=None, gbp_cta_url="",
               gbp_offer={"redeemOnlineUrl": "https://gym.com/join",
                          "termsConditions": "New members."},
               gbp_event={"schedule": {"startDate": "2026-09-01",
                                       "endDate": "2026-09-10"}})
    out = gw.publish_gbp_row(row, _conn(), client=c, draft=True)
    assert out["ok"]
    psd = c.calls[0]["payload"]["platforms"][0]["platformSpecificData"]
    assert "callToAction" not in psd
    assert "utm_campaign=echo" in psd["offer"]["redeemOnlineUrl"]


# ---- send-time rail re-validation (§7.1) ----------------------------------

def test_dash_caption_fails_at_send_never_ships():
    c = _FakeClient()
    out = gw.publish_gbp_row(_row(caption="Carmel gym - built for parents who are busy "
                                          "and want a plan that finally sticks now."),
                             _conn(), client=c, draft=True)
    assert not out["ok"] and out["status"] == "failed"
    assert "dash" in out["reject_reason"]
    assert c.calls == [], "a rail violation must never reach Zernio"


def test_phone_caption_fails_at_send():
    c = _FakeClient()
    out = gw.publish_gbp_row(_row(caption="Call 317-555-0198 to join our Carmel strength "
                                          "program for busy parents starting this week."),
                             _conn(), client=c, draft=True)
    assert not out["ok"] and "phone" in out["reject_reason"] and c.calls == []


def test_missing_image_fails_at_send():
    c = _FakeClient()
    out = gw.publish_gbp_row(_row(image_url=""), _conn(), client=c, draft=True)
    assert not out["ok"] and "image" in out["reject_reason"] and c.calls == []


def test_bad_payload_offer_without_fields_fails():
    c = _FakeClient()
    out = gw.publish_gbp_row(_row(gbp_topic_type="OFFER", gbp_offer=None,
                                  gbp_cta_type=None, gbp_cta_url=""),
                             _conn(), client=c, draft=True)
    assert not out["ok"] and "payload" in out["reject_reason"] and c.calls == []


# ---- §7.2 reconcile classifier --------------------------------------------

def test_reconcile_published():
    st, reason = gw.classify_reconcile(
        {"post": {"platforms": [{"platform": "googlebusiness", "status": "published"}]}})
    assert st == "published" and reason == ""


def test_reconcile_policy_rejection_is_failed_never_retry():
    st, reason = gw.classify_reconcile(
        {"post": {"platforms": [{"platform": "googlebusiness", "status": "failed",
                                 "error": "Post rejected: phone number not allowed"}]}})
    assert st == "failed"
    assert "phone number not allowed" in reason


def test_reconcile_transient_is_retry():
    st, _ = gw.classify_reconcile(
        {"post": {"platforms": [{"platform": "googlebusiness", "status": "failed",
                                 "error": "Upstream 503 service unavailable"}]}})
    assert st == "retry"


def test_reconcile_unknown_failure_is_failed_not_retry_loop():
    st, reason = gw.classify_reconcile(
        {"post": {"platforms": [{"platform": "googlebusiness", "status": "failed",
                                 "error": "weird"}]}})
    assert st == "failed" and reason


def test_reconcile_pending_keeps_polling():
    st, _ = gw.classify_reconcile(
        {"post": {"platforms": [{"platform": "googlebusiness", "status": "processing"}]}})
    assert st == "pending"


def test_reconcile_deleted():
    st, _ = gw.classify_reconcile(
        {"post": {"platforms": [{"platform": "googlebusiness", "status": "deleted"}]}})
    assert st == "deleted"


def test_reconcile_reason_scrubs_urls():
    st, reason = gw.classify_reconcile(
        {"post": {"platforms": [{"platform": "googlebusiness", "status": "failed",
                                 "error": "policy: bad url https://x.co/secret?t=abc"}]}})
    assert st == "failed" and "https://" not in reason


# ---- 48h reconcile window -------------------------------------------------

def test_reconcile_window():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    assert gw.in_reconcile_window("2026-08-15T06:00:00+00:00", now=now)     # 6h ago
    assert gw.in_reconcile_window("2026-08-13T13:00:00+00:00", now=now)     # 47h ago
    assert not gw.in_reconcile_window("2026-08-13T06:00:00+00:00", now=now)  # 54h ago
    assert not gw.in_reconcile_window("", now=now)


# ---- §7.1 connection routing + precheck -----------------------------------

def _c(loc="locations/1", status="connected"):
    return {"zernio_account_id": "acc1", "gbp_location_id": loc, "status": status}


def test_resolve_exactly_one_connection():
    assert gw.resolve_connection([_c()])["gbp_location_id"] == "locations/1"


def test_resolve_zero_connections_raises():
    with pytest.raises(gw.RoutingError):
        gw.resolve_connection([])


def test_resolve_two_connections_needs_location():
    conns = [_c("locations/1"), _c("locations/2")]
    with pytest.raises(gw.RoutingError):
        gw.resolve_connection(conns)                       # ambiguous
    assert gw.resolve_connection(conns, "locations/2")["gbp_location_id"] == "locations/2"


def test_publish_one_holds_on_needs_reconnect():
    c = _FakeClient()
    alerts = []
    out = gw.publish_one(_row(), [_c(status="needs_reconnect")], client=c,
                         draft=True, alert=alerts.append)
    assert out["status"] == "approved" and out.get("held") == "needs_reconnect"
    assert c.calls == [] and alerts == []      # silent hold, no send, no spam


def test_publish_one_routing_failure_is_failed_with_alert():
    c = _FakeClient()
    alerts = []
    out = gw.publish_one(_row(), [_c("locations/1"), _c("locations/2")], client=c,
                         draft=True, alert=alerts.append)
    assert out["status"] == "failed" and out["reject_reason"] == "connection routing"
    assert alerts and c.calls == []            # alerted, never sent


def test_publish_one_sends_draft_on_clean_route():
    c = _FakeClient()
    out = gw.publish_one(_row(), [_c()], client=c, draft=True)
    assert out["status"] == "published" and out["late_post_id"] == "zpost_gbp_1"
    assert c.calls[0]["draft"] is True


# ---- orchestration lanes (fake store) -------------------------------------

class _Store:
    def __init__(self, rows, conns):
        self._rows = rows
        self._conns = conns
        self.published = []
        self.failed = []
        self.status = []

    def approved_gbp_rows(self, run_date):
        return [dict(r) for r in self._rows]

    def connections_for(self, gym):
        return [dict(c) for c in self._conns.get(gym, [])]

    def recent_published_gbp(self, since):
        return [dict(r) for r in self._rows if r.get("status") == "published"]

    def mark_published(self, row_id, late, at):
        self.published.append((row_id, late))

    def mark_failed(self, row_id, reason):
        self.failed.append((row_id, reason))

    def mark_status(self, row_id, status):
        self.status.append((row_id, status))

    # G3 metrics capture (additive)
    def bump_posts_published(self, gym, loc, month_iso, *, now_iso, seed_top_post_id=None):
        self.__dict__.setdefault("bumps", []).append(
            (gym, loc, month_iso, seed_top_post_id))

    def top_post_by_clicks(self, gym, loc, month):
        return {"id": "m1", "top_post_id": None}

    def set_top_post(self, metrics_row_id, top_post_id, now_iso):
        self.__dict__.setdefault("top_sets", []).append((metrics_row_id, top_post_id))


def test_g3_publish_bumps_posts_published():
    rows = [dict(_row(), id="r1", gym_id="lasso")]
    store = _Store(rows, {"lasso": [_c()]})
    out = gw.publish_due_gbp(store, _FakeClient(), run_date="2026-09-01",
                             draft=True, alert=lambda m: None,
                             now=__import__("datetime").datetime(
                                 2026, 9, 1, 9, 0, tzinfo=__import__("datetime").timezone.utc))
    assert out["published"] == 1
    bumps = getattr(store, "bumps", [])
    assert len(bumps) == 1
    gym, loc, month_iso, seed = bumps[0]
    assert gym == "lasso" and month_iso == "2026-09-01"      # first of the publish month
    assert seed, "top_post_id must be seeded with the published post's late_post_id"


def test_g3_post_clicks_extractor():
    assert gw._post_clicks({"insights": {"clicks": 12}}) == 12
    assert gw._post_clicks({"metrics": {"clicks": 3}}) == 3
    assert gw._post_clicks({"clicks": 7}) == 7
    assert gw._post_clicks({"status": "published"}) is None   # no click signal -> None
    assert gw._post_clicks("nope") is None


def test_publish_due_gbp_publishes_and_fails_correctly():
    rows = [
        dict(_row(), id="r1", gym_id="lasso"),
        dict(_row(caption="Bad - dash here makes this fail the rail at send time now."),
             id="r2", gym_id="lasso"),
    ]
    store = _Store(rows, {"lasso": [_c()]})
    c = _FakeClient()
    out = gw.publish_due_gbp(store, c, run_date="2026-09-01", draft=True,
                             alert=lambda m: None)
    assert out["published"] == 1 and out["failed"] == 1
    assert store.published and store.published[0][0] == "r1"
    assert store.failed and store.failed[0][0] == "r2"


def test_publish_due_gbp_holds_needs_reconnect():
    rows = [dict(_row(), id="r1", gym_id="eng")]
    store = _Store(rows, {"eng": [_c(status="needs_reconnect")]})
    out = gw.publish_due_gbp(store, _FakeClient(), run_date="2026-09-01", draft=True)
    assert out["held"] == 1 and out["published"] == 0 and out["failed"] == 0
    assert store.published == [] and store.failed == []   # silent hold


def test_reconcile_gbp_demotes_policy_rejection():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    rows = [dict(_row(), id="r1", gym_id="lasso", status="published",
                 late_post_id="zp1", published_at="2026-09-02T06:00:00+00:00")]
    store = _Store(rows, {})

    class _C:
        def get_post(self, pid):
            return {"post": {"platforms": [{"platform": "googlebusiness",
                                            "status": "failed",
                                            "error": "policy: phone number"}]}}
    out = gw.reconcile_gbp(store, _C(), now=now, alert=lambda m: None)
    assert out["demoted"] == 1
    assert store.failed and "phone" in store.failed[0][1]


def test_g7_transient_retry_republishes_and_keeps_published():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    rows = [dict(_row(), id="r1", gym_id="lasso", status="published", late_post_id="zpOld",
                 published_at="2026-09-02T06:00:00+00:00")]
    store = _Store(rows, {"lasso": [_c()]})   # a live connection so the retry can route

    class _C:
        def get_post(self, pid):
            return {"post": {"platforms": [{"platform": "googlebusiness",
                                            "status": "failed",
                                            "error": "temporarily unavailable, try again"}]}}
        # the re-send succeeds with a fresh id
        def create_post_raw(self, payload, **k):
            return {"_id": "zpNew", "post": {"platforms": [
                {"platform": "googlebusiness", "status": "published"}]}}
    out = gw.reconcile_gbp(store, _C(), now=now, alert=lambda m: None)
    assert out["retried"] == 1 and out["demoted"] == 0      # retry succeeded, no demote
    assert store.published and store.published[-1][0] == "r1"   # re-published
    assert store.failed == []


def test_g7_transient_retry_failure_demotes_to_failed():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    rows = [dict(_row(), id="r1", gym_id="lasso", status="published", late_post_id="zpOld",
                 published_at="2026-09-02T06:00:00+00:00")]
    store = _Store(rows, {"lasso": [_c()]})

    class _C:
        def get_post(self, pid):
            return {"post": {"platforms": [{"platform": "googlebusiness",
                                            "status": "failed",
                                            "error": "request timeout"}]}}
        def create_post_raw(self, payload, **k):
            raise RuntimeError("still failing")     # retry also fails
    out = gw.reconcile_gbp(store, _C(), now=now, alert=lambda m: None)
    assert out["retried"] == 1 and out["demoted"] == 1
    assert store.failed and store.failed[0][0] == "r1"       # one retry, then failed


def test_g3_reconcile_ranks_top_post_by_clicks():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    rows = [
        dict(_row(), id="r1", gym_id="lasso", status="published", late_post_id="zpA",
             gbp_location_id="locations/1", published_at="2026-09-02T06:00:00+00:00"),
        dict(_row(), id="r2", gym_id="lasso", status="published", late_post_id="zpB",
             gbp_location_id="locations/1", published_at="2026-09-02T06:30:00+00:00"),
    ]
    store = _Store(rows, {})

    class _C:
        def get_post(self, pid):
            clicks = {"zpA": 4, "zpB": 19}[pid]      # zpB is the top by clicks
            return {"post": {"platforms": [{"platform": "googlebusiness",
                                            "status": "published"}]},
                    "insights": {"clicks": clicks}}
    out = gw.reconcile_gbp(store, _C(), now=now, alert=lambda m: None)
    assert out["top_ranked"] == 1
    assert getattr(store, "top_sets", []) == [("m1", "zpB")]   # the max-clicks post wins


# ---- §6.4 photo drops ------------------------------------------------------

def test_photo_drop_draft_does_not_call_gmb_media():
    class _C:
        def __init__(self): self.media = 0
        def create_gmb_media(self, a, u): self.media += 1; return {"_id": "m1"}
        def create_post_raw(self, *a, **k): raise AssertionError("photo must not post")
    c = _C()
    row = dict(_row(caption="", image_url="https://r2/floor.jpg"), id="p1",
               gym_id="lasso", format="photo")
    out = gw.publish_one(row, [_c()], client=c, draft=True)
    assert out["status"] == "published"        # simulated in draft
    assert c.media == 0, "draft build must NOT upload a live gallery photo"


def test_photo_drop_live_calls_gmb_media():
    class _C:
        def __init__(self): self.media = []
        def create_gmb_media(self, a, u): self.media.append((a, u)); return {"_id": "m1"}
    c = _C()
    row = dict(_row(caption="", image_url="https://r2/floor.jpg"), id="p1",
               gym_id="lasso", format="photo")
    out = gw.publish_one(row, [_c()], client=c, draft=False)
    assert out["status"] == "published" and out["late_post_id"] == "m1"
    assert c.media == [("acc1", "https://r2/floor.jpg")]


def test_photo_drop_empty_caption_is_fine_not_a_rail_fail():
    # a photo row with empty caption must NOT be rejected as 'empty caption' (that was
    # the audit MAJOR: photo rows are gallery uploads, no caption gate)
    c = _FakeClient()
    row = dict(_row(caption="", image_url="https://r2/x.jpg"), id="p1", gym_id="lasso",
               format="photo")
    out = gw.publish_one(row, [_c()], client=c, draft=True)
    assert out["status"] == "published" and "empty caption" not in (out["reject_reason"] or "")
