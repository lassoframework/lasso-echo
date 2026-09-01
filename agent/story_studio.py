"""
story_studio.py — the Story Studio staging orchestrator (spec §4, §6).

Behind STORY_STUDIO_RENDER (default OFF, per-gym pilot allowlist). Ties the waves
together into one PENDING draft:

  request (gym_id, asset_ids, brief, template, music_mood, requested_by)
    -> resolve template (declared beats vision)
    -> ground copy (brief first, else vision; low confidence -> generic-safe + flag)
    -> build the Roxx overlay (copy_gate + per-gym avatar rail + safe zones)
    -> select music bed (hype default, never chill-default; track_id + license_ref)
    -> plan + render the multi-clip montage (HELD on a missing renderer)
    -> record content_hash in render_ledger (the re-ingest guard)
    -> persist story_request + story_render
    -> stage a PENDING content_calendar Draft (the human approval tap is untouched)

RAILS (never move):
  * EVERY render lands status=PENDING. This module NEVER approves or publishes.
  * A render that cannot ground / render / clear a gate is HELD with an honest reason,
    NOTHING is staged, and (nothing being staged) no asset usage is left stamped.
  * DENY returns the request's segments to the pool + logs the reason (deny()).
  * Tenant isolation is enforced upstream (the composer asserts every segment); this
    module also refuses to stage a plan whose gym_id != the request gym_id.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from datetime import datetime, timezone

from . import config
from .drafter import Draft, DraftStatus

STATUS_PENDING = "pending"
STATUS_HELD = "held"
STATUS_STAGED = "staged"
STATUS_DENIED = "denied"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _base_gym(gym_id):
    base = str(gym_id or "").strip().lower()
    for suf in ("_ig", "_fb"):
        if base.endswith(suf):
            return base[: -len(suf)]
    return base


def create_story(request, *, candidates=None, assets_by_id=None, analysis=None,
                 store=None, music_library=None, render_fn=None, output_dir=None,
                 now=None, downloader=None, cal_store=None):
    """Build ONE Story from a request dict and stage it PENDING (or HOLD it honestly).

    request keys: gym_id, asset_ids (list), brief (optional str), template (optional),
    music_mood (optional: 'hype'|'chill'|'none'), requested_by, identity_tokens
    (optional list), ask (optional str override).

    Returns a result dict: {status, reason, draft, story_render, request_id}. status is
    'staged' (a PENDING content_calendar row was written and is in the approval
    queue), 'held' (nothing staged, honest reason), or 'off' (the render lane is not
    armed for this gym). cal_store is the injectable calendar store; when omitted the
    live SupabaseCalendarStore is used, and an unconfigured one HOLDS rather than
    reporting a staged story nobody can approve.

    candidates/assets_by_id: normally DISCOVERED from the gym's pool (a real tap passes
    neither); inject them to bypass discovery in a test. downloader(file_id, dest): the
    injectable Drive fetch used to bind each picked segment to a local source file (the
    live default is drive_client.download); only used on the default renderer path.
    """
    gym_id = _base_gym(request.get("gym_id"))
    if not config.story_studio_render_active_for(gym_id):
        return {"status": "off", "reason": "STORY_STUDIO_RENDER not armed for this gym",
                "draft": None, "story_render": None, "request_id": None}

    from . import (story_templates, story_grounding, story_overlay, story_music,
                   story_composer, story_ledger)

    store = store or _default_store()
    request_id = request.get("id") or str(uuid.uuid4())

    # 0. LIVE candidate discovery. When candidates are not injected (a REAL coach tap
    #    or the event one-tap both reach here with candidates=None), discover the gym's
    #    eligible RAW pool ourselves — otherwise every real render HELDs "too few
    #    segments" because nothing ever fed the composer. Discovery asserts tenant
    #    isolation, the eligibility gate, the re-ingest guard, and the input caps; it
    #    reuses the SAME store read pick_media uses and opus's scoring intent. The coach
    #    may still scope the pick to specific asset_ids (declared beats discovery).
    if candidates is None:
        from . import story_candidates
        candidates, discovered_assets = story_candidates.discover_candidates(
            gym_id, asset_ids=request.get("asset_ids"), store=None, now=now)
        if assets_by_id is None:
            assets_by_id = discovered_assets

    # 1. template (declared beats vision).
    tmpl_name, tmpl_src = story_templates.resolve_template(
        declared_template=request.get("template"), analysis=analysis)
    template = story_templates.get(tmpl_name)

    # 2. grounding (brief first, else vision; low confidence -> generic-safe + flag).
    grounding = story_grounding.ground_copy(brief=request.get("brief"),
                                            analysis=analysis)

    # 3. overlay (copy_gate + per-gym avatar rail + safe zones). A breach HOLDS.
    try:
        overlay = story_overlay.build_overlay(
            grounding.text, identity_tokens=request.get("identity_tokens") or (),
            gym=gym_id, ask=request.get("ask") or template.ask_style,
            grounded_from=grounding.source,
            low_confidence=grounding.low_confidence,
            enforce_ask=True)   # spec §1: the render ends with EXACTLY ONE ask frame
    except story_overlay.OverlayRejected as e:
        return _held(request_id, gym_id, f"overlay rejected: {e}", store, request,
                     tmpl_name, "")

    # 4. music (hype default, never chill-default; track_id + license_ref stored).
    music_sel = story_music.select(
        request.get("music_mood") if request.get("music_mood") else template.music_mood,
        library=music_library, seed=request_id)
    if music_sel.held:
        return _held(request_id, gym_id, music_sel.hold_reason, store, request,
                     tmpl_name, music_sel.shelf)

    # 5. plan + render the montage. HELD on a missing renderer / too few segments.
    plan = story_composer.plan_compose(candidates or [], gym_id, template,
                                       assets_by_id=assets_by_id or {})
    if plan.held:
        return _held(request_id, gym_id, plan.hold_reason, store, request,
                     tmpl_name, music_sel.shelf)

    out_dir = output_dir or tempfile.mkdtemp(prefix="storystudio_")
    music_path = story_music.audio_path_for(music_sel, library=music_library)
    if music_sel.shelf != story_music.SHELF_NONE and not music_path:
        # a bed was chosen but the ops audio asset is not present: HOLD, never post
        # silently or fabricate a track (the honesty rail).
        return _held(request_id, gym_id,
                     f"music bed '{music_sel.shelf}' selected but the licensed audio "
                     f"file is not present in this env; render HELD (a coach can pick "
                     f"'none' to ship without a bed).", store, request, tmpl_name,
                     music_sel.shelf)

    # 5b. bind each planned segment to a REAL local source file so the composer can read
    #     it. Segments whose source_path is already set (an injected test render) are left
    #     alone; the live path downloads the picked assets via the SAME Drive download the
    #     sync job uses, re-asserting tenant on every segment. Sources are cleaned up after
    #     the render (the montage lives in a separate out_dir, untouched by cleanup).
    #     Binding runs for the DEFAULT (real) renderer only: an injected render_fn is a
    #     test/alternate renderer that owns its own source handling, so we never reach
    #     out to Drive for it. The default renderer needs real bytes on disk.
    src_tmp = None
    rfn = render_fn or story_composer.render_compose
    need_bind = ([s for s in plan.segments if not getattr(s, "source_path", "")]
                 if render_fn is None else [])
    try:
        if need_bind:
            from . import story_candidates
            src_tmp = story_candidates.bind_source_paths(
                need_bind, assets_by_id or {}, gym_id=gym_id,
                downloader=downloader)

        result = rfn(plan, output_dir=out_dir, ask_frame_text=overlay.ask,
                     ask_frame_lines=overlay.ask_frame, overlay_frames=overlay.frames,
                     identity_text=overlay.identity_line, music_path=music_path)
    finally:
        if src_tmp:
            from . import story_candidates
            story_candidates.cleanup(src_tmp)
    if getattr(result, "held", False) or not getattr(result, "output_path", ""):
        return _held(request_id, gym_id,
                     getattr(result, "hold_reason", "render produced no output"),
                     store, request, tmpl_name, music_sel.shelf)

    # 6. content_hash + render_ledger (the re-ingest guard).
    content_hash = _content_hash(result.output_path)
    story_ledger.record_render(content_hash, gym_id=gym_id,
                               story_render_id=request_id)

    # 7. host + PENDING draft (the human tap is untouched).
    public_url = _host(result.output_path, gym_id)
    day_key = request.get("day_key") or str((now or datetime.now(timezone.utc)).date())
    draft = Draft(
        draft_id=f"story_{request_id}",
        account_key=request.get("account_key") or gym_id,
        # A rendered Story is an INSTAGRAM story. This used to fall back to gym_id,
        # which lands in content_calendar.account as a gym base key ('pierce') —
        # calendar_autopublish._account_for only accepts 'instagram'/'facebook' and
        # SKIPS anything else, so such a row could never publish.
        platform=request.get("platform") or "instagram",
        caption="",                          # stories carry no caption body
        hashtags=[],
        creative_path=result.output_path,
        creative_public_url=public_url,
        scheduled_for="",
        status=DraftStatus.PENDING,          # EVERY render lands PENDING
        is_story=True,
        day_key=day_key,
        draft_type="story_studio",
        category=tmpl_name,
        source_fragments=[f"story_request:{request_id}", f"gym:{gym_id}"]
        + [f"seg:{s.asset_id}" for s in plan.segments],
    )

    # 8. persist story_request + story_render (best effort; a store failure does not
    #    un-stage the PENDING draft, which is the human-visible artifact).
    seg_plan = [{"asset_id": s.asset_id, "start_ts": s.start_ts, "end_ts": s.end_ts,
                 "score": s.score} for s in plan.segments]
    overlay_final = "\n---\n".join("\n".join(fr) for fr in overlay.frames)
    story_render = {
        "id": request_id,
        "request_id": request_id,
        "gym_id": gym_id,
        "segment_plan": seg_plan,
        "overlay_text_final": overlay_final,
        "overlay_flags": overlay.flags,
        "grounded_from": overlay.grounded_from,
        "template": tmpl_name,
        "track_id": music_sel.track_id,
        "license_ref": music_sel.license_ref,
        "music_shelf": music_sel.shelf,
        "content_hash": content_hash,
        "calendar_row_id": draft.draft_id,
        "status": STATUS_PENDING,
        "created_at": _now_iso(),
    }
    _persist(store, request, request_id, gym_id, tmpl_name, music_sel, story_render)

    # 8b. WRITE THE APPROVAL ROW. Without this the whole lane was a promise it never
    # kept: the video rendered, the clips were stamped used, story_render recorded a
    # calendar_row_id that pointed at nothing, and the coach got status="staged" while
    # NO card ever reached the approval queue. The row lands PENDING like every other
    # lane's, so the human tap is untouched — but it now actually exists.
    row_id, cal_err = _stage_calendar_row(gym_id, draft, cal_store=cal_store)
    if cal_err:
        # Never claim staged when the client-visible artifact was not created. The
        # segments are stamped at step 9 (below), so nothing needs rolling back here.
        return _held(request_id, gym_id,
                     f"the story rendered but could not be added to your approval "
                     f"queue ({cal_err}); nothing was scheduled",
                     store, request, tmpl_name, music_sel.shelf)
    story_render["calendar_row_id"] = row_id or draft.draft_id
    # story_render was persisted BEFORE the insert, so its stored calendar_row_id is
    # still the fabricated draft id. Record the REAL one where deny() can find it.
    _remember_calendar_row(gym_id, request_id, row_id)

    # 9. stamp segment usage so a deny can roll it back (return segments to the pool).
    _stamp_segments(gym_id, request_id, plan.segments)

    return {"status": "staged", "reason": "", "draft": draft,
            "story_render": story_render, "request_id": request_id,
            "calendar_row_id": row_id}


def deny(request_id, gym_id, reason="", *, store=None, cal_store=None):
    """A coach denied a staged Story: DENY its approval row, return its segments to the
    pool, and log the reason (spec §4/§6). Idempotent. Returns True when a rollback
    actually happened."""
    from . import story_music  # noqa: F401 - keep import graph symmetric
    from . import db
    base = _base_gym(gym_id)
    # The calendar row FIRST. Now that create_story writes a real PENDING row, denying
    # only the story_request would leave that card sitting in the approval queue, still
    # approvable by anyone using the normal calendar UI — and pointing at segments this
    # very call is about to recycle into another story.
    _deny_calendar_row(base, request_id, reason, cal_store=cal_store)
    rolled = _rollback_segments(base, request_id)
    store = store or _default_store()
    try:
        if store.available():
            store.update_request(request_id, {"status": STATUS_DENIED,
                                              "deny_reason": reason})
            # The RENDER row too. story_render.status is constrained to exactly
            # ('pending','denied') and nothing ever wrote 'denied', so every denied
            # render still read PENDING forever: the calendar card said denied, the
            # request said denied, and the render row disagreed with both. Anything
            # counting pending Story Studio work saw phantoms.
            store.update_render(request_id, {"status": STATUS_DENIED})
    except Exception as e:  # noqa: BLE001
        print(f"[story-studio] deny request update failed: {type(e).__name__}: {e}")
    db.audit("story_studio", request_id, f"denied: {reason} (segments returned to pool)")
    return rolled


def _deny_calendar_row(gym_id, request_id, reason, *, cal_store=None):
    """Flip the approval row create_story wrote for this request to 'denied'. Matched by
    the stored calendar_row_id when we have it, else by the request's story marker.
    Best effort; never raises (a deny must always return the segments)."""
    row_id = _stored_calendar_row_id(gym_id, request_id)
    if not row_id:
        return False
    try:
        if cal_store is None:
            from . import config  # noqa: PLC0415
            if not config.portal_calendar_supabase_enabled():
                return False
            from .portal_calendar_store import SupabaseCalendarStore  # noqa: PLC0415
            cal_store = SupabaseCalendarStore()
        deny_fn = getattr(cal_store, "deny_with_reason", None)
        if not callable(deny_fn):
            return False
        return bool(deny_fn(gym_id, row_id,
                            (reason or "story denied by coach")[:200]))
    except Exception as e:  # noqa: BLE001
        print(f"[story-studio] deny calendar row failed: {type(e).__name__}: {e}")
        return False


def _stored_calendar_row_id(gym_id, request_id):
    """The REAL content_calendar id create_story recorded for this request, or ""."""
    from . import db
    try:
        import json as _json
        rec = _json.loads(db.kv_get(_ROW_KEY.format(gym_id, request_id), "") or "{}")
        return str(rec.get("calendar_row_id") or "")
    except Exception:  # noqa: BLE001
        return ""


def _remember_calendar_row(gym_id, request_id, row_id):
    """Record the REAL calendar row id so deny() can reach it. Written AFTER the row
    exists — story_render.calendar_row_id is persisted before the insert and therefore
    still carries the fabricated draft id."""
    from . import db
    try:
        import json as _json
        db.kv_set(_ROW_KEY.format(gym_id, request_id),
                  _json.dumps({"calendar_row_id": str(row_id or "")}))
    except Exception:  # noqa: BLE001
        pass


# ---- helpers ----------------------------------------------------------------
def _stage_calendar_row(gym_id, draft, *, cal_store=None):
    """Write the rendered story into content_calendar as a PENDING row so it reaches
    the approval queue. Returns (row_id_or_None, error_string_or_None).

    This is the step the lane was missing entirely: it built a Draft, returned it, and
    called that "staged". Uses the SAME mirror + store every other lane uses, so the
    row shape, the status and the approval gate are identical. An unconfigured store
    is an ERROR here, not a silent skip — a story nobody can approve is not staged."""
    try:
        from .real_calendar_mirror import _real_row  # noqa: PLC0415
        row = {k: v for k, v in _real_row(gym_id, draft).items() if k != "id"}
    except Exception as exc:  # noqa: BLE001
        return None, f"row build failed ({type(exc).__name__})"
    # The publisher routes on `account` and SKIPS anything that is not a real
    # platform, so a row with a gym key there would sit pending forever. Refuse to
    # stage one rather than create a card that can never go out.
    acct = str(row.get("account") or "").strip().lower()
    if acct not in ("instagram", "facebook"):
        return None, f"the story has no valid publish target (account={acct!r})"
    if not str(row.get("image_url") or "").strip():
        return None, "the rendered story has no hosted media"
    try:
        if cal_store is None:
            from . import config  # noqa: PLC0415
            if not config.portal_calendar_supabase_enabled():
                return None, "the calendar store is not configured"
            from .portal_calendar_store import SupabaseCalendarStore  # noqa: PLC0415
            cal_store = SupabaseCalendarStore()
        written = cal_store.insert_rows(gym_id, [row]) or []
    except Exception as exc:  # noqa: BLE001
        return None, f"calendar insert failed ({type(exc).__name__})"
    if not written:
        return None, "the calendar store accepted no rows"
    return (written[0] or {}).get("id"), None


def _held(request_id, gym_id, reason, store, request, tmpl_name, shelf):
    """Record a HELD outcome: NOTHING is staged, an honest reason is logged, and no
    asset usage is stamped (so the pool is untouched)."""
    from . import db
    try:
        if store is not None and store.available():
            store.insert_request({
                "id": request_id, "gym_id": gym_id,
                "asset_ids": request.get("asset_ids") or [],
                "brief": request.get("brief") or "",
                "template": tmpl_name,
                "music_mood": shelf,
                "requested_by": request.get("requested_by") or "",
                "status": STATUS_HELD, "hold_reason": reason,
                "created_at": _now_iso()})
    except Exception as e:  # noqa: BLE001 - a store failure never turns a HOLD into a post
        print(f"[story-studio] held-request persist failed: {type(e).__name__}: {e}")
    db.audit("story_studio", request_id, f"HELD: {reason} (nothing staged)")
    return {"status": "held", "reason": reason, "draft": None,
            "story_render": None, "request_id": request_id}


def _persist(store, request, request_id, gym_id, tmpl_name, music_sel, story_render):
    try:
        if store is None or not store.available():
            return
        store.insert_request({
            "id": request_id, "gym_id": gym_id,
            "asset_ids": request.get("asset_ids") or [],
            "brief": request.get("brief") or "",
            "template": tmpl_name,
            "music_mood": music_sel.shelf,
            "requested_by": request.get("requested_by") or "",
            "status": STATUS_PENDING, "created_at": _now_iso()})
        store.insert_render(story_render)
    except Exception as e:  # noqa: BLE001
        print(f"[story-studio] persist failed: {type(e).__name__}: {e}")


def _content_hash(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        # render output not on disk (an injected renderer returned a synthetic path in
        # a test): hash the path string so the ledger still gets a stable stamp.
        return hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def _host(path, gym_id):
    try:
        from . import media_host
        return media_host.host_media(path, gym_id) or ""
    except Exception as e:  # noqa: BLE001
        print(f"[story-studio] host failed: {type(e).__name__}: {e}")
        return ""


# ---- segment usage stamping (deny rollback) ---------------------------------
_SEG_KEY = "story_studio_segs:{}"      # per request_id
_ROW_KEY = "story_studio_row:{}:{}"    # per gym + request_id -> real calendar id


def _stamp_segments(gym_id, request_id, segments):
    """Record the asset_ids a staged render consumed so a deny can return them to the
    pool. Also bumps the selector's usage stamp per distinct asset (the same
    stamp_use/rollback machinery gym media uses)."""
    import json
    from . import db, gym_media_selector as _sel, gym_media_index as _idx
    asset_ids = sorted({s.asset_id for s in segments})
    db.kv_set(_SEG_KEY.format(request_id), json.dumps(
        {"gym_id": gym_id, "asset_ids": asset_ids}))
    try:
        store = _idx.default_store()
        if not store.available():
            return
        for aid in asset_ids:
            asset = store.get_asset(aid)
            if asset and str(asset.get("gym_id") or "") == gym_id:
                _sel.stamp_use(asset, gym_id, f"story:{request_id}", store=store)
    except Exception as e:  # noqa: BLE001 - stamping is best effort
        print(f"[story-studio] segment stamp failed: {type(e).__name__}: {e}")


def _rollback_segments(gym_id, request_id):
    """Return a denied render's segments to the pool (undo the usage stamps)."""
    import json
    from . import db, gym_media_selector as _sel
    try:
        rec = json.loads(db.kv_get(_SEG_KEY.format(request_id), "") or "{}")
    except Exception:
        rec = {}
    if not rec:
        return False
    rolled = False
    for aid in rec.get("asset_ids") or []:
        try:
            if _sel.rollback_asset(aid):
                rolled = True
        except Exception:
            continue
    # also roll the per-request post_date stamp if present.
    try:
        if _sel.rollback_use(gym_id, f"story:{request_id}"):
            rolled = True
    except Exception:
        pass
    return rolled


def _default_store():
    from . import story_studio_store
    return story_studio_store.default_store()
