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
      - no em dashes, en dashes, or hyphens (replaced via copy_gate.scrub)
      - never the word 'vendor' (replaced with 'partner')
    Applies to captions and text cards so the render pipeline's no-dash /
    no-vendor promise holds for everything the viewer actually reads. This is a
    mechanical spelling fix, not a claim edit (dash -> space keeps the words;
    vendor -> partner is LASSO's own house term)."""
    from . import copy_gate
    t = copy_gate.scrub(str(text or ""))
    # ON-SCREEN text is stricter than caption copy: NO ASCII hyphen survives at all
    # (copy_gate.scrub keeps a spaced ' - ' and protected-URL hyphens; burned-in text
    # must carry none, matching the pre-copy_gate on-screen law).
    t = t.replace("-", " ")
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
                   width=REEL_W, height=REEL_H, face_center=None):
    """
    Reframe to width x height (default 9:16 1080x1920; pass 1080x1080 for 1:1):
      video -> fill-scale to cover the target, then crop. When face_center is
               provided (cx_frac, cy_frac, bottom_frac), applies a 10% punch-in
               crop centered on the face before scaling back to target size.
               Falls back to center-crop when face_center is None.
      audio -> audiogram: navy canvas, animated red waveform centered,
               suitable for podcast/voiceover clips without a video source.

    Raises RenderError when the render flag is OFF or ffmpeg is absent.
    """
    _require_render()
    kind = media_kind or _probe_media_kind(input_path)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    if kind == "video":
        # Fill-scale so both dimensions cover the target, then crop.
        _scale = (
            f"scale=w='if(gt(iw/ih,{width}/{height}),-2,{width})':"
            f"h='if(gt(iw/ih,{width}/{height}),{height},-2)',"
            f"scale=w='if(lt(iw,{width}),{width},iw)':"
            f"h='if(lt(ih,{height}),{height},ih)'"
        )
        if face_center:
            # 10% punch-in: crop a tighter window centered on the face then
            # scale back up, making the speaker appear closer and centered.
            _PUNCH = 1.10
            cw = int(width / _PUNCH)
            ch = int(height / _PUNCH)
            cx, cy = face_center[0], face_center[1]
            # Clamp expressions without commas (ffmpeg parses commas as
            # filter separators inside crop arguments):
            #   min(a,b) = (a+b-abs(a-b))/2
            #   max(r,0) = (r+abs(r))/2
            def _clamp(val, hi):
                inner = f"({val}+{hi}-abs({val}-{hi}))/2"
                return f"({inner}+abs({inner}))/2"
            crop_x = _clamp(f"{cx:.4f}*iw-{cw}/2", f"iw-{cw}")
            crop_y = _clamp(f"{cy:.4f}*ih-{ch}/2", f"ih-{ch}")
            vf = (
                f"{_scale},"
                f"crop={cw}:{ch}:{crop_x}:{crop_y},"
                f"scale={width}:{height}"
            )
        else:
            # Center-crop fallback (no face detected).
            vf = (
                f"{_scale},"
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


# ---- Part 8b: the Roxx overlay standard burn (hook + ask, spec §1) ------------------
#
# The overlay CONTENT (lines, contrast target, safe-zone bounds) is ALWAYS produced by
# story_overlay.py's tested functions and passed in — this module never re-derives or
# guesses copy, contrast, or position. It only turns already-validated data into real
# ffmpeg drawbox/drawtext calls, reusing add_brand_frame's escaping + filter patterns.

_OVERLAY_FONT_SIZE = 58     # px — legible on a 1080-wide frame at <=8 words/line
_OVERLAY_LINE_GAP = 1.32    # line height multiplier
_OVERLAY_PAD = 26           # px padding inside the scrim box (top/bottom)
_OVERLAY_OUTER_MARGIN = 24  # px breathing room off the safe-zone boundary


def probe_duration(path):
    """ffprobe duration (seconds) of a media file. Raises RenderError when ffprobe
    is absent or the probe fails — the overlay timing needs a REAL duration, never a
    guess."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RenderError("ffprobe not found on PATH; cannot time the overlay burn")
    result = subprocess.run(
        [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", path],
        capture_output=True, text=True)
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except Exception as e:  # noqa: BLE001
        raise RenderError(f"could not probe duration of {path}: {e}") from e


def _extract_frame_png(input_path, at_t, out_png):
    """Extract one real frame at at_t seconds to out_png. Used only to SAMPLE actual
    backdrop pixels for the contrast math — never a stand-in for the burned output.

    ffmpeg EXITS 0 when a seek lands past the last video frame and simply writes no
    file ("Output file is empty, nothing was encoded"), so _run cannot catch it. That
    turned a real upstream defect (a truncated video track) into a bare PIL
    FileNotFoundError three frames up the stack. Check for the file and say what
    actually happened."""
    cmd = [_ffmpeg(), "-y", "-ss", str(max(0.0, float(at_t))), "-i", input_path,
          "-frames:v", "1", out_png]
    _run(cmd, "extract_frame")
    if not os.path.isfile(out_png) or os.path.getsize(out_png) == 0:
        raise RenderError(
            f"no video frame exists at t={float(at_t):.2f}s in {input_path} "
            f"(ffmpeg encoded nothing and exited 0). The clip's VIDEO track is "
            f"shorter than the duration its container reports, so the overlay "
            f"cannot be timed against it; render HELD, nothing burned")
    return out_png


def _sample_backdrop_rgb(input_path, at_t, box, width=REEL_W, height=REEL_H):
    """Mean RGB of the video's REAL pixels inside `box` (y_top, y_bottom), sampled
    across the full frame width at time at_t. Feeds story_overlay.scrim_alpha_for so
    the brand scrim is sized to the ACTUAL backdrop, never a fixed guess."""
    from PIL import Image, ImageStat
    with tempfile.TemporaryDirectory() as td:
        png = os.path.join(td, "sample.png")
        _extract_frame_png(input_path, at_t, png)
        img = Image.open(png).convert("RGB")
        if img.size != (width, height):
            img = img.resize((width, height))
        y_top, y_bottom = box
        y_top = max(0, min(int(y_top), height - 1))
        y_bottom = max(y_top + 1, min(int(y_bottom), height))
        crop = img.crop((0, y_top, width, y_bottom))
        r, g, b = ImageStat.Stat(crop).mean[:3]
        return (int(r), int(g), int(b))


def burn_overlay_block(input_path, output_path, lines, *, anchor="top",
                       start_t=None, end_t=None, sample_t=None,
                       width=REEL_W, height=REEL_H,
                       fg_rgb=(255, 255, 255), scrim_rgb=(18, 30, 60)):
    """
    Burn ONE block of ALREADY-VALIDATED overlay `lines` (ALL-CAPS strings coming
    straight out of story_overlay.py — density-wrapped, <=8 words/line, <=2
    lines/frame; never re-wrapped or re-derived here) onto a video, per the Roxx
    overlay standard:

      * a brand scrim whose alpha comes from story_overlay.scrim_alpha_for(), fed a
        REAL sampled backdrop color (never a fixed guess) so it clears the real
        4.5:1 WCAG contrast target against THIS footage
      * positioned inside the Story safe zone (top 250px / bottom 310px of a
        1080x1920 frame) per story_overlay.safe_zone_ok(); a box that would violate
        it raises RenderError instead of being drawn (the caller HOLDS the render
        with an honest reason rather than burn a frame that fails its own
        validation)
      * anchor='top' places the block just inside the top safe boundary (the hook);
        anchor='bottom' places it just inside the bottom safe boundary (the single
        ask frame), always clear of the LASSO brand bar drawn later by
        add_brand_frame
      * start_t/end_t (seconds, optional): the block is visible only in that
        window via ffmpeg drawtext's 'enable' expression; omitted = the whole clip

    Raises RenderError when the render flag is OFF, ffmpeg is absent, `lines` is
    empty, or the computed box fails the safe-zone check. Returns output_path.
    """
    from . import story_overlay as _ov

    _require_render()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    lines = [str(ln or "").strip() for ln in (lines or []) if str(ln or "").strip()]
    if not lines:
        raise RenderError("burn_overlay_block: no lines to burn (empty overlay block); "
                          "the render must HOLD rather than burn nothing")

    line_h = int(_OVERLAY_FONT_SIZE * _OVERLAY_LINE_GAP)
    block_h = _OVERLAY_PAD * 2 + line_h * len(lines)
    safe_top, safe_bottom = _ov.safe_zone_bounds(height)
    if anchor == "top":
        y_top = safe_top + _OVERLAY_OUTER_MARGIN
        y_bottom = y_top + block_h
    else:
        y_bottom = safe_bottom - _OVERLAY_OUTER_MARGIN
        y_top = y_bottom - block_h

    if not _ov.safe_zone_ok((y_top, y_bottom), frame_h=height):
        raise RenderError(
            f"overlay text box (top={y_top}px, bottom={y_bottom}px, "
            f"{len(lines)} line(s)) violates the story safe zone "
            f"(top>={_ov.SAFE_TOP}px, bottom<={height - _ov.SAFE_BOTTOM}px); "
            f"render HELD, nothing burned")

    sample_at = sample_t if sample_t is not None else (
        ((start_t or 0.0) + end_t) / 2.0 if end_t is not None else (start_t or 0.0))
    try:
        backdrop = _sample_backdrop_rgb(input_path, sample_at, (y_top, y_bottom),
                                        width=width, height=height)
    except RenderError:
        raise
    except Exception as e:  # noqa: BLE001 - a sampling failure HOLDS, never guesses
        raise RenderError(f"backdrop sample failed: {type(e).__name__}: {e}") from e
    alpha = _ov.scrim_alpha_for(fg_rgb, backdrop, scrim_rgb=scrim_rgb)

    def _esc(t):
        return t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    fg_hex = "".join(f"{c:02X}" for c in fg_rgb)
    scrim_hex = "".join(f"{c:02X}" for c in scrim_rgb)
    enable = f":enable='between(t,{start_t or 0},{end_t})'" if end_t is not None else ""

    vf_parts = []
    if alpha > 0:
        vf_parts.append(
            f"drawbox=x=0:y={y_top}:w={width}:h={block_h}:"
            f"color=0x{scrim_hex}@{alpha / 255:.3f}:t=fill{enable}")
    for i, ln in enumerate(lines):
        line_y = y_top + _OVERLAY_PAD + i * line_h
        vf_parts.append(
            f"drawtext=fontsize={_OVERLAY_FONT_SIZE}:fontcolor=0x{fg_hex}:"
            f"text='{_esc(ln)}':x=(w-tw)/2:y={line_y}:font=Arial{enable}")
    vf = ",".join(vf_parts)

    cmd = [
        _ffmpeg(), "-y", "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-crf", "22", "-preset", "fast",
        "-c:a", "copy",
        output_path,
    ]
    _run(cmd, "burn_overlay_block")
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
