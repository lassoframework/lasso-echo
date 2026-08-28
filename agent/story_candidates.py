"""
story_candidates.py — the LIVE candidate-discovery + source-download layer that turns
a real coach tap (or the event one-tap) into scored segments with REAL local source
paths, so story_studio.create_story can actually render instead of HELDing.

This closes the last gap that blocked arming Story Studio: create_story previously
required candidates/assets_by_id to be INJECTED, and no code path ever populated a
segment's source_path. Both a real tap and gym_event.render_event_story called
create_story with candidates=None, so every real render fell to HELD ("too few
segments"). This module discovers the gym's eligible RAW pool, scores it with the
SAME opus scoring intent the reel lane uses, and downloads each picked asset to a
local file via the SAME Drive download the sync job uses.

RAILS (never move — spec §3, §1.5d, CLAUDE.md):
  * TENANT ISOLATION: every candidate asserts asset.gym_id == the request gym_id
    (defense in depth beyond the store's gym_id filter). A cross-gym row can NEVER
    become a candidate.
  * RAW LANE ONLY: only eligible (gate=True), not-coach-hidden assets that are NOT
    one of Echo's own past renders (the re-ingest guard) and are NOT sitting
    unresolved in the "Sort these" ambiguous queue. Finished / ambiguous-unsorted
    media never enters the story lane.
  * INPUT CAPS: an asset over 5 min / 900 MB routes to the Opus reel lane (spec §3);
    it is skipped here, never truncated.
  * SCORING REUSE: candidate scoring reuses opus_factory's numeric-score reader
    (_first_num / _SCORE_KEYS) — the same scoring intent the reel lane gates on.
  * DOWNLOAD REUSE: source bytes are fetched with the SAME drive_client.download the
    gym-media sync uses (agent/jobs/sync_gym_media.py). No new download is invented.
  * READ-ONLY DRIVE: download only; nothing is written back to the gym's Drive.
  * Temp files are the caller's to clean up (cleanup() undoes bind_source_paths).
"""
from __future__ import annotations

import os
import tempfile

from . import story_composer as _comp

# One segment per raw asset: a leading window clamped to the per-segment length band
# (spec §3: 3..15s each). A raw clip longer than SEG_MAX contributes only its opening
# window; shorter clips contribute their whole length (if >= SEG_MIN).
_SEG_TARGET_SEC = _comp.SEG_MAX_SEC        # 15s target window per asset
_SEG_MIN_SEC = _comp.SEG_MIN_SEC           # 3s floor (below this an asset is unusable)
# When a video's duration is unknown (unprobed), assume a safe in-band window so the
# asset is still usable; the real cut is bounded by the actual file at render time.
_SEG_DEFAULT_SEC = 8.0


def _asset_score(asset):
    """Reuse opus_factory's numeric-score reader (the SAME scoring intent the reel lane
    gates on): pull the best score off the asset (vision_json first, then any top-level
    score field). A pool asset with no score reads 0.0 and still sorts deterministically
    by id downstream — it is not dropped for lacking a score."""
    from . import opus_factory
    vision = asset.get("vision_json")
    if isinstance(vision, dict):
        s = opus_factory._first_num(vision, opus_factory._SCORE_KEYS)
        if s:
            return s
    return opus_factory._first_num(asset, opus_factory._SCORE_KEYS)


def _seg_window(asset):
    """The (start_ts, end_ts) window this asset contributes, or None if it is too short
    to be a usable segment. Photos have no timeline and are skipped (the story lane
    composes VIDEO segments; a photo pool routes to the existing image story lane)."""
    kind = str(asset.get("kind") or "").strip().lower()
    if kind and kind != "video":
        return None
    dur = _comp._num(asset.get("duration_sec"))
    if dur is None:
        end = _SEG_DEFAULT_SEC
    elif dur < _SEG_MIN_SEC:
        return None                        # too short to slice a >= 3s segment
    else:
        end = min(dur, _SEG_TARGET_SEC)
    return 0.0, round(float(end), 2)


def _is_finished_render(asset, *, ledger_lookup=None):
    """True when this asset's bytes are one of Echo's OWN past renders (the EP124
    re-ingest guard): such an asset is FINISHED and blocked from the story lane so Echo
    can never eat its own output and repost it."""
    ch = asset.get("content_hash")
    if not ch:
        return False
    lookup = ledger_lookup
    if lookup is None:
        from . import story_ledger
        lookup = story_ledger.is_echo_render
    try:
        return bool(lookup(ch))
    except Exception as e:  # noqa: BLE001 - a ledger failure fails OPEN to "not finished"
        print(f"[story-candidates] ledger lookup failed: {type(e).__name__}: {e}")
        return False


def _pending_ambiguous_ids(gym_id):
    """Asset ids sitting UNRESOLVED in the gym's 'Sort these' ambiguous queue. These
    never auto-enter the story lane (spec §0.3: ambiguous NEVER auto-posts) — a human
    must tap Raw first. Best effort: a queue read failure yields an empty set (fail
    toward NOT blocking a genuinely-raw asset, since eligibility already gates)."""
    try:
        from . import story_sort_queue as _q
        return {str(it.get("asset_id") or "") for it in _q.pending(gym_id)}
    except Exception as e:  # noqa: BLE001
        print(f"[story-candidates] sort-queue read failed: {type(e).__name__}: {e}")
        return set()


def _eligible_raw(asset, gym_id, *, ambiguous_ids, ledger_lookup=None):
    """(ok, reason): is this asset an eligible RAW-lane candidate for THIS gym? Applies
    tenant isolation, the render eligibility gate, the coach hide, the re-ingest guard,
    the ambiguous-unsorted guard, and the input caps — in that order."""
    # TENANT ISOLATION (defense in depth beyond the store's gym_id filter).
    if str(asset.get("gym_id") or "") != str(gym_id or ""):
        return False, "cross-gym asset (tenant isolation)"
    # render eligibility gate (null/false fail closed, exactly like pick_media).
    if asset.get("eligible") is not True:
        return False, "not eligible (unprobed or gate-rejected)"
    if asset.get("excluded_by_coach"):
        return False, "coach-hidden"
    # re-ingest guard: never Echo's own past render.
    if _is_finished_render(asset, ledger_lookup=ledger_lookup):
        return False, "content_hash matches an Echo render (finished, re-ingest-blocked)"
    # ambiguous-unsorted never auto-enters the story lane.
    if str(asset.get("id") or "") in ambiguous_ids:
        return False, "ambiguous, awaiting a human sort"
    # input caps: an over-cap asset routes to the Opus reel lane (skip, never truncate).
    route, reason = _comp.route_asset(asset)
    if route == _comp.ROUTE_OPUS:
        return False, reason
    return True, ""


def discover_candidates(gym_id, asset_ids=None, *, store=None, now=None,
                        ledger_lookup=None):
    """Discover the gym's eligible RAW candidates and return (candidates, assets_by_id).

    candidates: list of scored slice dicts {asset_id, gym_id, start_ts, end_ts, score}
    in the shape story_composer.select_segments consumes.
    assets_by_id: {asset_id: asset_row} so plan_compose can apply the input-cap routing.

    asset_ids: when the coach picked specific clips, discovery is SCOPED to those ids
    (still fully gated). When None/empty, discovery scans the gym's whole eligible pool.

    Reuses the gym-media store's list_assets(gym_id) read (the SAME read pick_media
    uses via the index store). Every returned candidate has passed tenant isolation,
    the eligibility gate, the re-ingest guard, and the input caps. Deterministic:
    ranked by score desc, then asset id.
    """
    from . import gym_media_index as _idx
    gym_id = _base_gym(gym_id)
    store = store or _idx.default_store()
    if not store.available():
        print("[story-candidates] media store unavailable; no candidates discovered")
        return [], {}
    try:
        assets = store.list_assets(gym_id)
    except Exception as e:  # noqa: BLE001 - a read failure is an empty pick, not a crash
        print(f"[story-candidates] asset read failed for {gym_id}: "
              f"{type(e).__name__}: {e}")
        return [], {}

    wanted = {str(a) for a in (asset_ids or [])}
    ambiguous_ids = _pending_ambiguous_ids(gym_id)

    candidates, assets_by_id = [], {}
    for a in assets:
        aid = str(a.get("id") or "")
        if wanted and aid not in wanted:
            continue
        ok, _reason = _eligible_raw(a, gym_id, ambiguous_ids=ambiguous_ids,
                                    ledger_lookup=ledger_lookup)
        if not ok:
            continue
        window = _seg_window(a)
        if window is None:
            continue
        start_ts, end_ts = window
        candidates.append({
            "asset_id": aid, "gym_id": gym_id,
            "start_ts": start_ts, "end_ts": end_ts,
            "score": _asset_score(a),
        })
        assets_by_id[aid] = a

    candidates.sort(key=lambda c: (-float(c.get("score") or 0.0), c["asset_id"]))
    return candidates, assets_by_id


def bind_source_paths(segments, assets_by_id, *, gym_id=None, downloader=None,
                      tmp_root=None):
    """Download each picked segment's source asset to a REAL local file and set
    segment.source_path (in place). Returns the temp dir to clean up afterward.

    Reuses the SAME drive_client.download the gym-media sync uses
    (agent/jobs/sync_gym_media.py: drive.download(asset_id, tmp_path)). media_asset.id
    IS the Drive file id, so the segment's asset_id is the download key directly.

    downloader(file_id, dest) -> path is injectable so the suite runs offline (and so a
    later non-Drive source can bind the same way). The live default binds drive_client.

    TENANT re-assertion: every segment is re-asserted against gym_id before its bytes
    are fetched (a segment whose gym does not match the request is NEVER downloaded)."""
    if not segments:
        return None
    if downloader is None:
        from .integrations import drive_client as _dc
        downloader = _dc.download
    tmp_dir = tempfile.mkdtemp(prefix="storysrc_", dir=tmp_root)
    for seg in segments:
        if gym_id is not None:
            _comp.assert_segment_tenant(seg, _base_gym(gym_id))
        asset = (assets_by_id or {}).get(seg.asset_id) or {}
        # media_asset.id is the Drive file id; fall back to an explicit file id field
        # if a future source stores it separately.
        file_id = asset.get("drive_file_id") or asset.get("id") or seg.asset_id
        ext = _ext_for(asset)
        dest = os.path.join(tmp_dir, f"src_{seg.asset_id}{ext}")
        downloader(file_id, dest)
        seg.source_path = dest
    return tmp_dir


def cleanup(tmp_dir):
    """Remove a bind_source_paths temp dir and its downloaded sources (best effort).
    The rendered montage lives in a SEPARATE output dir, so cleaning the sources never
    touches the staged artifact."""
    if not tmp_dir or not os.path.isdir(tmp_dir):
        return
    import shutil
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        print(f"[story-candidates] temp cleanup failed: {type(e).__name__}: {e}")


def _ext_for(asset):
    title = str(asset.get("title") or "")
    _root, ext = os.path.splitext(title)
    if ext and len(ext) <= 6:
        return ext.lower()
    mime = str(asset.get("mime_type") or "").lower()
    if "quicktime" in mime or mime.endswith("/mov"):
        return ".mov"
    if "mp4" in mime:
        return ".mp4"
    return ".mp4"


def _base_gym(gym_id):
    base = str(gym_id or "").strip().lower()
    for suf in ("_ig", "_fb"):
        if base.endswith(suf):
            return base[: -len(suf)]
    return base
