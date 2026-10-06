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
        self.event_row_reads = 0
        self.before_insert = None
        self.after_insert = None
        self.preserve_id_calls = []

    def list_month(self, gym_id, month):
        return [r for r in self.existing
                if r.get("gym_id") == gym_id and str(r.get("post_date"))[:7] == month]

    def insert_rows(self, gym_id, rows, *, preserve_ids=False,
                    return_write_receipt=False):
        self.preserve_id_calls.append(preserve_ids)
        hook = self.before_insert
        self.before_insert = None
        if hook is not None:
            hook()
        out = []
        for r in rows:
            rr = dict(r)
            rr["gym_id"] = gym_id
            rr.setdefault("id", f"calendar-{len(self.inserted) + 1}")
            self.inserted.append(rr)
            out.append(dict(rr))
        hook = self.after_insert
        self.after_insert = None
        if hook is not None:
            hook()
        if return_write_receipt:
            return {"inserted_rows": out,
                    "expected_rows": [dict(row) for row in out]}
        return out

    def list_event_rows(self, gym_id, event_id):
        self.event_row_reads += 1
        return [dict(r) for r in self.inserted
                if r.get("gym_id") == gym_id and r.get("event_id") == event_id]

    def deny_with_reason(self, gym_id, row_id, reason):
        for r in self.inserted:
            if r.get("id") == row_id and r.get("gym_id") == gym_id:
                r["status"] = "denied"
                r["reject_reason"] = reason
                return r
        return None

    def deny_wipeable_with_reason(self, gym_id, row_id, reason):
        for r in self.inserted:
            if (r.get("id") == row_id and r.get("gym_id") == gym_id
                    and r.get("status") in ("pending", "draft", "queued")
                    and r.get("media_not_ready_reason") is None):
                r["status"] = "denied"
                r["reject_reason"] = reason
                return r
        return None

    def deny_event_wipeable_with_reason(self, gym_id, event_id, row_id, reason):
        for r in self.inserted:
            if (r.get("id") == row_id and r.get("gym_id") == gym_id
                    and r.get("event_id") == event_id
                    and r.get("status") in ("pending", "draft", "queued")):
                r["status"] = "denied"
                r["reject_reason"] = reason
                return r
        return None

    def list_rows_by_ids(self, gym_id, row_ids):
        wanted = set(row_ids)
        return [dict(row) for row in self.inserted
                if row.get("gym_id") == gym_id and row.get("id") in wanted]


class _EvStore:
    def __init__(self):
        self.rows = {}
        self.upsert_calls = 0
        self.conditional_update_calls = 0
        self.before_conditional_update = None

    def upsert_event(self, row):
        self.upsert_calls += 1
        self.rows[row["id"]] = dict(row)
        return self.rows[row["id"]]

    def get_event(self, gym_id, event_id):
        r = self.rows.get(event_id)
        return dict(r) if r and r.get("gym_id") == gym_id else None

    def update_event_if_status(self, gym_id, event_id, expected_status,
                               expected_row, row):
        self.conditional_update_calls += 1
        if self.before_conditional_update is not None:
            self.before_conditional_update(self, gym_id, event_id)
        current = self.rows.get(event_id)
        if (not current or current.get("gym_id") != gym_id
                or current.get("status") != expected_status
                or current != expected_row):
            return None
        self.rows[event_id] = dict(row)
        return dict(self.rows[event_id])

    def list_events(self, gym_id, statuses=None):
        return [dict(r) for r in self.rows.values() if r.get("gym_id") == gym_id]

    def set_status(self, gym_id, event_id, new_status):
        r = self.rows.get(event_id)
        if r and r.get("gym_id") == gym_id:
            r["status"] = new_status
            return r
        return None


@pytest.fixture(autouse=True)
def _fake_media(monkeypatch):
    """Event rows now REQUIRE a real photo (an image-less feed post cannot publish).
    Give these offline tests a pool so arcs stage; the held-when-no-photo behavior has
    its own test in test_event_calendar.py."""
    monkeypatch.setattr(
        "agent.event_calendar._attach_media",
        lambda gym_id, rows, log, picker=None, host=None: (
            [dict(r, image_url=r.get("image_url") or "https://cdn.test/e.jpg",
                  source_media_asset_id=r.get("source_media_asset_id") or "m1")
             for r in rows], []))
    yield


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


@pytest.mark.parametrize(("stored_status", "case_id"), [
    ("cancelled", "cancelled"),
    ("ended", "ended"),
    (" LIVE ", "whitespace"),
    ("Scheduled", "case-variant"),
    ("archived", "unknown"),
    ("", "empty"),
    (None, "null"),
    ({"value": "live"}, "object"),
])
def test_edit_refuses_noncanonical_status_before_calendar_read_or_write(
        monkeypatch, stored_status, case_id):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    original = {
        "id": f"e-{case_id}", "gym_id": "pete",
        "name": "Fall Cohort", "type": "new_offer",
        "starts_on": "2026-10-01", "ends_on": "2026-10-14",
        "tz": "America/New_York", "offer_text": "Reserve a place",
        "link": "", "brief": "", "media_ids": [],
        "status": stored_status, "created_by": "owner",
        "audit": [{"action": "seed"}],
    }
    ev.upsert_event(original)
    writes_before = ev.upsert_calls

    status, resp = pe.handle_edit_event(
        "pete", original["id"],
        {"starts_on": "2026-11-01", "ends_on": "2026-11-14",
         "actor_id": "stale-browser"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    assert status == 409
    assert resp == {"error": "this promotion can no longer be edited"}
    assert ev.rows[original["id"]] == original
    assert ev.upsert_calls == writes_before, "terminal edit must not persist a merged event"
    assert cal.event_row_reads == 0, "terminal edit must stop before arc inspection/restaging"
    assert cal.inserted == []


@pytest.mark.parametrize("editable_status", ["draft", "scheduled", "live"])
def test_edit_preserves_supported_active_statuses(monkeypatch, editable_status):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    event = {
        "id": f"e-{editable_status}", "gym_id": "pete",
        "name": "Fall Cohort", "type": "new_offer",
        "starts_on": "2026-10-01", "ends_on": "2026-10-14",
        "tz": "America/New_York", "offer_text": "Reserve a place",
        # Keep this status-only regression independent of the separate rule that an
        # edit must not advance when its new recap is intentionally held for no media.
        "link": "", "brief": "", "media_ids": ["m1"],
        "status": editable_status, "created_by": "owner", "audit": [],
    }
    ev.upsert_event(event)

    status, resp = pe.handle_edit_event(
        "pete", event["id"],
        {"starts_on": "2026-10-03", "ends_on": "2026-10-16",
         "actor_id": "owner"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    assert status == 200
    assert resp["event"]["status"] == editable_status
    assert ev.rows[event["id"]]["status"] == editable_status
    assert ev.rows[event["id"]]["starts_on"] == "2026-10-03"
    assert ev.rows[event["id"]]["ends_on"] == "2026-10-16"
    assert any(a["action"] == "edit" for a in ev.rows[event["id"]]["audit"])


@pytest.mark.parametrize("terminal_status", ["cancelled", "ended"])
def test_concurrent_terminal_transition_wins_edit_compare_and_set(
        monkeypatch, terminal_status):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    before = dict(ev.rows[event_id])
    staged_before = [dict(row) for row in cal.inserted]

    def _terminal_transition(store, gym_id, eid):
        assert gym_id == "pete" and eid == event_id
        store.rows[eid]["status"] = terminal_status

    ev.before_conditional_update = _terminal_transition
    status, resp = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "stale-editor"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    assert status == 409
    assert resp == {"error": "this promotion can no longer be edited"}
    assert ev.conditional_update_calls == 1
    assert ev.rows[event_id]["status"] == terminal_status
    assert ev.rows[event_id]["starts_on"] == before["starts_on"]
    assert ev.rows[event_id]["ends_on"] == before["ends_on"]
    assert ev.rows[event_id]["audit"] == before["audit"]
    assert cal.inserted == staged_before, "failed CAS must not restage calendar rows"


def test_cancel_after_event_cas_before_calendar_insert_is_compensated(monkeypatch):
    """A cancellation in the exact post-CAS/pre-insert gap leaves no active rows."""
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))

    def _cancel_in_gap():
        status, response = pe.handle_cancel_event(
            "pete", event_id, {"actor_id": "owner"}, store=cal,
            event_store=ev)
        assert status == 200
        assert response["cancelled"] is True

    cal.before_insert = _cancel_in_gap
    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "stale-editor"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    assert status == 409
    assert response["compensated"] > 0
    assert ev.rows[event_id]["status"] == "cancelled"
    active = [row for row in cal.inserted
              if row.get("event_id") == event_id
              and row.get("status") in ("pending", "draft", "queued")]
    assert active == []


def test_scheduled_event_becoming_live_after_stage_keeps_valid_edit(monkeypatch):
    """A lifecycle tick must not undo an otherwise current, editable revision."""
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    ev.rows[event_id]["status"] = "scheduled"

    def _go_live_after_insert():
        ev.rows[event_id]["status"] = "live"

    cal.after_insert = _go_live_after_insert
    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "owner"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    assert status == 200
    assert response["event"]["status"] == "live"
    assert ev.rows[event_id]["starts_on"] == "2026-10-20"
    assert any(row.get("event_id") == event_id and row["status"] == "pending"
               for row in cal.inserted)


def test_later_same_status_edit_owns_rows_earlier_edit_cannot_compensate(monkeypatch):
    """A winner must insert its own rows rather than adopt a loser's receipts."""
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    initial_ids = {row["id"] for row in cal.inserted}
    interleaving = {}

    def _winning_edit_after_loser_insert():
        loser_ids = {row["id"] for row in cal.inserted} - initial_ids
        interleaving["loser_ids"] = loser_ids
        status, response = pe.handle_edit_event(
            "pete", event_id,
            {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
             "offer_text": "Winner owns this revision", "actor_id": "winner"},
            store=cal, event_store=ev, today=date(2026, 9, 1))
        interleaving["status"] = status
        interleaving["response"] = response
        interleaving["winner_ids"] = (
            {row["id"] for row in cal.inserted} - initial_ids - loser_ids)

    cal.after_insert = _winning_edit_after_loser_insert
    losing_status, losing_response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "loser"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    assert interleaving["status"] == 200
    assert interleaving["response"]["restaged"] > 0
    assert losing_status == 409
    assert losing_response["compensated"] in (0, len(interleaving["loser_ids"]))
    assert interleaving["loser_ids"]
    assert interleaving["winner_ids"]
    assert cal.preserve_id_calls[-2:] == [True, True]
    assert interleaving["loser_ids"].isdisjoint(interleaving["winner_ids"])
    rows = {row["id"]: row for row in cal.inserted}
    assert all(rows[row_id]["status"] == "denied"
               for row_id in interleaving["loser_ids"])
    assert all(rows[row_id]["status"] == "pending"
               for row_id in interleaving["winner_ids"])
    assert ev.rows[event_id]["offer_text"] == "Winner owns this revision"


def test_edit_calendar_event_read_failure_fails_before_event_write(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    before = dict(ev.rows[event_id])
    cal.list_event_rows = lambda *_args: (_ for _ in ()).throw(
        RuntimeError("calendar unavailable"))

    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "owner"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    assert status == 502
    assert response["error"] == "calendar event read failed: RuntimeError"
    assert ev.rows[event_id] == before
    assert ev.conditional_update_calls == 0


def test_edit_occupancy_read_failure_preserves_protected_row_and_event(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    before = dict(ev.rows[event_id])
    protected = dict(cal.inserted[0], status="approved")
    cal.inserted[0] = protected
    inserted_before = [dict(row) for row in cal.inserted]
    cal.list_month = lambda *_args: (_ for _ in ()).throw(
        RuntimeError("occupancy unavailable"))

    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "owner"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    assert status == 502
    assert response["error"] == "calendar occupancy read failed: RuntimeError"
    assert ev.rows[event_id] == before
    assert ev.conditional_update_calls == 0
    assert cal.inserted == inserted_before
    assert cal.inserted[0]["status"] == "approved"


def test_edit_stage_failure_rolls_back_event_and_audit(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    before = dict(ev.rows[event_id])
    cal.insert_rows = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("insert unavailable"))

    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "owner"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    assert status == 502
    assert response["error"] == "calendar staging failed"
    assert response["rolled_back"] is True
    assert ev.rows[event_id] == before
    assert ev.conditional_update_calls == 2


def test_edit_commit_then_timeout_compensates_exact_ids_before_rollback(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    before = dict(ev.rows[event_id])
    initial_ids = {row["id"] for row in cal.inserted}
    real_insert = cal.insert_rows

    def _commit_then_timeout(*args, **kwargs):
        real_insert(*args, **kwargs)
        raise TimeoutError("response lost after commit")

    cal.insert_rows = _commit_then_timeout
    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "owner"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    attempted = [row for row in cal.inserted if row["id"] not in initial_ids]
    assert attempted
    assert status == 502
    assert response["error"] == "calendar staging failed"
    assert response["compensated"] == len(attempted)
    assert response.get("active", 0) == 0
    assert response["rolled_back"] is True
    assert ev.rows[event_id] == before
    assert all(row["status"] == "denied" for row in attempted)
    assert all(row["reject_reason"] == ec.REJECT_EDIT_CONFLICT for row in attempted)


def test_edit_ambiguous_insert_preserves_concurrent_approval_and_skips_rollback(
        monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    initial_ids = {row["id"] for row in cal.inserted}
    real_insert = cal.insert_rows

    def _commit_approve_then_timeout(*args, **kwargs):
        real_insert(*args, **kwargs)
        for row in cal.inserted:
            if row["id"] not in initial_ids:
                row["status"] = "approved"
        raise TimeoutError("response lost after approval")

    cal.insert_rows = _commit_approve_then_timeout
    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "owner"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    attempted = [row for row in cal.inserted if row["id"] not in initial_ids]
    assert status == 502
    assert response["error"] == "calendar staging and cleanup failed"
    assert response["active"] == len(attempted)
    assert response["rolled_back"] is False
    assert ev.rows[event_id]["starts_on"] == "2026-10-20"
    assert all(row["status"] == "approved" for row in attempted)


def test_date_edit_retires_old_pending_rows_but_preserves_protected(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    old_ids = {row["id"] for row in cal.inserted}
    protected_id = next(iter(old_ids))
    next(row for row in cal.inserted if row["id"] == protected_id)["status"] = "approved"

    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "owner"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    assert status == 200
    old_rows = [row for row in cal.inserted if row["id"] in old_ids]
    new_rows = [row for row in cal.inserted if row["id"] not in old_ids]
    assert next(row for row in old_rows if row["id"] == protected_id)["status"] == "approved"
    assert all(row["status"] == "denied" for row in old_rows
               if row["id"] != protected_id)
    assert all(row.get("reject_reason") == ec.REJECT_EDIT_SUPERSEDED
               for row in old_rows if row["id"] != protected_id)
    assert new_rows and all(row["status"] == "pending" for row in new_rows)
    assert response["removed"] == len(old_rows) - 1


def test_date_edit_accepts_all_replacements_intentionally_media_held(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    old_ids = {row["id"] for row in cal.inserted}
    monkeypatch.setattr(
        "agent.event_calendar._attach_media",
        lambda gym_id, rows, log, picker=None, host=None: ([], list(rows)))

    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "owner"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    assert status == 200
    assert response["restaged"] == 0
    assert response["held_media"] > 0
    assert ev.rows[event_id]["starts_on"] == "2026-10-20"
    assert {row["id"] for row in cal.inserted} == old_ids
    assert all(row["status"] == "denied" for row in cal.inserted)


def test_date_edit_accepts_intentionally_held_new_recap_and_preserves_old_approval(
        monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    old_ids = {row["id"] for row in cal.inserted}
    old_recap = cal.inserted[-1]
    old_recap["status"] = "approved"

    def _hold_new_recap(gym_id, rows, log, picker=None, host=None):
        return ([dict(row, image_url="https://cdn.test/e.jpg",
                      source_media_asset_id="m1") for row in rows[:-1]],
                [rows[-1]])

    monkeypatch.setattr("agent.event_calendar._attach_media", _hold_new_recap)
    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "owner"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    assert status == 200
    assert response["held_media"] == 1
    assert ev.rows[event_id]["starts_on"] == "2026-10-20"
    assert old_recap["status"] == "approved"
    old_rows = [row for row in cal.inserted if row["id"] in old_ids]
    assert all(row["status"] == "denied" for row in old_rows
               if row["id"] != old_recap["id"])
    replacements = [row for row in cal.inserted if row["id"] not in old_ids]
    assert replacements and all(row["status"] == "pending" for row in replacements)


@pytest.mark.parametrize("held_index", [0, 3])
def test_date_edit_accepts_intentionally_media_held_replacement(
        monkeypatch, held_index):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    old_ids = {row["id"] for row in cal.inserted}
    protected = cal.inserted[-1]
    protected["status"] = "approved"

    def _hold_one(gym_id, rows, log, picker=None, host=None):
        held = rows[held_index]
        kept = [dict(row, image_url="https://cdn.test/e.jpg",
                     source_media_asset_id="m1")
                for index, row in enumerate(rows) if index != held_index]
        return kept, [held]

    monkeypatch.setattr("agent.event_calendar._attach_media", _hold_one)
    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "owner"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    assert status == 200
    assert response["held_media"] == 1
    assert ev.rows[event_id]["starts_on"] == "2026-10-20"
    assert protected["status"] == "approved"
    replacements = [row for row in cal.inserted if row["id"] not in old_ids]
    assert replacements and len(replacements) == response["restaged"]
    assert all(row["status"] == "pending" for row in replacements)


def test_date_edit_accepts_legitimate_offer_ceiling_thinning(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    old_ids = {row["id"] for row in cal.inserted}
    real_thin = ec.overlap_thin

    def _thin_one(existing, rows):
        thinned = real_thin(existing, rows)
        return thinned[:-1]

    monkeypatch.setattr(ec, "overlap_thin", _thin_one)
    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "owner"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    replacements = [row for row in cal.inserted if row["id"] not in old_ids]
    assert status == 200
    assert response["restaged"] == len(replacements)
    assert replacements
    assert ev.rows[event_id]["starts_on"] == "2026-10-20"


def test_date_edit_accepts_legitimate_recap_hold(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))

    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "media_ids": [], "actor_id": "owner"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    assert status == 200
    assert response["restaged"] > 0
    assert ev.rows[event_id]["media_ids"] == []


def test_date_edit_partial_durable_insert_rolls_back_atomically(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    event_before = dict(ev.rows[event_id])
    old_ids = {row["id"] for row in cal.inserted}
    real_insert = cal.insert_rows

    def _commit_first_only(gym_id, rows, *, preserve_ids=False,
                           return_write_receipt=False):
        assert len(rows) > 1
        result = real_insert(gym_id, rows[:1], preserve_ids=preserve_ids,
                             return_write_receipt=return_write_receipt)
        if return_write_receipt:
            result["expected_rows"] = [dict(row, gym_id=gym_id) for row in rows]
        return result

    cal.insert_rows = _commit_first_only
    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "owner"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    inserted = [row for row in cal.inserted if row["id"] not in old_ids]
    assert status == 502
    assert response["reason"] == "replacement coverage incomplete"
    assert response["rolled_back"] is True
    assert ev.rows[event_id] == event_before
    assert len(inserted) == 1
    assert inserted[0]["status"] == "denied"
    assert all(row["status"] == "pending" for row in cal.inserted
               if row["id"] in old_ids)


def test_date_edit_count_only_insert_receipt_reconciles_exact_ids(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    event_before = dict(ev.rows[event_id])
    old_ids = {row["id"] for row in cal.inserted}
    real_insert = cal.insert_rows

    def _commit_with_count_only(gym_id, rows, *, preserve_ids=False,
                                return_write_receipt=False):
        written = real_insert(gym_id, rows, preserve_ids=preserve_ids)
        return len(written)

    cal.insert_rows = _commit_with_count_only
    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "owner"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    inserted = [row for row in cal.inserted if row["id"] not in old_ids]
    assert status == 502
    assert response["reason"] == "insert failed ValueError"
    assert response["rolled_back"] is True
    assert ev.rows[event_id] == event_before
    assert inserted and all(row["status"] == "denied" for row in inserted)


def test_superseded_cleanup_preserves_concurrent_pending_media_hold(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    held = cal.inserted[0]
    held["media_not_ready_reason"] = "manual_visual_review"

    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "owner"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    assert status == 200
    assert held["status"] == "pending"
    assert held["media_not_ready_reason"] == "manual_visual_review"
    assert held.get("reject_reason") is None
    assert response["removed"] == len([
        row for row in cal.inserted
        if row.get("reject_reason") == ec.REJECT_EDIT_SUPERSEDED])


def test_losing_edit_compensation_failure_returns_retryable_502(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event(
        "pete", _form(media_ids=["m1"]), store=cal, event_store=ev,
        today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    initial_ids = {row["id"] for row in cal.inserted}
    interleaving = {}

    def _winning_edit_then_break_cleanup():
        interleaving["loser_ids"] = {
            row["id"] for row in cal.inserted} - initial_ids
        status, _ = pe.handle_edit_event(
            "pete", event_id,
            {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
             "offer_text": "Winner owns this revision", "actor_id": "winner"},
            store=cal, event_store=ev, today=date(2026, 9, 1))
        assert status == 200
        cal.deny_wipeable_with_reason = lambda *_args: (_ for _ in ()).throw(
            RuntimeError("cleanup unavailable"))

    cal.after_insert = _winning_edit_then_break_cleanup
    status, response = pe.handle_edit_event(
        "pete", event_id,
        {"starts_on": "2026-10-20", "ends_on": "2026-10-27",
         "actor_id": "loser"},
        store=cal, event_store=ev, today=date(2026, 9, 1))

    assert status == 502
    assert response["error"] == "event edit cleanup failed"
    assert response["compensated"] == 0
    assert interleaving["loser_ids"]
    rows = {row["id"]: row for row in cal.inserted}
    assert all(rows[row_id]["status"] == "denied"
               for row_id in interleaving["loser_ids"])


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


def test_cancel_calendar_read_failure_is_not_reported_as_success(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_CAMPAIGNS_PETE", "true")
    cal, ev = _CalStore(), _EvStore()
    pe.handle_create_event("pete", _form(media_ids=["m1"]),
                           store=cal, event_store=ev, today=date(2026, 9, 1))
    event_id = next(iter(ev.rows))
    cal.list_event_rows = lambda *_args: (_ for _ in ()).throw(
        RuntimeError("calendar unavailable"))

    status, response = pe.handle_cancel_event(
        "pete", event_id, {"actor_id": "owner"}, store=cal, event_store=ev)

    assert status == 502
    assert response["cancelled"] is True
    assert response["error"] == "event cancelled but calendar sweep failed"
    assert ev.rows[event_id]["status"] == "cancelled"


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
