"""
story_studio_routes.py — the Echo API surface for the portal "Create a Story" lane
(ECHO_STORY_STUDIO_BUILD §4). The PORTAL calls these; intake_web resolves the
caller's signed token to an account_key and passes it in, so a gym only ever
touches ITS OWN media (tenant isolation starts at the route boundary).

Handlers return (status, body_dict), exactly like gym_media_routes / portal_routes.

RAILS (never move):
  * The render lane (create / deny) is gated per gym by
    config.story_studio_render_active_for — default OFF, pilot allowlist. A
    disabled lane returns 403 uniformly.
  * The sort queue (list / resolve) is gated by config.story_classifier_enabled
    (default ON) — it only sorts, posts nothing.
  * create_story ALWAYS stages status=PENDING (or HOLDS honestly); this surface
    never approves or publishes. The human approval tap is untouched.
  * deny returns the request's segments to the pool + logs the reason.
  * The footage picker REUSES the gym media pool (gym_media_routes.handle_list_assets)
    — the raw lane is just the gym's media tab, not a new store.
"""
from __future__ import annotations

from . import config
from . import gym_media_selector as _sel


def _base(account_key):
    return _sel.base_gym_key(account_key)


def _render_armed(account_key):
    """The Create-a-Story render lane must be armed for THIS gym (global flag OR the
    pilot allowlist). A disabled lane returns 403 uniformly."""
    return config.story_studio_render_active_for(_base(account_key))


# ---- POST /studio/story ------------------------------------------------------
def handle_create_story(account_key, body, actor_id="", *, candidates=None,
                        assets_by_id=None, analysis=None, store=None,
                        music_library=None, render_fn=None):
    """POST /studio/story — a coach tapped "Create a Story".

    body: {asset_ids: [...], brief?: str, template?: str, music_mood?: 'hype'|
    'chill'|'none', identity_tokens?: [...], ask?: str}

    Renders (or HOLDS) and stages a PENDING draft in the approval queue. Response:
      {ok, status, request_id, draft_id?, overlay?, music?, hold_reason?}
    status is 'staged' (a PENDING draft exists), 'held' (nothing staged, honest
    reason), or 'off' (the lane is not armed for this gym). NEVER publishes."""
    if not _render_armed(account_key):
        return 403, {"ok": False, "error": "Story Studio is not enabled for this gym"}
    from . import story_studio

    body = body or {}
    request = {
        "gym_id": _base(account_key),
        "account_key": account_key,
        "asset_ids": body.get("asset_ids") or [],
        "brief": body.get("brief") or "",
        "template": body.get("template"),
        "music_mood": body.get("music_mood"),
        "identity_tokens": body.get("identity_tokens") or [],
        "ask": body.get("ask") or "",
        "requested_by": actor_id or "",
    }
    if not request["asset_ids"]:
        return 400, {"ok": False, "error": "pick at least one clip from your media pool"}

    try:
        res = story_studio.create_story(
            request, candidates=candidates, assets_by_id=assets_by_id,
            analysis=analysis, store=store, music_library=music_library,
            render_fn=render_fn)
    except Exception as e:  # noqa: BLE001 - a lane failure is a 502, never a crash
        return 502, {"ok": False, "error": f"render failed ({type(e).__name__})"}

    status = res.get("status")
    if status == "off":
        return 403, {"ok": False, "error": "Story Studio is not enabled for this gym"}
    if status == "held":
        # HELD is a normal outcome (missing music asset, too few segments, avatar
        # breach): nothing staged, an honest reason the coach can act on.
        return 200, {"ok": True, "status": "held", "request_id": res.get("request_id"),
                     "hold_reason": res.get("reason")}
    draft = res.get("draft")
    sr = res.get("story_render") or {}
    return 200, {
        "ok": True, "status": "staged", "request_id": res.get("request_id"),
        "draft_id": getattr(draft, "draft_id", None),
        "overlay": sr.get("overlay_text_final"),
        "overlay_flags": sr.get("overlay_flags"),
        "music": {"shelf": sr.get("music_shelf"), "track_id": sr.get("track_id"),
                  "license_ref": sr.get("license_ref")},
    }


# ---- POST /studio/story/<request_id>/deny ------------------------------------
def handle_deny_story(account_key, request_id, reason="", *, store=None):
    """POST /studio/story/<id>/deny — a coach denied a staged Story. Returns its
    segments to the pool + logs the reason (spec §4). Response: {ok, returned}."""
    if not _render_armed(account_key):
        return 403, {"ok": False, "error": "Story Studio is not enabled for this gym"}
    from . import story_studio
    try:
        returned = story_studio.deny(request_id, _base(account_key), reason=reason,
                                     store=store)
    except Exception as e:  # noqa: BLE001
        return 502, {"ok": False, "error": f"deny failed ({type(e).__name__})"}
    return 200, {"ok": True, "returned": bool(returned)}


# ---- GET /studio/sort-queue --------------------------------------------------
def handle_list_sort_queue(account_key):
    """GET /studio/sort-queue — the gym's "Sort these" items (ambiguous media the
    classifier could not confidently call). Response: {items: [{asset_id, reasons,
    thumb_url}]}. Gated by STORY_CLASSIFIER (default ON)."""
    if not config.story_classifier_enabled():
        return 200, {"items": []}
    from . import story_sort_queue as _q
    gym = _base(account_key)
    try:
        items = _q.pending(gym)
    except Exception as e:  # noqa: BLE001
        return 502, {"error": f"sort queue read failed ({type(e).__name__})"}
    return 200, {"items": [
        {"asset_id": it.get("asset_id"), "reasons": it.get("reasons") or [],
         "thumb_url": f"/media/thumb/{it.get('asset_id')}"}
        for it in items]}


# ---- POST /studio/sort-queue/<asset_id>/resolve ------------------------------
def handle_resolve_sort_item(account_key, asset_id, lane, actor_id=""):
    """POST /studio/sort-queue/<asset_id>/resolve {lane} — a coach tapped Raw /
    Finished / Skip. Records the declared lane so the SAME bytes are then a declared
    lane on the next sync (never a re-guess). Response: {ok, lane}."""
    if not config.story_classifier_enabled():
        return 403, {"ok": False, "error": "the classifier lane is not enabled"}
    lane = str(lane or "").strip().lower()
    if lane not in ("raw", "finished", "skip"):
        return 400, {"ok": False, "error": "lane must be raw | finished | skip"}
    from . import story_sort_queue as _q
    try:
        out = _q.resolve(_base(account_key), asset_id, lane, resolved_by=actor_id)
    except Exception as e:  # noqa: BLE001
        return 502, {"ok": False, "error": f"resolve failed ({type(e).__name__})"}
    return 200, {"ok": True, "lane": out}
