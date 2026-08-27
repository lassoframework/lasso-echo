"""
podcast_library_builder.py — the podcast-slot flow from the spec's planner
wiring, behind PODCAST_LIBRARY_STAGE (default OFF):

  podcast slot -> podcast_selector.pick_clip()
               -> none? return None (the planner falls through to the existing
                  podcast builder / fallback pillars; pick_clip already fired
                  the one deduped pool-empty alert)
               -> got one? podcast_caption.draft (grounded in the episode's
                  notes Doc via export_doc_text — MISSING/EMPTY DOC = the slot
                  does NOT stage + one deduped alert)
               -> download to temp, ffprobe, validate (probe data written back;
                  a clip that fails the gate is marked and the NEXT clip tried)
               -> zernio media_generate_upload_link -> streamed PUT upload
               -> media_check_upload_status until ready
               -> Draft (status PENDING — the human tap is untouched)
               -> stamp_use (rolled back if the coach denies)

Temp files are deleted whether the slot stages or not (never accumulate).
Publish-time recheck is the existing publish_guard: a podcast row flows through
it like any row (caption rails + media_ready), nothing here bypasses it.
NO fabrication: the caption is the notes Doc's own text or the slot dies.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from . import config, podcast_caption as _cap, podcast_index as _idx
from . import podcast_selector as _sel
from .drafter import Draft, DraftStatus

_MAX_CLIP_ATTEMPTS = 3  # validation failures try the next clip, bounded


def _upload_clip(zc, path, filename, http=None):
    """Presign -> streamed PUT -> readiness check. Returns the hosted public
    URL, or None (logged) when any leg fails. Never logs a token."""
    try:
        presign = zc.media_generate_upload_link(filename, "video/mp4") or {}
        upload_url = presign.get("uploadUrl") or presign.get("upload_url") or ""
        public_url = presign.get("publicUrl") or presign.get("public_url") or ""
        if not upload_url or not public_url:
            print("[podcast-builder] presign returned no upload/public url")
            return None
        zc.media_upload_file(upload_url, path, "video/mp4")
        if not zc.media_check_upload_status(public_url):
            print("[podcast-builder] uploaded media never reported ready")
            return None
        return public_url
    except Exception as e:  # noqa: BLE001 - an upload failure skips the slot
        from . import ops_alerts
        print(f"[podcast-builder] upload failed: {type(e).__name__}: "
              f"{ops_alerts.scrub(e)}")
        return None


def build_podcast_clip_draft(account, day_key, *, store=None, drive=None,
                             zernio_client=None, probe_fn=None,
                             allowlist_fn=None, now=None):
    """A PENDING podcast Draft for `day_key` from the Drive clip library, or
    None (the planner then falls through to the existing podcast logic). Only
    ever called when PODCAST_LIBRARY_STAGE is ON (the caller gates)."""
    from .integrations import drive_client as _dc

    store = store or _idx.default_store()
    drive = drive or _dc.DriveClient()
    if not drive.available() or not store.available():
        print("[podcast-builder] lane unarmed (drive/store unavailable); "
              "falling through")
        return None
    if zernio_client is None:
        from .zernio import ZernioClient
        zernio_client = ZernioClient()
    probe_fn = probe_fn or _idx.probe_video

    acct_key = getattr(account, "key", "") or (account if isinstance(account, str) else "")
    # Same platform resolution the sprint builders use (real_month_run).
    platform = getattr(account, "platform", None) or acct_key or ""
    gym_base = _sel.base_gym_key(acct_key)

    tried = []
    for _attempt in range(_MAX_CLIP_ATTEMPTS):
        asset = _sel.pick_clip(store=store, now=now, exclude_ids=tuple(tried))
        if asset is None:
            return None  # pool empty: pick_clip already fired the one deduped alert
        tried.append(asset["id"])
        episode = asset.get("episode")

        # HARD RAIL (spec Wave 4): notes Doc missing or empty -> the slot does
        # NOT stage. One deduped alert per episode. No next-clip attempt: every
        # clip of the episode grounds in the same doc.
        notes_doc_id = asset.get("notes_doc_id")
        if not notes_doc_id:
            _idx.dedup_alert(f"notes_missing:{episode}",
                             f"podcast episode {episode} has no show-notes Doc in "
                             "Drive; its clips cannot stage (caption must ground "
                             "in the notes).")
            return None
        try:
            notes_text = drive.export_doc_text(notes_doc_id)
        except Exception as e:  # noqa: BLE001
            print(f"[podcast-builder] notes export failed for ep {episode}: "
                  f"{type(e).__name__}: {e}")
            return None
        if not str(notes_text or "").strip():
            _idx.dedup_alert(f"notes_empty:{episode}",
                             f"podcast episode {episode} show-notes Doc exports "
                             "EMPTY; its clips cannot stage (caption must ground "
                             "in the notes).")
            return None

        caption, meta = _cap.draft_caption(episode, notes_text, gym_id=gym_base,
                                           allowlist_fn=allowlist_fn)
        if not caption:
            print(f"[podcast-builder] ep {episode}: caption could not ground "
                  f"({(meta or {}).get('reason')}); slot not staged")
            return None

        # Download + probe + validate. Probe data is written back either way so
        # the index converges; a clip failing the gate is rejected and the next
        # clip is tried.
        tmp_dir = tempfile.mkdtemp(prefix="podclip_")
        tmp_path = Path(tmp_dir) / (asset.get("title") or "clip.mp4")
        try:
            try:
                drive.download(asset["id"], tmp_path)
            except Exception as e:  # noqa: BLE001
                print(f"[podcast-builder] download failed for {asset.get('title')!r}: "
                      f"{type(e).__name__}: {e}")
                return None
            info = probe_fn(tmp_path)
            if not info:
                print(f"[podcast-builder] probe failed for {asset.get('title')!r}; "
                      "not staging an unprobed file (fail closed)")
                continue
            aspect = _idx.aspect_of(info["width"], info["height"])
            size = tmp_path.stat().st_size or asset.get("size_bytes")
            postable, reject = _idx.postability(
                asset.get("kind"), size, info["duration_sec"], aspect)
            try:
                store.update_asset(asset["id"], {
                    "duration_sec": info["duration_sec"], "width": info["width"],
                    "height": info["height"], "aspect": aspect,
                    "postable": postable, "reject_reason": reject,
                })
            except Exception as e:  # noqa: BLE001 - write-back is best effort
                print(f"[podcast-builder] probe write-back failed: "
                      f"{type(e).__name__}: {e}")
            if postable is not True:
                print(f"[podcast-builder] {asset.get('title')!r} failed the gate "
                      f"({reject}); trying the next clip")
                continue

            public_url = _upload_clip(zernio_client, tmp_path, asset.get("title") or
                                      f"gmms_{episode}_clip.mp4")
            if not public_url:
                return None  # vendor-side failure: not a clip problem, stop the slot
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
                Path(tmp_dir).rmdir()
            except OSError:
                pass

        draft = Draft(
            draft_id=f"podlib_{episode}_{asset.get('clip_index') or 0}_{day_key}",
            account_key=acct_key,
            platform=platform,
            caption=caption,
            hashtags=[],
            creative_path=asset.get("title") or "",
            creative_public_url=public_url,
            scheduled_for="",
            status=DraftStatus.PENDING,   # the human tap is untouched
            day_key=day_key,
            draft_type="podcast",
            category="podcast",
            source_fragments=[f"drive_doc:{notes_doc_id}",
                              f"drive_clip:{asset['id']}"]
                             + [f"claim:{c}" for c in (meta or {}).get("claims", [])],
        )
        # Stamp ONLY now that the slot is staged; a coach deny rolls this back
        # (podcast_selector.on_draft_denied / observe_denials).
        try:
            _sel.stamp_use(asset, gym_base, day_key, store=store, now=now)
        except Exception as e:  # noqa: BLE001
            print(f"[podcast-builder] usage stamp failed: {type(e).__name__}: {e}")
        return draft
    return None
