"""
deny_streak_alarm.py — three consecutive coach denials on one account is a
CONTENT ALARM, not silence.

WHY THIS EXISTS
---------------
Blake, 2026-09-06: "Deny streak as a content alarm. Three consecutive denials
on one gym escalates to SOCIAL automatically. Tough Temple told us with the
deny button for a week and nobody read it. Log as its own detection defect: a
detection gap, not a content gap."

The content problem itself (repetitive captions, an FAQ-mined CTA, no reels)
already has a fix in flight this same track (caption_variety.py PR #54,
cta_self_question_gate.py this same PR). This module is the DIFFERENT half:
nothing in the system was READING the signal a coach was already sending.
portal_calendar_store.deny_with_reason writes status='denied' for exactly this
purpose and nothing downstream ever counted a run of them.

THE SIGNAL
----------
A coach denying three posts in a row on ONE account, with no approval landing
between them, is a person telling Echo the content is wrong far more loudly
than a single denial ever does -- and far more reliably than waiting for the
coach to also write a support ticket, which Tough Temple never did.

THE CHECK
---------
`deny_streak_violations(rows)`: group content_calendar-shaped rows by
(gym_id, account), order by post_date, and find any run of >= 3 consecutive
DENIED rows uninterrupted by an approved/published row between them. Pure,
offline, no clock dependency (unlike stale_escalation_reminder, a streak does
not need "now" -- it is a property of the rows themselves).

FLAGS
-----
  AGENT_DENY_STREAK_ALARM (config.deny_streak_alarm_enabled, default ON):
  informational only, can never block a write or change a post -- it only
  posts an alert naming a gym whose coach has denied 3+ in a row. Same
  "can only add a signal, never remove one" logic as day-shape and the CTA
  gate: it may default ON.

  DENY_STREAK_THRESHOLD (config.deny_streak_threshold, default 3): the run
  length that counts as an alarm.

Re-fires per Blake's own doctrine (alerts must re-fire while a condition
persists): deduped per (gym_id, account, streak END post_date), so a NEW
denial extending an existing streak (or a second gym hitting the threshold)
always re-alerts, but re-running against an unchanged streak does not spam.

Pure core (`deny_streak_violations`), thin I/O wrapper (`run`).
"""
from __future__ import annotations

from agent import config, db, ops_alerts

_DEDUP_PREFIX = "deny_streak_alarm_"

_DENIED = "denied"
# Any of these breaks a run: the coach approved or the platform published
# something in between, so the streak did not run uninterrupted.
_STREAK_BREAKERS = frozenset({"approved", "published", "posted"})


def _log(msg):
    print(f"[deny-streak-alarm] {msg}")


class DenyStreakViolation:
    """One gym+account whose most recent denials form a run >= threshold.
    Carries enough to name it out loud and to dedupe on."""

    __slots__ = ("gym_id", "account", "streak_len", "first_date", "last_date",
                "row_ids")

    def __init__(self, gym_id, account, streak_len, first_date, last_date, row_ids):
        self.gym_id = gym_id
        self.account = account
        self.streak_len = streak_len
        self.first_date = first_date
        self.last_date = last_date
        self.row_ids = list(row_ids)

    def message(self):
        return (f"{self.gym_id} ({self.account}): {self.streak_len} consecutive "
                f"denials, {self.first_date} through {self.last_date}. The coach "
                f"is telling us the content is wrong; escalate to SOCIAL.")


def deny_streak_violations(rows, *, threshold=3):
    """Every (gym_id, account) whose rows, ordered by post_date, end on a run
    of `threshold` or more DENIED rows uninterrupted by an approved/published
    row. Only the TRAILING run matters -- a coach who denied three then
    approved the next ten has been heard and answered; a coach denying three
    right up to the newest row has not.

    Rows missing post_date, or without a recognized status, are skipped for
    ordering purposes but never silently break a streak they are not part of.
    """
    groups = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        post_date = str(row.get("post_date") or "").strip()
        status = str(row.get("status") or "").strip().lower()
        if not post_date or not status:
            continue
        key = (str(row.get("gym_id") or ""), str(row.get("account") or ""))
        groups.setdefault(key, []).append((post_date, status, row.get("id")))

    out = []
    for (gym_id, account), items in sorted(groups.items()):
        items.sort(key=lambda x: x[0])
        # Walk from the newest row backwards, counting a trailing run of DENIED,
        # stopping at the first breaker (approved/published/posted). A status
        # this module does not recognize (e.g. 'pending') neither extends nor
        # breaks the run -- it is simply not part of it either way, same as a
        # gap in the calendar does not hide a day_shape collision.
        streak = []
        for post_date, status, row_id in reversed(items):
            if status == _DENIED:
                streak.append((post_date, row_id))
                continue
            if status in _STREAK_BREAKERS:
                break
            # any other status: skip, keep looking further back
        if len(streak) >= threshold:
            streak.reverse()  # oldest-first for reporting
            out.append(DenyStreakViolation(
                gym_id, account, len(streak),
                streak[0][0], streak[-1][0],
                [r for _, r in streak]))
    return out


def _dedup_key(v: DenyStreakViolation) -> str:
    return f"{_DEDUP_PREFIX}{v.gym_id}_{v.account}_{v.last_date}"


def run(*, rows=None, fetch_rows=None, threshold=None, enabled=None,
        alert=None, log=_log):
    """Find every deny-streak violation and alert once per (gym, account,
    streak-ending date), so extending a streak re-alerts but re-running
    against an unchanged one does not spam.

    `rows` is the caller's already-fetched content_calendar batch, OR pass
    `fetch_rows` (a zero-arg callable) to fetch lazily -- only when the flag
    is on, so a disabled job never touches the store at all."""
    if enabled is None:
        enabled = config.deny_streak_alarm_enabled()
    if not enabled:
        return {"ok": True, "alarmed": [], "reason": "disabled"}

    if rows is None:
        if fetch_rows is None:
            return {"ok": False, "alarmed": [], "reason": "no_rows_source"}
        rows = fetch_rows()

    threshold = threshold if threshold is not None else config.deny_streak_threshold()
    alert = alert or ops_alerts.alert

    alarmed = []
    for v in deny_streak_violations(rows, threshold=threshold):
        key = _dedup_key(v)
        if db.kv_get(key, ""):
            continue
        try:
            alert(v.message())
        except Exception as e:  # noqa: BLE001 - one gym's failure never sinks the rest
            log(f"could not alert for {v.gym_id}/{v.account}: {type(e).__name__}: {e}")
            continue
        db.kv_set(key, "1")
        alarmed.append({"gym_id": v.gym_id, "account": v.account,
                        "streak_len": v.streak_len, "last_date": v.last_date})
        log(f"alarmed {v.gym_id}/{v.account}: {v.streak_len} denials through {v.last_date}")

    return {"ok": True, "alarmed": alarmed}


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2))
