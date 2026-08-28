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
                 now=None):
    """Build ONE Story from a request dict and stage it PENDING (or HOLD it honestly).

    request keys: gym_id, asset_ids (list), brief (optional str), template (optional),
    music_mood (optional: 'hype'|'chill'|'none'), requested_by, identity_tokens
    (optional list), ask (optional str override).

    Returns a result dict: {status, reason, draft, story_render, request_id}. status is
    'staged' (a PENDING draft was written), 'held' (nothing staged, honest reason), or
    'off' (the render lane is not armed for this gym).
    """
    gym_id = _base_gym(request.get("gym_id"))
    if not config.story_studio_render_active_for(gym_id):
        return {"status": "off", "reason": "STORY_STUDIO_RENDER not armed for this gym",
                "draft": None, "story_render": None, "request_id": None}

    from . import (story_templates, story_grounding, story_overlay, story_music,
                   story_composer, story_ledger)

    store = store or _default_store()
    request_id = request.get("id") or str(uuid.uuid4())

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

    rfn = render_fn or story_composer.render_compose
    result = rfn(plan, output_dir=out_dir, ask_frame_text=overlay.ask,
                 music_path=music_path)
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
        platform=request.get("platform") or gym_id,
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

    # 9. stamp segment usage so a deny can roll it back (return segments to the pool).
    _stamp_segments(gym_id, request_id, plan.segments)

    return {"status": "staged", "reason": "", "draft": draft,
            "story_render": story_render, "request_id": request_id}


def deny(request_id, gym_id, reason="", *, store=None):
    """A coach denied a staged Story: return its segments to the pool + log the reason
    (spec §4/§6). Idempotent. Returns True when a rollback actually happened."""
    from . import story_music  # noqa: F401 - keep import graph symmetric
    from . import db
    base = _base_gym(gym_id)
    rolled = _rollback_segments(base, request_id)
    store = store or _default_store()
    try:
        if store.available():
            store.update_request(request_id, {"status": STATUS_DENIED,
                                              "deny_reason": reason})
    except Exception as e:  # noqa: BLE001
        print(f"[story-studio] deny request update failed: {type(e).__name__}: {e}")
    db.audit("story_studio", request_id, f"denied: {reason} (segments returned to pool)")
    return rolled


# ---- helpers ----------------------------------------------------------------
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
