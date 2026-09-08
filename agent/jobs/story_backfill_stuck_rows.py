"""
story_backfill_stuck_rows.py — one-time (idempotent, safe-to-rerun) backfill that
corrects story_request/story_render pairs left stuck forever by the pre-fix
_held() bug (PR #86, 2026-09-08, agent/story_studio.py:411-450).

THE BUG (fixed, live): when a story rendered but staging its content_calendar
approval row failed, _held() used to try to RE-INSERT the story_request +
story_render row _persist() had already written for that exact request_id. The
INSERT hit the table's primary key (a PostgREST 409), the surrounding try/except
swallowed it as a log line, and both rows were left stuck at status=pending
forever: no card ever reached the approval queue, no honest reason was recorded
anywhere a coach or LASSO operator could see, and there was zero client-visible
feedback. The code fix makes _held(already_persisted=True) UPDATE those rows
instead of re-inserting; this job is the one-time data correction for rows the
bug already produced before the fix shipped.

STUCK DEFINITION — this is the part that actually matters, and it is NOT just
"story_render.status == 'pending'". A SUCCESSFULLY staged story ALSO leaves
story_render.status == 'pending' forever: nothing in this codebase ever updates
story_render.status to anything but 'denied' (deny(), or the fixed _held()) --
there is no 'approved'/'published' transition for it. A story_render's
calendar_row_id column is ALSO not a signal: story_studio.py:274-275 records the
REAL content_calendar row id in the local KV store (agent.db kv, key
"story_studio_row:{gym}:{request_id}"), not back onto story_render.calendar_row_id
-- so calendar_row_id staying "story_<request_id>" (the fabricated draft id) is
the row's PERMANENT shape whether or not staging ever succeeded.

The one reliable signal is whether a REAL calendar row was ever recorded for the
request: agent.story_studio._stored_calendar_row_id(gym_id, request_id) reads
that KV entry. Empty means the calendar insert never succeeded (or never ran) --
the render can never be approved, published, or denied through the normal
calendar UI; it is permanently invisible. A non-empty value means a real,
client-visible card exists (whether or not a coach has acted on it yet) and the
pair is healthy, NOT stuck.

Confirmed on production 2026-09-08 (see the write-up in the accompanying PR/report):
  - Pete Mongeau / CrossFit Zanshin (8e1b4bdf-...): KV empty -> STUCK (paired
    story_request had already been hand-corrected to 'held'; its story_render was
    missed and still read 'pending').
  - CrossFit Reverb (f75eb466-...): KV empty -> STUCK.
  - ENG (143e10da-...): KV returns a REAL content_calendar row id
    (3b38495c-d5ca-40e1-9204-eedffd5cf937), confirmed via a live SQL read against
    that row (status='pending', a real hosted video, created 2026-09-01) -- this
    is a LEGITIMATE story sitting in ENG's approval queue awaiting a coach's
    review, not a stuck bug row. It was flagged as identical to the other two by
    surface shape alone (pending/pending) and would have been WRONGLY denied had
    this job not distinguished on the real signal instead. Left untouched.

CORRECTION (idempotent -- a re-run makes no further writes once a row is correct):
  story_request.status  -> 'held' (with an honest hold_reason), UNLESS already
                            'held' (an existing hold_reason, e.g. a prior by-hand
                            correction, is never overwritten)
  story_render.status   -> 'denied' -- the exact terminal shape a normal,
                            POST-FIX _held(already_persisted=True) call produces
                            today (story_studio.py:430-435). calendar_row_id is
                            left UNTOUCHED to match: _held() never clears or
                            rewrites it, and nothing downstream reads a denied
                            render's calendar_row_id for a lookup (the only reader
                            is the client-facing list route, which merely echoes
                            it back alongside status='denied' + hold_reason --
                            see agent/story_studio_routes.py:308).

Usage:
  python -m agent.jobs.story_backfill_stuck_rows              # dry run, reports only
  python -m agent.jobs.story_backfill_stuck_rows --apply      # writes the correction
  python -m agent.jobs.story_backfill_stuck_rows --apply eng crossfitreverb30b5b2
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from .. import story_studio_store as sss
from ..story_studio import STATUS_DENIED, STATUS_HELD, STATUS_PENDING

_HOLD_REASON = (
    "the story rendered but could not be added to your approval queue; nothing "
    "was scheduled (corrected by backfill on {date} -- this request previously "
    "showed pending forever with no visible reason)"
)


def _log(msg):
    print(f"[story-backfill] {msg}")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _default_gyms():
    """Every gym Echo runs Story Studio for, LASSO included (mirrors the
    _default_gyms() pattern used by every other cross-gym sweep job, e.g.
    agent/jobs/plan_horizon_sweep.py)."""
    try:
        from ..calendar_autopublish import client_gym_bases
        gyms = list(client_gym_bases() or [])
    except Exception:  # noqa: BLE001 - a registry read failure must not lose LASSO
        gyms = []
    if "lasso" not in gyms:
        gyms.append("lasso")
    return gyms


def _default_calendar_row_lookup(gym_id, request_id):
    from ..story_studio import _stored_calendar_row_id
    return _stored_calendar_row_id(gym_id, request_id)


def find_stuck_pairs(store, gyms, *, calendar_row_lookup=None):
    """Every (gym, story_request, story_render) triple matching the stuck-forever
    shape, across every gym in `gyms`. calendar_row_lookup(gym_id, request_id) ->
    real calendar row id or "" is injectable so tests never touch the live KV
    store or a real Supabase; production runs default to the real
    story_studio._stored_calendar_row_id.

    Returns a list of dicts: {gym_id, request, render}."""
    lookup = calendar_row_lookup or _default_calendar_row_lookup
    found = []
    for gym in gyms:
        candidates, seen_ids = [], set()
        for status in (STATUS_PENDING, STATUS_HELD):
            try:
                rows = store.list_requests(gym, status=status)
            except Exception as e:  # noqa: BLE001
                _log(f"{gym}: list_requests(status={status}) failed: "
                     f"{type(e).__name__}: {e}")
                continue
            for r in rows or []:
                rid = r.get("id")
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    candidates.append(r)
        for req in candidates:
            req_id = req.get("id")
            if not req_id:
                continue
            try:
                render = store.render_for_request(req_id, gym)
            except Exception as e:  # noqa: BLE001
                _log(f"{gym}: render_for_request({req_id}) failed: "
                     f"{type(e).__name__}: {e}")
                continue
            if not render or render.get("status") != STATUS_PENDING:
                continue  # already denied, or no render at all -- not stuck
            real_row = lookup(gym, req_id)
            if real_row:
                # A real, client-visible calendar card exists. Whether or not a
                # coach has acted on it yet, this is a healthy pending review --
                # never touch it.
                continue
            found.append({"gym_id": gym, "request": req, "render": render})
    return found


def correct_pair(store, pair, *, apply=False, now=None):
    """The terminal-state correction for one stuck pair. Returns a before/after
    audit record; makes no writes when the row is already correct (idempotent)."""
    gym, req, render = pair["gym_id"], pair["request"], pair["render"]
    req_id = req.get("id")
    now = now or _now_iso()
    before = {"request_status": req.get("status"), "render_status": render.get("status")}
    after = dict(before)
    request_patch, render_patch = None, None

    if req.get("status") != STATUS_HELD:
        request_patch = {"status": STATUS_HELD,
                          "hold_reason": _HOLD_REASON.format(date=now[:10])}
        after["request_status"] = STATUS_HELD
    if render.get("status") != STATUS_DENIED:
        render_patch = {"status": STATUS_DENIED}
        after["render_status"] = STATUS_DENIED

    changed = bool(request_patch or render_patch)
    if changed and apply:
        if request_patch:
            store.update_request(req_id, request_patch, gym_id=gym)
        if render_patch:
            store.update_render(req_id, render_patch, gym_id=gym)

    return {"gym_id": gym, "request_id": req_id, "changed": changed,
            "before": before, "after": after, "applied": bool(changed and apply)}


def run(gyms=None, *, apply=False, store=None, calendar_row_lookup=None, now=None):
    store = store or sss.default_store()
    gyms = list(gyms) if gyms else _default_gyms()
    pairs = find_stuck_pairs(store, gyms, calendar_row_lookup=calendar_row_lookup)
    results = [correct_pair(store, p, apply=apply, now=now) for p in pairs]
    changed = [r for r in results if r["changed"]]

    _log(f"scanned {len(gyms)} gym(s), found {len(pairs)} stuck pair(s), "
         f"{len(changed)} needed correction "
         f"({'APPLIED' if apply else 'DRY RUN -- pass --apply to write'})")
    for r in results:
        tag = "CHANGED" if r["changed"] else "already correct"
        _log(f"  {r['gym_id']} / {r['request_id']}: {tag} -- "
             f"request {r['before']['request_status']} -> {r['after']['request_status']}, "
             f"render {r['before']['render_status']} -> {r['after']['render_status']}")
    if not pairs:
        _log("  (no stuck pairs found)")
    return results


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                    help="write the correction (default: dry run, report only)")
    p.add_argument("gyms", nargs="*",
                    help="scope to specific gym bases (default: every gym)")
    args = p.parse_args(argv)
    run(gyms=args.gyms or None, apply=args.apply)


if __name__ == "__main__":
    main()
