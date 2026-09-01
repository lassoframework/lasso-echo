"""
gym_media_index.py — classify + gate a gym's Drive media into media_asset rows,
plus the HEIC/HEVC conversion pipeline (gym_media_drive spec §4, §5).

Mirrors podcast_index.py (classify -> gate -> probe, fail-closed) but for a gym's
photo/video folder rather than the podcast library:

  * MIME filter: image/* -> photo, video/* -> video, everything else (docs, pdf,
    zip) is LOGGED and SKIPPED (never indexed, never raised).
  * DEDUPE on content_hash (Drive md5Checksum): the SAME bytes re-uploaded (a
    coach dragging the folder in twice) or living in two folders index ONCE — the
    earliest-modified copy wins.
  * ELIGIBILITY gate (fail closed):
      - photo: short edge >= 640 px. Aspect in 4:5..1.91:1 is native-eligible; out
        of band is still eligible but carries a crop_hint to the nearest legal
        aspect (applied at draft time; the card shows the crop).
      - video: 3..90 s, <= 900 MB pre-transcode. Duration is unknown until ffprobe
        runs on first download -> eligible is NULL and the asset is NOT selectable
        (an unprobed video never stages).
  * HEIC/HEVC: an iPhone HEIC photo or HEVC/H.265 video is ELIGIBLE (conversion is
    Echo's job, §5). The rendition is produced on first use, cached by content_hash
    in Echo's bucket, and reused thereafter. Degrades gracefully: no pillow-heif /
    no ffmpeg -> the asset is marked not-eligible with a reason, never a crash.

Nothing here publishes, picks, or captions. It only turns a Drive listing into
gated rows and, on demand, produces a playable/serveable rendition.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config

FOLDER_MIME = "application/vnd.google-apps.folder"

KIND_PHOTO = "photo"
KIND_VIDEO = "video"
KIND_OTHER = "other"

# ---- eligibility constants (spec §4) ----------------------------------------
MIN_PHOTO_SHORT_EDGE = 640            # px, short edge
MIN_VIDEO_SEC = 3
MAX_VIDEO_SEC = 90
MAX_VIDEO_BYTES = 900_000_000         # 900 MB pre-transcode ceiling

# Legal photo aspect band (width/height): portrait 4:5 .. landscape 1.91:1.
ASPECT_MIN = 4 / 5                    # 0.8  (tallest allowed)
ASPECT_MAX = 1.91                     # widest allowed
_LEGAL_ASPECTS = (("4:5", 4 / 5), ("1:1", 1.0), ("1.91:1", 1.91))

REJECT_PHOTO_SMALL = "photo_short_edge_under_640"
REJECT_VIDEO_DURATION = "video_duration_out_of_range"
REJECT_VIDEO_SIZE = "video_over_900mb"
REJECT_UNREADABLE = "unreadable_image"
REJECT_REMOVED = "removed_from_drive"
REJECT_HIDDEN = "media_hidden"
REJECT_CONVERT_UNAVAILABLE = "conversion_unavailable"     # heic/hevc but no converter
# 2026-09-01 hardening (the finished-content-in-raw-pool proof-run miss): a
# confident story_classifier FINISHED verdict quarantines the SAME way any other
# ineligible asset does (eligible=False + reject_reason) so the EXISTING gates
# (story_candidates._eligible_raw, gym_media_selector.pick_media) exclude it —
# never a delete, never a new gate to keep in sync.
REJECT_FINISHED_CONTENT = "classifier_finished_content"
# A human/consent review flag (e.g. a minor visible in the frame). Set by hand or
# by a targeted sweep, never inferred by the raw/finished classifier itself (that
# is a different judgment than raw-vs-finished framing).
REJECT_CONSENT_REVIEW = "consent_review_required"

# HEIC / HEVC detection (both mime and extension, since Drive mislabels HEIC as
# image/heic OR the generic application/octet-stream depending on the client).
_HEIC_MIMES = ("image/heic", "image/heif", "image/heic-sequence")
_HEIC_EXTS = (".heic", ".heif")
_HEVC_HINT_EXTS = (".mov",)          # iPhone .mov is usually HEVC; probed to confirm
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".heic",
               ".heif")
_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv", ".hevc")


def _ext(title):
    return os.path.splitext(str(title or ""))[1].lower()


def classify(title, mime_type):
    """(kind) for one file: 'photo' | 'video' | 'other'. image/* -> photo,
    video/* -> video, else fall back to the extension, else 'other' (logged +
    skipped by the caller — docs/pdf/zip never index)."""
    mime = str(mime_type or "").strip().lower()
    if mime.startswith("image/"):
        return KIND_PHOTO
    if mime.startswith("video/"):
        return KIND_VIDEO
    ext = _ext(title)
    if ext in _IMAGE_EXTS:
        return KIND_PHOTO
    if ext in _VIDEO_EXTS:
        return KIND_VIDEO
    return KIND_OTHER


def is_heic(title, mime_type):
    """True when a photo is HEIC/HEIF (needs JPEG conversion before use)."""
    mime = str(mime_type or "").strip().lower()
    return mime in _HEIC_MIMES or _ext(title) in _HEIC_EXTS


def aspect_band(width, height):
    """('4:5'|'1:1'|'1.91:1'|'other', crop_hint). The label is the nearest LEGAL
    aspect; crop_hint is None when the native ratio is already inside 4:5..1.91:1,
    else the nearest legal aspect the draft-time center-crop should target. Unknown
    dims -> ('other', None)."""
    try:
        w, h = float(width), float(height)
    except (TypeError, ValueError):
        return "other", None
    if w <= 0 or h <= 0:
        return "other", None
    ratio = w / h
    # Native band: no crop needed. Label with the closest legal aspect anyway (UI).
    nearest = min(_LEGAL_ASPECTS, key=lambda p: abs(ratio - p[1]))[0]
    if ASPECT_MIN - 1e-6 <= ratio <= ASPECT_MAX + 1e-6:
        return nearest, None
    # Out of band: crop toward the nearest legal aspect (tall -> 4:5, wide -> 1.91:1).
    target = "4:5" if ratio < ASPECT_MIN else "1.91:1"
    return target, target


# ---- image dims (Pillow, lazy) ----------------------------------------------
def image_dims(path):
    """(width, height) for an image file via Pillow, or None when unreadable. HEIC
    is read only when pillow-heif is installed (registered lazily); without it a
    HEIC file returns None here and is handled by the convert path, not rejected as
    unreadable."""
    try:
        from PIL import Image  # lazy
        try:
            import pillow_heif  # noqa: F401 - registers the HEIF opener if present
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:
        return None


def probe_video(path, runner=None):
    """{'duration_sec','width','height','codec'} via ffprobe, or None when ffprobe
    is missing/fails (the asset then stays unprobed -> not selectable, fail
    closed). codec lets the caller decide whether an HEVC transcode is needed."""
    run = runner or subprocess.run
    try:
        proc = run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=120)
        data = json.loads(proc.stdout or "{}")
    except Exception as e:  # noqa: BLE001 - a probe failure is a skip, not a crash
        print(f"[gym-media] ffprobe failed for {path}: {type(e).__name__}: {e}")
        return None
    duration = None
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        pass
    width = height = codec = None
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video":
            width, height, codec = stream.get("width"), stream.get("height"), \
                str(stream.get("codec_name") or "").lower()
            if duration is None:
                try:
                    duration = float(stream.get("duration"))
                except (TypeError, ValueError):
                    pass
            break
    if duration is None or not width or not height:
        return None
    return {"duration_sec": duration, "width": int(width), "height": int(height),
            "codec": codec or ""}


# ---- eligibility gates (fail closed) ----------------------------------------
def photo_eligibility(width, height):
    """(eligible, reject_reason, aspect_label, crop_hint) for a photo. None dims ->
    (False, unreadable). Short edge < 640 -> rejected. Aspect never rejects — an
    out-of-band photo is eligible with a crop_hint."""
    if not width or not height:
        return False, REJECT_UNREADABLE, None, None
    short_edge = min(int(width), int(height))
    label, crop = aspect_band(width, height)
    if short_edge < MIN_PHOTO_SHORT_EDGE:
        return False, REJECT_PHOTO_SMALL, label, crop
    return True, None, label, crop


def video_eligibility(size_bytes, duration_sec=None, width=None, height=None):
    """(eligible, reject_reason, aspect_label) for a video. duration None (unprobed)
    -> (None, None) = candidate but NOT selectable (fail closed). Over 900 MB or
    outside 3..90 s -> rejected."""
    try:
        size = int(size_bytes or 0)
    except (TypeError, ValueError):
        size = 0
    if size > MAX_VIDEO_BYTES:
        return False, REJECT_VIDEO_SIZE, None
    if duration_sec is None:
        return None, None, None            # unprobed: never selectable
    try:
        dur = float(duration_sec)
    except (TypeError, ValueError):
        return False, REJECT_VIDEO_DURATION, None
    label = aspect_band(width, height)[0] if width and height else None
    if not (MIN_VIDEO_SEC <= dur <= MAX_VIDEO_SEC):
        return False, REJECT_VIDEO_DURATION, label
    return True, None, label


# ---- build rows from a walked listing (dedupe on content_hash) --------------
def build_rows(files, source_id, gym_id, now_iso=None, log=None):
    """Turn a walked Drive listing into media_asset row dicts for ONE source.

    Returns (rows, skipped). Dedupe: files sharing a content_hash index ONCE, the
    earliest drive_modified kept (a coach re-dropping the folder, or the same photo
    in two subfolders, is one asset). Photos are gated from Drive-reported
    dimensions when present; videos stay unprobed (eligible NULL) until the probe
    pass. HEIC photos and HEVC-suspect videos are eligible pending conversion (the
    convert step decides on first use). `skipped` is [(title, why)] for docs/pdf/
    zip and unclassifiable files — logged, never raised."""
    log = log or (lambda m: print(f"[gym-media] {m}"))
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()

    by_hash = {}           # content_hash -> chosen raw file
    no_hash = []           # files Drive did not md5 (Google-native etc.); keep each
    rows, skipped = [], []
    for f in files:
        if getattr(f, "mime_type", "") == FOLDER_MIME:
            continue
        kind = classify(getattr(f, "title", ""), getattr(f, "mime_type", ""))
        if kind == KIND_OTHER:
            skipped.append((getattr(f, "title", ""), "not image/* or video/*"))
            log(f"skip {getattr(f, 'title', '')!r}: not image/video (doc/pdf/zip)")
            continue
        h = getattr(f, "content_hash", "") or ""
        if not h:
            no_hash.append((kind, f))
            continue
        cur = by_hash.get(h)
        if cur is None or _modified_of(f) < _modified_of(cur[1]):
            by_hash[h] = (kind, f)

    chosen = list(by_hash.values()) + no_hash
    for kind, f in chosen:
        title = getattr(f, "title", "")
        mime = getattr(f, "mime_type", "")
        size = getattr(f, "size_bytes", 0)
        w = getattr(f, "width", None)
        h = getattr(f, "height", None)
        row = {
            "id": getattr(f, "id", ""),
            "source_id": source_id,
            "gym_id": gym_id,
            "kind": kind,
            "title": title,
            "mime_type": mime,
            "size_bytes": size,
            "content_hash": getattr(f, "content_hash", "") or None,
            "drive_modified": getattr(f, "modified_time", "") or None,
            "duration_sec": None,
            "width": w,
            "height": h,
            "aspect": None,
            "crop_hint": None,
            "vision_json": None,
            "rendition_key": None,
            "rendition_url": None,
            "excluded_by_coach": False,
            "used_count": 0,
            "indexed_at": now_iso,
            # PROVENANCE (2026-09-01): stamped ONCE, at true insert time, and never
            # touched by the changed-field PATCH path (it is deliberately absent
            # from sync_gym_media._OWNED_FIELDS) — unlike indexed_at, which is
            # bumped on every re-sync PATCH and so answers "last touched", not
            # "first seen". Answers a real "where/when did this footage first show
            # up" question with a fact Echo actually knows, not a guess.
            "first_indexed_at": now_iso,
        }
        if kind == KIND_PHOTO:
            # Native photo: gate from Drive dims when present, else leave NULL for
            # the convert/probe path to decide (HEIC has no Drive dims). HEIC is
            # eligible-pending-conversion: never rejected here.
            if is_heic(title, mime):
                row["eligible"] = True         # conversion is Echo's job (§5)
                row["reject_reason"] = None
                row["aspect"] = None
            elif w and h:
                el, reason, label, crop = photo_eligibility(w, h)
                row["eligible"] = el
                row["reject_reason"] = reason
                row["aspect"] = label
                row["crop_hint"] = crop
            else:
                row["eligible"] = None         # dims unknown until first download
        else:  # video
            el, reason, label = video_eligibility(size)
            row["eligible"] = el               # NULL until probed (fail closed)
            row["reject_reason"] = reason
            row["aspect"] = label
        rows.append(row)
    return rows, skipped


def _modified_of(f):
    return str(getattr(f, "modified_time", "") or "")


# ---- HEIC/HEVC conversion pipeline (§5) --------------------------------------
class ConversionUnavailable(Exception):
    """pillow-heif (HEIC) or ffmpeg (HEVC) is not installed. The caller marks the
    asset not-eligible with REJECT_CONVERT_UNAVAILABLE — never a crash."""


def heic_to_jpeg(src_path, dest_path):
    """Convert a HEIC/HEIF file to JPEG at dest_path via pillow-heif. Raises
    ConversionUnavailable when pillow-heif/Pillow is absent (degrade gracefully).
    Returns dest_path."""
    try:
        from PIL import Image
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception as e:  # noqa: BLE001
        raise ConversionUnavailable(f"pillow-heif unavailable: {type(e).__name__}") from e
    with Image.open(src_path) as im:
        rgb = im.convert("RGB")
        rgb.save(dest_path, format="JPEG", quality=90)
    return dest_path


def hevc_to_h264(src_path, dest_path, runner=None):
    """Transcode an HEVC/H.265 video to H.264 (yuv420p) at dest_path via ffmpeg.
    Raises ConversionUnavailable when ffmpeg is missing or the transcode fails
    (degrade gracefully). Returns dest_path."""
    run = runner or subprocess.run
    try:
        proc = run(
            ["ffmpeg", "-y", "-i", str(src_path),
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-movflags", "+faststart", str(dest_path)],
            capture_output=True, text=True, timeout=600)
    except Exception as e:  # noqa: BLE001 - missing ffmpeg reads as unavailable
        raise ConversionUnavailable(f"ffmpeg unavailable: {type(e).__name__}") from e
    if getattr(proc, "returncode", 1) != 0 or not os.path.exists(dest_path):
        raise ConversionUnavailable("ffmpeg transcode failed")
    return dest_path


def rendition_key(gym_id, content_hash, ext):
    """The Echo-bucket key for a cached rendition, PATH-SCOPED by gym (spec §1.5e):
    gym_id/content_hash.ext. content_hash keying means the SAME source bytes
    convert once and every later use is a cache hit (no re-transcode)."""
    safe_gym = str(gym_id or "unknown").strip().strip("/") or "unknown"
    ch = str(content_hash or "nohash").strip()
    return f"{safe_gym}/{ch}{ext}"


def ensure_rendition(asset, src_path, *, store=None, host_fn=None, exists_fn=None,
                     public_url_fn=None, heic_fn=None, hevc_fn=None, probe_fn=None):
    """Produce (or reuse) a playable/serveable rendition for a HEIC photo or HEVC
    video and return its public url. Cache is keyed by content_hash under
    gym_id/... in Echo's bucket, so a SECOND use is a pure cache hit (no
    re-transcode). The Drive original is never touched.

    Returns (public_url, converted): public_url is None when no conversion is
    needed (a plain JPEG/H.264 asset — caller uses the original) OR when a
    converter is unavailable (the caller then marks the asset not-eligible with
    REJECT_CONVERT_UNAVAILABLE). converted is True only when a NEW rendition was
    produced this call (False on a cache hit or a no-op)."""
    from . import media_host as _mh
    store = store or default_store()
    host_fn = host_fn or _mh.host_media
    exists_fn = exists_fn or (lambda key: False)
    public_url_fn = public_url_fn or _mh.public_url_for
    heic_fn = heic_fn or heic_to_jpeg
    hevc_fn = hevc_fn or hevc_to_h264

    title = asset.get("title") or ""
    mime = asset.get("mime_type") or ""
    gym_id = asset.get("gym_id")
    content_hash = asset.get("content_hash")

    # Already cached? Reuse without any decode/transcode.
    if asset.get("rendition_url"):
        return asset["rendition_url"], False

    needs_heic = asset.get("kind") == KIND_PHOTO and is_heic(title, mime)
    needs_hevc = False
    if asset.get("kind") == KIND_VIDEO:
        codec = ""
        if probe_fn and _ext(title) in _HEVC_HINT_EXTS:
            info = probe_fn(src_path) or {}
            codec = str(info.get("codec") or "").lower()
        needs_hevc = codec in ("hevc", "h265", "h.265")
    if not needs_heic and not needs_hevc:
        return None, False               # plain asset: caller uses the original

    ext = ".jpg" if needs_heic else ".mp4"
    key = rendition_key(gym_id, content_hash, ext)
    # Cache hit by content-addressed key (a re-run before the url was persisted).
    try:
        if exists_fn(key):
            url = public_url_fn(key)
            _persist_rendition(store, asset, key, url)
            return url, False
    except Exception:  # noqa: BLE001 - a cache probe failure just re-converts
        pass

    tmp_dir = tempfile.mkdtemp(prefix="gymrend_")
    out_path = Path(tmp_dir) / os.path.basename(key)
    try:
        try:
            if needs_heic:
                heic_fn(src_path, out_path)
            else:
                hevc_fn(src_path, out_path)
        except ConversionUnavailable as e:
            print(f"[gym-media] rendition skipped for {title!r}: {e}")
            return None, False           # caller marks not-eligible, never crashes
        url = host_fn(str(out_path), gym_id) if host_fn else None
        if not url:
            print(f"[gym-media] rendition upload returned no url for {title!r}")
            return None, False
        _persist_rendition(store, asset, key, url)
        return url, True
    finally:
        try:
            if out_path.exists():
                out_path.unlink()
            os.rmdir(tmp_dir)
        except OSError:
            pass


def _persist_rendition(store, asset, key, url):
    try:
        store.update_asset(asset["id"], {"rendition_key": key, "rendition_url": url})
        asset["rendition_key"] = key
        asset["rendition_url"] = url
    except Exception as e:  # noqa: BLE001 - best effort; a cache write failure re-converts
        print(f"[gym-media] rendition persist failed: {type(e).__name__}: {e}")


# ---- deduped ops alert (shared by the sync job + selector) -------------------
def dedup_alert(stamp_key, message):
    """Fire ops_alerts.alert(message) at most ONCE per stamp_key, durable-or-silent
    (db.kv_is_durable — the storm rule: a process whose kv stamps die with it must
    not alert at all). Returns True when the alert actually fired this call."""
    from . import db, ops_alerts
    if not db.kv_is_durable():
        print(f"[gym-media] alert suppressed (kv not durable): {message}")
        return False
    key = f"gym_media_alert:{stamp_key}"
    if db.kv_get(key, ""):
        return False
    db.kv_set(key, datetime.now(timezone.utc).isoformat())
    ops_alerts.alert(message)
    return True


def clear_alert_stamp(stamp_key):
    """Reset a dedup stamp so the NEXT occurrence alerts once again."""
    from . import db
    try:
        db.kv_set(f"gym_media_alert:{stamp_key}", "")
    except Exception:
        pass


def default_store():
    from . import media_source_store
    return media_source_store.default_store()


def asset_provenance(asset, source=None, *, store=None):
    """Where this pool item's footage actually came from, from data Echo already
    has at ingest time (2026-09-01, per-clip provenance): the Drive folder
    (media_source.folder_name / folder_id), the original filename (title), when
    Drive last touched the file (drive_modified), and when Echo first indexed it
    (first_indexed_at — NOT indexed_at, which is bumped on every re-sync PATCH and
    so answers 'last touched', not 'first seen'). No new collection: every field
    here was already known, just not assembled into one answer before.

    `source` may be passed in (a media_source row) to avoid a lookup when the
    caller already has it (e.g. a per-source sweep); otherwise it is looked up
    via `store` (default_store()). Never raises: a lookup failure just leaves the
    folder fields blank rather than losing the fields Echo DOES know."""
    asset = asset or {}
    folder_name = folder_id = None
    source_id = asset.get("source_id")
    if source is None and source_id:
        try:
            store = store or default_store()
            for s in store.list_sources():
                if s.get("id") == source_id:
                    source = s
                    break
        except Exception as e:  # noqa: BLE001
            print(f"[gym-media] provenance source lookup failed: "
                  f"{type(e).__name__}: {e}")
    if source:
        folder_name = source.get("folder_name")
        folder_id = source.get("folder_id")
    # first_indexed_at may be absent on rows inserted before this column existed
    # (backfilled from indexed_at, a lower bound: the true first-seen time for
    # those rows is not recoverable, so this is the earliest fact Echo has).
    first_seen = asset.get("first_indexed_at") or asset.get("indexed_at")
    return {
        "asset_id": asset.get("id"),
        "title": asset.get("title"),
        "source_id": source_id,
        "drive_folder_name": folder_name,
        "drive_folder_id": folder_id,
        "drive_modified": asset.get("drive_modified"),
        "first_indexed_at": first_seen,
        "first_indexed_is_estimate": not bool(asset.get("first_indexed_at")),
    }
