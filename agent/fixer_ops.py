"""
fixer_ops.py -- the FIXER's ops-action lane (D72, Blake 2026-09-11).

"Any message someone sends Echo, Ranger, Wrangler, or Scout is addressed, and if it needs
a fix it goes to Claude Code (the FIXER) -- autonomous, without me." The FIXER
(scout-listener, on the Mac) triages support_tickets and drafts answers; this module gives
it HANDS: a small, closed catalog of operator actions Echo already knew how to do by hand
(a connect-link resend, a budget reset, a photo swap, a re-stage), each a thin, idempotent
wrapper over the existing function, reachable over HTTP with a shared secret.

CONTRACT (the FIXER builder reads this block):

  POST /ops/actions/<action>
      headers   X-Fixer-Ops-Secret: <env FIXER_OPS_SECRET>   (constant-time compare)
                Content-Type: application/json
      body      {"gym_key": "<echo account key>", "ticket_id": "<support_tickets.id>",
                 "args": {...}}                              (args per action, below)
      200 {"ok": true, "action": ..., "gym_key": ..., "ticket_id": ..., "result": {...}}
      202 {"ok": true, "action": "restage_month", "job_id": "...", "status": "running", ...}
      400 {"error": "bad_request", "detail": ...}          malformed body / bad args
      401 {"error": "unauthorized"}                          missing or wrong secret
      403 {"error": "org_floor", ...}                        a refused action (see below)
      404 {"error": "unknown_action" | "gym_not_found" | "row_not_found", ...}
      409 {"error": ..., ...}                                the wrapped function refused
      503 {"error": "ops_secret_unset" | "volume_unavailable" | "store_unavailable", ...}
  GET  /ops/actions/jobs/<job_id>          (same header)
      200 {"ok": true, "job": {"id", "action", "gym_key", "ticket_id", "status":
           "running"|"done"|"failed"|"timed_out", "started_at", "finished_at",
           "result"|"error", "steps": [...]}}
      404 {"error": "unknown_job"}
  GET  /ops/actions                        (same header)  -> the catalog, as JSON

  Catalog (`args` keys):
    resend_connect_link     {}                 owner DM with a fresh connect link (forced)
    reset_recreate_budget   {}                 zero this gym's month deny/recreate spend
    release_denied_assets   {}                 gym_media_selector.observe_denials sweep
    swap_media              {"row_id": ...}    portal_social.handle_swap_media (free swap)
    requeue_failed_row      {"row_id": ...}    calendar row failed -> approved (GBP lane)
    restage_month           {"days": <=31, "start_date"?: "YYYY-MM-DD",
                             "render_budget"?: <=60}   background job: prerender ->
                             observe_denials -> build_client_month (PR #98 recipe)

  NOT in the catalog, 403 org_floor by name: anything billing/Stripe, pixel/CAPI, ad
  budget, targeting, or deleting a published post. Unknown names are 404, never run.

  Every accepted call writes a support_messages SYSTEM row on the ticket
  ("OPS ACTION <name> by fixer: <summary>", kind escalation so the portal hides it from the
  client, delivery_status null so no outbox posts it) and prints one AUDIT log line.

WHERE IT RUNS. Two Echo services share this repo: `echo-intake-web` (R2 only, NO /data
volume) and the `echo` worker (the volume: content_library, brand_voice, the sqlite kv
that gym_media_use and the recreate budget live in). intake_web mounts these routes on
the web service; connect_web mounts the same routes inside the worker (it already serves
HTTP there on AGENT_CONNECT_PORT). Actions that need the volume (release_denied_assets,
restage_month) answer 503 volume_unavailable on a host without it and say which host to
call instead, rather than pretending. `python -m agent ops-action ...` runs the same
catalog from a shell on the worker.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

SECRET_ENV = "FIXER_OPS_SECRET"
HEADER = "X-Fixer-Ops-Secret"
ACTOR = "fixer"
ROUTE_PREFIX = "/ops/actions"
MAX_BODY_BYTES = 64 * 1024
MAX_DAYS = 31
MAX_RENDER_BUDGET = 60
DEFAULT_RENDER_BUDGET = 60
RESTAGE_DEADLINE_SEC = 3 * 3600   # PR #98: 56 clips is 15-45 min; hard ceiling ~3h
MAX_JOBS_KEPT = 200

_GYM_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,79}$")
_ROW_ID = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
_TICKET_ID = re.compile(r"^[A-Za-z0-9_-]{4,80}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Named so a FIXER that asks for one of these gets a 403 that says WHY, not a 404 that
# reads like a typo. Nothing here has an implementation and nothing here may get one
# without Blake's explicit approval (org instructions: billing, pixel/CAPI, budget over
# 20%, targeting over 3 ad sets; plus deleting anything already published).
ORG_FLOOR_ACTIONS = frozenset({
    "charge_card", "refund", "issue_refund", "change_billing", "update_billing",
    "cancel_subscription", "update_stripe", "change_price", "set_price",
    "set_pixel", "update_pixel", "update_capi", "set_capi", "change_ad_budget",
    "set_ad_budget", "increase_budget", "decrease_budget", "change_targeting",
    "set_targeting", "update_audience", "delete_published_post", "delete_post",
    "unpublish_post", "remove_post",
})


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------------------

def secret_configured():
    return bool(os.environ.get(SECRET_ENV, "").strip())


def authorize(headers_get):
    """(status, body) when the request must be refused, else None. 503 when the secret is
    not configured on this host (nothing can authenticate; say so), 401 otherwise."""
    secret = os.environ.get(SECRET_ENV, "").strip()
    if not secret:
        return 503, {"error": "ops_secret_unset",
                     "detail": f"{SECRET_ENV} is not set on this service"}
    supplied = str(headers_get(HEADER, "") or headers_get(HEADER.lower(), "") or "").strip()
    if not supplied or not hmac.compare_digest(supplied.encode("utf-8"),
                                               secret.encode("utf-8")):
        return 401, {"error": "unauthorized"}
    return None


# --------------------------------------------------------------------------------------
# the catalog
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionSpec:
    name: str
    run: object                    # (ctx) -> (status, result_dict)
    needs_volume: bool = False
    background: bool = False
    args_schema: dict = field(default_factory=dict)
    description: str = ""


@dataclass
class Ctx:
    gym_key: str
    ticket_id: str
    args: dict
    deps: dict                     # injectables (tests): see _default_deps
    log: object = print


def _default_deps():
    return {}


def _dep(ctx, name, default_factory):
    if name in ctx.deps and ctx.deps[name] is not None:
        return ctx.deps[name]
    return default_factory()


def volume_available():
    """True when this process has the worker's data volume (the sqlite kv, the media
    libraries, the brand bibles). db.kv_is_durable is the existing single test for it."""
    try:
        from . import db as _db
        return bool(_db.kv_is_durable())
    except Exception:  # noqa: BLE001
        return False


# -- resend_connect_link -----------------------------------------------------------------

def _gym_row_for(ctx):
    """(gym_id, gym_name) for an Echo account key from the portal's own tables, or
    (None, None). Injectable as deps['gym_lookup'] -> (gym_id, name)."""
    lookup = ctx.deps.get("gym_lookup")
    if lookup is not None:
        return lookup(ctx.gym_key)
    from . import intake_web as _iw
    row = _iw._supabase_token_gym(ctx.gym_key)
    if not row or not row.get("gym_id"):
        return None, None
    gym_id = str(row["gym_id"])
    name = ""
    try:
        from .slack_convo.bus import Bus
        rows = Bus()._get("gyms", {"id": f"eq.{gym_id}", "select": "name", "limit": "1"})
        name = str((rows or [{}])[0].get("name") or "").strip()
    except Exception:  # noqa: BLE001 - a name miss falls back to the key below
        name = ""
    return gym_id, (name or ctx.gym_key)


def _run_resend_connect_link(ctx):
    gym_id, name = _gym_row_for(ctx)
    if not gym_id:
        return 404, {"error": "gym_not_found", "gym_key": ctx.gym_key}
    notify = _dep(ctx, "notify_new_gym", lambda: __import__(
        "agent.connect_link_notify", fromlist=["notify_new_gym"]).notify_new_gym)
    alerts = []
    sent = bool(notify(ctx.gym_key, gym_id, name, force=True, alert=alerts.append))
    if sent:
        return 200, {"sent": True, "gym_id": gym_id, "gym_name": name,
                     "summary": f"connect link re-sent to the owner of {name}"}
    return 409, {"error": "not_sent", "sent": False, "gym_id": gym_id, "gym_name": name,
                 "detail": (alerts[-1] if alerts else "notify_new_gym declined to send"),
                 "summary": "connect link NOT sent: " + (alerts[-1] if alerts else "declined")}


# -- reset_recreate_budget ---------------------------------------------------------------

def _run_reset_recreate_budget(ctx):
    reset = _dep(ctx, "reset_recreate_budget", lambda: __import__(
        "agent.portal_social", fromlist=["reset_recreate_budget"]).reset_recreate_budget)
    out = reset(ctx.gym_key)
    before = (out or {}).get("before") or {}
    after = (out or {}).get("after") or {}
    return 200, {**(out or {}),
                 "summary": (f"recreate budget {before.get('used', '?')} used -> "
                             f"{after.get('used', '?')} used "
                             f"({after.get('remaining', '?')} of {after.get('limit', '?')} left)")}


# -- release_denied_assets ---------------------------------------------------------------

def _run_release_denied_assets(ctx):
    observe = _dep(ctx, "observe_denials", lambda: __import__(
        "agent.gym_media_selector", fromlist=["observe_denials"]).observe_denials)
    out = observe() or {}
    return 200, {**out, "summary": (f"deny sweep checked {out.get('checked', 0)} date(s), "
                                    f"rolled back {out.get('rolled_back', 0)} asset(s)")}


# -- swap_media --------------------------------------------------------------------------

def _row_id(ctx):
    rid = str((ctx.args or {}).get("row_id") or "").strip()
    if not _ROW_ID.match(rid):
        return None
    return rid


def _run_swap_media(ctx):
    rid = _row_id(ctx)
    if not rid:
        return 400, {"error": "bad_request", "detail": "args.row_id required"}
    handler = _dep(ctx, "handle_swap_media", lambda: __import__(
        "agent.portal_social", fromlist=["handle_swap_media"]).handle_swap_media)
    status, body = handler(ctx.gym_key, rid, f"{ACTOR}:{ctx.ticket_id}")
    body = dict(body or {})
    body["summary"] = (f"swap-media on row {rid}: "
                       + ("ok" if body.get("ok") else f"refused ({body.get('error', status)})"))
    return int(status), body


# -- requeue_failed_row ------------------------------------------------------------------

def _run_requeue_failed_row(ctx):
    rid = _row_id(ctx)
    if not rid:
        return 400, {"error": "bad_request", "detail": "args.row_id required"}
    store = _dep(ctx, "calendar_store", lambda: __import__(
        "agent.portal_calendar_store", fromlist=["SupabaseCalendarStore"]).SupabaseCalendarStore())
    # Ownership first: get_row is gym scoped, so a row on another gym can never load here.
    row = store.get_row(ctx.gym_key, rid)
    if row is None:
        return 404, {"error": "row_not_found", "row_id": rid, "gym_key": ctx.gym_key}
    if str(row.get("status") or "").lower() != "failed":
        return 409, {"error": "row_not_failed", "row_id": rid,
                     "status": row.get("status"),
                     "summary": f"row {rid} is {row.get('status')!r}, not failed; nothing changed"}
    updated = store.requeue_failed_row(rid)
    if updated is None:
        return 409, {"error": "requeue_matched_nothing", "row_id": rid,
                     "summary": (f"row {rid} did not match failed + googlebusiness + no post "
                                 "id at write time; nothing changed")}
    return 200, {"row_id": rid, "status": updated.get("status"),
                 "post_date": updated.get("post_date"),
                 "summary": f"row {rid} requeued: failed -> {updated.get('status')}"}


# -- restage_month (background) -----------------------------------------------------------

def _restage_args(ctx):
    a = ctx.args or {}
    try:
        days = int(a.get("days", 21))
    except (TypeError, ValueError):
        return None, "args.days must be an integer"
    if not 1 <= days <= MAX_DAYS:
        return None, f"args.days must be 1..{MAX_DAYS}"
    start = str(a.get("start_date") or "").strip()
    if start and not _DATE.match(start):
        return None, "args.start_date must be YYYY-MM-DD"
    try:
        budget = int(a.get("render_budget", DEFAULT_RENDER_BUDGET))
    except (TypeError, ValueError):
        return None, "args.render_budget must be an integer"
    if not 0 <= budget <= MAX_RENDER_BUDGET:
        return None, f"args.render_budget must be 0..{MAX_RENDER_BUDGET}"
    return {"days": days, "start_date": start, "render_budget": budget}, ""


def run_restage_month(gym_key, *, days=21, start_date="", render_budget=DEFAULT_RENDER_BUDGET,
                      deps=None, log=print, steps=None):
    """The PR #98 post-deploy recipe as one function: (0) prerender every active Drive
    source filed under this gym (stale keys remapped), (1) observe_denials, (2)
    build_client_month for `days` from `start_date` (today). Returns a summary; each step's
    outcome is appended to `steps` as it finishes so a poller sees progress."""
    deps = deps or {}
    steps = steps if steps is not None else []
    from datetime import date as _date
    start = start_date or _date.today().isoformat()
    out = {"gym_key": gym_key, "days": days, "start_date": start,
           "render_budget": render_budget}

    # 0. prerender
    if "sync_sources" in deps:
        prerender = deps["sync_sources"](gym_key, render_budget)
    else:
        from . import gym_media_index as _idx, gym_media_routes as _gmr
        from .jobs import sync_gym_media as _sync
        store = _idx.default_store()
        prerender = []
        for src in store.list_sources():
            if str(src.get("kind", "gym_drive")) != "gym_drive":
                continue
            resolved = _gmr._resolve_stale_fingerprint(src.get("gym_id"))
            if resolved != gym_key:
                continue
            src = dict(src)
            src["gym_id"] = resolved
            prerender.append(_sync.sync_source(src, store=store, log=log,
                                               render_budget=render_budget))
    out["prerender"] = prerender
    steps.append({"step": "prerender", "at": _now_iso(),
                  "sources": len(prerender) if isinstance(prerender, list) else None})

    # 1. release denied assets
    observe = deps.get("observe_denials") or __import__(
        "agent.gym_media_selector", fromlist=["observe_denials"]).observe_denials
    out["observe_denials"] = observe() or {}
    steps.append({"step": "observe_denials", "at": _now_iso(), **out["observe_denials"]})

    # 2. build
    if "build_month" in deps:
        built = deps["build_month"](gym_key, start, days)
    else:
        from . import client_media_sync as cms
        from .accounts import get_account
        from .voice import load_voice
        from .client_month_run import build_client_month
        account = get_account(f"{gym_key}_ig")
        if account is None:
            raise LookupError(f"no generation account {gym_key}_ig")
        voice = load_voice(cms._resolve_client_voice_path(gym_key, account.voice_doc_path()))
        if voice is None:
            raise LookupError(f"no brand voice doc for {gym_key}")
        store = cms._default_store()
        if store is None:
            raise LookupError("portal calendar store unavailable (Supabase plane off)")
        built = build_client_month(account, gym_key, start, days, voice=voice,
                                   library_path=cms._library_dir(gym_key), store=store,
                                   banned_words=cms._banned_words_for(gym_key), logger=log)
    out["build"] = built
    steps.append({"step": "build", "at": _now_iso(), "ok": bool((built or {}).get("ok")),
                  "upserted": (built or {}).get("upserted")})
    out["summary"] = (f"restage {gym_key}: {len(prerender) if isinstance(prerender, list) else '?'}"
                      f" source(s) prerendered, {out['observe_denials'].get('rolled_back', 0)} "
                      f"asset(s) released, build ok={bool((built or {}).get('ok'))} "
                      f"upserted={(built or {}).get('upserted', '?')} days={days}")
    return out


class Jobs:
    """In-memory job registry for background actions. One per process; bounded."""

    def __init__(self, keep=MAX_JOBS_KEPT):
        self._lock = threading.Lock()
        self._jobs = {}
        self._keep = keep

    def create(self, *, action, gym_key, ticket_id, deadline_sec):
        job = {"id": uuid.uuid4().hex, "action": action, "gym_key": gym_key,
               "ticket_id": ticket_id, "status": "running", "started_at": _now_iso(),
               "finished_at": None, "result": None, "error": None, "steps": [],
               "deadline_sec": deadline_sec}
        with self._lock:
            self._jobs[job["id"]] = job
            if len(self._jobs) > self._keep:
                oldest = sorted(self._jobs.values(), key=lambda j: j["started_at"])
                for j in oldest[: len(self._jobs) - self._keep]:
                    if j["status"] != "running":
                        self._jobs.pop(j["id"], None)
        return job

    def finish(self, job_id, *, result=None, error=None):
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return
            j["finished_at"] = _now_iso()
            j["status"] = "failed" if error else "done"
            j["result"], j["error"] = result, error

    def get(self, job_id, now=None):
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return None
            j = dict(j)
        if j["status"] == "running":
            try:
                started = datetime.fromisoformat(j["started_at"])
                age = ((now or datetime.now(timezone.utc)) - started).total_seconds()
                if age > j.get("deadline_sec", RESTAGE_DEADLINE_SEC):
                    j["status"] = "timed_out"
            except Exception:  # noqa: BLE001
                pass
        return j


JOBS = Jobs()


def _run_restage_month(ctx):
    parsed, err = _restage_args(ctx)
    if err:
        return 400, {"error": "bad_request", "detail": err}
    jobs = _dep(ctx, "jobs", lambda: JOBS)
    job = jobs.create(action="restage_month", gym_key=ctx.gym_key, ticket_id=ctx.ticket_id,
                      deadline_sec=RESTAGE_DEADLINE_SEC)
    bus = ctx.deps.get("bus")

    def _work():
        try:
            result = run_restage_month(ctx.gym_key, days=parsed["days"],
                                       start_date=parsed["start_date"],
                                       render_budget=parsed["render_budget"],
                                       deps=ctx.deps, log=ctx.log, steps=job["steps"])
            jobs.finish(job["id"], result=result)
            _ticket_note(bus, ctx.ticket_id, "restage_month",
                         f"job {job['id']} done: {result.get('summary', '')}", ctx.log)
        except Exception as e:  # noqa: BLE001 - the job records its own failure
            jobs.finish(job["id"], error=f"{type(e).__name__}: {e}")
            _ticket_note(bus, ctx.ticket_id, "restage_month",
                         f"job {job['id']} FAILED: {type(e).__name__}: {e}", ctx.log)

    runner = ctx.deps.get("thread_runner")
    if runner is not None:
        runner(_work)
    else:
        threading.Thread(target=_work, daemon=True, name=f"fixer-ops-{job['id'][:8]}").start()
    return 202, {"job_id": job["id"], "status": "running",
                 "summary": (f"restage_month started as job {job['id']} "
                             f"(days={parsed['days']}, start={parsed['start_date'] or 'today'}, "
                             f"render_budget={parsed['render_budget']}); poll "
                             f"GET {ROUTE_PREFIX}/jobs/{job['id']}")}


CATALOG = {
    "resend_connect_link": ActionSpec(
        "resend_connect_link", _run_resend_connect_link,
        description="DM the gym owner a fresh connect link (owner from the portal's records)"),
    "reset_recreate_budget": ActionSpec(
        "reset_recreate_budget", _run_reset_recreate_budget,
        description="zero this gym's month deny/recreate spend"),
    "release_denied_assets": ActionSpec(
        "release_denied_assets", _run_release_denied_assets, needs_volume=True,
        description="roll portal-denied Drive assets back into the pool (observe_denials)"),
    "swap_media": ActionSpec(
        "swap_media", _run_swap_media, args_schema={"row_id": "content_calendar row id"},
        description="free photo/video swap on one pending row (siblings move with it)"),
    "requeue_failed_row": ActionSpec(
        "requeue_failed_row", _run_requeue_failed_row,
        args_schema={"row_id": "content_calendar row id"},
        description="move ONE failed googlebusiness row back to approved"),
    "restage_month": ActionSpec(
        "restage_month", _run_restage_month, needs_volume=True, background=True,
        args_schema={"days": f"1..{MAX_DAYS} (default 21)", "start_date": "YYYY-MM-DD (today)",
                     "render_budget": f"0..{MAX_RENDER_BUDGET} (default {DEFAULT_RENDER_BUDGET})"},
        description="prerender -> observe_denials -> build_client_month, as a background job"),
}


def catalog_json():
    return {"ok": True, "actions": [
        {"name": s.name, "args": s.args_schema, "needs_volume": s.needs_volume,
         "background": s.background, "description": s.description}
        for s in CATALOG.values()],
        "org_floor": sorted(ORG_FLOOR_ACTIONS), "volume_available": volume_available()}


# --------------------------------------------------------------------------------------
# the ticket record + audit line
# --------------------------------------------------------------------------------------

def _ticket_note(bus, ticket_id, action, summary, log=print):
    """One support_messages SYSTEM row on the ticket, client-invisible (kind escalation is
    on the portal's denylist), delivery_status null so no outbox ever posts it -- it is a
    record, not a message. Never raises: the action already happened."""
    line = f"OPS ACTION {action} by {ACTOR}: {summary}"
    try:
        if bus is None:
            from .slack_convo.bus import Bus
            bus = Bus()
        bus.record_outbound(ticket_id=ticket_id, author_type="system", body=line[:8000],
                            delivery_status=None, kind="escalation",
                            meta={"ops_action": action, "actor": ACTOR, "record_only": True,
                                  "at": _now_iso()})
        return True
    except Exception as e:  # noqa: BLE001
        log(f"[fixer-ops] ticket note failed ticket={ticket_id}: {type(e).__name__}: {e}")
        return False


def _audit(action, gym_key, ticket_id, status, summary, log=print):
    log(f"[fixer-ops] AUDIT action={action} gym={gym_key} ticket={ticket_id} actor={ACTOR} "
        f"status={status} at={_now_iso()} summary={summary!r}")


# --------------------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------------------

def run_action(action, gym_key, ticket_id, args, *, deps=None, log=print):
    """Validate, run, record. Returns (status, body). Auth is the transport's job."""
    deps = dict(deps or {})
    action = str(action or "").strip()
    if action in ORG_FLOOR_ACTIONS:
        _audit(action, gym_key, ticket_id, 403, "refused: org floor", log)
        return 403, {"error": "org_floor", "action": action,
                     "detail": ("billing/Stripe, pixel/CAPI, ad budget, targeting and "
                                "deleting published posts are never automated; a person "
                                "with explicit approval does these")}
    spec = CATALOG.get(action)
    if spec is None:
        return 404, {"error": "unknown_action", "action": action,
                     "known": sorted(CATALOG)}
    gym_key = str(gym_key or "").strip()
    ticket_id = str(ticket_id or "").strip()
    if not _GYM_KEY.match(gym_key):
        return 400, {"error": "bad_request", "detail": "gym_key required (account key)"}
    if not _TICKET_ID.match(ticket_id):
        return 400, {"error": "bad_request", "detail": "ticket_id required"}
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return 400, {"error": "bad_request", "detail": "args must be an object"}
    if spec.needs_volume:
        has_volume = deps["volume_available"]() if "volume_available" in deps \
            else volume_available()
        if not has_volume:
            return 503, {"error": "volume_unavailable", "action": action,
                         "detail": (f"{action} needs the worker's data volume (/data); this "
                                    "host has none. Call the same route on the echo worker "
                                    "(connect_web, AGENT_CONNECT_PORT) or run `python -m "
                                    f"agent ops-action {action} ...` there.")}
    ctx = Ctx(gym_key=gym_key, ticket_id=ticket_id, args=args, deps=deps, log=log)
    try:
        status, result = spec.run(ctx)
    except Exception as e:  # noqa: BLE001 - a wrapped function's fault is a 500 with a name
        status, result = 500, {"error": f"{type(e).__name__}", "detail": str(e)[:300]}
    result = dict(result or {})
    summary = str(result.get("summary") or result.get("error") or status)
    _audit(action, gym_key, ticket_id, status, summary, log)
    if status < 500:
        _ticket_note(deps.get("bus"), ticket_id, action, f"{status} {summary}", log)
    body = {"ok": 200 <= status < 300, "action": action, "gym_key": gym_key,
            "ticket_id": ticket_id}
    if 200 <= status < 300 and not spec.background:
        body["result"] = result
    else:
        body.update(result)
    return status, body


def handle(method, path, headers_get, raw_body=b"", *, deps=None, log=print, now=None):
    """Transport-agnostic router for the two hosts. Returns (status, body_dict), or None
    when `path` is not one of ours (the caller falls through to its own routes)."""
    path = (path or "").split("?")[0].rstrip("/") or "/"
    if path != ROUTE_PREFIX and not path.startswith(ROUTE_PREFIX + "/"):
        return None
    refused = authorize(headers_get)
    if refused:
        return refused
    deps = dict(deps or {})
    method = (method or "").upper()
    if method == "GET" and path == ROUTE_PREFIX:
        return 200, catalog_json()
    m = re.match(rf"^{re.escape(ROUTE_PREFIX)}/jobs/([0-9a-f]{{32}})$", path)
    if m:
        if method != "GET":
            return 405, {"error": "method_not_allowed"}
        jobs = deps.get("jobs") or JOBS
        job = jobs.get(m.group(1), now=now)
        if not job:
            return 404, {"error": "unknown_job"}
        return 200, {"ok": True, "job": job}
    m = re.match(rf"^{re.escape(ROUTE_PREFIX)}/([A-Za-z0-9_]{{1,64}})$", path)
    if not m:
        return 404, {"error": "not_found"}
    if method != "POST":
        return 405, {"error": "method_not_allowed"}
    if raw_body and len(raw_body) > MAX_BODY_BYTES:
        return 413, {"error": "too_large"}
    try:
        body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception:  # noqa: BLE001
        return 400, {"error": "bad_request", "detail": "invalid JSON"}
    if not isinstance(body, dict):
        return 400, {"error": "bad_request", "detail": "body must be an object"}
    return run_action(m.group(1), body.get("gym_key"), body.get("ticket_id"),
                      body.get("args"), deps=deps, log=log)
