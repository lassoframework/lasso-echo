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


def _episode_asset_ids(store, episode):
    """Every asset id of `episode` in the store — so an un-groundable pick can
    exclude its whole episode and move to the next groundable clip (a stray
    un-groundable pick must never sink a slot). Read failure -> [] (the attempt
    loop simply retries; bounded by _MAX_CLIP_ATTEMPTS)."""
    try:
        return [a.get("id") for a in store.list_assets()
                if a.get("episode") == episode and a.get("id")]
    except Exception as e:  # noqa: BLE001
        print(f"[podcast-builder] episode-asset sweep failed for ep {episode}: "
              f"{type(e).__name__}: {e}")
        return []


def _grounding_feed_map():
    """The RSS feed's {episode -> entry} grounding map, best-effort. Any failure
    -> {} (grounding then leans on the Drive Doc alone). Never raises."""
    try:
        from . import podcast_feed_notes as _fn
        return _fn.episode_map()
    except Exception as e:  # noqa: BLE001
        print(f"[podcast-builder] feed grounding map lookup failed "
              f"({type(e).__name__}: {e}); grounding on Drive Docs only")
        return {}


def _cap_feed_text(feed_map, episode):
    """The flattened feed grounding text (title + description) for `episode`, or
    '' when the feed does not carry it."""
    try:
        entry = (feed_map or {}).get(int(episode)) if episode is not None else None
    except (TypeError, ValueError):
        entry = None
    if not entry:
        return ""
    from . import podcast_feed_notes as _fn
    return _fn.feed_text_for_grounding(entry)


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
                             allowlist_fn=None, now=None, feed_map=None):
    """A PENDING podcast Draft for `day_key` from the Drive clip library, or
    None (the planner then falls through to the existing podcast logic). Only
    ever called when PODCAST_LIBRARY_STAGE is ON (the caller gates).

    `feed_map` ({episode -> {'title','description','pubdate'}}) is the RSS feed
    grounding map; pass it to keep the slot offline/deterministic (tests do), or
    leave None to have it fetched best-effort (6h-cached)."""
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

    # The RSS feed is the PRIMARY grounding source now (Blake 2026-08-27). Fetch
    # its episode map ONCE for this slot (6h-cached, best-effort empty on failure)
    # and hand the selector the set of episodes the feed grounds so it prefers
    # groundable clips.
    feed_map = _grounding_feed_map() if feed_map is None else dict(feed_map)
    feed_episodes = set(feed_map.keys())

    tried = []
    for _attempt in range(_MAX_CLIP_ATTEMPTS):
        asset = _sel.pick_clip(store=store, now=now, exclude_ids=tuple(tried),
                               feed_episodes=feed_episodes)
        if asset is None:
            return None  # pool empty: pick_clip already fired the one deduped alert
        tried.append(asset["id"])
        episode = asset.get("episode")

        # Assemble grounding: RSS feed entry (primary) + Drive show-notes Doc
        # (supplement/fallback). Either source alone can ground the caption.
        feed_text = _cap_feed_text(feed_map, episode)
        notes_doc_id = asset.get("notes_doc_id")
        notes_text = ""
        if str(notes_doc_id or "").strip():
            try:
                notes_text = drive.export_doc_text(notes_doc_id) or ""
            except Exception as e:  # noqa: BLE001 - a Doc read failure falls back to the feed
                print(f"[podcast-builder] notes export failed for ep {episode}: "
                      f"{type(e).__name__}: {e}; leaning on the feed if it grounds")
                notes_text = ""

        # HARD RAIL (belt and suspenders): pick_clip already filters to groundable
        # clips, so an un-groundable asset only reaches here if a stray slips the
        # selector (or a Doc that exported empty while the feed also lacks it).
        # When it does, alert once for the episode, exclude EVERY asset of that
        # episode, and try the NEXT groundable clip — a stray un-groundable pick
        # must never sink a slot that has groundable clips left.
        if not str(feed_text or "").strip() and not str(notes_text or "").strip():
            _idx.dedup_alert(f"notes_missing:{episode}",
                             f"podcast episode {episode} has neither an RSS feed "
                             "entry nor a show-notes Doc; its clips cannot stage "
                             "(caption must ground in real episode text).")
            tried.extend(_episode_asset_ids(store, episode))
            continue

        caption, meta = _cap.draft_caption(episode, notes_text, feed_text=feed_text,
                                           gym_id=gym_base, allowlist_fn=allowlist_fn)
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

        ground_frags = []
        if str(feed_text or "").strip():
            ground_frags.append(f"rss_feed:episode_{episode}")
        if str(notes_text or "").strip() and str(notes_doc_id or "").strip():
            ground_frags.append(f"drive_doc:{notes_doc_id}")
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
            source_fragments=ground_frags + [f"drive_clip:{asset['id']}"]
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
