"""
stale_escalation_reminder.py — an escalated support ticket must NAG while it sits
unresolved, not go quiet the moment it posts once.

WHY THIS EXISTS
---------------
Blake, 2026-09-06, mid-incident: "Dean's ticket returned question_not_groundable and
parked. A client reporting content quality is highly groundable. Fix the classifier.
New rule: an unclassifiable ticket routes to a human immediately, never parks."

Checked against production before writing anything (this repo's own doctrine: no
agent may act on a summary). Dean's actual ticket
(4941e162-2923-495f-8efb-d2554dea5aec) shows the escalation path already worked:
the classifier ran, could not ground an answer, escalated it, and
`support_messages` shows a `kind: escalation` row POSTED to #fixer 5 seconds after
the ticket was created, plus an ack POSTED to Dean in the same 5 seconds. The
routing was never broken and never delayed.

What was actually true: the ticket sat in `status='hold', escalated=True` for
hours with NOTHING that speaks again. One escalation post, then silence, reads
exactly like "parked" to anyone who was not watching #fixer at 02:45 in the
morning -- which is the real defect, and it is the SAME defect this incident's
own doctrine already named: "Alerts must RE-FIRE while a condition persists.
Alerted once, muted forever is a defect pattern; it has shipped twice already
(stuck_publishing, the held-grade dedupe)." This is occurrence three, in a
different subsystem, found by the same rule.

THE FIX
-------
Query every ticket with `status='hold' AND escalated=True AND resolved_at IS
NULL` older than `STALE_HOLD_HOURS`. Post ONE re-fire escalation to #fixer per
ticket per calendar day it remains unresolved (kv-deduped,
`stale_escalation_reminder_<ticket_id>_<YYYY-MM-DD>`), naming how long it has
been waiting and its original text. Reuses the SAME outbound row + outbox
delivery machinery the original escalation used (kind=escalation), so this adds
no new Slack wiring, no new channel resolution, no new failure mode -- it can
only ever ADD a reminder post, never silence one, so it is prevent-only under
today's "prevent-only guards default ON" doctrine.

Does NOT change the classifier. The classifier is not what is broken; re-running
that audit here rather than trusting the report is the whole point of building
THIS instead of a classifier change nobody's evidence asked for.

FLAGS
-----
  AGENT_STALE_ESCALATION_REMINDER (config.stale_escalation_reminder_enabled,
  default ON): prevent-only, can only add a reminder post, never cause one.

  STALE_HOLD_HOURS (config.stale_hold_hours, default 4): how long a ticket may
  sit in hold+escalated before it is considered stale. Four hours, not four
  minutes -- a human triaging #fixer needs room to work a ticket without every
  ticket nagging every poll cycle; not four days either, because Dean's ticket
  sitting since 02:42 with a client waiting on a rebuild is itself the harm.

Pure core (`stale_tickets`, `reminder_body`), thin I/O wrapper (`run`).
"""
from __future__ import annotations

from datetime import datetime, timezone

from agent import config, db
from agent.slack_convo import adapter as _a
from agent.slack_convo.bus import Bus

_DEDUP_PREFIX = "stale_escalation_reminder_"


def _log(msg):
    print(f"[stale-escalation-reminder] {msg}")


def _parse_ts(value):
    try:
        s = str(value or "").replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def stale_tickets(tickets, *, now, stale_hours):
    """Every ticket (support_tickets-shaped dicts) that is status='hold',
    escalated True, resolved_at empty, and created_at older than `stale_hours`.

    Pure. `tickets` is the caller's already-fetched batch; `now` is injected so
    tests never depend on the clock."""
    out = []
    for t in tickets or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("status") or "").strip().lower() != "hold":
            continue
        if not t.get("escalated"):
            continue
        if t.get("resolved_at"):
            continue
        created = _parse_ts(t.get("created_at"))
        if not created:
            continue
        age_hours = (now - created).total_seconds() / 3600.0
        if age_hours >= stale_hours:
            out.append(t)
    return out


def reminder_body(ticket, *, now):
    """The line posted to #fixer. Names the age out loud -- a human scanning
    #fixer should never have to compute how long this has been sitting."""
    created = _parse_ts(ticket.get("created_at"))
    age_hours = (now - created).total_seconds() / 3600.0 if created else 0.0
    tid = ticket.get("id")
    excerpt = (ticket.get("raw_text") or "")[:200]
    return (f"STILL WAITING: ticket {tid} has been in hold {age_hours:.1f}h with "
            f"no resolution. Raw message: {excerpt}")


def _dedup_key(ticket_id, day):
    return f"{_DEDUP_PREFIX}{ticket_id}_{day}"


def run(*, bus=None, now=None, stale_hours=None, enabled=None, log=_log):
    """Find every stale held-escalated ticket and post ONE re-fire reminder per
    ticket per calendar day, deduped in kv so a job re-run the same day is a
    no-op. Returns {"ok": True, "reminded": [...ticket ids...]} or
    {"ok": True, "reminded": [], "reason": "disabled"} when the flag is off."""
    if enabled is None:
        enabled = config.stale_escalation_reminder_enabled()
    if not enabled:
        return {"ok": True, "reminded": [], "reason": "disabled"}

    now = now or datetime.now(timezone.utc)
    stale_hours = stale_hours if stale_hours is not None else config.stale_hold_hours()
    bus = bus or Bus()
    if not bus.available():
        log("bus not configured (SUPABASE_URL / service key); nothing to do")
        return {"ok": False, "reminded": [], "reason": "bus_unavailable"}

    tickets = bus._get(  # noqa: SLF001 - no public list-all-held query exists yet
        "support_tickets",
        {"status": "eq.hold", "escalated": "eq.true", "resolved_at": "is.null",
         "select": "*", "order": "created_at.asc", "limit": "200"})

    today = now.date().isoformat()
    reminded = []
    for ticket in stale_tickets(tickets, now=now, stale_hours=stale_hours):
        tid = ticket.get("id")
        key = _dedup_key(tid, today)
        if db.kv_get(key, ""):
            continue
        body = reminder_body(ticket, now=now)
        try:
            bus.record_outbound(
                ticket_id=tid, author_type="system", body=body,
                delivery_status="ready", kind=_a.KIND_ESCALATION,
                meta={"identity": ticket.get("bot_identity") or "echo",
                      "surface": "stale_escalation_reminder"})
        except Exception as e:  # noqa: BLE001 - one ticket's failure never sinks the rest
            log(f"could not queue reminder for {tid}: {type(e).__name__}: {e}")
            continue
        db.kv_set(key, "1")
        reminded.append(tid)
        log(f"reminded {tid} (stale {stale_hours}h+)")

    return {"ok": True, "reminded": reminded}


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2))
