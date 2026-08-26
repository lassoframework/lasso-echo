"""grade_sweep.py — nightly per-gym calendar grader.

For each gym:
1. Grade the trailing 30 days (published rows from content_calendar)
2. Grade the forward book (pending/approved future rows)
3. Write both to gym_social_grades
4. Alert coach channel when either grade drops below B (80)

Behind AGENT_CALENDAR_GRADE flag. Run via: python3 -m agent jobs grade_sweep
"""
from __future__ import annotations

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


def _alert_low_grade(gym_id: str, window: str, grade, alert_fn) -> None:
    """Fire one ops alert when a grade drops below B (80)."""
    if grade.total < 80:
        top_defects = [d[2] for d in (grade.defects or [])[:3]]
        alert_fn(
            f"calendar grade sweep: {gym_id} {window} scored "
            f"{grade.total} ({grade.letter}). "
            f"Top defects: {top_defects}. Review the forward book or trailing posts."
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

    if not config.calendar_grade_enabled():
        return {"ok": False, "reason": "AGENT_CALENDAR_GRADE is OFF"}

    from agent.calendar_grade import grade_month
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

    results = {}
    for gym_id in gyms:
        profile = _profile_for(gym_id)
        gym_result = {"gym_id": gym_id, "profile": profile}

        # --- trailing 30 days ---
        trailing_rows = _fetch_rows(store, gym_id, trailing_start, today_str)
        if trailing_rows:
            t_grade = grade_month(trailing_rows, profile=profile)
            _write_grade(store, gym_id, "trailing_30", t_grade)
            _alert_low_grade(gym_id, "trailing_30", t_grade, alert_fn)
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
            f_grade = grade_month(forward_rows, profile=profile)
            _write_grade(store, gym_id, "forward_book", f_grade)
            _alert_low_grade(gym_id, "forward_book", f_grade, alert_fn)
            gym_result["forward_book"] = {
                "total": f_grade.total,
                "letter": f_grade.letter,
                "rows": len(forward_rows),
            }
        else:
            gym_result["forward_book"] = {"total": None, "reason": "no rows"}

        results[gym_id] = gym_result
        print(f"[grade-sweep] {gym_id}: "
              f"trailing={gym_result.get('trailing_30',{}).get('letter','N/A')} "
              f"forward={gym_result.get('forward_book',{}).get('letter','N/A')}")

    return {"ok": True, "gyms": results}


if __name__ == "__main__":
    import sys
    gyms_arg = sys.argv[1:] if len(sys.argv) > 1 else None
    result = run(gyms=gyms_arg)
    print(json.dumps(result, indent=2, default=str))
