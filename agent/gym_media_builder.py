"""
gym_media_builder.py — the gym-media planner lane (gym_media_drive spec §7),
behind GYM_DRIVE_STAGE (default OFF). Adds the Drive-sourced media pool as ANOTHER
eligible source for a CLIENT gym's faces/community/results slots. Client gyms
already build from uploaded media; this simply widens the pool.

Flow (mirrors podcast_library_builder + client_month_run's vision path):

  pick_media(gym_id, kind_preference) -> none? return None (planner falls through
                                          to the existing uploaded-media logic;
                                          pick_media already fired the deduped
                                          pool-empty alert)
    -> download to a temp file
    -> ensure rendition (HEIC->JPEG / HEVC->H.264, cached by content_hash; §5)
    -> VIDEO: ffprobe + re-gate (unprobed never stages, fail closed)
       PHOTO: read dims + re-gate
    -> run ECHO_VISION (vision.analyze_and_store) on the frame; write vision_json
       back to the asset. auto_plannable gate: a safety-flagged / identity-leaking
       / unusable frame is NOT staged (the next asset is tried).
    -> draft a caption GROUNDED IN THE FRAME (client_content's SB7 + photo_grounding
       + crop_verify), never from imagination. A caption that cannot ground -> the
       slot does not stage.
    -> host the (rendition or original) media, build a PENDING Draft
    -> stamp_use (rolled back if the coach denies)

Temp files are always deleted. Nothing here bypasses the A+ gates or the human
tap: every row lands PENDING and flows through publish_guard like any other.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, gym_media_index as _idx
from . import gym_media_selector as _sel
from .drafter import Draft, DraftStatus

_MAX_ASSET_ATTEMPTS = 3      # validation/vision failures try the next asset, bounded

# Which media kind a slot prefers. faces/community/results are people-forward, so
# photos first; a slot with no photo falls through to the uploaded-media logic.
_SLOT_KIND = {"faces": "photo", "community": "photo", "results": "photo"}


class _PickedCreative:
    """The minimal 'creative' shape client_content.photo_grounding + the SB7
    generator expect: a local .path whose sidecar carries the vision analysis."""

    def __init__(self, path):
        self.path = path


def build_gym_media_draft(account, day_key, pillar, voice, source, *, store=None,
                          drive=None, now=None, library_dir=None):
    """A PENDING Draft for `day_key` sourced from the gym's Drive media pool, or
    None (the planner then falls through to the existing uploaded-media logic).
    Only ever called when GYM_DRIVE_STAGE is ON AND the gym-drive lane is armed for
    this gym (the caller gates both).

    account: the client account (carries .key, .platform). pillar: the slot job
    (faces/community/results/...). voice/source: the approved voice doc + the day's
    approved fact, handed straight to the caption generator so CLAIMS still come
    only from approved sources (the frame only shapes the SCENE, never the facts)."""
    from .integrations import drive_client as _dc
    from . import client_content, vision, media_host

    store = store or _idx.default_store()
    drive = drive or _dc.DriveClient()
    if not drive.available() or not store.available():
        print("[gym-media-builder] lane unarmed (drive/store unavailable); falling through")
        return None

    acct_key = getattr(account, "key", "") or (account if isinstance(account, str) else "")
    gym_base = _sel.base_gym_key(acct_key)
    platform = getattr(account, "platform", None) or acct_key or ""
    kind_pref = _SLOT_KIND.get(str(pillar or "").strip().lower())

    lib = Path(library_dir or tempfile.mkdtemp(prefix="gymmedia_"))
    lib.mkdir(parents=True, exist_ok=True)

    tried = []
    for _attempt in range(_MAX_ASSET_ATTEMPTS):
        asset = _sel.pick_media(gym_base, kind_preference=kind_pref, store=store,
                                now=now, exclude_ids=tuple(tried))
        if asset is None:
            return None  # pool empty: pick_media already fired the deduped alert
        tried.append(asset["id"])

        # TENANT ISOLATION stage-time assertion (§1.5d): the picked asset MUST
        # belong to this gym. A mismatch is blocked, alerted, and NEVER staged.
        if not assert_tenant(asset, gym_base):
            continue

        title = asset.get("title") or f"{asset['id']}.bin"
        tmp_path = lib / os.path.basename(title)
        try:
            try:
                drive.download(asset["id"], tmp_path)
            except Exception as e:  # noqa: BLE001
                print(f"[gym-media-builder] download failed for {title!r}: "
                      f"{type(e).__name__}: {e}")
                return None

            # HEIC/HEVC -> rendition (cached by content_hash; §5). A missing
            # converter marks the asset not-eligible and tries the next asset.
            local_for_vision = tmp_path
            public_override = None
            rend_url, _converted = _idx.ensure_rendition(
                asset, tmp_path, store=store, probe_fn=_idx.probe_video)
            if rend_url:
                public_override = rend_url
                if asset.get("kind") == _idx.KIND_PHOTO:
                    # For a HEIC photo, vision must analyze the JPEG rendition, not
                    # the undecodable original. Re-download the rendition locally.
                    # (In practice ensure_rendition wrote it to the bucket; for
                    # analysis we convert once more to a temp JPEG.)
                    jpeg = lib / (os.path.splitext(os.path.basename(title))[0] + ".jpg")
                    try:
                        _idx.heic_to_jpeg(tmp_path, jpeg)
                        local_for_vision = jpeg
                    except _idx.ConversionUnavailable:
                        _mark_not_eligible(store, asset,
                                           _idx.REJECT_CONVERT_UNAVAILABLE)
                        continue
            elif asset.get("kind") == _idx.KIND_PHOTO and _idx.is_heic(
                    title, asset.get("mime_type")):
                # HEIC but no converter available: not eligible, try the next.
                _mark_not_eligible(store, asset, _idx.REJECT_CONVERT_UNAVAILABLE)
                continue

            # Re-gate from real bytes (fail closed) + probe videos.
            if asset.get("kind") == _idx.KIND_VIDEO:
                info = _idx.probe_video(tmp_path)
                if not info:
                    print(f"[gym-media-builder] probe failed for {title!r}; not "
                          "staging an unprobed video (fail closed)")
                    continue
                el, reason, label = _idx.video_eligibility(
                    tmp_path.stat().st_size or asset.get("size_bytes"),
                    info["duration_sec"], info["width"], info["height"])
                _writeback_probe(store, asset, info, label, el, reason)
                if el is not True:
                    print(f"[gym-media-builder] {title!r} failed the video gate "
                          f"({reason}); trying the next asset")
                    continue

            # ECHO_VISION on the frame (photos). vision writes the analysis to the
            # DAM sidecar; we mirror it into media_asset.vision_json.
            analysis = None
            if asset.get("kind") == _idx.KIND_PHOTO:
                analysis = vision.analyze_and_store(str(local_for_vision),
                                                    gym=gym_base)
                _persist_vision(store, asset, analysis)
                ok, reasons = vision.auto_plannable(analysis)
                if not ok:
                    print(f"[gym-media-builder] {title!r} not auto-plannable "
                          f"({reasons}); trying the next asset")
                    continue

            # GROUNDED caption from the frame (never from imagination). Facts still
            # come only from `source`/`voice`; the frame shapes the scene hint and
            # the crop-verify gates people/detail claims.
            verified = None
            if analysis:
                try:
                    with open(local_for_vision, "rb") as _fh:
                        verified = vision.crop_verify(_fh.read(), analysis)
                except OSError:
                    verified = None
            caption, hashtags = client_content.make_caption(
                account, source, voice, os.path.basename(str(local_for_vision)),
                creative=_PickedCreative(str(local_for_vision)), verified=verified)
            if not (caption or "").strip():
                print(f"[gym-media-builder] {title!r}: caption could not ground; "
                      "slot not staged")
                continue

            # Host the served media (rendition if we made one, else the original).
            public_url = public_override or media_host.host_media(
                str(tmp_path), gym_base)
            if not public_url:
                print(f"[gym-media-builder] hosting returned no url for {title!r}; "
                      "stopping the slot")
                return None
        finally:
            _cleanup(lib)

        draft = Draft(
            draft_id=f"gymmedia_{asset['id']}_{day_key}",
            account_key=acct_key,
            platform=platform,
            caption=caption,
            hashtags=hashtags or [],
            creative_path=title,
            creative_public_url=public_url,
            scheduled_for="",
            status=DraftStatus.PENDING,      # the human tap is untouched
            day_key=day_key,
            draft_type="gym_media",
            category=str(pillar or ""),
            source_fragments=[f"drive_media:{asset['id']}",
                              f"gym:{gym_base}"],
        )
        try:
            _sel.stamp_use(asset, gym_base, day_key, store=store, now=now)
        except Exception as e:  # noqa: BLE001
            print(f"[gym-media-builder] usage stamp failed: {type(e).__name__}: {e}")
        return draft
    return None


def assert_tenant(asset, gym_base):
    """TENANT ISOLATION stage-time assertion (spec §1.5d): the asset's gym_id MUST
    equal the gym we are building for. A mismatch is BLOCKED (return False), ops is
    alerted once, and the row NEVER publishes. Returns True only when the asset is
    genuinely this gym's."""
    if str(asset.get("gym_id") or "") == str(gym_base or ""):
        return True
    _idx.dedup_alert(
        f"tenant_mismatch:{asset.get('id')}",
        f"gym-media tenant isolation BLOCKED a cross-gym asset: asset "
        f"{asset.get('id')} is tagged gym={asset.get('gym_id')!r} but was picked "
        f"for gym={gym_base!r}. The row was blocked and never published.")
    return False


def _mark_not_eligible(store, asset, reason):
    try:
        store.update_asset(asset["id"], {"eligible": False, "reject_reason": reason})
    except Exception as e:  # noqa: BLE001
        print(f"[gym-media-builder] mark-not-eligible failed: {type(e).__name__}: {e}")


def _writeback_probe(store, asset, info, label, el, reason):
    try:
        store.update_asset(asset["id"], {
            "duration_sec": info["duration_sec"], "width": info["width"],
            "height": info["height"], "aspect": label,
            "eligible": el, "reject_reason": reason,
            "indexed_at": datetime.now(timezone.utc).isoformat()})
    except Exception as e:  # noqa: BLE001
        print(f"[gym-media-builder] probe write-back failed: {type(e).__name__}: {e}")


def _persist_vision(store, asset, analysis):
    if analysis is None:
        return
    try:
        store.update_asset(asset["id"], {"vision_json": analysis})
        asset["vision_json"] = analysis
    except Exception as e:  # noqa: BLE001
        print(f"[gym-media-builder] vision persist failed: {type(e).__name__}: {e}")


def _cleanup(lib):
    try:
        for name in os.listdir(lib):
            try:
                os.unlink(os.path.join(lib, name))
            except OSError:
                pass
    except OSError:
        pass
