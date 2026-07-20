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

Render target: 1080x1920 (Instagram / TikTok vertical, 9:16) and 1080x1080 (1:1).
Brand palette (LASSO V3 house style, locked 2026-07-17):
  Navy #121E3C  — lower-third, text outlines
  Red  #FF0000  — accent, social handle
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
LOWER_H = 70    # lower-third bar height (pixels)

# Caption vertical position as a fraction of frame height, measured from the
# bottom. 0.417 of a 1920px frame ~= 800px (lower-middle / second-third of the
# frame). Scales correctly to any height (e.g. 1:1 1080 -> ~450px).
_CAPTION_MARGIN_FRAC = 0.417

_BRAND_NAVY_HEX = "121E3C"   # without # — LASSO V3 house-style navy
_BRAND_RED_HEX = "FF0000"    # LASSO V3 house-style red
_BRAND_WHITE_HEX = "FFFFFF"

_CAPTION_FONT_SIZE = 100   # px — large enough for mobile
_WORDS_PER_GROUP = 3       # words shown per caption event
_ACTIVE_COLOR = "FFFFFF"   # white — currently spoken word
_CONTEXT_COLOR = "888888"  # gray — other words in the group


_VENDOR_RE = re.compile(r"(?i)\bvendors\b|\bvendor\b")


def scrub_onscreen(text):
    """Enforce the LASSO on-screen text rules on any burned-in string:
      - no em dashes, en dashes, or hyphens (replaced with a space)
      - never the word 'vendor' (replaced with 'partner')
    Applies to captions and text cards so the render pipeline's no-dash /
    no-vendor promise holds for everything the viewer actually reads. This is a
    mechanical spelling fix, not a claim edit (dash -> space keeps the words;
    vendor -> partner is LASSO's own house term)."""
    t = str(text or "")
    t = t.replace("—", " ").replace("–", " ").replace("-", " ")
    t = _VENDOR_RE.sub(lambda m: "PARTNERS" if m.group(0).lower().endswith("s")
                       else "PARTNER", t) if t.isupper() else _VENDOR_RE.sub(
        lambda m: "partners" if m.group(0).lower().endswith("s") else "partner", t)
    return re.sub(r"\s+", " ", t).strip()


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
    """Raise RenderError when the render flag is OFF or ffmpeg is absent.
    The ffmpeg layer is armed by EITHER the clipper render flag
    (AGENT_CLIPPER_RENDER_ENABLED) or the video editor master
    (AGENT_VIDEO_EDITOR_ENABLED) — the video editor reuses these render
    primitives under its own flag."""
    if not (config.clipper_render_enabled() or config.video_editor_enabled()):
        raise RenderError(
            "render is OFF (neither AGENT_CLIPPER_RENDER_ENABLED nor "
            "AGENT_VIDEO_EDITOR_ENABLED set to true). Render is disabled until armed.")
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


def frame_vertical(input_path, output_path, media_kind=None, segments=None,
                   width=REEL_W, height=REEL_H):
    """
    Reframe to width x height (default 9:16 1080x1920; pass 1080x1080 for 1:1):
      video -> fill-scale to cover the target, center-safe crop (active speaker
               tracking via segments is a Phase 2 enhancement; center crop when
               segments are absent).
      audio -> audiogram: navy canvas, animated red waveform centered,
               suitable for podcast/voiceover clips without a video source.

    Raises RenderError when the render flag is OFF or ffmpeg is absent.
    """
    _require_render()
    kind = media_kind or _probe_media_kind(input_path)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    if kind == "video":
        # Fill-scale: scale so BOTH width >= W AND height >= H, then
        # center-crop to exactly WxH. This never letterboxes.
        vf = (
            f"scale=w='if(gt(iw/ih,{width}/{height}),-2,{width})':"
            f"h='if(gt(iw/ih,{width}/{height}),{height},-2)',"
            f"scale=w='if(lt(iw,{width}),{width},iw)':"
            f"h='if(lt(ih,{height}),{height},ih)',"
            f"crop={width}:{height}:(iw-{width})/2:(ih-{height})/2"
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
        wave_h = max(200, int(height * 0.21))
        lavfi = (
            f"color=c=0x{_BRAND_NAVY_HEX}:s={width}x{height}:r=30[bg];"
            f"[0:a]showwaves=s={width}x{wave_h}:mode=line:colors=0x{_BRAND_RED_HEX}[wave];"
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


def _make_ass_subtitles(transcript, start_ts, end_ts, ass_path,
                        width=REEL_W, height=REEL_H):
    """
    Multi-word group karaoke captions (modern Reels style).
    Words are grouped in sets of _WORDS_PER_GROUP. For each word's event, all
    words in the group are visible at once: the currently-spoken word in white,
    context words in gray. Dark semi-transparent box behind each event
    (BorderStyle=3). MarginV is _CAPTION_MARGIN_FRAC of frame height from the
    bottom, positioning captions in the second/third of the frame for BOTH 9:16
    (1920 -> ~800px) and 1:1 (1080 -> ~450px) — clear of headline overlays and
    the brand bar. Only words within [start_ts, end_ts] are included
    (fabrication-safe).
    """
    start_ts = float(start_ts)
    end_ts = float(end_ts)
    words = [
        w for w in transcript.get("words", [])
        if float(w.get("start", 0)) >= start_ts - 0.05
        and float(w.get("start", 0)) < end_ts + 0.05
    ]

    # Group into chunks of _WORDS_PER_GROUP
    chunks = []
    for i in range(0, len(words), _WORDS_PER_GROUP):
        chunks.append(words[i:i + _WORDS_PER_GROUP])

    margin_v = int(height * _CAPTION_MARGIN_FRAC)
    # Scale font down a touch for the shorter 1:1 canvas so 3 words fit on a line
    font_size = _CAPTION_FONT_SIZE if height >= REEL_H else int(_CAPTION_FONT_SIZE * 0.8)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        # Bold large text; semi-transparent dark box; lower-middle (second-third)
        f"Style: Karaoke,Arial,{font_size},"
        f"&H00{_ACTIVE_COLOR},&H003131E0,"
        f"&H00{_BRAND_NAVY_HEX},&H50000000,"
        f"-1,0,0,0,100,100,2,0,3,0,0,2,20,20,{margin_v},0",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for chunk in chunks:
        for word_idx, w in enumerate(chunk):
            w_start = max(0.0, float(w.get("start", 0)) - start_ts)
            w_end = max(w_start + 0.05, float(w.get("end", 0)) - start_ts)
            parts = []
            for ci, cw in enumerate(chunk):
                text = scrub_onscreen(str(cw.get("word", "") or "").strip().upper())
                if not text:
                    continue
                color = _ACTIVE_COLOR if ci == word_idx else _CONTEXT_COLOR
                parts.append(f"{{\\c&H00{color}&}}{text}")
            if not parts:
                continue
            display_text = " ".join(parts)
            lines.append(
                f"Dialogue: 0,{_fmt_ass_ts(w_start)},{_fmt_ass_ts(w_end)},"
                f"Karaoke,,0,0,0,,{display_text}"
            )

    os.makedirs(os.path.dirname(os.path.abspath(ass_path)), exist_ok=True)
    with open(ass_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return ass_path


def burn_captions(input_path, output_path, transcript, start_ts, end_ts,
                  width=REEL_W, height=REEL_H):
    """
    Burn word-by-word karaoke captions from the word-level transcript into the
    video. Each word appears at its exact spoken timestamp (relative to
    start_ts) and disappears when the next word starts. Positioned in the
    lower-middle (second/third) of the frame, scaled to frame height.
    Only words within [start_ts, end_ts] are included — never fabricates text.
    Raises RenderError when the render flag is OFF or ffmpeg is absent.
    """
    _require_render()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".ass", delete=False) as tf:
        ass_path = tf.name

    try:
        _make_ass_subtitles(transcript, start_ts, end_ts, ass_path,
                            width=width, height=height)
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
                    logo_text="LASSO", handle_text="@GymMarketingMadeSimple",
                    width=REEL_W, height=REEL_H):
    """
    Overlay the LASSO brand frame on a vertical or square video.
    Design: thin LOWER_H-px solid navy bar at the bottom.
    - 3px LASSO red accent line at the very top of the bar
    - Logo left-aligned, white, vertically centered in bar
    - Handle right-aligned, white, vertically centered in bar
    Uses ffmpeg 'ih'/'iw' expressions so it adapts to any frame size.
    Raises RenderError when the render flag is OFF or ffmpeg is absent.
    """
    _require_render()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    def _esc(t):
        return t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    vf_parts = [
        # Solid navy brand bar (full width, LOWER_H tall, anchored to bottom)
        f"drawbox=x=0:y=ih-{LOWER_H}:w=iw:h={LOWER_H}:"
        f"color=0x{_BRAND_NAVY_HEX}@1.0:t=fill",
        # 3px LASSO red accent line at the very top of the bar
        f"drawbox=x=0:y=ih-{LOWER_H}:w=iw:h=3:"
        f"color=0x{_BRAND_RED_HEX}@1.0:t=fill",
        # Logo: white, left-aligned, vertically centered via th expression
        f"drawtext=fontsize=42:fontcolor=0x{_BRAND_WHITE_HEX}:"
        f"text='{_esc(logo_text)}':x=28:y=h-{LOWER_H}+({LOWER_H}-th)/2:font=Arial",
        # Handle: white, right-aligned, same vertical center
        f"drawtext=fontsize=22:fontcolor=0x{_BRAND_WHITE_HEX}:"
        f"text='{_esc(handle_text)}':x=w-tw-28:y=h-{LOWER_H}+({LOWER_H}-th)/2:font=Arial",
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

def render_clip(moment, media_path, transcript, output_dir, llm=None):
    """
    Full Phase 2 render pipeline for one approved moment:
      cut → frame_vertical → burn_captions → [B-roll overlay] → add_brand_frame
    B-roll step runs only when AGENT_CLIPPER_BROLL_ENABLED=true (default OFF).
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

    brolled_out = captioned_out
    try:
        from . import clipper_broll
        if clipper_broll.broll_enabled():
            brolled_out = clipper_broll.add_broll(
                moment, captioned_out, transcript, output_dir, llm=llm)
    except Exception as exc:
        print(f"[broll] skipped: {exc}", flush=True)

    add_brand_frame(brolled_out, final_out)

    return {"reel_path": final_out}
