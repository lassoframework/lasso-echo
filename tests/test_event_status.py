"""
event_status: the nightly date job (gym-tz flips), the publish gate, dead-link guard,
ended-blocks-publish, recap photo request. All offline (injected store + http + today).

Covers EVENT_CAMPAIGNS_BUILD.md §5:
  * status flips scheduled->live->ended in the GYM'S tz (test a Pacific gym vs UTC)
  * dead offer link at publish -> row flips back + alert
  * event ended -> nothing with that event_id publishes
  * recap photo request fires the morning after end; recap blocked until media exists
"""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import event_status as es
from agent import event_calendar as ec
from agent import gym_event as ge


def _event(**over):
    base = dict(
        id="evt_baf_x", gym_id="pete", name="Bring a Friend Week",
        type="bring_a_friend", starts_on="2026-09-22", ends_on="2026-09-28",
        tz="America/New_York", offer_text="Your partner trains free all week",
        link="https://petegym.com/baf", brief="Who are you bringing?",
        media_ids=(), status="scheduled",
    )
    base.update(over)
    return ge.GymEvent.from_row(base)


# ---- status_for: gym-tz lifecycle ----------------------------------------------

def test_status_before_window_scheduled():
    assert es.status_for(_event(), today=date(2026, 9, 1)) == "scheduled"


def test_status_in_window_live():
    assert es.status_for(_event(), today=date(2026, 9, 24)) == "live"


def test_status_after_window_ended():
    assert es.status_for(_event(), today=date(2026, 9, 29)) == "ended"


def test_status_boundaries_inclusive():
    ev = _event()
    assert es.status_for(ev, today=date(2026, 9, 22)) == "live"   # first day
    assert es.status_for(ev, today=date(2026, 9, 28)) == "live"   # last day


# ---- publish gate: ended/cancelled blocks --------------------------------------

def test_publish_allowed_while_live():
    assert es.publish_allowed(_event(), today=date(2026, 9, 24)) is True


def test_publish_blocked_after_ended():
    assert es.publish_allowed(_event(), today=date(2026, 9, 29)) is False


def test_publish_blocked_when_cancelled():
    assert es.publish_allowed(_event(status="cancelled"), today=date(2026, 9, 24)) is False


# ---- dead link guard ------------------------------------------------------------

class _HttpLive:
    def head(self, url, timeout=10, allow_redirects=True):
        return type("R", (), {"status_code": 200})()


class _HttpDead:
    def head(self, url, timeout=10, allow_redirects=True):
        return type("R", (), {"status_code": 404})()

    def get(self, url, timeout=10, allow_redirects=True):
        return type("R", (), {"status_code": 404})()


def test_verify_link_live():
    assert es.verify_link("https://x.com", http=_HttpLive()) is True


def test_verify_link_dead():
    assert es.verify_link("https://x.com/gone", http=_HttpDead()) is False


def test_verify_empty_link_ok():
    assert es.verify_link("", http=_HttpDead()) is True   # no link -> DM ask, fine


# ---- guard_publish: dead link flips back + alert -------------------------------

class _GuardStore:
    def __init__(self):
        self.reverted = []

    def mark_publish_failed(self, row_id, revert_status="pending", reject_reason=None):
        self.reverted.append((row_id, revert_status, reject_reason))
        return {"id": row_id, "status": revert_status, "reject_reason": reject_reason}


def test_guard_dead_link_reverts_row(monkeypatch):
    alerts = []
    import agent.ops_alerts as oa
    monkeypatch.setattr(oa, "alert", lambda *a, **k: alerts.append(a))
    ev = _event()
    row = {"id": "arc1", "arc_kind": ge.DURING, "event_id": ev.id}
    store = _GuardStore()
    allowed, reason = ec.guard_publish(store, ev, row, http=_HttpDead(),
                                       today=date(2026, 9, 24))
    assert allowed is False
    assert reason == ec.REJECT_DEAD_LINK
    assert store.reverted and store.reverted[0][2] == ec.REJECT_DEAD_LINK
    assert alerts   # alerted


def test_guard_ended_blocks_publish():
    ev = _event()
    row = {"id": "arc1", "arc_kind": ge.DURING, "event_id": ev.id}
    store = _GuardStore()
    allowed, reason = ec.guard_publish(store, ev, row, http=_HttpLive(),
                                       today=date(2026, 9, 29))  # after end
    assert allowed is False
    assert reason == ec.REJECT_ENDED
    assert store.reverted


def test_guard_live_ok_publishes():
    ev = _event()
    row = {"id": "arc1", "arc_kind": ge.DURING, "event_id": ev.id}
    store = _GuardStore()
    allowed, reason = ec.guard_publish(store, ev, row, http=_HttpLive(),
                                       today=date(2026, 9, 24))
    assert allowed is True and reason == ""
    assert not store.reverted


def test_guard_recap_blocked_without_media():
    ev = _event(media_ids=())
    row = {"id": "recap1", "arc_kind": ge.RECAP, "event_id": ev.id}
    store = _GuardStore()
    allowed, reason = ec.guard_publish(store, ev, row, http=_HttpLive(),
                                       today=date(2026, 9, 30))
    assert allowed is False and reason == "recap_blocked"


def test_guard_recap_ok_with_media_after_window():
    # recap on T+2 (Sep 30): the event has ended, but a RECAP with real media IS
    # allowed to publish after the window (it is the post-event proof).
    ev = _event(media_ids=("photo1",), status="ended", link="")
    row = {"id": "recap1", "arc_kind": ge.RECAP, "event_id": ev.id}
    store = _GuardStore()
    allowed, reason = ec.guard_publish(store, ev, row, http=_HttpLive(),
                                       today=date(2026, 9, 30))
    assert allowed is True and reason == ""
    assert not store.reverted


def test_guard_recap_blocked_when_cancelled():
    # a CANCELLED event blocks even a recap that has media.
    ev = _event(media_ids=("photo1",), status="cancelled")
    row = {"id": "recap1", "arc_kind": ge.RECAP, "event_id": ev.id}
    store = _GuardStore()
    allowed, reason = ec.guard_publish(store, ev, row, http=_HttpLive(),
                                       today=date(2026, 9, 30))
    assert allowed is False and reason == ec.REJECT_CANCELLED


# ---- nightly job: gym-tz flips + ended sweep -----------------------------------

class _EventStore:
    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.status_sets = []

    def list_active(self):
        return [dict(r) for r in self.rows.values()
                if r.get("status") in ("draft", "scheduled", "live")]

    def set_status(self, gym_id, event_id, new_status):
        r = self.rows.get(event_id)
        if r and r.get("gym_id") == gym_id:
            r["status"] = new_status
            self.status_sets.append((event_id, new_status))
            return r
        return None


class _CalStore:
    def __init__(self, arc_rows):
        self.rows = {r["id"]: dict(r) for r in arc_rows}
        self.denied = []

    def list_event_rows(self, gym_id, event_id):
        return [dict(r) for r in self.rows.values()
                if r.get("gym_id") == gym_id and r.get("event_id") == event_id]

    def deny_with_reason(self, gym_id, row_id, reason):
        r = self.rows.get(row_id)
        if r and r.get("gym_id") == gym_id:
            r["status"] = "denied"
            r["reject_reason"] = reason
            self.denied.append((row_id, reason))
            return r
        return None


def test_status_job_flips_ended_and_sweeps(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    import agent.ops_alerts as oa
    monkeypatch.setattr(oa, "alert", lambda *a, **k: None)
    ev_row = dict(id="evt_baf_x", gym_id="pete", name="Bring a Friend Week",
                  type="bring_a_friend", starts_on="2026-09-22", ends_on="2026-09-28",
                  tz="America/New_York", offer_text="free week", link="",
                  brief="", media_ids=[], status="live")
    estore = _EventStore([ev_row])
    arc = [{"id": "a1", "gym_id": "pete", "event_id": "evt_baf_x", "status": "pending"},
           {"id": "a2", "gym_id": "pete", "event_id": "evt_baf_x", "status": "approved"}]
    cstore = _CalStore(arc)
    res = es.run_status_job(estore, cstore, today=date(2026, 9, 29))
    assert res["ok"]
    assert estore.rows["evt_baf_x"]["status"] == "ended"
    assert res["ended"] == 1
    # pending arc row denied event_ended; approved left.
    assert cstore.rows["a1"]["status"] == "denied"
    assert cstore.rows["a1"]["reject_reason"] == ec.REJECT_ENDED
    assert cstore.rows["a2"]["status"] == "approved"


def test_status_job_skips_unarmed_gym(monkeypatch):
    monkeypatch.delenv("AGENT_EVENT_CAMPAIGNS", raising=False)
    monkeypatch.delenv("AGENT_EVENT_CAMPAIGNS_PETE", raising=False)
    ev_row = dict(id="e", gym_id="pete", name="X", type="party",
                  starts_on="2026-09-22", ends_on="2026-09-28",
                  tz="America/New_York", media_ids=[], status="live")
    estore = _EventStore([ev_row])
    cstore = _CalStore([])
    res = es.run_status_job(estore, cstore, today=date(2026, 9, 29))
    # gym not armed -> nothing flipped.
    assert res["flipped"] == 0
    assert estore.rows["e"]["status"] == "live"


def test_pacific_gym_flips_on_its_own_day():
    # A Pacific event ending Sep 28. At UTC midnight of Sep 29, it is still Sep 28
    # in LA (not ended). status_for in the gym tz respects that: today=Sep 28 -> live.
    ev = _event(tz="America/Los_Angeles")
    assert es.status_for(ev, today=date(2026, 9, 28)) == "live"
    assert es.status_for(ev, today=date(2026, 9, 29)) == "ended"


# ---- recap photo request -------------------------------------------------------

def test_recap_photo_request_when_no_media(monkeypatch):
    posted = []
    import agent.ops_alerts as oa
    monkeypatch.setattr(oa, "alert", lambda msg, *a, **k: posted.append(msg))
    ev = _event(media_ids=())
    msg = es.recap_photo_request(ev)
    assert msg and "photos" in msg.lower()
    assert posted


def test_no_recap_request_when_media_exists(monkeypatch):
    posted = []
    import agent.ops_alerts as oa
    monkeypatch.setattr(oa, "alert", lambda msg, *a, **k: posted.append(msg))
    ev = _event(media_ids=("p1",))
    assert es.recap_photo_request(ev) is None
    assert not posted
