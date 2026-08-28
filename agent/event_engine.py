"""
event_engine.py — the ONE window-timed campaign engine, gym-parameterized.

CORE PRINCIPLE (EVENT_CAMPAIGNS_BUILD.md): do NOT build a second scheduler. The
window-timed engine already exists and runs Summit. This module is the single seam
that plans a dated ARC for ANY gym_event, and expresses LASSO Summit as just another
gym_event on gym_id='lasso' so there is ONE engine for both.

  plan_event_arc(event, *, today, avatar, brief) -> arc row dicts
      the generalized entry: gym_event.plan_arc (the window+lead-time arc) then
      gym_event.draft_arc (grounded copy, gates, avatar). Works for a client gym's
      Bring-a-Friend Week and for LASSO Summit identically.

  summit_as_event() -> GymEvent
      LASSO Summit as a gym_event row on 'lasso'. Its window is the real summit
      dates (SUMMIT_DATES / the sprint cycles), tz is LASSO's posting tz, facts are
      the VERIFIED summit facts (November 7 and 8, Virgin Hotel Nashville, the
      registration URL). This is the proof that Summit is expressible as a gym_event.

  summit_still_plans() -> bool
      a regression guard: the engine plans a non-empty Summit arc for the Summit
      gym_event. Called by the Summit-still-green test.

The DETAILED Summit sprint machinery (summit_queue.SPRINT_CYCLES, sprint_assets,
sprint_calendar, real_month_planner's sprint layout) is UNTOUCHED — the existing
Summit path keeps running exactly as before (regression-tested). This module adds
the generalized gym_event entry ON TOP without cloning that machinery: a client
event uses the arc planner; the LASSO Summit's rich multi-post sprint continues to
run through real_month_planner as today, and is ALSO representable here as a
gym_event so the "one engine, Summit is a gym_event" invariant holds and is tested.
"""

from datetime import date

from . import config, gym_event as ge


# LASSO Summit's verified window + facts. These are the SAME verified facts the
# sprint captions already state (summit_queue: November 7 and 8, Virgin Hotel
# Nashville, lassoframework.com/summit) — never re-invented here, referenced.
SUMMIT_GYM_ID = "lasso"
SUMMIT_START = "2026-11-07"
SUMMIT_END = "2026-11-08"


def summit_as_event():
    """LASSO Summit expressed as a gym_event on gym_id='lasso'. Proves Summit is
    just another gym_event: the window is the real event days, tz is LASSO's posting
    tz, and the offer facts are the verified summit facts (no invention). Recap media
    is left empty (the recap of a LASSO event would draw from real event photos, same
    rail). The rich pre-event SPRINT that already runs through real_month_planner is
    unaffected; this row is the single-record view the generalized engine plans from."""
    from . import summit_queue as _sq
    offer = (f"The LASSO Growth Summit. {_sq.SUMMIT_DATES}. {_sq.SUMMIT_VENUE}. "
             "100 seats. You leave with your 2027 growth plan.")
    return ge.GymEvent.from_row({
        "id": "evt_lasso_summit_2026",
        "gym_id": SUMMIT_GYM_ID,
        "name": "LASSO Growth Summit",
        "type": "open_house",   # a gathering/event type; the arc shape fits the run-up
        "starts_on": SUMMIT_START,
        "ends_on": SUMMIT_END,
        "tz": config.POSTING_TIMEZONE,
        "offer_text": offer,
        "link": f"https://{_sq.SUMMIT_URL}",
        "brief": "100 owners. 10 leaders. 2 days. 1 plan.",
        "media_ids": (),
        "status": "scheduled",
        "created_by": "lasso",
    })


def plan_event_arc(event, *, today=None, avatar=None, logger=None):
    """The generalized engine entry: plan the dated arc for ANY gym_event and ground
    it into content_calendar-ready row dicts. This is the SAME code path for a client
    gym's Bring-a-Friend Week and for LASSO Summit — one engine, gym-parameterized by
    event.gym_id and event.tz. Pure: `today` injected; no I/O, no writes.

    Returns the list of grounded arc row dicts (every row 'pending', event_id stamped,
    recap blocked until real media). The insertion into the month plan + A-gate re-run
    is event_calendar.insert_arc (Wave 3); this only plans + drafts."""
    if isinstance(event, dict):
        event = ge.GymEvent.from_row(event)
    arc = ge.plan_arc(event, today=today)
    return ge.draft_arc(event, arc, avatar=avatar, logger=logger)


def summit_still_plans(today=None):
    """Regression guard for the refactor invariant: the ONE engine plans a non-empty
    Summit arc from the Summit gym_event. If this returns False the 'Summit is a
    gym_event on the same engine' invariant has broken. `today` injectable."""
    if today is None:
        today = date(2026, 10, 1)   # a month before the event -> a full run-up arc
    ev = summit_as_event()
    rows = plan_event_arc(ev, today=today)
    return bool(rows)
