"""plan_horizon_sweep.py — RETIRE the calendar rows the horizon cap left behind
(audit item 2, 2026-08-31).

THE GAP THIS CLOSES. agent/plan_horizon.py shipped two guards on 2026-08-28:
horizon_clamp (a build may not PLAN past today+31) and belt_filter (a lane may not
STAGE past today+31). Both act on rows being CREATED. Neither has ever looked at a row
that was already sitting there — so every row staged BEFORE the cap existed is beyond
its reach permanently. Three days later the live calendar still carried 68 non-exempt
PENDING rows past the horizon (LASSO: platform 43, doctrine 17, b2b 7, podcast 1, out
to 2026-12-04). They are exactly the churn Blake's rule exists to prevent: the monthly
relearn rebuilds those days before they ever post, so they are pure token waste, and
meanwhile they make the portal's forward book look far fuller than it honestly is.

WHAT IT DOES, per gym: read every row beyond today+plan_horizon_days, split it with
plan_horizon.select_retirable, DELETE the retirable ones, and REPORT the rest.

WHAT IT WILL NOT DO:
  * touch an approved / publishing / published row (a live or promised post),
  * touch a denied / killed / failed row (a decision a human already made),
  * touch an EXEMPT row: anything carrying an event_id (a gym event arc is anchored
    to a real date) or LASSO's own dated lanes (pillar summit / book / welcome —
    the Summit runs Nov 7-8, the book and welcome posts are dated offers).
    Live proof this matters: LASSO holds 25 pending summit rows and CrossFit Zanshin
    5 pending event-anchored offer rows out past the horizon. Both are correct, both
    stay, and the digest names them so "why is that row still there" has an answer.
  * publish, approve, or re-stage anything. It only ever deletes.

Deleting (rather than flipping a status) is the deliberate choice: the rebuild PRESERVES
denied/killed rows and skips their day, so a "retired" status would leave a permanent
hole in the cadence when that month finally comes into the window. A deleted day simply
gets built fresh — which is the whole point of the one-month horizon.

Flag: AGENT_PLAN_HORIZON_SWEEP, default ON (it PREVENTS spend, like the belt).
AGENT_PLAN_HORIZON_DAYS=0 disables the cap and this sweep with it.
Dry-run by default from the CLI; the nightly lane runs it with apply=True.

Usage:
  python -m agent.jobs.plan_horizon_sweep              # dry run, all gyms
  python -m agent.jobs.plan_horizon_sweep --apply
  python -m agent.jobs.plan_horizon_sweep --apply lasso
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from agent import config, plan_horizon

# How far past the horizon to look. Rows further out than this are not a thing any
# lane can produce (the clamp caps every build), so a bounded read is honest and keeps
# the per-gym query to one page.
LOOKAHEAD_DAYS = 400
_CHUNK = 50


def _log(msg):
    print(f"[plan-horizon-sweep] {msg}")


def _default_gyms():
    """Every gym Echo plans for, LASSO included (LASSO is where the live backlog is)."""
    try:
        from agent.calendar_autopublish import client_gym_bases
        gyms = list(client_gym_bases() or [])
    except Exception:  # noqa: BLE001 - a registry read failure must not lose LASSO
        gyms = []
    if "lasso" not in gyms:
        gyms.append("lasso")
    return gyms


def _summarize(rows):
    """{(pillar, status): count} for the digest, so a line names WHAT was retired."""
    out = {}
    for r in rows or []:
        key = (str((r or {}).get("pillar") or "-"),
               str((r or {}).get("status") or "-"))
        out[key] = out.get(key, 0) + 1
    return out


def sweep_gym(base, store, *, apply=False, now=None):
    """One gym: read beyond-horizon rows, retire the retirable, report the rest."""
    end = plan_horizon.horizon_end(now)
    if end is None:
        return {"gym": base, "skipped": "horizon cap disabled"}
    start = (end + timedelta(days=1)).isoformat()
    far = (end + timedelta(days=LOOKAHEAD_DAYS)).isoformat()
    try:
        rows = store.rows_in_range(base, start, far) or []
    except Exception as exc:  # noqa: BLE001 - one gym's read never stops the fleet
        _log(f"{base}: read failed ({type(exc).__name__}); skipped")
        return {"gym": base, "error": type(exc).__name__}

    retire, exempt, protected = plan_horizon.select_retirable(rows, now=now)
    result = {
        "gym": base, "horizon_end": end.isoformat(),
        "beyond_horizon": len(rows),
        "retirable": len(retire), "retired": 0,
        "exempt": len(exempt), "protected": len(protected),
        "breakdown": _summarize(retire),
        "exempt_breakdown": _summarize(exempt),
        "protected_breakdown": _summarize(protected),
        "detail": [],
    }
    if not retire:
        return result

    dates = sorted(str(r.get("post_date") or "")[:10] for r in retire)
    span = dates[0] if len(dates) == 1 else f"{dates[0]} to {dates[-1]}"
    result["span"] = span
    if not apply:
        result["detail"].append(f"{len(retire)} row(s) {span} [dry-run]")
        return result

    ids = [r.get("id") for r in retire if r.get("id")]
    deleted = 0
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i:i + _CHUNK]
        try:
            deleted += store.delete_rows(base, chunk) or 0
        except Exception as exc:  # noqa: BLE001 - a failed chunk is reported, not fatal
            result["detail"].append(
                f"delete failed for {len(chunk)} row(s): {type(exc).__name__}")
    result["retired"] = deleted
    return result


def run(gyms=None, *, apply=False, store=None, now=None, alert=None):
    """The nightly retirement sweep. Behind AGENT_PLAN_HORIZON_SWEEP (default ON);
    a disabled horizon cap (AGENT_PLAN_HORIZON_DAYS=0) also makes it a no-op.

    Returns {"ok", "gyms": [...]}. Fires ONE honest digest alert per run when
    anything was retired (never a line per row, never a line per gym) — the
    belt_filter / flush_needs_media_alerts posture."""
    if not config.plan_horizon_sweep_enabled():
        return {"ok": False, "reason": "AGENT_PLAN_HORIZON_SWEEP is OFF.", "gyms": []}
    if plan_horizon.horizon_end(now) is None:
        return {"ok": False, "reason": "AGENT_PLAN_HORIZON_DAYS=0 (cap disabled).",
                "gyms": []}
    if store is None:
        from agent.portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
    gyms = list(gyms) if gyms else _default_gyms()
    results = [sweep_gym(base, store, apply=apply, now=now) for base in gyms]

    total = sum(r.get("retired", 0) for r in results)
    would = sum(r.get("retirable", 0) for r in results)
    kept_exempt = sum(r.get("exempt", 0) for r in results)
    if would:
        touched = [r for r in results if r.get("retirable")]
        per_gym = ", ".join(
            f"{r['gym']} {r.get('retired', 0) if apply else r['retirable']}"
            f" ({r.get('span', '')})" for r in touched)
        verb = f"retired {total}" if apply else f"would retire {would}"
        msg = (f"plan horizon sweep: {verb} pending row(s) beyond "
               f"today+{config.plan_horizon_days()} — {per_gym}. "
               f"{kept_exempt} dated row(s) (events, LASSO summit/book/welcome) kept "
               "on purpose. Echo relearns monthly, so those days are rebuilt when "
               "they come into the window; nothing approved, live, or published was "
               "touched.")
        _log(msg)
        try:
            (alert or _default_alert)(msg)
        except Exception:  # noqa: BLE001 - alerting never fails the sweep
            pass
    return {"ok": True, "apply": bool(apply), "retired": total,
            "retirable": would, "gyms": results}


def _default_alert(msg):
    try:
        from agent import ops_alerts
        ops_alerts.alert(msg)
    except Exception:  # noqa: BLE001
        pass


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="make the deletes")
    p.add_argument("gyms", nargs="*")
    args = p.parse_args(argv)
    out = run(args.gyms or None, apply=args.apply)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n=== plan_horizon_sweep [{mode}] today={date.today().isoformat()} ===")
    if not out.get("ok"):
        print(out.get("reason"))
        return
    print(f"{'gym':<26}{'beyond':>7}{'retirable':>10}{'retired':>8}"
          f"{'exempt':>7}{'protected':>10}")
    for r in out["gyms"]:
        if r.get("error"):
            print(f"{r['gym']:<26} ERROR {r['error']}")
            continue
        if r.get("skipped"):
            print(f"{r['gym']:<26} SKIPPED {r['skipped']}")
            continue
        print(f"{r['gym']:<26}{r['beyond_horizon']:>7}{r['retirable']:>10}"
              f"{r['retired']:>8}{r['exempt']:>7}{r['protected']:>10}")
    for r in out["gyms"]:
        for label, key in (("retire", "breakdown"), ("exempt", "exempt_breakdown"),
                           ("protected", "protected_breakdown")):
            for (pillar, status), n in sorted((r.get(key) or {}).items()):
                print(f"  {r['gym']}: {label:<9} {pillar}/{status} x{n}")
        for line in r.get("detail") or []:
            print(f"  {r['gym']}: {line}")


if __name__ == "__main__":
    main(sys.argv[1:])
