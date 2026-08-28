"""
portal_events: the Echo API for self-serve Events & Promos (EVENT_CAMPAIGNS_BUILD.md §6).

Offline: injected fake calendar + event stores; no network, no Supabase. Covers:
  * flag OFF -> 404 for every gym (indistinguishable from an unknown route)
  * create -> gym_event persisted (gym_id forced) + arc drafted PENDING + preview
  * on-behalf create is logged in the event audit
  * bad form -> 400
  * list is gym-scoped
  * edit re-times; cancel denies pending; recur clones with blank dates
  * tenant isolation: a create can never write another gym's id
"""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_events as pe
from agent import gym_event as ge
from agent import event_calendar as ec


class _CalStore:
    def __init__(self, existing=None):
        self.existing = existing or []
        self.inserted = []

    def list_month(self, gym_id, month):
        return [r for r in self.existing
                if r.get("gym_id") == gym_id and str(r.get("post_date"))[:7] == month]

    def insert_rows(self, gym_id, rows):
        out = []
        for r in rows:
            rr = dict(r)
            rr["gym_id"] = gym_id
            self.inserted.append(rr)
            out.append(rr)
        return out

    def list_event_rows(self, gym_id, event_id):
        return [dict(r) for r in self.inserted
                if r.get("gym_id") == gym_id and r.get("event_id") == event_id]

    def deny_with_reason(self, gym_id, row_id, reason):
        for r in self.inserted:
            if r.get("id") == row_id and r.get("gym_id") == gym_id:
                r["status"] = "denied"
                r["reject_reason"] = reason
                return r
        return None


class _EvStore:
    def __init__(self):
        self.rows = {}

    def upsert_event(self, row):
        self.rows[row["id"]] = dict(row)
        return self.rows[row["id"]]

    def get_event(self, gym_id, event_id):
        r = self.rows.get(event_id)
        return dict(r) if r and r.get("gym_id") == gym_id else None

    def list_events(self, gym_id, statuses=None):
        return [dict(r) for r in self.rows.values() if r.get("gym_id") == gym_id]

    def set_status(self, gym_id, event_id, new_status):
        r = self.rows.get(event_id)
        if r and r.get("gym_id") == gym_id:
            r["status"] = new_status
            return r
        return None


def _form(**over):
    base = dict(name="Bring a Friend Week", type="bring_a_friend",
                starts_on="2026-09-22", ends_on="2026-09-28",
                tz="America/New_York",
                offer_text="Your partner trains free all week",
                link="", brief="Who are you bringing?", media_ids=[])
    base.update(over)
    return base


# ---- flag gating ---------------------------------------------------------------

def test_create_404_when_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_EVENT_CAMPAIGNS", raising=False)
    monkeypatch.delenv("AGENT_EVENT_CAMPAIGNS_PETE", raising=False)
    status, resp = pe.handle_create_event("pete", _form(),
                                          store=_CalStore(), event_store=_EvStore())
    assert status == 404


def test_list_404_when_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_EVENT_CAMPAIGNS", raising=False)
    monkeypatch.delenv("AGENT_EVENT_CAMPAIGNS_PETE", raising=False)
    status, resp = pe.handle_list_events("pete", event_store=_EvStore())
    assert status == 404


# ---- create --------------------------------------------------------------------

def test_create_drafts_arc_and_persists_event(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    status, resp = pe.handle_create_event("pete", _form(media_ids=["m1"]),
                                          store=cal, event_store=ev,
                                          today=date(2026, 9, 1))
    assert status == 201
    # gym_event persisted, gym_id forced to the token's gym.
    saved = list(ev.rows.values())[0]
    assert saved["gym_id"] == "pete"
    # a labeled arc preview came back.
    assert resp["arc"] and resp["label"].startswith("Bring a Friend Week")
    # every staged row is pending.
    assert cal.inserted and all(r["status"] == "pending" for r in cal.inserted)
    # a one-tap story studio offer rides along.
    assert resp["story_studio"]["event_id"] == saved["id"]


def test_create_forces_gym_id_isolation(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    # even if the form tried to smuggle a gym_id, the handler forces account_key.
    form = _form()
    form["gym_id"] = "someone_else"
    status, resp = pe.handle_create_event("pete", form, store=cal, event_store=ev,
                                          today=date(2026, 9, 1))
    assert status == 201
    assert list(ev.rows.values())[0]["gym_id"] == "pete"
    assert all(r["gym_id"] == "pete" for r in cal.inserted)


def test_on_behalf_is_logged(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    status, resp = pe.handle_create_event(
        "pete", _form(actor_id="coach_dave", on_behalf=True),
        store=cal, event_store=ev, today=date(2026, 9, 1))
    assert status == 201
    saved = list(ev.rows.values())[0]
    audit = saved["audit"]
    assert audit and audit[0]["on_behalf"] is True
    assert audit[0]["actor"] == "coach_dave"


def test_bad_form_400(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    status, resp = pe.handle_create_event(
        "pete", _form(type="not_a_type"), store=_CalStore(), event_store=_EvStore())
    assert status == 400


def test_missing_name_400(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    status, resp = pe.handle_create_event(
        "pete", _form(name=""), store=_CalStore(), event_store=_EvStore())
    assert status == 400


# ---- list ----------------------------------------------------------------------

def test_list_gym_scoped(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS", "true")
    ev = _EvStore()
    ev.upsert_event({"id": "e1", "gym_id": "pete", "name": "A", "type": "party",
                     "starts_on": "2026-09-01", "ends_on": "2026-09-01",
                     "tz": "America/New_York", "status": "scheduled"})
    ev.upsert_event({"id": "e2", "gym_id": "other", "name": "B", "type": "party",
                     "starts_on": "2026-09-01", "ends_on": "2026-09-01",
                     "tz": "America/New_York", "status": "scheduled"})
    status, resp = pe.handle_list_events("pete", event_store=ev)
    assert status == 200
    assert [e["id"] for e in resp["events"]] == ["e1"]


# ---- edit ----------------------------------------------------------------------

def test_edit_retimes_arc(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event("pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
                           today=date(2026, 9, 1))
    eid = list(ev.rows.keys())[0]
    status, resp = pe.handle_edit_event(
        "pete", eid, {"starts_on": "2026-09-29", "ends_on": "2026-10-05"},
        store=cal, event_store=ev, today=date(2026, 9, 1))
    assert status == 200
    # the event window moved.
    assert ev.rows[eid]["starts_on"] == "2026-09-29"


def test_edit_404_for_missing_or_cross_gym(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    status, resp = pe.handle_edit_event("pete", "nope", {"link": "x"},
                                        store=_CalStore(), event_store=_EvStore())
    assert status == 404


# ---- cancel --------------------------------------------------------------------

def test_cancel_denies_pending_arc(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    st, resp = pe.handle_create_event("pete", _form(media_ids=["m1"]),
                                      store=cal, event_store=ev, today=date(2026, 9, 1))
    eid = list(ev.rows.keys())[0]
    # give the inserted arc rows ids (the fake insert didn't).
    for i, r in enumerate(cal.inserted):
        r["id"] = f"arc{i}"
    st2, resp2 = pe.handle_cancel_event("pete", eid, {"actor_id": "owner"},
                                        store=cal, event_store=ev)
    assert st2 == 200
    assert ev.rows[eid]["status"] == "cancelled"
    # audit rowed.
    assert any(a["action"] == "cancel" for a in ev.rows[eid]["audit"])


# ---- recur ---------------------------------------------------------------------

def test_recur_clones_with_blank_dates(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    ev = _EvStore()
    ev.upsert_event({"id": "e1", "gym_id": "pete", "name": "Bring a Friend Week",
                     "type": "bring_a_friend", "starts_on": "2026-09-22",
                     "ends_on": "2026-09-28", "tz": "America/New_York",
                     "offer_text": "free week", "link": "", "brief": "",
                     "media_ids": [], "status": "ended"})
    status, resp = pe.handle_recur_event("pete", "e1", event_store=ev)
    assert status == 200
    assert resp["form"]["name"] == "Bring a Friend Week"
    assert resp["form"]["starts_on"] == "" and resp["form"]["ends_on"] == ""
    assert resp["form"]["type"] == "bring_a_friend"
