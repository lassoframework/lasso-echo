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
def render_compose(plan, *, output_dir, ask_frame_text="",
                   reframe_fn=None, normalize_fn=None, assemble_fn=None,
                   end_frame_fn=None, music_path="", music_burn_fn=None):
    """Render a ComposePlan into a 9:16 1080x1920 montage. Every heavy step is
    injectable so the suite runs offline:

      reframe_fn(segment, out_dir)                 -> path (9:16 subject-aware reframe)
      normalize_fn(paths)                          -> paths (loudness + color across phones)
      assemble_fn(paths, out_dir)                  -> path (concat)
      end_frame_fn(path, out_dir, ask_text)        -> path (brand end-frame, ONE ask)
      music_burn_fn(path, music_path, out_dir)     -> path (licensed bed burn)

    A None default binds the clipper_render primitives lazily. If ANY primitive raises
    (e.g. ffmpeg absent), the render is HELD with an honest reason (held=True), never a
    crash and never a fabricated output path."""
    if plan.held:
        return ComposeResult(plan=plan, held=True, hold_reason=plan.hold_reason)
    if plan.route == ROUTE_OPUS:
        return ComposeResult(plan=plan, held=True,
                             hold_reason="request routed to the Opus reel lane")
    try:
        reframe_fn = reframe_fn or _default_reframe
        normalize_fn = normalize_fn or _default_normalize
        assemble_fn = assemble_fn or _default_assemble
        end_frame_fn = end_frame_fn or _default_end_frame
        # A licensed bed defaults to the real ffmpeg burn (amix under the video
        # audio). An injected music_burn_fn still overrides; music_path='' (a 'none'
        # selection) skips the burn entirely.
        if music_path and music_burn_fn is None:
            music_burn_fn = _default_music_burn

        reframed = [reframe_fn(seg, output_dir) for seg in plan.segments]
        normalized = normalize_fn(reframed)
        assembled = assemble_fn(normalized, output_dir)
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
                        f"this env, or a primitive failed). Nothing was staged.")


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
    H.264/AAC so every segment shares one loudness + codec profile before concat.
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
            "-vf", "normalize=blackpt=black:whitept=white",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:v", "libx264", "-crf", "22", "-preset", "fast",
            "-c:a", "aac", "-b:a", "128k",
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


def _default_end_frame(path, out_dir, ask_text):
    from . import clipper_render
    out = f"{out_dir}/story_final.mp4"
    clipper_render.add_brand_frame(path, out)
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
