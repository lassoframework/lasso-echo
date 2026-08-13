"""
Action-cut Reels for CLIENT gym videos (flag: AGENT_CLIENT_VIDEO_EDIT, default OFF).

A gym uploads raw action footage (a class, a workout, members moving). Posting it as-is
makes a weak Reel: too long, wrong aspect, no hook. This lane edits it into an engaging
one — pure ffmpeg, ZERO AI spend, deterministic (same input -> same output):

  1. MOTION PROFILE   sample per-frame scene-change scores (a cheap motion proxy) on a
                      downscaled stream.
  2. PICK SEGMENTS    choose the highest-action non-overlapping windows (2.0-3.5s each)
                      up to ~AGENT_REEL_TARGET_SEC total, then play them in
                      CHRONOLOGICAL order — fast cuts carry the energy, the timeline
                      stays coherent. A short clip (<= target) keeps its full length.
  3. ASSEMBLE         cut + concat -> 1080x1920 (9:16 cover crop) 30fps -> a TEXT HOOK
                      overlaid for the first seconds (from the day's approved caption,
                      scrubbed by the on-screen copy law: NO dashes ever) -> h264/aac,
                      faststart.

HARD RULES
  * The hook text comes ONLY from the post's own approved caption (never invented) and
    goes through clipper_render.scrub_onscreen (the no-dash law).
  * Editing only ENHANCES: any failure returns None and the RAW video posts as-is.
    This lane can never block a post.
  * Nothing here publishes. The edited reel replaces the draft's creative and waits for
    the client's approval in the portal like every other post.
  * Cache: edits land in <library>/reels/ keyed by content hash — a re-run or rebuild
    never re-encodes, and the subdir is invisible to the media count / pick pool.
"""

import hashlib
import os
import re
import subprocess

from . import config
from .clipper_render import scrub_onscreen

# Segment shape: fast-cut pacing.
SEG_MIN_SEC = 2.0
SEG_MAX_SEC = 3.5
SEG_GAP_SEC = 0.5          # minimum spacing between chosen windows
PROFILE_SCALE = 160        # motion pass runs on a tiny stream: cheap + plenty accurate
HOOK_SECONDS = 3.0         # how long the hook text stays up
HOOK_MAX_CHARS = 42

_REEL_SUBDIR = "reels"


class ReelError(Exception):
    pass


# --------------------------------------------------------------------------
# ffmpeg seams (injectable so every decision above them unit-tests offline)
# --------------------------------------------------------------------------

def _run(cmd, label="ffmpeg"):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ReelError(f"{label} failed ({result.returncode}): "
                        f"{(result.stderr or '')[-300:]}")
    return result


def probe(path, runner=None):
    """(duration_sec, width, height, has_audio) via ffprobe. Raises ReelError."""
    run = runner or _run
    result = run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,width,height",
        "-of", "default=noprint_wrappers=1", str(path),
    ], "ffprobe")
    duration, width, height, has_audio = 0.0, 0, 0, False
    for line in (result.stdout or "").splitlines():
        k, _, v = line.partition("=")
        if k == "duration":
            try:
                duration = float(v)
            except ValueError:
                pass
        elif k == "width" and v.isdigit() and not width:
            width = int(v)
        elif k == "height" and v.isdigit() and not height:
            height = int(v)
        elif k == "codec_type" and v.strip() == "audio":
            has_audio = True
    if duration <= 0:
        raise ReelError("could not probe duration")
    return duration, width, height, has_audio


_SCORE_RE = re.compile(r"pts_time:(?P<t>[0-9.]+)")
_SCENE_RE = re.compile(r"lavfi\.scene_score=(?P<s>[0-9.]+)")


def motion_profile(path, runner=None):
    """[(t_seconds, score), ...] per frame, from ffmpeg's scene-change scores on a
    downscaled stream. Higher score = more visual change = more action."""
    run = runner or _run
    result = run([
        "ffmpeg", "-hide_banner", "-i", str(path),
        "-vf", f"scale={PROFILE_SCALE}:-2,select='gte(scene,0)',metadata=print",
        "-f", "null", "-",
    ], "motion-profile")
    out = (result.stderr or "") + (result.stdout or "")
    profile = []
    t = None
    for line in out.splitlines():
        mt = _SCORE_RE.search(line)
        if mt:
            t = float(mt.group("t"))
            continue
        ms = _SCENE_RE.search(line)
        if ms and t is not None:
            profile.append((t, float(ms.group("s"))))
            t = None
    return profile


# --------------------------------------------------------------------------
# PURE segment selection (no I/O; fully unit-tested)
# --------------------------------------------------------------------------

def pick_segments(profile, duration, target_total=None):
    """Choose non-overlapping high-action windows totalling ~target_total seconds,
    returned in CHRONOLOGICAL order as [(start, end), ...].

    A clip shorter than the target is kept whole ([(0, duration)]). An empty/flat
    profile falls back to evenly spaced segments so a static camera still yields a
    valid fast-cut reel (deterministic, never random)."""
    target = float(target_total or config.reel_target_sec())
    if duration <= target:
        return [(0.0, round(duration, 2))]

    # Bucket per-frame scores into half-second bins.
    bins = {}
    for t, s in (profile or []):
        bins[int(t * 2)] = bins.get(int(t * 2), 0.0) + s

    seg_len = min(SEG_MAX_SEC, max(SEG_MIN_SEC, target / 8.0))
    n_segs = max(1, int(round(target / seg_len)))
    half_steps = int(seg_len * 2)

    # Score every candidate window start (half-second grid).
    last_start = duration - seg_len
    candidates = []
    step = 0
    while step / 2.0 <= last_start:
        score = sum(bins.get(step + i, 0.0) for i in range(half_steps))
        candidates.append((score, step / 2.0))
        step += 1

    if not candidates or all(c[0] == 0.0 for c in candidates):
        # Flat/no profile: spread segments evenly across the clip.
        spacing = duration / n_segs
        return [(round(i * spacing, 2), round(min(i * spacing + seg_len, duration), 2))
                for i in range(n_segs)]

    # Greedy: best-scoring windows first, enforcing non-overlap (+gap).
    candidates.sort(key=lambda c: (-c[0], c[1]))
    chosen = []
    for score, start in candidates:
        if len(chosen) >= n_segs:
            break
        end = start + seg_len
        if all(end + SEG_GAP_SEC <= s or start >= e + SEG_GAP_SEC
               for s, e in chosen):
            chosen.append((start, end))
    chosen.sort()
    return [(round(s, 2), round(min(e, duration), 2)) for s, e in chosen]


def hook_from_caption(caption):
    """The on-screen hook: the caption's first sentence, word-truncated to
    HOOK_MAX_CHARS, scrubbed by the on-screen copy law (NO dashes). Empty in -> ''."""
    text = (caption or "").strip().splitlines()[0] if (caption or "").strip() else ""
    for stop in (". ", "! ", "? "):
        if stop in text:
            text = text.split(stop, 1)[0] + stop.strip()
            break
    text = scrub_onscreen(text).strip()
    if len(text) > HOOK_MAX_CHARS:
        cut = text[:HOOK_MAX_CHARS + 1]
        text = cut[: cut.rfind(" ")].strip() if " " in cut else text[:HOOK_MAX_CHARS]
    return text


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def _drawtext(hook):
    """The hook overlay filter: boxed white text, top-safe area, first seconds only."""
    esc = hook.replace("\\", "").replace("'", "’").replace(":", "\\:")
    return (
        f"drawtext=text='{esc}':fontsize=64:fontcolor=white:"
        "box=1:boxcolor=black@0.55:boxborderw=18:"
        "x=(w-text_w)/2:y=h*0.14:"
        f"enable='lte(t,{HOOK_SECONDS})'"
    )


def assemble(path, segments, out_path, hook="", has_audio=True, runner=None):
    """One ffmpeg pass: trim+concat the segments, cover-crop to 1080x1920@30, overlay
    the hook, encode h264/aac faststart. Raises ReelError on failure."""
    run = runner or _run
    parts_v, parts_a, chains = [], [], []
    for i, (s, e) in enumerate(segments):
        chains.append(f"[0:v]trim=start={s}:end={e},setpts=PTS-STARTPTS[v{i}]")
        parts_v.append(f"[v{i}]")
        if has_audio:
            chains.append(f"[0:a]atrim=start={s}:end={e},"
                          f"asetpts=PTS-STARTPTS[a{i}]")
            parts_a.append(f"[a{i}]")
    n = len(segments)
    if has_audio:
        concat = (f"{''.join(v + a for v, a in zip(parts_v, parts_a))}"
                  f"concat=n={n}:v=1:a=1[cv][ca]")
    else:
        concat = f"{''.join(parts_v)}concat=n={n}:v=1:a=0[cv]"
    vf = ("[cv]scale=1080:1920:force_original_aspect_ratio=increase,"
          "crop=1080:1920,fps=30,format=yuv420p")
    if hook:
        vf += "," + _drawtext(hook)
    vf += "[vout]"
    filter_complex = ";".join(chains + [concat, vf])

    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(path),
           "-filter_complex", filter_complex, "-map", "[vout]"]
    if has_audio:
        cmd += ["-map", "[ca]", "-c:a", "aac", "-b:a", "128k"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-movflags", "+faststart", str(out_path)]
    run(cmd, "assemble")
    return str(out_path)


# --------------------------------------------------------------------------
# Entry point + cache
# --------------------------------------------------------------------------

def _content_key(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def poster_frame(video_path, out_path, at_sec=1.0, runner=None):
    """Grab ONE representative frame as a JPG (a video's calendar preview). Tries
    `at_sec` in, then 0.0 for very short clips. Raises ReelError on total failure."""
    run = runner or _run
    for ss in (at_sec, 0.0):
        try:
            run(["ffmpeg", "-hide_banner", "-y", "-ss", str(ss), "-i", str(video_path),
                 "-frames:v", "1", "-q:v", "3", "-vf",
                 "scale='min(1080,iw)':-2", str(out_path)], "poster")
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                return str(out_path)
        except ReelError:
            continue
    raise ReelError("could not extract a poster frame")


def get_or_make_poster(video_path, library_path, *, runner=None, logger=None):
    """The hosted-ready poster JPG for a video (cached in <library>/reels/), or None
    on failure. Pure ffmpeg, no flag gate: a video's calendar preview is always safe
    to make. NEVER raises."""
    log = logger or (lambda m: print(f"[video-poster] {m}"))
    try:
        cache_dir = os.path.join(str(library_path), _REEL_SUBDIR)
        os.makedirs(cache_dir, exist_ok=True)
        out = os.path.join(cache_dir, f"{_content_key(video_path)}__poster.jpg")
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
        return poster_frame(video_path, out, runner=runner)
    except Exception as exc:  # noqa: BLE001 - a missing preview must never block a post
        log(f"poster failed for {os.path.basename(str(video_path))}: "
            f"{type(exc).__name__}")
        return None


def get_or_make_reel(video_path, caption, library_path, *, runner=None,
                     prober=None, profiler=None, assembler=None, logger=None):
    """The edited reel for this video (cached), or None when editing is off/failed.

    Flag OFF -> None (raw posts as-is, byte-for-byte today's behavior). The cache key
    is the video's CONTENT hash + the hook text, so a rebuild or re-run never
    re-encodes and a caption change re-hooks. NEVER raises: any failure logs and
    returns None so the raw video still posts."""
    log = logger or (lambda m: print(f"[action-reel] {m}"))
    if not config.client_video_edit_enabled():
        return None
    try:
        hook = hook_from_caption(caption)
        cache_dir = os.path.join(str(library_path), _REEL_SUBDIR)
        os.makedirs(cache_dir, exist_ok=True)
        hook_tag = hashlib.sha256(hook.encode("utf-8")).hexdigest()[:6]
        out = os.path.join(cache_dir,
                           f"{_content_key(video_path)}_{hook_tag}__reel.mp4")
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
        duration, _w, _h, has_audio = (prober or probe)(video_path, runner=runner)
        prof = (profiler or motion_profile)(video_path, runner=runner)
        segments = pick_segments(prof, duration)
        (assembler or assemble)(video_path, segments, out, hook=hook,
                                has_audio=has_audio, runner=runner)
        log(f"edited {os.path.basename(str(video_path))} -> "
            f"{len(segments)} cut(s), hook={bool(hook)}")
        return out
    except Exception as exc:  # noqa: BLE001 - editing may never block a post
        log(f"edit failed for {os.path.basename(str(video_path))}: "
            f"{type(exc).__name__}; posting the raw video")
        return None
