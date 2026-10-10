"""video_thumb_fallback.py — representative-JPEG fallback for VIDEO assets whose
Google Drive generated thumbnail is materially uninformative (black, washed out,
featureless, anomalously tiny, or undecodable).

Fail-closed contract (echo thumbnail proxy repair, 2026-10-09):
  * The Drive thumbnail is preferred only when it decodes as an informative
    image of plausible size. Production audit 2026-10-09: real Drive posters
    are 10-16 KB; black posters were 632 bytes (std 0) and a blurred
    unidentifiable HYROX poster was 2332 bytes at high contrast. Anomalously
    tiny posters (< TINY_POSTER_SUSPECT_BYTES) are therefore treated as
    suspect even when their luma/std look "informative", and UNDECODABLE
    poster bytes are never served as an image (no 200 for garbage).
  * The fallback extracts candidate frames from the ORIGINAL Drive bytes with
    ffmpeg at bounded timestamps (fixed seeks including 2.5s plus fractions of
    the probed duration), scores each with balanced brightness/detail
    (grayscale mean + stddev from the histogram), and serves the BEST
    representative frame — not the first merely acceptable one.
  * If NO candidate frame is informative, callers return non-200. We never
    fabricate, tint, or placeholder a thumbnail.
  * Bounds: source videos with unknown, zero, or over-cap size are never
    downloaded (cap is MAX_SOURCE_BYTES, far below the old 300 MB); the
    actual downloaded size is re-verified; TOTAL ffmpeg time across all
    candidates is budget-limited (not 20s per timestamp); output is
    re-encoded, downscaled, and byte-capped. Results are cached in-process
    keyed by (gym, asset_id, content_hash), only when the content_hash is
    nonempty, so a content change invalidates the cache naturally.

ffmpeg/Pillow are existing project dependencies; both are imported lazily so
this module is import-safe in any environment.
"""
from __future__ import annotations

import io
import subprocess
import time

THUMB_MAX_DIM = 480                 # px, longest edge of the served JPEG
THUMB_MAX_BYTES = 512 * 1024        # never serve a fallback frame above this
MAX_SOURCE_BYTES = 60 * 1024 * 1024   # never download a larger original
FFMPEG_TOTAL_BUDGET_SEC = 18.0      # TOTAL across probe + all candidates (<20s)
FFMPEG_RUN_CAP_SEC = 6.0            # per individual ffmpeg/ffprobe invocation
TINY_POSTER_SUSPECT_BYTES = 4096    # smaller Drive posters are never trusted

FIXED_SEEKS = (0.25, 1.0, 2.5, 5.0)         # seconds; 2.5s verified informative
DURATION_FRACTIONS = (0.15, 0.35, 0.55, 0.8)  # portions of probed duration

MIN_MEAN_LUMA = 12.0    # below this a frame reads as black
MAX_MEAN_LUMA = 244.0   # above this a frame reads as washed out
MIN_DETAIL_STDDEV = 5.0  # below this a frame is featureless at ANY brightness
TARGET_MEAN_LUMA = 125.0  # production-verified good frames sit near 122-125

_CACHE_MAX = 128
_frame_cache: dict = {}


def cache_key(gym_id, asset_id, content_hash):
    return (str(gym_id), str(asset_id), str(content_hash or ""))


def get_cached(key):
    return _frame_cache.get(key)


def put_cached(key, data):
    if not key[2]:
        return                        # never cache without a real content hash
    if len(_frame_cache) >= _CACHE_MAX:
        _frame_cache.pop(next(iter(_frame_cache)))  # FIFO evict, bounded memory
    _frame_cache[key] = data


def clear_cache():
    _frame_cache.clear()


def _luma_stats(img):
    """(mean, stddev) of grayscale luma from the histogram — no numpy needed."""
    hist = img.convert("L").histogram()
    total = sum(hist) or 1
    mean = sum(i * n for i, n in enumerate(hist)) / total
    var = sum(((i - mean) ** 2) * n for i, n in enumerate(hist)) / total
    return mean, var ** 0.5


def _is_informative(mean, stddev):
    if stddev < MIN_DETAIL_STDDEV:
        return False          # featureless at any brightness (pure black counts)
    return MIN_MEAN_LUMA <= mean <= MAX_MEAN_LUMA


def _decode_stats(data):
    """(mean, stddev) or None when the bytes do not decode as an image."""
    from PIL import Image  # lazy: Pillow is a project dependency
    try:
        with Image.open(io.BytesIO(data)) as img:
            return _luma_stats(img)
    except Exception:  # noqa: BLE001 - undecodable bytes are not an image
        return None


def is_informative_image(data, *, suspect_tiny_bytes=None):
    """True only when JPEG/PNG bytes DECODE to an image with usable brightness
    and detail and (when suspect_tiny_bytes is set) a plausible byte size.

    Fail-closed changes (2026-10-09 audit of ENG production posters):
      * Undecodable bytes are False — they must never be served as a 200 image.
      * Bytes smaller than suspect_tiny_bytes are False even with good
        luma/std: real Drive posters are 10-16 KB; a 2.3 KB "poster" was a
        blurred unidentifiable smear, and sub-1 KB posters were pure black.
    Frame candidates produced by our own ffmpeg re-encode are judged WITHOUT
    the size heuristic (suspect_tiny_bytes=None): a clean frame is a frame.
    """
    if not data:
        return False
    if suspect_tiny_bytes is not None and len(data) < suspect_tiny_bytes:
        return False
    stats = _decode_stats(data)
    if stats is None:
        return False
    return _is_informative(*stats)


def _default_runner(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)


def _bounded_jpeg(raw_jpeg):
    """Downscale + byte-cap a candidate frame. Returns JPEG bytes or None."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(raw_jpeg)) as img:
            img = img.convert("RGB")
            img.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM))
            for quality in (80, 60, 40):
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality)
                data = buf.getvalue()
                if len(data) <= THUMB_MAX_BYTES:
                    return data
    except Exception:  # noqa: BLE001 - a bad candidate is skipped, never served
        return None
    return None


def _probe_duration(video_path, run, timeout):
    """Duration in seconds via ffprobe, or None when unavailable/unparseable."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)]
    try:
        proc = run(cmd, timeout)
    except Exception:  # noqa: BLE001 - missing binary/timeout: unknown duration
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    try:
        dur = float((getattr(proc, "stdout", b"") or b"").decode("utf-8", "replace").strip())
    except ValueError:
        return None
    return dur if dur > 0 else None


def _candidate_timestamps(duration):
    stamps = list(FIXED_SEEKS)
    if duration:
        stamps.extend(round(duration * f, 3) for f in DURATION_FRACTIONS)
        stamps = [s for s in stamps if s < max(duration - 0.05, 0.05)]
    # unique, sorted: near-start frames first, duration-fraction frames included
    return sorted(set(stamps))


def _representative_score(mean, stddev):
    """Balanced brightness/detail: reward detail, penalize distance from the
    well-exposed luma band (production-good frames sit near 122-125)."""
    return stddev - abs(mean - TARGET_MEAN_LUMA)


def representative_frame(video_path, *, runner=None, clock=None):
    """(jpeg_bytes | None): the BEST informative frame among bounded candidate
    timestamps (fixed seeks incl. 2.5s plus fractions of the probed duration),
    re-encoded and byte-bounded. Not the first acceptable frame: every
    candidate within the TOTAL ffmpeg time budget is scored on balanced
    brightness/detail and the most representative wins. None when ffmpeg fails
    on every candidate or every candidate is black/washed/featureless — never
    a fabricated image. Total ffmpeg wall time is capped at
    FFMPEG_TOTAL_BUDGET_SEC across the probe and all candidate extractions.
    """
    run = runner or _default_runner
    now = clock or time.monotonic
    deadline = now() + FFMPEG_TOTAL_BUDGET_SEC

    def remaining():
        left = deadline - now()
        return left if left > 0.25 else 0.0

    budget = remaining()
    if budget <= 0:
        return None
    duration = _probe_duration(video_path, run, min(FFMPEG_RUN_CAP_SEC, budget))

    best = None
    best_score = None
    for ts in _candidate_timestamps(duration):
        budget = remaining()
        if budget <= 0:
            break
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", str(ts),
               "-i", str(video_path), "-frames:v", "1",
               "-vf", f"scale='min({THUMB_MAX_DIM},iw)':-2",
               "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
        try:
            proc = run(cmd, min(FFMPEG_RUN_CAP_SEC, budget))
        except Exception:  # noqa: BLE001 - timeout/missing binary: try next ts
            continue
        out = getattr(proc, "stdout", b"") or b""
        rc = getattr(proc, "returncode", 1)
        if rc != 0 or not out:
            continue
        bounded = _bounded_jpeg(out)
        if bounded is None:
            continue
        stats = _decode_stats(bounded)
        if stats is None or not _is_informative(*stats):
            continue
        score = _representative_score(*stats)
        if best_score is None or score > best_score:
            best, best_score = bounded, score
    return best
