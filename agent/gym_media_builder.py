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
    -> IF the gym is on AGENT_VISION_GYMS (config.vision_enabled_for): run ECHO_VISION
       (vision.analyze_and_store) on the frame; write vision_json back to the asset.
       auto_plannable gate: a safety-flagged / identity-leaking / unusable frame is
       NOT staged (the next asset is tried). A gym NOT on the allowlist skips vision
       entirely (no spend, no analysis) and gets an ungrounded caption instead --
       this lane must never be a second, unguarded way to burn vision calls.
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

# MEDIA MIX (John Weeks / Tough Temple, 2026-09-10). This lane used to map every
# pillar to kind_preference="photo" (_SLOT_KIND), and pick_media hard-filters on kind,
# so a gym with 57 eligible Drive VIDEOS and 6 photos was staged photo-only for its
# whole life: not one Drive video ever reached content_calendar, the 6 photos burned
# their 90-day cooldown in a week, and the book fell back to the same local stills.
# Now a slot's kind comes from a deterministic per-day/slot pattern: VIDEO_MIX_PATTERN
# out of every VIDEO_MIX_CYCLE consecutive (day, slot) ordinals are video slots
# (5/11 = 45%, the "roughly 40-50% video" target), the rest photo. Deterministic on the
# calendar date + slot index so a re-run of the same month stages the same shape. The
# preferred kind is only ever ASKED FOR when the pool can supply it, and when the
# preferred kind is exhausted mid-month the slot falls back to the other kind rather
# than leaving the day empty (kinds_for_slot returns the ordered preference list).
VIDEO_MIX_CYCLE = 11
VIDEO_MIX_PATTERN = frozenset({0, 2, 4, 7, 9})


def is_video_slot(day_key, slot_index=0):
    """True when this (day, slot) ordinal falls on a video beat of the mix pattern.
    Pure and deterministic: the same date + slot always answers the same way."""
    try:
        from datetime import date as _date
        ordinal = _date.fromisoformat(str(day_key)[:10]).toordinal()
    except (TypeError, ValueError):
        ordinal = 0
    return (ordinal + int(slot_index or 0)) % VIDEO_MIX_CYCLE in VIDEO_MIX_PATTERN


def kinds_for_slot(pool_kinds, day_key, slot_index=0):
    """The ordered kind preference for one slot given what the pool can supply.
    ['video', 'photo'] on a video beat when the pool has videos, ['photo', 'video']
    otherwise; a pool with only one kind collapses to that kind. [] when the pool has
    neither (the caller falls through)."""
    kinds = {str(k) for k in (pool_kinds or ()) if k in (_idx.KIND_PHOTO, _idx.KIND_VIDEO)}
    if not kinds:
        return []
    if kinds == {_idx.KIND_VIDEO}:
        return [_idx.KIND_VIDEO]
    if kinds == {_idx.KIND_PHOTO}:
        return [_idx.KIND_PHOTO]
    # Approved stills are always consumed before clips.  The video cadence is a
    # preference only when no pickable photo remains; it must never bypass an
    # available client photo in the same Drive pool.
    return [_idx.KIND_PHOTO, _idx.KIND_VIDEO]


def _vision_alert(msg):
    """One loud line for anything vision wants to say on this lane — the per-gym
    monthly cap being hit, or an identity flag routed to review. Local log always,
    ops alert best effort; alerting never blocks a stage."""
    print(f"[gym-media-builder] {msg}")
    try:
        from . import ops_alerts
        ops_alerts.alert(msg)
    except Exception:  # noqa: BLE001
        pass


class _PickedCreative:
    """The minimal 'creative' shape client_content.photo_grounding + the SB7
    generator expect: a local .path whose sidecar carries the vision analysis."""

    def __init__(self, path):
        self.path = path


def build_gym_media_draft(account, day_key, pillar, voice, source, *, store=None,
                          drive=None, now=None, library_dir=None, exclude_ids=(),
                          slot_index=0, rendition_budget=None, kind_prefs=None):
    """A PENDING Draft for `day_key` sourced from the gym's Drive media pool, or
    None (the planner then falls through to the existing uploaded-media logic).
    Only ever called when GYM_DRIVE_STAGE is ON AND the gym-drive lane is armed for
    this gym (the caller gates both).

    account: the client account (carries .key, .platform). pillar: the slot job
    (faces/community/results/...). voice/source: the approved voice doc + the day's
    approved fact, handed straight to the caption generator so CLAIMS still come
    only from approved sources (the frame only shapes the SCENE, never the facts).

    exclude_ids (independent audit, 2026-09-08): Drive asset ids the caller already
    knows must never be picked -- a photo live elsewhere in the gym's book, or (for
    a denied-slot replacement) the denied post's own asset. Merged with this
    function's own per-call retry tracking; without a caller-supplied set, a denied
    Drive asset's used_count is reset by gym_media_selector.rollback_use the moment
    it's denied, making it the pool's least-used candidate again -- so the exact
    photo just denied could come right back as its own "fresh" replacement.

    slot_index: the slot ordinal within the day (0 = the day's first post, 1 = the
    PM post on a 2x day). Feeds the media-mix pattern together with day_key so the
    two slots of one day can differ in kind and a re-run stages the same shape.

    rendition_budget: the build's gym_media_index.RenditionBudget (audit R-D1 #3).
    None = one transcode allowed for this call (a single denied-slot replacement).
    Once spent, video picks are restricted to assets that ALREADY carry a
    rendition_url, then the slot falls back to photos."""
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

    # CROSS-GYM SOURCE GUARD, read-once per build (independent review P2,
    # 2026-10-05): validate the gym's same-gym active source set ONCE and reuse it
    # for every initial candidate check in the retry loop, instead of re-reading
    # the store per candidate. The FRESH re-verification immediately before the
    # durable usage stamp (below) intentionally re-reads and fails closed on drift.
    try:
        build_source_ids = _sel.verified_source_ids(store, gym_base)
    except Exception as e:  # noqa: BLE001 - unproven evidence never stages
        print(f"[gym-media-builder] linked-source evidence read failed for "
              f"{gym_base} ({type(e).__name__}); holding the slot (fail closed)")
        return None

    lib = Path(library_dir or tempfile.mkdtemp(prefix="gymmedia_"))
    lib.mkdir(parents=True, exist_ok=True)

    caller_excludes = {str(i) for i in (exclude_ids or ()) if i}
    # MEDIA MIX: ask the pool what it can supply, then order the kinds for this
    # (day, slot). The pool-empty alert stays with pick_media below. A caller may
    # pin the kinds (the video pre-pass asks for ("video",) ONLY: no photo fallback,
    # a video beat the pool cannot serve is left to Lane A).
    if kind_prefs is None:
        kind_prefs = kinds_for_slot(
            _sel.pool_kinds(gym_base, store=store, now=now,
                            exclude_ids=tuple(caller_excludes)),
            day_key, slot_index)
    else:
        kind_prefs = [k for k in kind_prefs if k in (_idx.KIND_PHOTO, _idx.KIND_VIDEO)]
        if not kind_prefs:
            return None
    if rendition_budget is None:
        rendition_budget = _idx.RenditionBudget(1)

    def _pick(kind_pref, excl):
        # BUDGET SPENT (audit R-D1 #3): only a video that already carries a rendition
        # may be picked (nothing left to transcode it with); an unrenditioned video is
        # left for the nightly pre-render pass and the slot moves on to photos.
        if kind_pref == _idx.KIND_VIDEO and rendition_budget.spent:
            cands = [c for c in _sel.pickable(gym_base, kind_pref, store=store, now=now,
                                              exclude_ids=excl)
                     if c.get("rendition_url")]
            return cands[0] if cands else None
        return _sel.pick_media(gym_base, kind_preference=kind_pref, store=store,
                               now=now, exclude_ids=excl)

    tried = []
    for _attempt in range(_MAX_ASSET_ATTEMPTS):
        asset = None
        for kind_pref in (kind_prefs or [None]):
            asset = _pick(kind_pref, tuple(caller_excludes) + tuple(tried))
            if asset is not None:
                break
            # The preferred kind is exhausted (or every one of it just failed
            # validation): fall back to the other kind before giving the day up.
        if asset is None:
            return None  # pool empty: pick_media already fired the deduped alert
        tried.append(asset["id"])

        # TENANT ISOLATION stage-time assertion (§1.5d): the picked asset MUST
        # belong to this gym. A mismatch is blocked, alerted, and NEVER staged.
        if not assert_tenant(asset, gym_base):
            continue
        # CROSS-GYM SOURCE GUARD (2026-10-05): the asset's linked media_source must
        # exist, be active and belong to this gym BEFORE any hosting or stamping.
        # Uses the build-once validated source set (no per-candidate store reads);
        # the pre-stamp FRESH re-check below still catches any drift.
        if not assert_source(asset, gym_base, store, source_ids=build_source_ids):
            continue

        title = asset.get("title") or f"{asset['id']}.bin"
        tmp_path = lib / os.path.basename(title)
        media_observations = [] if writer_prep_enabled() else None
        source_observation_bytes = None
        source_observation_hold = ""
        try:
            try:
                drive.download(asset["id"], tmp_path)
            except Exception as e:  # noqa: BLE001
                print(f"[gym-media-builder] download failed for {title!r}: "
                      f"{type(e).__name__}: {e}")
                return None

            if media_observations is not None:
                try:
                    source_observation_bytes = _idx.bounded_materialization_bytes(tmp_path)
                    fresh_source = store.get_asset(asset["id"])
                    # Re-read authoritative indexed identity rather than treating
                    # the candidate or its local filename as an original receipt.
                    import hashlib
                    checksum = str((fresh_source or {}).get("content_hash") or "")
                    if (not fresh_source or fresh_source.get("id") != asset["id"]
                            or fresh_source.get("gym_id") != gym_base
                            or fresh_source.get("source_id") != asset.get("source_id")
                            or not _sel.asset_source_ok(fresh_source, gym_base, store)
                            or checksum.lower() != hashlib.md5(source_observation_bytes).hexdigest()):
                        source_observation_bytes = None
                        source_observation_hold = "drive_identity_or_checksum_unverified"
                except Exception:
                    source_observation_bytes = None
                    source_observation_hold = "drive_source_observation_unavailable"

            # Re-gate a VIDEO from real bytes FIRST (fail closed): the probe also
            # yields the codec every rendition decision below needs.
            info = None
            poster_url = ""
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

            # HEIC / HEVC / odd container -> rendition (cached by content_hash; §5),
            # BUDGETED (audit R-D1): a spent budget or a clip past its per-clip
            # timeout skips the asset (rendition_missing, still eligible, the nightly
            # pre-render pass catches up); a missing converter marks it not-eligible.
            local_for_vision = tmp_path
            public_override = None
            needs = _idx.needs_rendition(asset, info)
            try:
                rend_url, _converted = _idx.ensure_rendition(
                    asset, tmp_path, store=store, probe_info=info,
                    budget=rendition_budget,
                    **({"observation_sink": media_observations}
                       if media_observations is not None else {}))
            except _idx.RenditionBudgetExhausted:
                print(f"[gym-media-builder] transcode budget spent; {title!r} skipped "
                      "until the nightly pre-render pass renders it")
                _note_rendition_missing(store, asset)
                continue
            except _idx.RenditionTimeout as e:
                print(f"[gym-media-builder] {title!r} transcode timed out ({e}); skipped")
                _note_rendition_missing(store, asset)
                continue
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
            elif needs:
                # HEIC with no converter, or an HEVC / .webm / .avi / .mkv video that
                # could not be transcoded: RAW is never hosted (an HEVC .mov used to
                # slip through here because .mov is a "publishable container").
                print(f"[gym-media-builder] {title!r} needs a rendition and none could "
                      "be made; never staging it raw")
                _mark_not_eligible(store, asset, _idx.REJECT_CONVERT_UNAVAILABLE)
                continue

            poster_evidence = None
            video_source_url = ""
            if asset.get("kind") == _idx.KIND_VIDEO:
                if writer_prep_enabled():
                    # GLOBAL WRITER PREP: the poster must carry a byte-bound render
                    # receipt. The source of truth is the EXACT hosted video URL --
                    # the rendition URL when one was made, else the hosted original
                    # (hosted HERE, before the poster, so the URL attests real
                    # served bytes; never guessed from the local path/title).
                    video_source_url = public_override or media_host.host_media(
                        str(tmp_path), gym_base)
                    if not video_source_url:
                        print(f"[gym-media-builder] no hosted video url for "
                              f"{title!r}; holding the slot (writer prep)")
                        return None
                    _poster = video_poster_with_evidence(
                        tmp_path, lib, gym_base, video_source_url)
                    if _poster is None:
                        # Evidence failed: HOLD the slot. The legacy path would skip
                        # the preview; writer prep refuses to stage an unattested
                        # video card.
                        print(f"[gym-media-builder] evidenced poster failed for "
                              f"{title!r}; holding the slot (writer prep)")
                        return None
                    poster_url, poster_evidence = _poster
                else:
                    # POSTER FRAME while the download still exists on disk. The month
                    # run's _attach_video_poster reads creative_path, which for a Drive
                    # draft is the asset TITLE (nothing on disk by then), so without
                    # this a Drive video shows as a BLANK card in the portal. Display
                    # only: the row publishes the video itself. Best effort, never
                    # blocks.
                    poster_url = video_poster_url(tmp_path, lib, gym_base)

            # ECHO_VISION on the frame (photos). vision writes the analysis to the
            # DAM sidecar; we mirror it into media_asset.vision_json.
            #
            # ALLOWLIST GATE (vision_allowlist_watch drift report, 2026-09):
            # AGENT_VISION_GYMS gates every other vision caller (client_content's
            # pick_image, caption_swap) but this Drive lane called vision.analyze_and_store
            # unconditionally -- confirmed live: crossfitlocal, crossfitreverb30b5b2,
            # hillcountry, theboltonclub, toughtemple52040e, train7164ae502,
            # zanshinfitness630e22 and others burning real vision spend despite never
            # being on the allowlist. A gym not on AGENT_VISION_GYMS gets the same
            # experience the module's own docstring promises ("client gyms already
            # build from uploaded media; this simply widens the pool") -- Drive media
            # without vision grounding, exactly like the legacy non-vision path
            # everywhere else, not a blocked slot.
            analysis = None
            if asset.get("kind") == _idx.KIND_PHOTO and config.vision_enabled_for(gym_base):
                # alert= is REQUIRED here (audit item 5, 2026-08-31). Without it the
                # per-gym monthly runaway guard (vision.within_gym_budget) can only
                # return False — it can never SAY anything — so a gym silently stops
                # being analyzed the moment it hits the cap. That is exactly what
                # happened to gritx: 400/400 burned for the month, analysis quietly
                # paused, zero alerts in the kv, and nobody knew until an audit
                # counted rows. The Drive lane is the ONLY vision caller that was
                # missing this.
                analysis = vision.analyze_and_store(
                    str(local_for_vision), gym=gym_base,
                    alert=_vision_alert)
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
            public_url = (public_override or video_source_url
                          or media_host.host_media(str(tmp_path), gym_base))
            if not public_url:
                print(f"[gym-media-builder] hosting returned no url for {title!r}; "
                      "stopping the slot")
                return None
            if media_observations is not None:
                if not source_observation_bytes:
                    media_observations.append({"provenance_status": "unverified",
                                               "hold_reasons": [source_observation_hold]})
                elif not public_override:
                    try:
                        media_observations.append(_idx.materialization_observation(
                            source_observation_bytes, source_observation_bytes,
                            public_url, tenant=gym_base, source_asset_id=asset["id"],
                            source_url=public_url,
                            recipe={"name": "identity", "version": 1,
                                    "runtime_verified": True}))
                    except Exception:
                        media_observations.append({"provenance_status": "unverified",
                                                   "hold_reasons": ["original_hosted_readback_unverified"]})
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
            # Stamp the media_asset id onto the row (content_calendar
            # .source_media_asset_id) so the portal hide + the removed-from-Drive
            # sweep can flip this PENDING post back to needs_media (§4, §8).
            source_media_asset_id=str(asset["id"]),
        )
        # Keep the hosted original as provenance when it is also the served media.
        # A rendition URL is a transformed delivery asset, not the raw source.
        if not public_override:
            draft.source_media_url = public_url
        if media_observations is not None:
            draft.media_materialization_observations = media_observations
            # No registry or manifest can be issued from a draft side channel.
            draft.media_provenance_status = "unverified"
        if poster_url:
            draft.thumbnail_url = poster_url          # -> content_calendar.thumbnail_url
        if poster_evidence:
            # NON-DB side channel (Draft has no poster_render_evidence column):
            # the byte-bound receipt rides the draft object for the writer-prep
            # lane (portal_calendar_store / client_month_run integration reads it
            # before persistence; persistence itself is a separate migration).
            draft.poster_render_evidence = poster_evidence
        # The grounding this caption was written against, so a caption RETRY
        # (client_month_run._recaption_drive_draft) grounds the same way instead of
        # from nothing (audit round 5 minor).
        draft.caption_grounding = {
            "creative_name": os.path.basename(str(local_for_vision)),
            "verified": verified}
        # The claim is shared with GBP and swaps. A selector snapshot alone is
        # insufficient: another lane can claim this asset during materialization.
        from . import db
        try:
            claim_id = _sel.claim_drive_content(gym_base, asset, store)
        except Exception as e:  # noqa: BLE001 - unknown claim state holds the slot
            _vision_alert(f"{gym_base} {day_key}: Drive claim failed ({type(e).__name__})")
            return None
        if claim_id is None:
            return None
        draft._drive_claim_id = claim_id
        claim_account = f"{gym_base}_gbp"
        try:
            # A legacy or independent stamp could predate the claim. Do not
            # release this claim after an uncertain authoritative read.
            fresh = store.get_asset(asset["id"])
            if fresh is None or _sel._has_prior_use(fresh):
                return None
            # CROSS-GYM SOURCE GUARD: re-verify the linked source on the FRESH row
            # before stamping; unproven evidence raises and fails closed below.
            if not _sel.asset_source_ok(fresh, gym_base, store):
                _vision_alert(f"{gym_base} {day_key}: Drive asset "
                              f"{asset['id']} failed linked-source verification; "
                              "draft held before approval/card persistence")
                return None
            _sel.stamp_use(fresh, gym_base, day_key, store=store, now=now)
        except Exception as e:  # noqa: BLE001
            # The card must never become durable while its media is still
            # reofferable. A partial stamp fails closed too: returning no draft
            # holds the slot, while any counter that did land keeps the asset out.
            _vision_alert(
                f"{gym_base} {day_key}: Drive usage stamp failed "
                f"({type(e).__name__}); draft held before approval/card persistence")
            return None
        try:
            db.socialapi_claim_done(claim_id, claim_account, str(asset["id"]))
        except Exception as e:  # noqa: BLE001 - in-flight claim still protects asset
            _vision_alert(f"{gym_base} {day_key}: Drive claim receipt failed "
                          f"({type(e).__name__}); claim retained")
        return draft
    return None



def writer_prep_enabled():
    """AGENT_VISUAL_GLOBAL_WRITER_PREP (default OFF): the global visual writer
    prep lane. When ON, Drive/podcast video poster frames must be rendered from
    the EXACT hosted video bytes with a byte-bound rendition receipt; a poster
    without evidence holds the slot. When OFF, the legacy best-effort
    video_poster_url path and behavior are unchanged."""
    return os.environ.get("AGENT_VISUAL_GLOBAL_WRITER_PREP", "").lower() in (
        "1", "true", "yes", "on")


def video_poster_url(video_path, work_dir, tenant):
    """A hosted poster JPG for a local video file, or '' on any failure. Pure ffmpeg
    frame grab (action_reel.poster_frame) into work_dir, hosted under the gym's
    tenant. Shared by the Drive builder and the portal media swap so a Drive video
    row always carries a preview frame. NEVER raises."""
    try:
        from . import action_reel, media_host
        if not config.hosting_enabled():
            return ""
        out = Path(work_dir) / (os.path.splitext(os.path.basename(str(video_path)))[0]
                                + "__poster.jpg")
        action_reel.poster_frame(str(video_path), str(out))
        if not out.is_file() or out.stat().st_size == 0:
            return ""
        return media_host.host_media(str(out), tenant) or ""
    except Exception as exc:  # noqa: BLE001 - a missing preview never blocks a post
        print(f"[gym-media-builder] poster skipped for "
              f"{os.path.basename(str(video_path))}: {type(exc).__name__}")
        return ""


def video_poster_with_evidence(video_path, work_dir, tenant, source_exact_url):
    """Return ``(poster_url, render_evidence)`` for an explicitly attested video.

    Unlike :func:`video_poster_url`, this is an opt-in receipt-producing path.  It
    never treats ``video_path`` as the source of truth: it reads the exact hosted
    video URL, materializes those observed bytes for ffmpeg, then reads the hosted
    JPEG back before returning a byte-bound rendition receipt.  Any unavailable,
    changed, or non-JPEG object fails closed with ``None``.

    ``video_path`` supplies only a file extension for ffmpeg's temporary input; its
    local bytes must not establish source lineage.
    """
    source_path = poster_path = None
    try:
        from urllib.parse import urlsplit
        import hashlib
        import uuid

        from . import action_reel, media_host, visual_writer_prepare

        if not config.hosting_enabled():
            return None

        # _bytes_for_url accepts only our configured host and _exact_bytes enforces
        # a valid exact URL, bounded non-empty read, and no redirects/query guessing.
        source_bytes = visual_writer_prepare._exact_bytes(
            source_exact_url, visual_writer_prepare._bytes_for_url, "source")
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
        suffix = Path(urlsplit(source_exact_url).path).suffix.lower()
        if not suffix or len(suffix) > 12:
            suffix = Path(str(video_path)).suffix.lower() or ".mp4"
        with tempfile.NamedTemporaryFile(dir=work, prefix="poster-source-",
                                         suffix=suffix, delete=False) as source_file:
            # Record the allocated file before a write or close can fail, so the
            # fail-closed path can still remove the byte-bearing temporary object.
            source_path = Path(source_file.name)
            source_file.write(source_bytes)
        with tempfile.NamedTemporaryFile(dir=work, prefix="poster-render-",
                                         suffix=".jpg", delete=False) as poster_file:
            poster_path = Path(poster_file.name)

        action_reel.poster_frame(str(source_path), str(poster_path))
        size = poster_path.stat().st_size
        if size <= 0 or size > visual_writer_prepare.MAX_VISUAL_BYTES:
            return None
        rendered_bytes = poster_path.read_bytes()
        # A .jpg suffix or magic prefix is not an attestation that this is a
        # decodable JPEG.  Pillow's verify() reads the actual image structure.
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(rendered_bytes)) as image:
            if image.format != "JPEG":
                return None
            image.verify()
        if not rendered_bytes.startswith(b"\xff\xd8\xff"):
            return None
        delivered_url = media_host.host_media(str(poster_path), tenant)
        if not delivered_url or delivered_url == source_exact_url:
            return None
        delivered_bytes = visual_writer_prepare._exact_bytes(
            delivered_url, visual_writer_prepare._bytes_for_url, "delivered")
        if delivered_bytes != rendered_bytes:
            return None

        source_hash = hashlib.md5(source_bytes).hexdigest()
        delivered_hash = hashlib.md5(delivered_bytes).hexdigest()
        evidence = {
            "source_exact_url": source_exact_url,
            "delivered_exact_url": delivered_url,
            "source_fingerprint": "md5:" + source_hash,
            "delivered_fingerprint": "md5:" + delivered_hash,
            "source_byte_length": len(source_bytes),
            "delivered_byte_length": len(delivered_bytes),
            "operation": "render",
            "evidence_ref": ("gym_media_builder:poster_render:" + source_hash + ":"
                             + delivered_hash + ":" + str(uuid.uuid4())),
            "observed_by": "gym_media_builder",
            "rendered_by": "gym_media_builder.video_poster_with_evidence",
        }
        from . import gym_media_index
        evidence["materialization_observation"] = gym_media_index.materialization_observation(
            source_bytes, rendered_bytes, delivered_url, tenant=tenant,
            source_url=source_exact_url,
            recipe={"name": "video_poster", "version": 1,
                    "seek_attempts_seconds": [1.0, 0.0], "frames": 1,
                    "jpeg_quality_parameter": 3, "max_width": 1080,
                    "runtime_verified": False})
        return delivered_url, evidence
    except Exception as exc:  # noqa: BLE001 - an unverifiable preview must not stage
        print(f"[gym-media-builder] evidenced poster skipped for "
              f"{os.path.basename(str(video_path))}: {type(exc).__name__}")
        return None
    finally:
        for path in (source_path, poster_path):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


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


def assert_source(asset, gym_base, store, source_ids=None):
    """CROSS-GYM SOURCE GUARD (2026-10-05): the picked asset's linked media_source
    MUST exist, be active and belong to the same gym before the builder hosts or
    stamps anything. A mismatch or missing source_id is BLOCKED (return False),
    ops is alerted once, and the asset is never staged. Unproven evidence (store
    cannot answer) also fails closed. Returns True only with complete same-gym
    active source evidence.

    source_ids: an already-validated same-gym active source-id set (the builder
    reads it once per build and reuses it across retry candidates). When None the
    evidence is read from the store; the pre-stamp re-check always reads fresh."""
    sid = str((asset or {}).get("source_id") or "").strip()
    try:
        if sid and source_ids is not None:
            return str(sid) in {str(i) for i in source_ids}
        if sid and _sel.asset_source_ok(asset, gym_base, store):
            return True
    except Exception as e:  # noqa: BLE001 - unproven evidence never stages
        _idx.dedup_alert(
            f"source_evidence_unproven:{asset.get('id')}",
            f"gym-media cross-gym source guard HELD an asset: the linked "
            f"media_source evidence for asset {asset.get('id')} (source_id="
            f"{sid!r}) could not be proven for gym={gym_base!r} "
            f"({type(e).__name__}). The asset was blocked and never published.")
        return False
    _idx.dedup_alert(
        f"source_mismatch:{asset.get('id')}",
        f"gym-media cross-gym source guard BLOCKED an asset: asset "
        f"{asset.get('id')} links source_id={sid!r} with no active same-gym "
        f"media_source for gym={gym_base!r}. The asset was blocked and never "
        "published.")
    return False


def _note_rendition_missing(store, asset):
    """A TRANSIENT skip (budget spent / transcode timed out): record the reason so the
    digest can count it, but leave the asset ELIGIBLE so the nightly pre-render pass
    (sync_gym_media) renders it and the next build can use it."""
    try:
        store.update_asset(asset["id"], {"reject_reason": _idx.REJECT_RENDITION_MISSING})
    except Exception as e:  # noqa: BLE001
        print(f"[gym-media-builder] rendition-missing note failed: {type(e).__name__}: {e}")


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
