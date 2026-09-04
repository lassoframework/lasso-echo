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
  * The READ lane (list / get / bounds, added 2026-09-04) is READ-ONLY by
    construction: it reads story_request + story_render through the gym-scoped store
    and returns what was persisted. It cannot render, stage, approve or publish. It
    exists because the create response was the ONLY place a story's music + overlay
    were ever visible — reopen an older approval and that evidence was gone, since
    nothing ever called the store's get/list readers.
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
      {ok, status, request_id, draft_id?, overlay?, music?, clips?, clip_bounds?,
       hold_reason?}
    asset_ids has NO upper bound: pick as many clips as you like and the composer uses
    as many as fit the total window (clip_bounds.max_used_clips), best-scoring first.
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
    # NO upper cap here, deliberately (Blake 2026-09-04). The old 3-clip ceiling was the
    # portal's, never the engine's: extra clips widen the pool the composer picks the
    # best moments from, and it now fits as many cuts as the total window allows. The
    # only real floor is the template minimum, which plan_compose HOLDS on honestly.

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
    return 200, _staged_payload(res)


def _asset_ids_of(row):
    """The asset_ids a persisted story_request recorded. PostgREST may hand a jsonb
    column back as a real list OR as a JSON string depending on the client, so both are
    accepted; anything else reads as no clips (the caller refuses the rebuild rather
    than re-rendering from a guess)."""
    raw = (row or {}).get("asset_ids")
    if isinstance(raw, str):
        import json as _json
        try:
            raw = _json.loads(raw)
        except Exception:  # noqa: BLE001
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(a) for a in raw if str(a or "").strip()]


def _staged_payload(res):
    """The staged-story response body. ONE builder for create and rebuild: a coach who
    edits their copy must get back exactly the shape they got the first time, or the
    portal has to special-case which call it made."""
    draft = res.get("draft")
    sr = res.get("story_render") or {}
    from . import story_layout as _sl
    from . import story_templates as _st
    used = res.get("used_clips") or []
    requested = res.get("requested_clips") or []
    skipped = res.get("skipped_clips") or []
    return {
        "ok": True, "status": "staged", "request_id": res.get("request_id"),
        "draft_id": getattr(draft, "draft_id", None),
        "overlay": sr.get("overlay_text_final"),
        "overlay_flags": sr.get("overlay_flags"),
        # Clip accounting: which of the coach's picks made the reel, which did not, and
        # why. Once they can hand Echo a dozen clips, "12 selected -> a 10-cut reel"
        # has to be legible or the two clips that lost look like a bug.
        "clips": {
            "requested": len(requested), "used": len(used),
            "used_ids": used,
            "skipped": [{"asset_id": k.get("asset_id"), "reason": k.get("reason")}
                        for k in skipped],
        },
        # The picker's real bounds, read from the templates (one source of truth, not a
        # number retyped in TypeScript).
        "clip_bounds": _st.selection_bounds(),
        # The character budget a portal-side overlay-text editor should mirror
        # (2026-09-01 ruling 1: ONE source of truth, not a hardcoded duplicate
        # in TypeScript). Measured live from the exact font the render burns —
        # see story_layout.py's show-your-work derivation.
        "overlay_char_budget": _sl.MAX_CHARS_PER_LINE,
        "music": {"shelf": sr.get("music_shelf"), "track_id": sr.get("track_id"),
                  "license_ref": sr.get("license_ref")},
    }


# ---- POST /studio/story/<request_id>/rebuild ---------------------------------
# Blake, 2026-09-04, lifting the 2026-09-01 "backlog, it is a convenience" ruling on the
# inline overlay editor. Worth stating what is and is not possible here, because the
# backlog note asked for an "inline TEXT EDITOR" and that cannot exist: the overlay is
# BURNED INTO PIXELS at render time, and nothing persists a pre-burn artifact for a
# Story Studio reel (story_reburn.stamp_source_media is only ever called from the daily
# story lane, and renders land in a temp dir). So there is no image to patch. Changing
# the words means rendering again from the same clips, which is what this does.
#
# The coach's text is NOT trusted verbatim onto the frame: it enters as the `brief`, so
# it flows through story_grounding (source=brief, text kept as written) and then
# story_overlay's copy_gate scrub, the ALL-CAPS layout, the identity anchor and the
# per-gym avatar rail -- the same gauntlet an original render passes. Their words are
# used, subject to the gates, and a breach HOLDS instead of shipping.
def handle_rebuild_story(account_key, request_id, body, actor_id="", *, store=None,
                         candidates=None, assets_by_id=None, analysis=None,
                         music_library=None, render_fn=None, cal_store=None):
    """POST /studio/story/<id>/rebuild — a coach edited the burned copy.

    body: {overlay_text: str, identity_tokens?: [...]}

    Re-renders the SAME clips with the new copy and stages a fresh PENDING draft, then
    denies the old one. Response mirrors create: {ok, status, request_id, overlay, ...}
    plus replaced_request_id.

    ORDER MATTERS: the new story is built FIRST and the old one is denied only once the
    new one is genuinely staged. Denying first would mean a HELD rebuild (a copy_gate
    breach in the coach's own wording, say) left them with no story at all -- their
    original destroyed by an edit that never landed."""
    if not _render_armed(account_key):
        return 403, {"ok": False, "error": "Story Studio is not enabled for this gym"}

    body = body or {}
    text = str(body.get("overlay_text") or "").strip()
    if not text:
        return 400, {"ok": False, "error": "the new text is empty"}
    if len(text) > 300:
        return 400, {"ok": False, "error": "that text is too long for a story overlay"}
    tokens = body.get("identity_tokens") or []
    if not tokens:
        # Same rail as create: Echo will not burn a Story with no identity anchor, and
        # holding for it AFTER re-rendering would waste the work and confuse the coach.
        return 400, {"ok": False,
                     "error": "no identity anchor was sent for this gym; a rebuild "
                              "cannot be branded without one"}

    gym = _base(account_key)
    st = store
    if st is None:
        from . import story_studio_store as _sss
        st = _sss.default_store()
    if not getattr(st, "available", lambda: False)():
        return 503, {"ok": False,
                     "error": "story history is not available in this environment"}

    try:
        original = st.get_request(request_id, gym_id=gym)
    except Exception as e:  # noqa: BLE001
        return 502, {"ok": False, "error": f"story read failed ({type(e).__name__})"}
    if not original:
        # gym-scoped read: another gym's request id is absent, never content.
        return 404, {"ok": False, "error": "no such story for this gym"}
    if str(original.get("status") or "").lower() == "denied":
        return 409, {"ok": False,
                     "error": "that story was already denied; create a new one instead"}

    asset_ids = _asset_ids_of(original)
    if not asset_ids:
        return 409, {"ok": False,
                     "error": "the original story did not record which clips it used, "
                              "so it cannot be rebuilt; create a new one instead"}

    from . import story_studio
    request = {
        "gym_id": gym,
        "account_key": account_key,
        "asset_ids": asset_ids,
        # the edited copy drives the overlay, through every gate (see the note above).
        "brief": text,
        "template": original.get("template"),
        "music_mood": original.get("music_mood"),
        "identity_tokens": list(tokens),
        "requested_by": actor_id or "",
    }
    try:
        res = story_studio.create_story(
            request, candidates=candidates, assets_by_id=assets_by_id,
            analysis=analysis, store=st, music_library=music_library,
            render_fn=render_fn, cal_store=cal_store)
    except Exception as e:  # noqa: BLE001
        return 502, {"ok": False, "error": f"rebuild failed ({type(e).__name__})"}

    status = res.get("status")
    if status == "off":
        return 403, {"ok": False, "error": "Story Studio is not enabled for this gym"}
    if status == "held":
        # The ORIGINAL is untouched and still in the approval queue -- say so, or a coach
        # is left guessing whether they still have a story.
        return 200, {"ok": True, "status": "held", "request_id": res.get("request_id"),
                     "hold_reason": res.get("reason"),
                     "original_kept": True,
                     "replaced_request_id": None}

    # Only now is it safe to retire the old one.
    replaced, deny_error = True, ""
    try:
        story_studio.deny(request_id, gym, reason=f"rebuilt with edited copy by {actor_id or 'a coach'}",
                          store=st, cal_store=cal_store)
    except Exception as e:  # noqa: BLE001 - the NEW story is already staged; never claim
        # the old card is gone when it may not be. Two cards is confusing; a lie is worse.
        replaced, deny_error = False, f"{type(e).__name__}"

    out = _staged_payload(res)
    out["replaced_request_id"] = request_id if replaced else None
    if not replaced:
        out["warning"] = ("the new story is staged, but the old card could not be "
                          f"denied ({deny_error}); deny it by hand so it is not "
                          "approved twice")
    return 200, out


# ---- GET /studio/story (list) ------------------------------------------------
def _render_row(row, request=None):
    """One persisted render, shaped for a client. Only the fields a coach or a LASSO
    operator needs to understand a story that already exists — never the whole row,
    and never a field this build does not actually persist."""
    row = row or {}
    req = request or {}
    seg_plan = row.get("segment_plan") or []
    if isinstance(seg_plan, str):            # PostgREST may hand jsonb back as text
        import json as _json
        try:
            seg_plan = _json.loads(seg_plan)
        except Exception:                    # noqa: BLE001
            seg_plan = []
    from . import story_layout as _sl
    return {
        "request_id": row.get("request_id") or req.get("id"),
        "render_id": row.get("id"),
        "status": row.get("status") or req.get("status"),
        "template": row.get("template") or req.get("template"),
        "created_at": row.get("created_at") or req.get("created_at"),
        "overlay": row.get("overlay_text_final"),
        "overlay_flags": row.get("overlay_flags") or [],
        "overlay_char_budget": _sl.MAX_CHARS_PER_LINE,
        "grounded_from": row.get("grounded_from"),
        "music": {"shelf": row.get("music_shelf"), "track_id": row.get("track_id"),
                  "license_ref": row.get("license_ref")},
        "segments": [{"asset_id": sg.get("asset_id"), "start_ts": sg.get("start_ts"),
                      "end_ts": sg.get("end_ts")} for sg in (seg_plan or [])
                     if isinstance(sg, dict)],
        "clip_count": len([sg for sg in (seg_plan or []) if isinstance(sg, dict)]),
        "calendar_row_id": row.get("calendar_row_id"),
        "brief": req.get("brief"),
        "hold_reason": req.get("hold_reason"),
        "deny_reason": req.get("deny_reason"),
    }


def handle_list_stories(account_key, *, store=None, status=None):
    """GET /studio/story — this gym's Story Studio history, newest first, plus the
    clip-picker bounds. Response: {ok, stories: [...], clip_bounds: {...}}.

    READ-ONLY. The bounds ride along so the picker reads the count Echo can actually
    use from Echo itself instead of retyping it (the same one-source-of-truth rule as
    overlay_char_budget). An unconfigured/absent store is an honest empty list with
    the bounds still answered — the picker must not break because history is."""
    if not _render_armed(account_key):
        return 403, {"ok": False, "error": "Story Studio is not enabled for this gym"}
    from . import story_templates as _st
    bounds = _st.selection_bounds()
    gym = _base(account_key)
    st = store
    if st is None:
        from . import story_studio_store as _sss
        st = _sss.default_store()
    if not getattr(st, "available", lambda: False)():
        return 200, {"ok": True, "stories": [], "clip_bounds": bounds,
                     "note": "story history is not available in this environment"}
    try:
        renders = st.list_renders(gym, status=status)
        requests = {str(r.get("id")): r for r in (st.list_requests(gym) or [])}
    except Exception as e:  # noqa: BLE001 - a read failure is never a crash
        return 502, {"ok": False, "error": f"story history read failed "
                                           f"({type(e).__name__})",
                     "clip_bounds": bounds}
    return 200, {
        "ok": True, "clip_bounds": bounds,
        "stories": [_render_row(r, requests.get(str(r.get("request_id"))))
                    for r in (renders or [])],
    }


# ---- GET /studio/story/<request_id> -----------------------------------------
def handle_get_story(account_key, request_id, *, store=None):
    """GET /studio/story/<id> — one story's persisted detail: the burned overlay, the
    licensed track, the segment plan, the approval row it staged. Response:
      {ok, story: {...}} or 404 when this gym has no such story.

    This is what makes music + overlay readable on an OLD approval rather than only in
    the create response (the 2026-09-01 backlog item). READ-ONLY, gym-scoped: the
    store filters on gym_id, so another gym's request id is a 404, not a leak."""
    if not _render_armed(account_key):
        return 403, {"ok": False, "error": "Story Studio is not enabled for this gym"}
    gym = _base(account_key)
    st = store
    if st is None:
        from . import story_studio_store as _sss
        st = _sss.default_store()
    if not getattr(st, "available", lambda: False)():
        return 503, {"ok": False,
                     "error": "story history is not available in this environment"}
    try:
        req = st.get_request(request_id, gym_id=gym)
        render = st.render_for_request(request_id, gym_id=gym)
    except Exception as e:  # noqa: BLE001
        return 502, {"ok": False, "error": f"story read failed ({type(e).__name__})"}
    if not req and not render:
        return 404, {"ok": False, "error": "no such story for this gym"}
    return 200, {"ok": True, "story": _render_row(render, req)}


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
        out, err = _q.resolve(_base(account_key), asset_id, lane, resolved_by=actor_id)
    except Exception as e:  # noqa: BLE001
        return 502, {"ok": False, "error": f"resolve failed ({type(e).__name__})"}
    if err:
        # Never answer "saved" to a tap that recorded nothing: the coach would move on
        # and the same file would come back unsorted on the next sync.
        return 502, {"ok": False, "lane": out, "error": err}
    return 200, {"ok": True, "lane": out}
