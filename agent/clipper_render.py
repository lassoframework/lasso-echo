"""
Native clipper Phase 2: render pipeline.
Cut → vertical frame → karaoke captions → LASSO brand frame.

Guards (both must be true to render):
  AGENT_CLIPPER_RENDER_ENABLED=true  (second flag under master, default OFF)
  ffmpeg on PATH                      (HAS_FFMPEG — detected at call time)

Every function raises RenderError when either guard is unmet instead of
silently skipping, so callers know exactly why nothing happened. The
orchestrator (clip_episode) self-skips when the flag is OFF; these
functions raise so tests can verify the guard clearly.

Render target: 1080x1920 (Instagram / TikTok vertical, 9:16).
Brand palette:
  Navy #1A2340  — lower-third, text outlines
  Red  #E03131  — accent, social handle
  White         — primary caption text
"""

import json
import os
import re
import shutil
import subprocess
import tempfile

from . import config

REEL_W = 1080
REEL_H = 1920
LOWER_H = 180   # lower-third bar height (pixels)

_BRAND_NAVY_HEX = "1A2340"   # without #
_BRAND_RED_HEX = "E03131"
_BRAND_WHITE_HEX = "FFFFFF"


class RenderError(Exception):
    """A render step could not proceed. Never raised when the flag is just OFF
    inside the orchestrator; raised loudly when called directly so tests can
    confirm the guard."""


# ---- guards -------------------------------------------------------------------------

def _ffmpeg():
    """Return the ffmpeg path or raise RenderError."""
    path = shutil.which("ffmpeg")
    if not path:
        raise RenderError(
            "ffmpeg not found on PATH. Install it and retry:\n"
            "  brew install ffmpeg   (macOS)\n"
            "  apt-get install ffmpeg   (Linux/Railway)")
    return path


def _require_render():
    """Raise RenderError when the render flag is OFF or ffmpeg is absent."""
    if not config.clipper_render_enabled():
        raise RenderError(
            "render is OFF (AGENT_CLIPPER_RENDER_ENABLED not set to true). "
            "Phase 2 render is disabled until armed.")
    _ffmpeg()


def _run(cmd, label="ffmpeg"):
    """Run a subprocess, raising RenderError on non-zero exit."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RenderError(
            f"{label} failed (exit {result.returncode}): "
            + (result.stderr[-600:] if result.stderr else "(no stderr)"))
    return result


# ---- Part 5: lossless cut -----------------------------------------------------------

def cut_segment(source_path, start_ts, end_ts, output_dir, label="clip"):
    """
    Cut a segment from source_path between start_ts and end_ts (seconds).
    Uses stream-copy codecs (lossless) where the container allows. Returns
    the output path. Raises RenderError when the render flag is OFF or
    ffmpeg is absent.
    """
    _require_render()
    os.makedirs(output_dir, exist_ok=True)
    ext = os.path.splitext(source_path)[1].lower() or ".mp4"
    stem = f"{re.sub(r'[^A-Za-z0-9_-]', '_', label)}_{int(start_ts):05d}_{int(end_ts):05d}"
    out = os.path.join(output_dir, stem + ext)
    duration = end_ts - start_ts
    cmd = [
        _ffmpeg(), "-y",
        "-ss", str(float(start_ts)),
        "-i", source_path,
        "-t", str(float(duration)),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        out,
    ]
    _run(cmd, "cut_segment")
    return out


# ---- Part 6: vertical framing -------------------------------------------------------

def _probe_media_kind(path):
    """Use ffprobe to check if the source has a video stream. Returns 'video' or 'audio'."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return "video"
    result = subprocess.run(
        [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", path],
        capture_output=True, text=True)
    try:
        for s in json.loads(result.stdout).get("streams", []):
            if s.get("codec_type") == "video":
                return "video"
    except Exception:
        pass
    return "audio"


def frame_vertical(input_path, output_path, media_kind=None, segments=None):
    """
    Reframe to 9:16 (1080x1920):
      video -> fill-scale to cover 1080x1920, center-safe crop (active speaker
               tracking via segments is a Phase 2 enhancement; center crop when
               segments are absent).
      audio -> audiogram: navy canvas 1080x1920, animated red waveform centered,
               suitable for podcast/voiceover clips without a video source.

    Raises RenderError when the render flag is OFF or ffmpeg is absent.
    """
    _require_render()
    kind = media_kind or _probe_media_kind(input_path)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    if kind == "video":
        # Fill-scale: scale so BOTH width >= 1080 AND height >= 1920, then
        # center-crop to exactly 1080x1920. This never letterboxes.
        vf = (
            f"scale=w='if(gt(iw/ih,{REEL_W}/{REEL_H}),-2,{REEL_W})':"
            f"h='if(gt(iw/ih,{REEL_W}/{REEL_H}),{REEL_H},-2)',"
            f"scale=w='if(lt(iw,{REEL_W}),{REEL_W},iw)':"
            f"h='if(lt(ih,{REEL_H}),{REEL_H},ih)',"
            f"crop={REEL_W}:{REEL_H}:(iw-{REEL_W})/2:(ih-{REEL_H})/2"
        )
        cmd = [
            _ffmpeg(), "-y", "-i", input_path,
            "-vf", vf,
            "-c:v", "libx264", "-crf", "22", "-preset", "fast",
            "-c:a", "aac", "-b:a", "128k",
            output_path,
        ]
    else:
        # Audiogram: navy background, red waveform, silent where no audio.
        lavfi = (
            f"color=c=0x{_BRAND_NAVY_HEX}:s={REEL_W}x{REEL_H}:r=30[bg];"
            f"[0:a]showwaves=s={REEL_W}x400:mode=line:colors=0x{_BRAND_RED_HEX}[wave];"
            f"[bg][wave]overlay=(W-w)/2:(H-h)/2[v]"
        )
        cmd = [
            _ffmpeg(), "-y",
            "-i", input_path,
            "-filter_complex", lavfi,
            "-map", "[v]", "-map", "0:a",
            "-c:v", "libx264", "-crf", "22", "-preset", "fast",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            output_path,
        ]
    _run(cmd, "frame_vertical")
    return output_path


# ---- Part 7: karaoke captions -------------------------------------------------------

def _fmt_ass_ts(seconds):
    """Convert seconds to ASS timestamp h:mm:ss.cs (centiseconds)."""
    try:
        t = max(0.0, float(seconds))
    except (TypeError, ValueError):
        t = 0.0
    h = int(t) // 3600
    m = (int(t) % 3600) // 60
    s = int(t) % 60
    cs = int(round((t - int(t)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _make_ass_subtitles(transcript, start_ts, end_ts, ass_path):
    """
    Write an ASS subtitle file with one Dialogue event per word so each word
    pops up and disappears at the exact moment it is spoken (karaoke-style).
    Only words whose start time falls in [start_ts, end_ts] are included.
    Timestamps in the ASS file are relative to the start of the segment.
    Never includes words outside the segment (fabrication-safe).
    """
    start_ts = float(start_ts)
    end_ts = float(end_ts)
    words = [
        w for w in transcript.get("words", [])
        if float(w.get("start", 0)) >= start_ts - 0.05
        and float(w.get("start", 0)) < end_ts + 0.05
    ]

    caption_y = REEL_H - LOWER_H - 60   # above the lower-third brand frame

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {REEL_W}",
        f"PlayResY: {REEL_H}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        # White text, navy outline, bottom-center, 220px margin from bottom
        f"Style: Karaoke,Arial,80,&H00{_BRAND_WHITE_HEX},&H00{_BRAND_WHITE_HEX},"
        f"&H00{_BRAND_NAVY_HEX},&H80000000,-1,0,0,0,100,100,0,0,1,4,0,2,10,10,220,0",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for w in words:
        w_start = max(0.0, float(w.get("start", 0)) - start_ts)
        w_end = max(w_start + 0.05, float(w.get("end", 0)) - start_ts)
        text = str(w.get("word", "") or "").strip().upper()
        if not text:
            continue
        lines.append(
            f"Dialogue: 0,{_fmt_ass_ts(w_start)},{_fmt_ass_ts(w_end)},"
            f"Karaoke,,0,0,0,,{text}"
        )

    os.makedirs(os.path.dirname(os.path.abspath(ass_path)), exist_ok=True)
    with open(ass_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return ass_path


def burn_captions(input_path, output_path, transcript, start_ts, end_ts):
    """
    Burn word-by-word karaoke captions from the word-level transcript into the
    video. Each word appears at its exact spoken timestamp (relative to
    start_ts) and disappears when the next word starts. Positioned above the
    LASSO brand-frame lower-third (220px margin from bottom).
    Only words within [start_ts, end_ts] are included — never fabricates text.
    Raises RenderError when the render flag is OFF or ffmpeg is absent.
    """
    _require_render()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".ass", delete=False) as tf:
        ass_path = tf.name

    try:
        _make_ass_subtitles(transcript, start_ts, end_ts, ass_path)
        # ffmpeg ass filter: escape backslashes and colons in the path for the
        # vf string. On macOS/Linux this is typically safe; use absolute path.
        safe = os.path.abspath(ass_path).replace("\\", "/").replace(":", "\\:")
        cmd = [
            _ffmpeg(), "-y", "-i", input_path,
            "-vf", f"ass={safe}",
            "-c:v", "libx264", "-crf", "22", "-preset", "fast",
            "-c:a", "copy",
            output_path,
        ]
        _run(cmd, "burn_captions")
    finally:
        try:
            os.unlink(ass_path)
        except OSError:
            pass

    return output_path


# ---- Part 8: LASSO brand frame + safe margins ---------------------------------------

def add_brand_frame(input_path, output_path,
                    logo_text="LASSO", handle_text="@GymMarketingMadeSimple"):
    """
    Overlay the LASSO brand frame on a 1080x1920 vertical video:
      - Navy lower-third bar at the bottom (LOWER_H px)
      - Logo name and social handle centered in the bar
    Design tokens match Echo's infographic house style: navy #1A2340, red #E03131.
    Raises RenderError when the render flag is OFF or ffmpeg is absent.
    """
    _require_render()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    bar_y = REEL_H - LOWER_H          # top of the lower-third bar
    logo_y = bar_y + 28               # logo text baseline
    handle_y = bar_y + 108            # handle text baseline

    vf_parts = [
        # Navy lower-third bar
        f"drawbox=x=0:y={bar_y}:w={REEL_W}:h={LOWER_H}:"
        f"color=0x{_BRAND_NAVY_HEX}@1.0:t=fill",
        # Logo text: white, centered (bold via font-name on systems that support it)
        f"drawtext=fontsize=56:fontcolor=0x{_BRAND_WHITE_HEX}:"
        f"text='{logo_text}':x=(w-text_w)/2:y={logo_y}:font=Arial",
        # Social handle: brand red, smaller, centered
        f"drawtext=fontsize=36:fontcolor=0x{_BRAND_RED_HEX}:"
        f"text='{handle_text}':x=(w-text_w)/2:y={handle_y}:font=Arial",
    ]
    vf = ",".join(vf_parts)

    cmd = [
        _ffmpeg(), "-y", "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-crf", "22", "-preset", "fast",
        "-c:a", "copy",
        output_path,
    ]
    _run(cmd, "add_brand_frame")
    return output_path


# ---- orchestrator -------------------------------------------------------------------

def render_clip(moment, media_path, transcript, output_dir):
    """
    Full Phase 2 render pipeline for one approved moment:
      cut → frame_vertical → burn_captions → add_brand_frame
    Returns {"reel_path": str} or None if the render flag is OFF or ffmpeg absent.
    Callers should check config.clipper_render_enabled() before calling if they want
    to skip silently; this function raises RenderError so partial builds are loud.
    """
    if not config.clipper_render_enabled():
        return None

    os.makedirs(output_dir, exist_ok=True)
    base = f"clip_{int(moment.start_ts):05d}_{int(moment.end_ts):05d}"

    framed_out = os.path.join(output_dir, base + "_framed.mp4")
    captioned_out = os.path.join(output_dir, base + "_captioned.mp4")
    final_out = os.path.join(output_dir, base + "_reel.mp4")

    cut_out = cut_segment(media_path, moment.start_ts, moment.end_ts, output_dir,
                          label=base)
    frame_vertical(cut_out, framed_out)
    burn_captions(framed_out, captioned_out, transcript, moment.start_ts, moment.end_ts)
    add_brand_frame(captioned_out, final_out)

    return {"reel_path": final_out}
