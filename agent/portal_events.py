"""
portal_events.py — the Echo API for self-serve Events & Promos (EVENT_CAMPAIGNS_BUILD.md §6).

The portal form ("Add an Event or Promo", 5 steps) POSTs here. Each handler is a pure
function returning (status_code, response_dict); the HTTP layer (intake_web.do_POST /
do_GET) resolves the token -> account_key, enforces _origin_ok + the per-token rate
limit, and calls these. Handlers NEVER publish: a create drafts a labeled arc into the
approval queue as PENDING rows; a coach taps through.

Gated per-gym by AGENT_EVENT_CAMPAIGNS (config.event_campaigns_enabled_for). A disabled
gym gets a 404 (indistinguishable from an unknown route). Roles: owner + coach create;
a LASSO coach on-behalf is logged in the event audit. Edits + cancels are audit-rowed.

Endpoints (token already resolved to account_key by the caller):
  POST /portal/<token>/event                 create -> draft arc -> preview
  GET  /portal/<token>/events                list the gym's events
  POST /portal/<token>/event/<id>/edit       edit (re-times the arc, re-stages changed)
  POST /portal/<token>/event/<id>/cancel     cancel (flip pending arc rows denied)
  POST /portal/<token>/event/recur           clone an event with dates blank
"""

from datetime import date

from . import config, gym_event as ge, event_calendar as ec, event_engine as ee


def _flag_off():
    """A disabled gym / feature is a 404, indistinguishable from an unknown route."""
    return 404, {"error": "not found"}


def _stores(store=None, event_store=None):
    """Resolve the calendar + event stores, defaulting to the live Supabase ones when
    the portal calendar data plane is configured. Injectable for tests."""
    if store is None and config.portal_calendar_supabase_enabled():
        from . import portal_calendar_store as _pcs
        store = _pcs.SupabaseCalendarStore()
    if event_store is None and config.portal_calendar_supabase_enabled():
        from . import gym_event_store as _ges
        event_store = _ges.SupabaseGymEventStore()
    return store, event_store


# ---- create -------------------------------------------------------------------

def handle_create_event(account_key, body, *, store=None, event_store=None,
                        today=None, avatar=None):
    """POST /portal/<token>/event. Body (the 5-step form):
      {name, type, starts_on, ends_on, tz?, offer_text?, link?, brief?,
       media_ids?, actor_id?, on_behalf?}
    Validate -> build a gym_event (an OFFER RECORD) -> plan + draft its arc -> stage the
    PENDING arc into the approval queue -> return the drafted arc as a LABELED SET the
    portal previews. Nothing publishes. Gym-scoped: gym_id is FORCED to account_key.

    Returns (201, {event, arc:[...], staged, grade}) on success; (400, {error}) on a
    bad form; (404) when the feature is off for this gym."""
    if not config.event_campaigns_enabled_for(account_key):
        return _flag_off()
    body = dict(body or {})
    # tz defaults to the gym's configured posting tz when the form omits it.
    tz = str(body.get("tz") or "").strip() or config.POSTING_TIMEZONE
    name = str(body.get("name") or "").strip()
    starts_on = str(body.get("starts_on") or "").strip()
    ends_on = str(body.get("ends_on") or "").strip() or starts_on
    actor = str(body.get("actor_id") or "").strip()
    on_behalf = bool(body.get("on_behalf"))
    media_ids = body.get("media_ids") or []
    if isinstance(media_ids, str):
        media_ids = [media_ids]

    try:
        event = ge.GymEvent.from_row({
            "id": ge.make_event_id(account_key, name, starts_on),
            "gym_id": account_key,             # RAIL: forced to the token's gym
            "name": name, "type": str(body.get("type") or "").strip(),
            "starts_on": starts_on, "ends_on": ends_on, "tz": tz,
            "offer_text": str(body.get("offer_text") or "").strip(),
            "link": str(body.get("link") or "").strip(),
            "brief": str(body.get("brief") or "").strip(),
            "media_ids": tuple(str(m) for m in media_ids if str(m).strip()),
            "status": "scheduled",
            "created_by": actor or ("lasso_coach" if on_behalf else "owner"),
        })
    except ValueError as exc:
        return 400, {"error": f"invalid event: {exc}"}

    # Persist the gym_event (the offer record). Audit the create (+ on-behalf).
    audit = [{"action": "create", "actor": actor or "owner",
              "on_behalf": on_behalf, "at": _now_iso()}]
    _store, _estore = _stores(store, event_store)
    if _estore is not None:
        try:
            _estore.upsert_event({**_event_row(event), "audit": audit})
        except Exception as exc:  # noqa: BLE001
            return 502, {"error": f"could not save event: {type(exc).__name__}"}

    # Plan + draft the arc (the ONE engine), then stage it PENDING.
    arc_rows = ee.plan_event_arc(event, today=today, avatar=avatar)
    staged = {"ok": True, "staged": 0}
    if _store is not None:
        staged = ec.stage_arc(_store, event, arc_rows, profile=_profile_for(account_key))

    preview = [_preview(r) for r in arc_rows]
    return 201, {
        "event": _event_row(event),
        "arc": preview,
        "staged": staged.get("staged", 0),
        "held_recap": staged.get("held_recap", 0),
        # Rows held because no photo was available. Surfaced so a coach whose arc
        # stages fewer posts than the preview sees WHY, instead of a silent shortfall.
        "held_media": staged.get("held_media", 0),
        "thinned": staged.get("thinned", 0),
        "ok": bool(staged.get("ok", True)),
        "reason": staged.get("reason", ""),
        "grade": staged.get("grade"),
        "story_studio": ge.story_studio_request(event),   # one-tap story offer
        "label": f"{event.name} arc",
    }


# ---- list ---------------------------------------------------------------------

def handle_list_events(account_key, *, event_store=None):
    """GET /portal/<token>/events. The gym's events (all statuses), newest window
    first. Gym-scoped. (404 when the feature is off.)"""
    if not config.event_campaigns_enabled_for(account_key):
        return _flag_off()
    _, _estore = _stores(None, event_store)
    if _estore is None:
        return 200, {"events": []}
    try:
        rows = _estore.list_events(account_key) or []
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": f"could not list events: {type(exc).__name__}"}
    return 200, {"events": rows}


# ---- edit (re-time) -----------------------------------------------------------

def handle_edit_event(account_key, event_id, body, *, store=None, event_store=None,
                      today=None, avatar=None):
    """POST /portal/<token>/event/<id>/edit. Editing dates RE-TIMES the arc and
    re-stages ONLY changed rows; approved unaffected rows stay approved. Body carries
    the changed fields (starts_on/ends_on/offer_text/link/brief/media_ids). Gym-scoped.

    Returns (200, {event, restaged, kept, removed}) or (404)."""
    if not config.event_campaigns_enabled_for(account_key):
        return _flag_off()
    _store, _estore = _stores(store, event_store)
    if _estore is None:
        return 502, {"error": "event store unavailable"}
    try:
        cur = _estore.get_event(account_key, event_id)
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": f"read failed: {type(exc).__name__}"}
    if not cur:
        return 404, {"error": "not found"}

    merged = dict(cur)
    for k in ("name", "type", "starts_on", "ends_on", "tz", "offer_text",
              "link", "brief", "media_ids"):
        if k in (body or {}):
            merged[k] = body[k]
    try:
        new_event = ge.GymEvent.from_row(merged)
    except ValueError as exc:
        return 400, {"error": f"invalid edit: {exc}"}

    # Read the current arc rows to compute what moved.
    old_arc = []
    if _store is not None and hasattr(_store, "list_event_rows"):
        try:
            old_arc = _store.list_event_rows(account_key, event_id) or []
        except Exception:
            old_arc = []
    restage, keep, remove_keys = ec.retime_arc(old_arc, new_event, today=today,
                                               avatar=avatar)

    # Persist the event with the new dates + an audit row.
    audit = list(cur.get("audit") or [])
    audit.append({"action": "edit", "actor": str((body or {}).get("actor_id") or ""),
                  "at": _now_iso()})
    try:
        _estore.upsert_event({**_event_row(new_event), "audit": audit})
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": f"save failed: {type(exc).__name__}"}

    # Stage only the changed rows (pending); approved unaffected rows are left as-is.
    staged = 0
    held_media = 0
    stage_reason = ""
    if _store is not None and restage:
        res = ec.stage_arc(_store, new_event, restage,
                           profile=_profile_for(account_key))
        staged = res.get("staged", 0)
        held_media = res.get("held_media", 0)
        stage_reason = res.get("reason", "")
    return 200, {"event": _event_row(new_event), "restaged": staged,
                 "held_media": held_media, "reason": stage_reason,
                 "kept": len(keep), "removed": len(remove_keys)}


# ---- cancel -------------------------------------------------------------------

def handle_cancel_event(account_key, event_id, body=None, *, store=None,
                        event_store=None):
    """POST /portal/<token>/event/<id>/cancel. Flip the event cancelled and deny every
    PENDING arc row (reject_reason=event_cancelled); approved/published rows are left.
    Audit-rowed. Gym-scoped. Returns (200, {cancelled, denied}) or (404)."""
    if not config.event_campaigns_enabled_for(account_key):
        return _flag_off()
    _store, _estore = _stores(store, event_store)
    if _estore is None:
        return 502, {"error": "event store unavailable"}
    try:
        cur = _estore.get_event(account_key, event_id)
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": f"read failed: {type(exc).__name__}"}
    if not cur:
        return 404, {"error": "not found"}
    audit = list(cur.get("audit") or [])
    audit.append({"action": "cancel", "actor": str((body or {}).get("actor_id") or ""),
                  "at": _now_iso()})
    try:
        _estore.set_status(account_key, event_id, "cancelled")
        _estore.upsert_event({**cur, "status": "cancelled", "audit": audit})
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": f"cancel failed: {type(exc).__name__}"}
    denied = 0
    if _store is not None:
        res = ec.cancel_event(_store, account_key, event_id, ended=False)
        denied = res.get("denied", 0)
    return 200, {"cancelled": True, "denied": denied}


# ---- recur --------------------------------------------------------------------

def handle_recur_event(account_key, event_id, *, event_store=None):
    """POST /portal/<token>/event/recur. "Run this again": clone an event's form with
    the DATES BLANK (the owner re-picks the window). Returns the pre-filled form the
    portal shows (NOT a new event yet — the owner submits it via /event). Gym-scoped."""
    if not config.event_campaigns_enabled_for(account_key):
        return _flag_off()
    _, _estore = _stores(None, event_store)
    if _estore is None:
        return 502, {"error": "event store unavailable"}
    try:
        cur = _estore.get_event(account_key, event_id)
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": f"read failed: {type(exc).__name__}"}
    if not cur:
        return 404, {"error": "not found"}
    form = {
        "name": cur.get("name", ""), "type": cur.get("type", ""),
        "starts_on": "", "ends_on": "",          # blank — owner re-picks
        "tz": cur.get("tz", ""), "offer_text": cur.get("offer_text", ""),
        "link": cur.get("link", ""), "brief": cur.get("brief", ""),
        "media_ids": cur.get("media_ids", []),
    }
    return 200, {"form": form, "recurred_from": event_id}


# ---- helpers ------------------------------------------------------------------

def _event_row(event: ge.GymEvent):
    """gym_event DB row dict from a GymEvent (media_ids as a list for JSON)."""
    return {
        "id": event.id, "gym_id": event.gym_id, "name": event.name,
        "type": event.type, "starts_on": event.starts_on, "ends_on": event.ends_on,
        "tz": event.tz, "offer_text": event.offer_text, "link": event.link,
        "brief": event.brief, "media_ids": list(event.media_ids),
        "status": event.status, "created_by": event.created_by,
    }


def _preview(row):
    """The labeled-preview shape the approval queue shows for one arc post."""
    return {
        "arc_kind": row.get("arc_kind"),
        "post_date": row.get("post_date"),
        "caption": row.get("caption"),
        "status": row.get("status"),
        "recap_blocked": bool(row.get("recap_blocked")),
        "note": row.get("arc_note", ""),
        "scheduled_at": row.get("scheduled_at"),
    }


def _profile_for(account_key):
    if str(account_key or "").strip().lower() in ("lasso", "lasso_demo", ""):
        return "B2B"
    return "GYM"


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
