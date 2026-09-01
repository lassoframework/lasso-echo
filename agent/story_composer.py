"""
story_composer.py — the multi-clip composer (spec §3).

Pick 2..6 segments (3..15s each, total 15..60s) from the SAME gym's raw pool via
opus scoring, 9:16 subject-aware reframe (REUSE clipper_render), loudness + color
normalize across segments (they were shot on different phones), assemble, and cap a
brand end-frame with exactly ONE ask. Every render lands PENDING (staging is Wave 6).

RAILS (never move):
  * INPUT CAPS: a raw asset enters the story lane only when it is <= 5 min AND
    <= 900 MB. Longer/bigger routes to the existing Opus reel lane (out of scope
    here) — the composer REFUSES it (returns a routed=opus result), never silently
    truncates.
  * TENANT ISOLATION: assert asset.gym_id == request.gym_id on EVERY segment (spec
    §1.5d). A cross-gym segment BLOCKS the whole compose (never mixes two gyms).
  * SCORING REUSE: segment selection reuses opus_factory scoring intent (score,
    duration window). Selection is deterministic and offline; ffmpeg is only needed
    at the RENDER step.
  * GRACEFUL DEGRADE: ffmpeg/probe may be absent in the test env. Every heavy step is
    injectable. A missing renderer/probe -> the compose is HELD with an honest reason
    (held=True), never a crash, never a fabricated post.

This module PLANS + (optionally) RENDERS. Planning is pure/offline; rendering calls
the injected primitives. The staging (Wave 6) turns a rendered plan into a PENDING
content_calendar row + story_render row.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Input caps (spec §3): what may enter the STORY lane vs route to the Opus reel lane.
MAX_RAW_SEC = 5 * 60           # 5 minutes
MAX_RAW_BYTES = 900_000_000    # 900 MB

# Segment / total windows (spec §3).
SEG_MIN_SEC = 3.0
SEG_MAX_SEC = 15.0
TOTAL_MIN_SEC = 15.0
TOTAL_MAX_SEC = 60.0
MIN_SEGMENTS = 2
MAX_SEGMENTS = 6

ROUTE_STORY = "story"
ROUTE_OPUS = "opus"

# Roxx overlay burn timing (spec §1): the hook plays across the opening of the
# montage, the single validated ask frame owns the closing window.
HOOK_FRAME_MIN_SEC = 3.0
ASK_FRAME_SEC = 3.0

# The ONE video profile every segment is re-encoded to before the concat demuxer
# joins them (see _default_normalize). Mixed frame rates / timebases across phone
# clips silently truncate the concatenated VIDEO track, so both are pinned.
CONCAT_FPS = 30
CONCAT_TIMESCALE = 15360        # 512 * 30: an exact 30fps mp4 timescale


@dataclass
class Segment:
    """One chosen slice of one raw asset."""
    asset_id: str
    gym_id: str
    start_ts: float
    end_ts: float
    score: float = 0.0
    source_path: str = ""       # local path (filled at render time)

    @property
    def duration(self):
        return round(self.end_ts - self.start_ts, 2)


@dataclass
class ComposePlan:
    gym_id: str
    segments: list = field(default_factory=list)
    route: str = ROUTE_STORY
    held: bool = False
    hold_reason: str = ""
    total_sec: float = 0.0


@dataclass
class ComposeResult:
    plan: ComposePlan
    output_path: str = ""
    held: bool = False
    hold_reason: str = ""


class TenantMismatch(Exception):
    """A segment's asset belongs to a different gym than the request. The whole
    compose is blocked; two gyms are NEVER mixed into one Story."""


# ---- input caps + routing ---------------------------------------------------
def route_asset(asset):
    """(route, reason): does this raw asset enter the STORY lane or route to the OPUS
    reel lane? Spec §3: <= 5 min AND <= 900 MB -> story; longer/bigger -> opus."""
    dur = _num(asset.get("duration_sec"))
    size = _int(asset.get("size_bytes")) or 0
    if dur is not None and dur > MAX_RAW_SEC:
        return ROUTE_OPUS, f"raw is {dur:g}s > {MAX_RAW_SEC}s: routes to the Opus reel lane"
    if size > MAX_RAW_BYTES:
        return ROUTE_OPUS, f"raw is {size} bytes > {MAX_RAW_BYTES}: routes to the Opus reel lane"
    return ROUTE_STORY, ""


# ---- tenant assertion (every segment) ---------------------------------------
def assert_segment_tenant(segment, gym_id):
    """Raise TenantMismatch when a segment's gym_id != the request gym_id. Called on
    EVERY segment before it enters a plan (defense in depth beyond the pool filter)."""
    if str(segment.gym_id or "") != str(gym_id or ""):
        raise TenantMismatch(
            f"segment from asset {segment.asset_id} is gym={segment.gym_id!r} but the "
            f"request is gym={gym_id!r}; two gyms are never mixed into one Story")
    return True


# ---- segment selection (opus scoring intent, offline) -----------------------
def _score_candidate(cand):
    """Reuse the opus scoring intent: prefer higher-scored, in-window candidates. A
    candidate is a dict with score + start/end. Pure + offline."""
    return float(cand.get("score") or 0.0)


def select_segments(candidates, gym_id, plan_bounds, *, seed=None):
    """Pick 2..max segments from the SAME gym's candidate slices to fill the total
    window. `candidates` is a list of dicts {asset_id, gym_id, start_ts, end_ts,
    score}. Enforces the per-segment length window (3..15s) and the total window
    (15..60s) AND the tenant assertion on every pick. Returns a list of Segment.

    Deterministic: candidates are ranked by score (desc), then by asset_id for a
    stable tiebreak, and greedily added until the total window is satisfied or the max
    segment count is hit. Raises TenantMismatch if any candidate is cross-gym."""
    max_seg = plan_bounds.get("max_segments", MAX_SEGMENTS)
    min_seg = plan_bounds.get("min_segments", MIN_SEGMENTS)
    total_max = plan_bounds.get("total_max_sec", TOTAL_MAX_SEC)
    total_min = plan_bounds.get("total_min_sec", TOTAL_MIN_SEC)

    # keep only in-window, this-gym candidates.
    usable = []
    for c in candidates:
        seg = Segment(asset_id=c.get("asset_id", ""), gym_id=c.get("gym_id", ""),
                      start_ts=float(c.get("start_ts", 0)),
                      end_ts=float(c.get("end_ts", 0)), score=_score_candidate(c))
        assert_segment_tenant(seg, gym_id)        # tenant assertion on EVERY segment
        if SEG_MIN_SEC <= seg.duration <= SEG_MAX_SEC:
            usable.append(seg)

    usable.sort(key=lambda s: (-s.score, s.asset_id, s.start_ts))

    chosen, total = [], 0.0
    for seg in usable:
        if len(chosen) >= max_seg:
            break
        if total + seg.duration > total_max:
            continue
        chosen.append(seg)
        total += seg.duration
        if total >= total_min and len(chosen) >= min_seg:
            # enough to satisfy the window; keep going only if we are still short of a
            # good montage length, capped by max_seg (handled above).
            if total >= total_min:
                pass
    return chosen


def plan_compose(candidates, gym_id, template, *, assets_by_id=None):
    """Build a ComposePlan for one request. Applies input-cap routing on each source
    asset (a source over the caps routes the WHOLE request to the Opus lane), the
    per-segment + total windows, and the tenant assertion. Returns a ComposePlan that
    is HELD (with a reason) when it cannot assemble a valid 15..60s / 2..6 montage."""
    assets_by_id = assets_by_id or {}
    bounds = {
        "min_segments": template.segment_plan.min_segments,
        "max_segments": template.segment_plan.max_segments,
        "total_min_sec": template.segment_plan.total_min_sec,
        "total_max_sec": template.segment_plan.total_max_sec,
    }

    # input caps: any source asset over the caps routes to the Opus reel lane.
    for c in candidates:
        a = assets_by_id.get(c.get("asset_id"))
        if a is not None:
            route, reason = route_asset(a)
            if route == ROUTE_OPUS:
                return ComposePlan(gym_id=gym_id, route=ROUTE_OPUS, held=True,
                                   hold_reason=reason)

    try:
        segments = select_segments(candidates, gym_id, bounds)
    except TenantMismatch as e:
        return ComposePlan(gym_id=gym_id, held=True, hold_reason=str(e))

    total = round(sum(s.duration for s in segments), 2)
    if len(segments) < bounds["min_segments"] or total < TOTAL_MIN_SEC:
        return ComposePlan(
            gym_id=gym_id, segments=segments, total_sec=total, held=True,
            hold_reason=(f"only {len(segments)} usable segment(s) totaling {total:g}s; "
                         f"need >= {bounds['min_segments']} segments and "
                         f">= {TOTAL_MIN_SEC:g}s. Nothing staged."))
    return ComposePlan(gym_id=gym_id, segments=segments, total_sec=total,
                       route=ROUTE_STORY)


# ---- render (injectable heavy steps; HELD on a missing renderer) ------------
def render_compose(plan, *, output_dir, ask_frame_text="", ask_frame_lines=None,
                   overlay_frames=None,
                   reframe_fn=None, normalize_fn=None, assemble_fn=None,
                   overlay_fn=None, end_frame_fn=None, music_path="",
                   music_burn_fn=None):
    """Render a ComposePlan into a 9:16 1080x1920 montage. Every heavy step is
    injectable so the suite runs offline:

      reframe_fn(segment, out_dir)                 -> path (9:16 subject-aware reframe)
      normalize_fn(paths)                          -> paths (loudness + color across phones)
      assemble_fn(paths, out_dir)                  -> path (concat)
      overlay_fn(path, out_dir, overlay_frames)    -> path (Roxx BODY overlay: the hook)
      end_frame_fn(path, out_dir, ask_text)        -> path (single ask frame + brand cap)
      music_burn_fn(path, music_path, out_dir)     -> path (licensed bed burn)

    overlay_frames is story_overlay.OverlaySpec.frames (ALREADY validated: ALL-CAPS,
    density-wrapped, identity anchor on frame 0) — the body/hook overlay is burned
    ONLY when it is non-empty, so a caller that never built one (e.g. a bare unit
    test) gets byte-identical behavior to before this overlay wiring existed.
    ask_frame_lines is story_overlay's single validated ask-frame lines
    (assert_one_ask_frame's output); the default end_frame_fn burns THOSE lines,
    never re-deriving them from ask_frame_text.

    A None default binds the clipper_render primitives lazily. If ANY primitive raises
    (e.g. ffmpeg absent, or a burned box fails its own safe-zone/contrast check), the
    render is HELD with an honest reason (held=True), never a crash, never a fabricated
    output path, and never a frame burned that fails its own validation."""
    if plan.held:
        return ComposeResult(plan=plan, held=True, hold_reason=plan.hold_reason)
    if plan.route == ROUTE_OPUS:
        return ComposeResult(plan=plan, held=True,
                             hold_reason="request routed to the Opus reel lane")
    try:
        reframe_fn = reframe_fn or _default_reframe
        normalize_fn = normalize_fn or _default_normalize
        assemble_fn = assemble_fn or _default_assemble
        overlay_fn = overlay_fn or _default_overlay_burn
        end_frame_fn = end_frame_fn or _partial_end_frame(ask_frame_lines)
        # A licensed bed defaults to the real ffmpeg burn (amix under the video
        # audio). An injected music_burn_fn still overrides; music_path='' (a 'none'
        # selection) skips the burn entirely.
        if music_path and music_burn_fn is None:
            music_burn_fn = _default_music_burn

        reframed = [reframe_fn(seg, output_dir) for seg in plan.segments]
        normalized = normalize_fn(reframed)
        assembled = assemble_fn(normalized, output_dir)
        if overlay_frames:
            assembled = overlay_fn(assembled, output_dir, overlay_frames)
        final = end_frame_fn(assembled, output_dir, ask_frame_text)
        if music_path and music_burn_fn is not None:
            final = music_burn_fn(final, music_path, output_dir)
        if not final:
            return ComposeResult(plan=plan, held=True,
                                 hold_reason="renderer returned no output path")
        return ComposeResult(plan=plan, output_path=final)
    except Exception as e:  # noqa: BLE001 - a heavy-step failure HOLDS, never crashes
        return ComposeResult(
            plan=plan, held=True,
            hold_reason=f"render held: {type(e).__name__}: {e} (no ffmpeg / probe in "
                        f"this env, or a primitive failed, or a burned overlay failed "
                        f"its own safe-zone/contrast validation). Nothing was staged.")


# ---- default primitives (bind clipper_render lazily; raise when unavailable) -
def _default_reframe(segment, out_dir):
    from . import clipper_render
    cut = clipper_render.cut_segment(segment.source_path, segment.start_ts,
                                     segment.end_ts, out_dir, label="storyseg")
    out = f"{out_dir}/seg_{segment.asset_id}_{int(segment.start_ts)}_framed.mp4"
    clipper_render.frame_vertical(cut, out)   # 9:16 subject-aware reframe
    return out


def _default_normalize(paths):
    """Loudness (EBU R128) + color normalize per segment across phones (spec §3).
    Real ffmpeg: loudnorm=I=-16:TP=-1.5:LRA=11 + a color normalize, re-encoded
    H.264/AAC so every segment shares ONE loudness + codec profile before concat.

    "One profile" is literal and load-bearing: the concat demuxer adopts the FIRST
    input's frame rate, timebase and pixel format and reinterprets every later
    segment's timestamps in it. Phone clips arrive at 29.98 / 29.99 / 30.09 fps with
    timebases like 1/71400, 1/48900 and 1/11856, so segments 2..n produced
    non-monotonic timestamps and ffmpeg DROPPED most of their frames: a 3-clip
    montage muxed 29.6s of audio over a 13.4s video track (the container still
    reported 29.6s, so nothing looked wrong until a frame was pulled past 13.4s).
    So FPS, timebase, pixel format and the audio rate/layout are all pinned here —
    without that, this function's own "one profile" promise was not true and every
    multi-clip Story shipped a truncated video track.

    Reuses clipper_render's ffmpeg guard (raises when the render flag is OFF or
    ffmpeg is absent -> the compose HOLDS honestly, never a silent bad render)."""
    from . import clipper_render
    clipper_render._require_render()
    out = []
    for p in paths:
        root, ext = os.path.splitext(p)
        dst = f"{root}_norm{ext or '.mp4'}"
        clipper_render._run([
            clipper_render._ffmpeg(), "-y", "-i", p,
            # fps + format pin the video profile the concat demuxer needs; yuv420p is
            # also the only pixel format the social platforms reliably accept.
            "-vf", f"normalize=blackpt=black:whitept=white,fps={CONCAT_FPS},"
                   f"format=yuv420p",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:v", "libx264", "-crf", "22", "-preset", "fast",
            "-video_track_timescale", str(CONCAT_TIMESCALE),
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            dst,
        ], "story_normalize")
        out.append(dst)
    return out


def _default_assemble(paths, out_dir):
    """Concat the normalized 9:16 segments into one montage (real ffmpeg concat
    demuxer; the segments share a codec + size profile after _default_normalize).
    Reuses clipper_render's ffmpeg guard so an ffmpeg-absent env HOLDS."""
    from . import clipper_render
    clipper_render._require_render()
    os.makedirs(out_dir, exist_ok=True)
    listfile = os.path.join(out_dir, "story_concat.txt")
    with open(listfile, "w", encoding="utf-8") as fh:
        for p in paths:
            # single-quote escaping per the concat demuxer's syntax.
            safe = os.path.abspath(p).replace("'", "'\\''")
            fh.write(f"file '{safe}'\n")
    dst = os.path.join(out_dir, "story_assembled.mp4")
    clipper_render._run([
        clipper_render._ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", listfile,
        "-c:v", "libx264", "-crf", "22", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        dst,
    ], "story_assemble")
    return dst


def _default_overlay_burn(path, out_dir, frames):
    """Burn the Roxx BODY overlay (the hook) onto the opening of the assembled
    montage. `frames` is story_overlay.OverlaySpec.frames — ALREADY validated
    ALL-CAPS, density-wrapped lines with the identity anchor on frame 0; this
    function only sequences and times them, it never re-wraps or re-derives copy.
    Each frame gets >= HOOK_FRAME_MIN_SEC seconds, sequenced from t=0. Raises (via
    clipper_render.burn_overlay_block) when a frame's box would violate the story
    safe zone — the caller HOLDS rather than burning a bad frame."""
    from . import clipper_render
    total = clipper_render.probe_duration(path)
    n = max(len(frames), 1)
    per = max(HOOK_FRAME_MIN_SEC, total / n) if total > 0 else HOOK_FRAME_MIN_SEC
    cur = path
    t = 0.0
    for i, frame_lines in enumerate(frames):
        end_t = min(t + per, total) if total > 0 else t + per
        out = f"{out_dir}/story_hook_{i}.mp4"
        cur = clipper_render.burn_overlay_block(
            cur, out, frame_lines, anchor="top", start_t=t, end_t=end_t)
        t = end_t
    return cur


def _partial_end_frame(ask_frame_lines):
    """Bind the validated ask_frame_lines into the REAL default end-frame primitive
    without changing end_frame_fn's (path, out_dir, ask_text) contract that injected
    test/alternate renderers rely on. Only used when end_frame_fn is not injected."""
    def _bound(path, out_dir, ask_text):
        return _default_end_frame(path, out_dir, ask_text, ask_lines=ask_frame_lines)
    return _bound


def _default_end_frame(path, out_dir, ask_text, *, ask_lines=None):
    """Burn the single validated ASK frame (story_overlay's assert_one_ask_frame
    output, `ask_lines` — ALREADY validated: exactly one ask, will be safe-zone +
    contrast checked at burn time) onto the closing ASK_FRAME_SEC seconds of the
    clip, then caps the render with the LASSO brand watermark (unchanged, a
    separate brand system from the Roxx overlay). `ask_text` is kept only for the
    hold-reason / back-compat signature.

    When ask_text is set but ask_lines is not wired through, this REFUSES to guess
    or silently drop the ask (the exact failure mode this module used to have) —
    it raises so the render HOLDS with an honest reason instead."""
    from . import clipper_render
    if ask_text and not ask_lines:
        raise RuntimeError(
            "an ask was requested but no validated ask_frame_lines were wired "
            "through from story_overlay.build_overlay(); refusing to burn an "
            "unvalidated ask or silently drop it")
    out = f"{out_dir}/story_final.mp4"
    cur = path
    if ask_lines:
        total = clipper_render.probe_duration(path)
        start_t = max(0.0, total - ASK_FRAME_SEC) if total > 0 else 0.0
        end_t = total if total > 0 else start_t + ASK_FRAME_SEC
        cur = clipper_render.burn_overlay_block(
            path, f"{out_dir}/story_ask.mp4", list(ask_lines), anchor="bottom",
            start_t=start_t, end_t=end_t)
    clipper_render.add_brand_frame(cur, out)
    return out


def _default_music_burn(path, music_path, out_dir):
    """Burn the licensed music bed UNDER the video's own audio (real ffmpeg amix,
    shortest duration). Only ever called with a non-empty music_path (a 'none'
    selection skips the burn). Reuses clipper_render's ffmpeg guard."""
    from . import clipper_render
    clipper_render._require_render()
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, "story_music.mp4")
    clipper_render._run([
        clipper_render._ffmpeg(), "-y", "-i", path, "-i", music_path,
        "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=shortest[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        dst,
    ], "story_music_burn")
    return dst


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
