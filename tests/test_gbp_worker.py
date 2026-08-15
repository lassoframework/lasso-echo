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
