"""grade_sweep.py — nightly per-gym calendar grader.

For each gym:
1. Grade the trailing 30 days (published rows from content_calendar)
2. Grade the forward book (pending/approved future rows)
3. Write both to gym_social_grades
4. Alert coach channel when either grade drops below B (80)

Behind AGENT_CALENDAR_GRADE flag. Run via: python3 -m agent jobs grade_sweep

SELF-FIX MODE (AGENT_GRADE_SELF_FIX, default OFF; Blake's 2026-08-27 ruling
"it should fix it on its own without sending me alot of slacks"). Flag OFF ->
everything above is byte-for-byte unchanged. Flag ON:
  * a forward book below A is first self-remediated (agent/jobs/grade_fix.py)
    and then REGRADED; the final grade is what lands in gym_social_grades.
    Remediation runs in up to _MAX_FIX_PASSES passes per sweep: another pass
    runs only while the regraded score IMPROVED and is still below A (heavy
    lanes keep their own once-per-gym-per-day kv stamp inside grade_fix);
  * trailing_30 is graded + stored but NEVER alerts (history is not fixable);
  * a still-below-A forward book alerts ONLY when the (score, defect set)
    differs from the last alerted state for that gym (kv stamp) AND at most
    once per gym per day, in <= 3 lines saying what was auto-fixed and what
    remains;
  * at most ONE aggregated sweep summary line fires per run, and only when
    something changed (a gym self-fixed to A or a new held alert fired).

THE REGRESSION GUARD (2026-08-31). Every alert above answers "is this book
below the bar", which is a standing CONDITION — it fires night after night
until everyone stops reading it, which is the failure mode this sweep exists
to avoid. A grade that DROPPED since the last stored run is an EVENT, and a
different one: it means defects are being BUILT into the book faster than the
nightly repair clears them. topfuel went 74 -> 64 -> 67 -> 71 -> 74 across live
runs and nothing ever said so. The guard fires at any letter (a book sliding
from A to B is the early warning that a below-B alert would not give until far
too late), reads the previous total BEFORE this run's is written, and is
deduped to once per gym per day.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta, timezone, datetime


def _today():
    return date.today().isoformat()


def _date_range(start_date: str, end_date: str) -> tuple:
    """Return (start_iso, end_iso) as strings."""
    return start_date, end_date


def _fetch_rows(store, gym_id: str, start_date: str, end_date: str) -> list:
    """Pull rows from the calendar store for gym_id in [start_date, end_date].
    Returns [] on any error (fail open)."""
    try:
        if hasattr(store, "rows_in_range"):
            return store.rows_in_range(gym_id, start_date, end_date) or []
        # Fallback: use due_rows style if that is the only method available.
        return []
    except Exception as exc:
        print(f"[grade-sweep] fetch rows failed for {gym_id}: {type(exc).__name__}: {exc}")
        return []


def _write_grade(store_or_db, gym_id: str, window: str, grade) -> None:
    """Upsert a grade record into gym_social_grades (via injectable store or Supabase)."""
    record = {
        "gym_id": gym_id,
        "window": window,
        "total": grade.total,
        "letter": grade.letter,
        "scores": grade.scores,
        "defects": [(d[0], str(d[1]), d[2]) for d in (grade.defects or [])],
        "graded_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        if hasattr(store_or_db, "insert_grade"):
            store_or_db.insert_grade(record)
            return
        # Supabase REST path
        from agent import config
        url = config.supabase_url()
        key = config.supabase_service_key()
        if not url or not key:
            return
        import urllib.request
        body = json.dumps(record).encode()
        # Note: the "window" column is quoted in DDL (reserved word); the REST
        # API uses the column name as-is in the JSON body (no quoting needed there).
        req = urllib.request.Request(
            f"{url}/rest/v1/gym_social_grades",
            data=body,
            method="POST",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()
    except Exception as exc:
        print(f"[grade-sweep] write grade failed for {gym_id}/{window}: "
              f"{type(exc).__name__}: {exc}")


def _previous_grade(store_or_db, gym_id: str, window: str):
    """The most recently STORED total for this gym+window, or None.

    Read BEFORE the current grade is written, so it is genuinely the previous
    run's number. Fail-open: any error returns None and the drop guard simply
    stays quiet rather than crying wolf."""
    try:
        if hasattr(store_or_db, "latest_grade"):
            rec = store_or_db.latest_grade(gym_id, window)
            return None if rec is None else int(rec.get("total"))
        from agent import config
        url = config.supabase_url()
        key = config.supabase_service_key()
        if not url or not key:
            return None
        import urllib.parse
        import urllib.request
        q = urllib.parse.urlencode({
            "gym_id": f"eq.{gym_id}",
            "window": f"eq.{window}",
            "select": "total,graded_at",
            "order": "graded_at.desc",
            "limit": "1",
        })
        req = urllib.request.Request(
            f"{url}/rest/v1/gym_social_grades?{q}",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read().decode() or "[]")
        if not rows:
            return None
        return int(rows[0].get("total"))
    except Exception:  # noqa: BLE001 - the guard never breaks the sweep
        return None


def _should_alert_drop(gym_id: str, prev_total: int, total: int,
                       today_str: str, db=None) -> bool:
    """Drop guard dedup: at most one drop alert per gym per day.

    Durable-or-silent, the same convention the held alert uses: a process
    without durable kv would re-alert every run, so it stays silent."""
    try:
        _db = db
        if _db is None:
            from agent import db as _dbmod
            _db = _dbmod
        if hasattr(_db, "kv_is_durable") and not _db.kv_is_durable():
            return False
        key = f"grade_drop_state_{gym_id}"
        raw = _db.kv_get(key, "")
        state = json.loads(raw) if raw else {}
        if state.get("date") == today_str:
            return False
        _db.kv_set(key, json.dumps({"date": today_str, "from": prev_total,
                                    "to": total}))
        return True
    except Exception:  # noqa: BLE001
        return False


def _drop_alert_text(gym_id: str, prev_total: int, grade) -> str:
    """The signal Blake actually needs: not 'this book is bad' (which fires
    nightly until everyone stops reading it) but 'this book got WORSE than it
    was', which means something is RE-CREATING defects the repair already
    cleared. Names the top new defects so the culprit build is findable."""
    top = [str(d[2]) for d in (grade.defects or [])[:3]]
    return (
        f"calendar grade DROPPED: {gym_id} forward book went "
        f"{prev_total} -> {grade.total} ({grade.letter}) since the last run.\n"
        f"A drop means new defects are being BUILT into the book faster than "
        f"the nightly repair clears them.\n"
        f"Top defects now: {top}"
    )


def _alert_low_grade(gym_id: str, window: str, grade, alert_fn) -> None:
    """Fire one ops alert when a grade drops below B (80). LEGACY path: only
    used when AGENT_GRADE_SELF_FIX is OFF (byte-for-byte today's behavior)."""
    if grade.total < 80:
        top_defects = [d[2] for d in (grade.defects or [])[:3]]
        alert_fn(
            f"calendar grade sweep: {gym_id} {window} scored "
            f"{grade.total} ({grade.letter}). "
            f"Top defects: {top_defects}. Review the forward book or trailing posts."
        )


# ---------------------------------------------------------------------------
# Self-fix remediation loop (AGENT_GRADE_SELF_FIX only)
# ---------------------------------------------------------------------------

_MAX_FIX_PASSES = 3

_FIX_COUNT_KEYS = ("captions_fixed", "repillared", "craft_fixed",
                   "craft_attempted", "booking_asks_added", "audience_fixed",
                   "audience_attempted", "skipped")


def _merge_fix(agg: dict, step: dict) -> dict:
    """Fold one remediation pass into the aggregated per-gym fix report."""
    for k in _FIX_COUNT_KEYS:
        agg[k] = int(agg.get(k) or 0) + int((step or {}).get(k) or 0)
    seen = agg.setdefault("actions", [])
    for a in (step or {}).get("actions") or []:
        if a not in seen:
            seen.append(a)
    gap = (step or {}).get("gap_fill")
    if gap and gap != "none" and agg.get("gap_fill") in (None, "none"):
        agg["gap_fill"] = gap
    agg["ok"] = bool(agg.get("ok", True)) and bool((step or {}).get("ok", False))
    agg["passes"] = int(agg.get("passes") or 0) + 1
    return agg


# ---------------------------------------------------------------------------
# Self-fix alert policy (AGENT_GRADE_SELF_FIX only)
# ---------------------------------------------------------------------------

def _defect_state_hash(grade) -> str:
    """A stable fingerprint of the grade's defect SET (order-independent)."""
    parts = sorted(str(d[2]) for d in (grade.defects or []))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def _should_alert_held(gym_id: str, grade, today_str: str, db=None) -> bool:
    """Held-alert dedup: fire only when the (score, defect set) differs from
    the last alerted state for this gym AND at most once per gym per day.
    Durable-or-silent (the repo's alert-dedup convention): a process without a
    durable kv would re-alert every run, so it stays silent instead."""
    try:
        _db = db
        if _db is None:
            from agent import db as _dbmod
            _db = _dbmod
        if hasattr(_db, "kv_is_durable") and not _db.kv_is_durable():
            return False
        key = f"grade_alert_state_{gym_id}"
        raw = _db.kv_get(key, "")
        state = json.loads(raw) if raw else {}
        new = {"total": grade.total, "defects": _defect_state_hash(grade)}
        if (state.get("total") == new["total"]
                and state.get("defects") == new["defects"]):
            return False                       # same state as last alert: quiet
        if state.get("date") == today_str:
            return False                       # already alerted this gym today
        new["date"] = today_str
        _db.kv_set(key, json.dumps(new))
        return True
    except Exception:  # noqa: BLE001 - alert plumbing must never break the sweep
        return False


def _held_alert_text(gym_id: str, grade, fix: dict) -> str:
    """<= 4 lines: score, what self-fix repaired, what remains, what was exempt.

    The exemption line is not optional. Caption-less posts (a story whose
    caption is burned onto its media, a GBP photo post) are held OUT of the
    caption legs, and a score that quietly excluded rows without saying so
    would be exactly the kind of dishonesty this grader is meant to end."""
    fixed_txt = "; ".join((fix or {}).get("actions") or []) or "nothing auto-fixable"
    remaining = [str(d[2]) for d in (grade.defects or [])[:3]]
    lines = [
        f"calendar grade: {gym_id} forward book held at {grade.total} "
        f"({grade.letter}) after self-fix.",
        f"Auto-fixed: {fixed_txt}.",
        f"Remaining: {remaining}",
    ]
    exempt = getattr(grade, "exempt", None) or {}
    if exempt:
        parts = "; ".join(f"{v} post(s) {k}" for k, v in sorted(exempt.items()))
        lines.append(f"Exempt by rule (not defects): {parts}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# C7 — a gym stuck below A must ASK A NAMED HUMAN, not wait forever
#
# THE DEFECT: the remediation loop is bounded (_MAX_FIX_PASSES = 3 here, 4 in the
# planner gate) and fix["passes"] / fix["trajectory"] live only in memory for the
# length of one sweep. Nothing is persisted, so there is no such thing as "this gym
# has been stuck at B for four nights". The held alert is deduped on (score, defect
# set) -- which means a gym that is stuck in EXACTLY the same way goes SILENT after
# the first night, precisely when it most needs a person. Nobody is named, nothing
# is greppable, and the mechanics have already proven they cannot fix it.
#
# THE FIX: persist a consecutive-nights-held streak. Once a gym has been held for
# grade_stuck_nights() runs in a row with the mechanics no longer moving the score,
# raise a DIFFERENT alert: one stable tag (GRADE-STUCK) so it can be searched and
# routed, the named approver so it lands on a person, the trajectory so the reader
# can see the loop has flattened, and the exact decision being asked for.
#
# Flag: config.grade_stuck_escalation_enabled() (ECHO_GRADE_STUCK_ESCALATION,
# default OFF). Flag off, not one extra kv read or alert happens.
# ---------------------------------------------------------------------------

STUCK_TAG = "GRADE-STUCK"


def _streak_key(gym_id):
    return f"grade_stuck_streak_{gym_id}"


def _bump_stuck_streak(gym_id, grade, today_str, db=None):
    """Advance (or start) this gym's consecutive-nights-held streak and return it as
    {nights, first_seen, first_total, last_total, escalated_on}. A run on a day
    already counted is idempotent, so a double sweep cannot inflate the count.
    Never raises: streak plumbing may not break the sweep."""
    try:
        _db = db
        if _db is None:
            from agent import db as _dbmod
            _db = _dbmod
        raw = _db.kv_get(_streak_key(gym_id), "")
        st = json.loads(raw) if raw else {}
        if st.get("last_seen") == today_str:
            return st                            # already counted today
        if not st:
            st = {"nights": 0, "first_seen": today_str,
                  "first_total": grade.total, "escalated_on": ""}
        st["nights"] = int(st.get("nights") or 0) + 1
        st["last_seen"] = today_str
        st["last_total"] = grade.total
        st["letter"] = grade.letter
        _db.kv_set(_streak_key(gym_id), json.dumps(st))
        return st
    except Exception:  # noqa: BLE001
        return {}


def _clear_stuck_streak(gym_id, db=None):
    """A book that reached A is not stuck. Clearing on recovery is what keeps the
    escalation honest: the count means CONSECUTIVE nights, never a lifetime total."""
    try:
        _db = db
        if _db is None:
            from agent import db as _dbmod
            _db = _dbmod
        if _db.kv_get(_streak_key(gym_id), ""):
            _db.kv_set(_streak_key(gym_id), "")
    except Exception:  # noqa: BLE001
        pass


def _should_escalate_stuck(streak, threshold, today_str):
    """True when this gym has been held for `threshold` consecutive nights and has
    not already been escalated today. Deliberately NOT deduped on the defect set:
    an unchanging defect set is the WHOLE POINT of this alert, and the existing
    (score, defects) dedup is exactly why a stuck gym went quiet."""
    if not streak:
        return False
    if int(streak.get("nights") or 0) < int(threshold):
        return False
    return streak.get("escalated_on") != today_str


def _mark_escalated(gym_id, streak, today_str, db=None):
    try:
        _db = db
        if _db is None:
            from agent import db as _dbmod
            _db = _dbmod
        st = dict(streak or {})
        st["escalated_on"] = today_str
        _db.kv_set(_streak_key(gym_id), json.dumps(st))
    except Exception:  # noqa: BLE001
        pass


def _stuck_alert_text(gym_id, grade, fix, streak, approver_id=""):
    """The escalation a human can actually act on. One stable tag to route and
    search on, the gym, how long, what the loop has stopped doing, the defect that
    will not move, and the decision being asked for. Names a person so it is not
    addressed to nobody."""
    nights = int((streak or {}).get("nights") or 0)
    first = (streak or {}).get("first_total")
    traj = (fix or {}).get("trajectory") or []
    traj_txt = " -> ".join(str(t[0]) for t in traj) if traj else str(grade.total)
    stuck_on = [str(d[2]) for d in (grade.defects or [])[:3]] or ["no named defect"]
    who = f"<@{approver_id}> " if approver_id else ""
    moved = ("" if first is None or first == grade.total
             else f" It has moved {first} to {grade.total} over that time.")
    return (
        f"{STUCK_TAG} {gym_id}: the forward book has been held at {grade.total} "
        f"({grade.letter}) for {nights} run(s) in a row and the automatic "
        f"remediation loop has stopped improving it (this run: {traj_txt})." "\n"
        f"{who}this one needs a person: the mechanics have had "
        f"{int((fix or {}).get('passes') or 0)} pass(es) and cannot close it."
        f"{moved}" "\n"
        f"Stuck on: {stuck_on}" "\n"
        f"Decide one: fix the source material (photos / approved sources / the "
        f"gym's own asks), accept the grade for this gym, or change the rule that "
        f"is firing. Nothing was published and nothing was fabricated."
    )


def run(gyms=None, store=None, now=None, alert_fn=None) -> dict:
    """
    Main entry point: grade each gym's trailing 30 days and forward book.

    Args:
        gyms:      list of gym_id strings; defaults to all client gyms + 'lasso'
        store:     injectable calendar store (must implement rows_in_range)
        now:       injectable today date (YYYY-MM-DD string or date object)
        alert_fn:  injectable alert function (defaults to ops_alerts.alert)

    Returns:
        dict with per-gym results
    """
    from agent import config

    from agent.calendar_grade import A_THRESHOLD, grade_month
    from agent.real_month_planner import _profile_for

    if alert_fn is None:
        from agent import ops_alerts
        alert_fn = ops_alerts.alert

    if store is None:
        try:
            from agent.portal_calendar_store import SupabaseCalendarStore
            store = SupabaseCalendarStore()
        except Exception as exc:
            return {"ok": False, "reason": f"store init failed: {type(exc).__name__}: {exc}"}

    today_str = now if isinstance(now, str) else (now.isoformat() if now else _today())
    today = date.fromisoformat(today_str)
    trailing_start = (today - timedelta(days=30)).isoformat()
    forward_end = (today + timedelta(days=60)).isoformat()

    if gyms is None:
        from agent.calendar_autopublish import client_gym_bases
        gyms = client_gym_bases() or []
        if "lasso" not in gyms:
            gyms = ["lasso"] + list(gyms)

    # Per-gym rollout: sweep any gym whose per-gym flag (or the global flag)
    # is on. A sweep gated only on the global flag would skip every gym during
    # the gym-by-gym rollout, which is exactly when the nightly grades matter.
    gyms = [g for g in gyms if config.calendar_grade_enabled_for(g)]
    if not gyms:
        return {"ok": False,
                "reason": "AGENT_CALENDAR_GRADE off for every requested gym "
                          "(per-gym AGENT_CALENDAR_GRADE_{GYM} and global both false)"}

    # SELF-FIX MODE (AGENT_GRADE_SELF_FIX, default OFF). OFF -> the legacy
    # per-gym-per-window alert path below runs byte-for-byte as today.
    self_fix = config.grade_self_fix_enabled()
    fixed_gyms, held_gyms, alerted_gyms = [], [], []
    dropped_gyms = []
    stuck_gyms = []          # C7: gyms escalated to a NAMED human this run

    results = {}
    for gym_id in gyms:
        profile = _profile_for(gym_id)
        gym_result = {"gym_id": gym_id, "profile": profile}

        # --- trailing 30 days ---
        trailing_rows = _fetch_rows(store, gym_id, trailing_start, today_str)
        if trailing_rows:
            t_grade = grade_month(trailing_rows, profile=profile)
            _write_grade(store, gym_id, "trailing_30", t_grade)
            if not self_fix:
                _alert_low_grade(gym_id, "trailing_30", t_grade, alert_fn)
            # self-fix ON: trailing NEVER alerts (history is not fixable);
            # the grade is still stored above for the portal/digest.
            gym_result["trailing_30"] = {
                "total": t_grade.total,
                "letter": t_grade.letter,
                "rows": len(trailing_rows),
            }
        else:
            gym_result["trailing_30"] = {"total": None, "reason": "no rows"}

        # --- forward book ---
        forward_rows = _fetch_rows(store, gym_id, today_str, forward_end)
        if forward_rows:
            # Read the last stored total BEFORE writing this run's, so the drop
            # guard compares against the genuinely previous run.
            prev_total = _previous_grade(store, gym_id, "forward_book")
            f_grade = grade_month(forward_rows, profile=profile)
            fix = None
            if self_fix and f_grade.total < A_THRESHOLD:
                # Self-remediate, re-read, regrade — up to _MAX_FIX_PASSES
                # passes, another only while the score IMPROVED and the book
                # is still below A. The FINAL grade is what gets stored.
                fix = {"ok": True, "passes": 0, "gap_fill": "none",
                       "actions": []}
                fix["trajectory"] = [(f_grade.total, len(f_grade.defects or []))]
                for _pass in range(_MAX_FIX_PASSES):
                    # NB: deliberately NOT named prev_total — that holds the
                    # PREVIOUS RUN's stored score for the drop guard below, and
                    # reusing the name here silently disarmed it.
                    pass_prev_total = f_grade.total
                    prev_defects = len(f_grade.defects or [])
                    try:
                        from agent.jobs import grade_fix
                        step = grade_fix.remediate_forward_book(
                            gym_id, forward_rows, store, profile=profile,
                            defects=f_grade.defects, today_iso=today_str)
                    except Exception as exc:  # noqa: BLE001 - never sink the sweep
                        print(f"[grade-sweep] {gym_id}: self-fix failed: "
                              f"{type(exc).__name__}: {exc}")
                        step = {"ok": False, "actions": []}
                    _merge_fix(fix, step)
                    forward_rows = _fetch_rows(store, gym_id, today_str,
                                               forward_end) or forward_rows
                    f_grade = grade_month(forward_rows, profile=profile)
                    fix["trajectory"].append(
                        (f_grade.total, len(f_grade.defects or [])))
                    # CONVERGENCE CONTRACT (2026-08-31): keep going while the
                    # pass is still making the book better by EITHER measure —
                    # a higher score or strictly fewer defects. The old test
                    # looked at the score alone, so a pass that cleared real
                    # defects without yet moving a floored leg was read as "no
                    # progress" and the loop stopped one step short of the
                    # improvement. Stopping when neither moves IS the floor.
                    improved = (f_grade.total > pass_prev_total
                                or len(f_grade.defects or []) < prev_defects)
                    if (not step.get("ok")
                            or f_grade.total >= A_THRESHOLD
                            or not improved):
                        break
            _write_grade(store, gym_id, "forward_book", f_grade)
            if not self_fix:
                _alert_low_grade(gym_id, "forward_book", f_grade, alert_fn)
            else:
                if fix is not None and f_grade.total >= A_THRESHOLD:
                    fixed_gyms.append(gym_id)
                if f_grade.total < A_THRESHOLD:
                    held_gyms.append(gym_id)
                    # Alert ONLY when remediation ran, the book is still
                    # below A, the state changed, and none fired today yet.
                    if fix is not None and _should_alert_held(gym_id, f_grade,
                                                              today_str):
                        alert_fn(_held_alert_text(gym_id, f_grade, fix))
                        alerted_gyms.append(gym_id)
                    # C7: a gym stuck for several nights running is waiting on a
                    # HUMAN, and the (score, defect set) dedup above goes silent on
                    # exactly that gym. Count the nights and name a person.
                    if fix is not None and config.grade_stuck_escalation_enabled():
                        streak = _bump_stuck_streak(gym_id, f_grade, today_str)
                        if _should_escalate_stuck(streak,
                                                  config.grade_stuck_nights(),
                                                  today_str):
                            alert_fn(_stuck_alert_text(
                                gym_id, f_grade, fix, streak,
                                approver_id=getattr(config, "APPROVER_SLACK_ID", "")))
                            _mark_escalated(gym_id, streak, today_str)
                            stuck_gyms.append(gym_id)
                            alerted_gyms.append(gym_id)
                elif config.grade_stuck_escalation_enabled():
                    _clear_stuck_streak(gym_id)   # reached A: the streak is over
            # REGRESSION GUARD: a book that got WORSE than its last run means a
            # build is re-creating defects. That fires whatever the letter is —
            # a book sliding from A to B is the early warning that the below-B
            # alert would not give until it was far too late.
            if prev_total is not None and f_grade.total < prev_total:
                dropped_gyms.append((gym_id, prev_total, f_grade.total))
                if _should_alert_drop(gym_id, prev_total, f_grade.total,
                                      today_str):
                    alert_fn(_drop_alert_text(gym_id, prev_total, f_grade))
                    alerted_gyms.append(gym_id)

            gym_result["forward_book"] = {
                "total": f_grade.total,
                "letter": f_grade.letter,
                "rows": len(forward_rows),
                "posts_exempt": dict(getattr(f_grade, "exempt", {}) or {}),
                "previous_total": prev_total,
            }
            if fix is not None:
                gym_result["self_fix"] = fix
        else:
            gym_result["forward_book"] = {"total": None, "reason": "no rows"}

        results[gym_id] = gym_result
        print(f"[grade-sweep] {gym_id}: "
              f"trailing={gym_result.get('trailing_30',{}).get('letter','N/A')} "
              f"forward={gym_result.get('forward_book',{}).get('letter','N/A')}")

    # ONE aggregated summary line per run, only when something changed
    # (replaces the per-gym-per-window spam when self-fix is armed).
    if self_fix and (fixed_gyms or alerted_gyms):
        held_txt = ", ".join(held_gyms) if held_gyms else "none"
        alert_fn(f"grade sweep: {len(results)} gyms, "
                 f"{len(fixed_gyms)} self-fixed to A, "
                 f"{len(held_gyms)} held ({held_txt})")

    out = {"ok": True, "gyms": results}
    if self_fix:
        out["self_fixed"] = fixed_gyms
        out["held"] = held_gyms
        # C7 reports only when it is ARMED: flag off leaves the return shape
        # byte for byte what every existing caller already reads.
        if config.grade_stuck_escalation_enabled():
            out["stuck_escalated"] = stuck_gyms
    out["dropped"] = dropped_gyms
    return out


if __name__ == "__main__":
    import sys
    gyms_arg = sys.argv[1:] if len(sys.argv) > 1 else None
    result = run(gyms=gyms_arg)
    print(json.dumps(result, indent=2, default=str))
