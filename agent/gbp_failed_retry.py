"""
gbp_failed_retry.py — requeue and RE-ALERT Google Business rows stuck in failed.

THE DEFECT (AUD-003). A googlebusiness content_calendar row that fails is written to
status='failed' with a reason, alerted ONCE, and then never looked at again. Nothing
retries it and nothing says it again. Verified live 2026-09-05:

    b11ba9c0  lasso                googlebusiness  2026-08-20  "connection routing"
    333f90a3  crossfitnine7f7dadc  googlebusiness  2026-09-03  "photo upload: ZernioError"

Sixteen days for the first one, with nobody told a second time. All 13 GBP connections
read connected and healthy at the time of that check, so the older row's "connection
routing" failure was very likely already fixable and simply nobody knew to look.

WHAT THIS DOES
  requeue     a failed row whose failure is RETRYABLE goes back to status='approved' so
              the ordinary gbp_worker picks it up on its next in-window tick. This module
              never publishes anything itself: publishing is the worker's job and giving
              a second thing the ability to publish is how a gym gets double posted.
  re-alert    a failed row that is NOT retryable is said again on a cadence, so a row
              waiting on a human cannot go quiet for sixteen days.

THE DOUBLE POST RAIL, which is the whole reason this is careful. A row is requeued ONLY
when every one of these holds:
  * account is googlebusiness (this module touches no other lane, ever)
  * status is exactly 'failed'
  * late_post_id is EMPTY. A row carrying a post id may have reached Google, and a
    republish would put the same post in front of the same strangers twice. A row with
    a post id is re-alerted, never requeued, no matter how retryable its reason looks.
  * the failure classifies as retryable (see _classify)
  * it has not already been requeued MAX_ATTEMPTS times

A permanent failure (a rejected image, a bad payload) is NEVER requeued: it would fail
identically forever, burn quota, and bury the real reason under N identical alerts.
A needs_reconnect failure is not requeued either. The fix is a reconnect link to the
owner, and this says so by name.

Behind AGENT_GBP_FAILED_RETRY, default OFF. Nothing here arms itself.

    python -m agent gbp-failed-retry --dry-run
    python -m agent gbp-failed-retry
"""

from datetime import date, datetime, timezone

from . import config

ACCOUNT = "googlebusiness"

#: How many times one row may be requeued before it is left alone and only re-alerted.
#: A row that has failed this many times is not having a transient problem.
MAX_ATTEMPTS = 3

#: Re-alert cadence for a row that cannot be requeued, in days. Daily would be noise on
#: a row waiting for a person; silence for sixteen days is the defect.
REALERT_EVERY_DAYS = 3

#: Reasons Echo itself writes for a PRE PUBLISH failure. Nothing reached Google, so a
#: requeue cannot double post. Matched as a prefix on the stored reject_reason.
_PREPUBLISH_REASONS = (
    "connection routing",
)

CLASS_RETRYABLE = "retryable"
CLASS_PERMANENT = "permanent"
CLASS_RECONNECT = "needs_reconnect"


def _classify(row):
    """(classification, why) for one failed row. Pure.

    Reads the STRUCTURED error first (rows written after the C14 fix carry the status
    code, endpoint and retryability), and falls back to the reason string for older rows
    like the two live ones above, which predate it.
    """
    reason = str(row.get("reject_reason") or "").strip()
    low = reason.lower()

    err = row.get("error")
    if isinstance(err, dict) and err:
        if err.get("needs_reconnect"):
            return CLASS_RECONNECT, reason or "the connection needs reconnecting"
        return (CLASS_RETRYABLE if err.get("retryable") else CLASS_PERMANENT), reason

    if "[reconnect needed]" in low or " 401" in low or " 403" in low:
        return CLASS_RECONNECT, reason
    if "[retryable]" in low:
        return CLASS_RETRYABLE, reason
    if "[permanent]" in low:
        return CLASS_PERMANENT, reason
    for pre in _PREPUBLISH_REASONS:
        if low.startswith(pre):
            return CLASS_RETRYABLE, reason
    # A bare exception CLASS with nothing else (the pre C14 shape, e.g. "photo upload:
    # ZernioError") carries no evidence either way. Treat it as PERMANENT: we would be
    # guessing, and guessing toward a retry is the direction that spends money and can
    # repeat a post. It still gets re-alerted, which is what was missing.
    return CLASS_PERMANENT, reason or "no reason recorded"


def _can_requeue(row):
    """The double post rail. True only when a requeue is provably safe."""
    if str(row.get("account") or "") != ACCOUNT:
        return False, "not a googlebusiness row"
    if str(row.get("status") or "") != "failed":
        return False, "not failed"
    if str(row.get("late_post_id") or "").strip():
        return False, "carries a post id, so it may already be live"
    return True, ""


def plan(rows, attempts=None, last_alert=None, today=None):
    """PURE. What a run WOULD do, as {requeue:[...], realert:[...], skip:[...]}.

    `attempts` is {row_id: int} and `last_alert` is {row_id: 'YYYY-MM-DD'}, both read
    from kv by the caller. Separating the decision from the I/O is what lets the whole
    rail be tested without a network.
    """
    attempts = attempts or {}
    last_alert = last_alert or {}
    today = today or date.today()
    out = {"requeue": [], "realert": [], "skip": []}
    for row in rows or []:
        rid = str(row.get("id") or "")
        if not rid:
            continue
        cls, why = _classify(row)
        safe, blocked = _can_requeue(row)
        tried = int(attempts.get(rid) or 0)
        item = {"id": rid, "gym_id": row.get("gym_id"), "post_date": row.get("post_date"),
                "classification": cls, "reason": why, "attempts": tried}
        if safe and cls == CLASS_RETRYABLE and tried < MAX_ATTEMPTS:
            out["requeue"].append(item)
            continue
        if not safe:
            item["blocked"] = blocked
        elif cls == CLASS_RETRYABLE:
            item["blocked"] = f"already retried {tried} times"
        last = str(last_alert.get(rid) or "")
        due = True
        if last:
            try:
                gap = today.toordinal() - date.fromisoformat(last).toordinal()
                due = gap >= REALERT_EVERY_DAYS
            except ValueError:
                due = True
        (out["realert"] if due else out["skip"]).append(item)
    return out


def _describe(item):
    """The alert line. Copy rules: no dashes of any kind, never the outside supplier word."""
    if item["classification"] == CLASS_RECONNECT:
        tail = ("The Google Business connection needs reconnecting. Send the owner a "
                "connect link.")
    elif item["classification"] == CLASS_RETRYABLE:
        tail = f"Retryable, but it has already been requeued {item['attempts']} times."
    else:
        tail = "This will not fix itself. A person needs to look at it."
    return (f"Google Business post still failed for {item['gym_id']} "
            f"(row {item['id'][:8]}, {item['post_date']}): "
            f"{item['reason'] or 'no reason recorded'}. {tail}")


def run(store=None, kv=None, alert=None, logger=None, dry_run=False, today=None):
    """Sweep failed googlebusiness rows. Never raises out.

    Returns {ok, reason?, requeued, realerted, skipped, dry_run}.
    """
    log = logger or (lambda m: print(f"[gbp-failed-retry] {m}"))
    if not dry_run and not config.gbp_failed_retry_enabled():
        return {"ok": False, "reason": "AGENT_GBP_FAILED_RETRY is off",
                "requeued": 0, "realerted": 0}
    if store is None:
        from . import zernio_routes as _zr
        store = _zr._shared_store()
    if store is None:
        return {"ok": False, "reason": "supabase creds absent on this host",
                "requeued": 0, "realerted": 0}
    if kv is None:
        from . import db
        kv = type("_KV", (), {"get": staticmethod(db.kv_get),
                              "set": staticmethod(db.kv_set)})()
    if alert is None:
        try:
            from .ops_alerts import post as _post
            alert = _post
        except Exception:  # noqa: BLE001
            alert = lambda m: log(m)  # noqa: E731

    try:
        rows = store.failed_gbp_rows()
    except Exception as exc:  # noqa: BLE001
        log(f"read failed ({type(exc).__name__}); did nothing")
        return {"ok": False, "reason": f"read failed: {type(exc).__name__}",
                "requeued": 0, "realerted": 0}

    def _kv(key, default=""):
        try:
            return kv.get(key, default)
        except Exception:  # noqa: BLE001
            return default

    attempts = {str(r.get("id")): int(_kv(f"gbp_retry_attempts_{r.get('id')}", 0) or 0)
                for r in rows}
    last_alert = {str(r.get("id")): _kv(f"gbp_retry_alerted_{r.get('id')}", "")
                  for r in rows}
    p = plan(rows, attempts=attempts, last_alert=last_alert, today=today)

    if dry_run:
        log(f"DRY RUN: would requeue {len(p['requeue'])}, re-alert {len(p['realert'])}, "
            f"skip {len(p['skip'])}")
        return {"ok": True, "requeued": 0, "realerted": 0,
                "skipped": len(p["skip"]), "plan": p, "dry_run": True}

    requeued = 0
    for item in p["requeue"]:
        try:
            store.requeue_failed_row(item["id"])
        except Exception as exc:  # noqa: BLE001 - one row never blocks the sweep
            log(f"requeue failed for {item['id'][:8]}: {type(exc).__name__}")
            continue
        requeued += 1
        try:
            kv.set(f"gbp_retry_attempts_{item['id']}", str(item["attempts"] + 1))
        except Exception:  # noqa: BLE001
            pass
        log(f"requeued {item['id'][:8]} for {item['gym_id']} "
            f"(attempt {item['attempts'] + 1} of {MAX_ATTEMPTS})")

    stamp = str(today or date.today())
    realerted = 0
    for item in p["realert"]:
        try:
            alert(_describe(item))
            realerted += 1
            kv.set(f"gbp_retry_alerted_{item['id']}", stamp)
        except Exception:  # noqa: BLE001
            pass
    log(f"requeued {requeued}, re-alerted {realerted}, quiet {len(p['skip'])}")
    return {"ok": True, "requeued": requeued, "realerted": realerted,
            "skipped": len(p["skip"]), "dry_run": False}
