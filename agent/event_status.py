"""
event_status.py — the nightly gym_event date job + the publish gate + dead-link guard.

Wave 5 (STATUS job): once a night, walk every active gym_event and flip its status
scheduled -> live -> ended in the GYM'S timezone (never UTC — a Pacific gym's event
must go 'live' on its own calendar day, not a day early/late). An event that has
ended sweeps its still-pending arc rows denied (reject_reason=event_ended), so posting
stops dead.

Wave 4 gates:
  * publish_allowed(event, row): an arc row may publish ONLY while its event is live
    (or scheduled, for a pre-window post) AND not ended/cancelled. An ended/cancelled
    event blocks every remaining publish for that event_id.
  * verify_link(link, http): a provided offer link is checked before each publish; a
    dead link flips the row back to pending + reject_reason and alerts (never posts a
    dead link).
  * recap_photo_request(event): the morning AFTER the event ends (T+0 am in the gym's
    tz) auto-requests photos from the coach channel; the recap post stays BLOCKED until
    real event media exists.

Gated by AGENT_EVENT_CAMPAIGNS (per-gym). Flag off -> the job is a no-op. Pure helpers
(status_for, publish_allowed, verify_link) are offline-testable; the job wrapper reads
the store and applies flips.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import config, event_calendar as ec, gym_event as ge


# The status lifecycle order. scheduled -> live -> ended; draft is pre-arm; cancelled
# is terminal (a human/portal action, never the date job).
def status_for(event: ge.GymEvent, *, today=None):
    """The status this event SHOULD carry given the gym-local date `today` (injected;
    defaults to the gym's tz today). Pure, tz-correct:
      * before starts_on            -> 'scheduled' (was draft/scheduled)
      * starts_on .. ends_on        -> 'live'
      * after ends_on               -> 'ended'
    A 'cancelled' event is terminal and is NEVER moved by the date job (the caller
    skips cancelled events). A 'draft' event (not yet armed) is left as-is until the
    portal marks it scheduled."""
    if today is None:
        today = datetime.now(ZoneInfo(event.tz)).date()
    today = today if isinstance(today, date) else date.fromisoformat(str(today)[:10])
    if today < event.starts:
        return "scheduled"
    if today <= event.ends:
        return "live"
    return "ended"


def publish_allowed(event: ge.GymEvent, *, today=None):
    """True when an arc post for this event may publish today (gym-local). Publishing
    is allowed while the event is scheduled (pre-window posts) or live, and BLOCKED once
    the window has passed (ended) or the event is cancelled. This is the offer-record
    gate: the event's dates gate publishing exactly like the offer rails."""
    if event.status == "cancelled":
        return False
    st = status_for(event, today=today)
    return st in ("scheduled", "live")


def verify_link(link, *, http=None, timeout=10):
    """True when `link` is reachable (a live offer link), False when it is dead. An
    empty link is treated as OK (the event simply has no link; the DM ask is used).

    A HEAD (falling back to GET) that returns < 400 is live; a 4xx/5xx or any network
    error is dead. `http` is injectable so this is offline-testable; the live path uses
    requests. This is called before EACH publish (event_calendar / the publish guard);
    a dead link flips the row back to pending + alerts, never posts a dead link."""
    if not link:
        return True
    client = http
    if client is None:
        import requests
        client = requests
    try:
        r = client.head(link, timeout=timeout, allow_redirects=True)
        code = getattr(r, "status_code", 599)
        if code == 405 or code >= 400:
            # some hosts reject HEAD; try GET before calling it dead.
            r = client.get(link, timeout=timeout, allow_redirects=True)
            code = getattr(r, "status_code", 599)
        return code < 400
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The nightly job (store-facing).
# ---------------------------------------------------------------------------

def run_status_job(event_store, calendar_store, *, today=None, logger=None):
    """Walk every active gym_event and flip its status to what the gym-local date says
    (status_for). On a transition INTO 'ended', sweep the event's still-pending arc rows
    denied (reject_reason=event_ended) so posting stops dead, and fire the recap photo
    request (recap stays blocked until real media). Gated per-gym by AGENT_EVENT_CAMPAIGNS;
    an event whose gym is not armed is skipped.

    `today` is a single injected date for tests; live runs pass None so each event is
    evaluated in ITS OWN gym tz (status_for computes per-event when today is None).

    Returns a summary dict. Never publishes."""
    log = logger or (lambda m: print(f"[event-status] {m}"))
    flipped, ended, denied = 0, 0, 0
    try:
        active = event_store.list_active() or []
    except Exception as exc:  # noqa: BLE001
        log(f"run_status_job: list_active failed {type(exc).__name__}")
        return {"ok": False, "flipped": 0, "ended": 0, "denied": 0}

    for row in active:
        try:
            event = ge.GymEvent.from_row(row)
        except ValueError as exc:
            log(f"skip bad gym_event {row.get('id')}: {exc}")
            continue
        if not config.event_campaigns_enabled_for(event.gym_id):
            continue
        if event.status == "cancelled":
            continue
        want = status_for(event, today=today)
        if want == event.status:
            continue
        # Apply the flip.
        try:
            event_store.set_status(event.gym_id, event.id, want)
            flipped += 1
        except Exception as exc:  # noqa: BLE001
            log(f"set_status {event.id} -> {want} failed {type(exc).__name__}")
            continue
        if want == "ended":
            ended += 1
            # Sweep pending arc rows denied (posting stops dead).
            res = ec.cancel_event(calendar_store, event.gym_id, event.id, ended=True,
                                  logger=logger)
            denied += res.get("denied", 0)
            # Recap photo request the morning after the end (best-effort).
            try:
                recap_photo_request(event, logger=logger)
            except Exception as exc:  # noqa: BLE001
                log(f"recap photo request for {event.id} failed {type(exc).__name__}")
    return {"ok": True, "flipped": flipped, "ended": ended, "denied": denied}


def recap_photo_request(event: ge.GymEvent, *, poster=None, logger=None):
    """Ask the coach channel for real event photos the morning after the event ends.
    The recap post is BLOCKED until real media exists (gym_event.draft_arc sets
    recap_blocked when media_ids is empty), so this nudge is what unblocks it: a coach
    uploads photos, the media lands in the pool, and the recap can then draft from REAL
    event media (never stock, never invented).

    Best-effort Slack/ops nudge; a failure never blocks the status job. Returns the
    message posted (or None when media already exists so no nudge is needed)."""
    if event.has_media:
        return None
    msg = (f"Event recap: '{event.name}' ended {event.ends_on}. Send us real photos "
           f"from the event so we can draft the recap post. Until real media lands, the "
           f"recap stays blocked (we never use stock or invented photos).")
    try:
        from . import ops_alerts
        ops_alerts.alert(msg)
    except Exception:
        pass
    if logger:
        logger(f"recap photo request posted for {event.id}")
    return msg
