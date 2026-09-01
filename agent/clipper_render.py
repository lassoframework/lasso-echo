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
from . import story_layout as _sl

REEL_W = 1080
REEL_H = 1920
# LOWER_H is the brand bar's height. DEFERRED to story_layout.BRAND_BAR_H (the
# merged layout authority, ruling 3 2026-09-01) so the overlay's safe-zone
# check and the brand bar's actual drawn footprint can never drift apart —
# change the bar's height in ONE place (story_layout.py) and both move
# together. Kept as a module-level name for existing callers/tests.
LOWER_H = _sl.BRAND_BAR_H    # lower-third bar height (pixels)

# Caption vertical position as a fraction of frame height, measured from the
# bottom. 0.417 of a 1920px frame ~= 800px (lower-middle / second-third of the
# frame). Scales correctly to any height (e.g. 1:1 1080 -> ~450px).
_CAPTION_MARGIN_FRAC = 0.417

_BRAND_NAVY_HEX = "121E3C"   # without # — LASSO V3 house-style navy (canonical)
_BRAND_RED_HEX = "FF0000"    # LASSO V3 house-style red (canonical)
_BRAND_SKY_HEX = "5EB9E6"    # LASSO V3 house-style sky (canonical)
_BRAND_CREAM_HEX = "FAF6F0"  # LASSO V3 house-style cream (canonical)
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

def _esc_ffmpeg(t):
    """Escape a string for ffmpeg's drawtext filter arguments (text=, fontfile=)."""
    return str(t).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def add_brand_frame(input_path, output_path,
                    logo_text="LASSO", handle_text="@GymMarketingMadeSimple",
                    width=REEL_W, height=REEL_H, identity_text=None):
    """
    Overlay the LASSO brand frame on a vertical or square video.
    Design: thin LOWER_H-px solid navy bar at the bottom.
    - 3px LASSO red accent line at the very top of the bar
    - Logo left-aligned, white, vertically centered in bar
    - Handle right-aligned, white, vertically centered in bar — UNLESS
      `identity_text` is given (ruling 3, 2026-09-01: on the Story's ask frame,
      the identity anchor city/gym name moves INTO the brand bar in place of
      the handle, so the ask frame's own overlay budget stays free for the ask
      alone; see story_layout.identity_text_for_bar, the one place this text
      is formatted).
    Uses ffmpeg 'ih'/'iw' expressions so it adapts to any frame size, and an
    explicit fontfile= (story_layout.OVERLAY_FONT_PATH) so the brand bar's
    text renders in the SAME font this module measures for the overlay
    character budget — no dependence on fontconfig name resolution.
    Raises RenderError when the render flag is OFF or ffmpeg is absent.
    """
    _require_render()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    right_text = identity_text if identity_text else handle_text
    right_size = 26 if identity_text else 22
    fontfile = _esc_ffmpeg(_sl.OVERLAY_FONT_PATH)

    vf_parts = [
        # Solid navy brand bar (full width, LOWER_H tall, anchored to bottom)
        f"drawbox=x=0:y=ih-{LOWER_H}:w=iw:h={LOWER_H}:"
        f"color=0x{_BRAND_NAVY_HEX}@1.0:t=fill",
        # 3px LASSO red accent line at the very top of the bar
        f"drawbox=x=0:y=ih-{LOWER_H}:w=iw:h=3:"
        f"color=0x{_BRAND_RED_HEX}@1.0:t=fill",
        # Logo: white, left-aligned, vertically centered via th expression
        # (unchanged brand color -- ruling 2's cream-type call is about the
        # Roxx overlay scrim card specifically, not this pre-existing bar used
        # by every clipper render, Story or not).
        f"drawtext=fontsize=42:fontcolor=0x{_BRAND_WHITE_HEX}:"
        f"text='{_esc_ffmpeg(logo_text)}':x=28:y=h-{LOWER_H}+({LOWER_H}-th)/2:"
        f"fontfile='{fontfile}'",
        # Handle / identity: white, right-aligned, same vertical center
        f"drawtext=fontsize={right_size}:fontcolor=0x{_BRAND_WHITE_HEX}:"
        f"text='{_esc_ffmpeg(right_text)}':x=w-tw-28:y=h-{LOWER_H}+({LOWER_H}-th)/2:"
        f"fontfile='{fontfile}'",
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
# The overlay CONTENT (lines, character budget, safe-zone bounds, fonts, box
# geometry) is ALWAYS produced by story_layout.py (the merged layout authority,
# ruling 3 2026-09-01) and story_overlay.py's tested functions — this module
# never re-derives or guesses copy, contrast, position, or font. It only turns
# already-validated data into real ffmpeg drawbox/drawtext/overlay calls.

_OVERLAY_FONT_SIZE = _sl.OVERLAY_FONT_SIZE   # kept as a module alias; story_layout owns it

# The scrim alpha is sized against a SAFETY-MARGINED target, not the bare 4.5
# WCAG bar (story_overlay.TARGET_CONTRAST). A discrete worst-case grid
# (_worst_backdrop_rgb samples a finite number of instants/points) can still
# miss a genuinely worse pixel between samples, and H.264 compression can
# introduce a stray bright/dark pixel at a sharp scrim/footage boundary that a
# single continuous frame wouldn't have. ~12% headroom absorbs both without
# materially darkening the card on already-easy backdrops (scrim_alpha_for
# only adds alpha when the backdrop actually needs it).
_CONTRAST_SAFETY_TARGET = 5.0


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


def _sample_times(start_t, end_t, n=5, margin=0.15):
    """n evenly-spaced instants across [start_t, end_t] (n=1 -> just start_t).
    Ruling 2 (2026-09-01): contrast must be sized from the WORST moment across
    the full on-screen window, not one midpoint instant -- a frame can be dark
    when the block first appears and pan to a bright wall two seconds later.

    Insets `margin` seconds from BOTH edges: a seek landing exactly at (or
    past) the container's reported end can hit past the last encoded video
    frame, and _extract_frame_png raises loudly in that case (by design, it
    means a real truncated track) -- but here it would misfire on a totally
    healthy clip just because the sample window's own end_t equals the
    video's total duration. Sampling strictly inside the window avoids that
    false alarm while still covering the real displayed range."""
    s = float(start_t or 0.0)
    e = float(end_t) if end_t is not None else s
    if e <= s:
        return [s]
    inset = min(margin, (e - s) / 4)
    lo, hi = s + inset, e - inset
    if hi <= lo:
        return [(s + e) / 2.0]
    if n <= 1:
        return [lo]
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


def _worst_backdrop_rgb(input_path, start_t, end_t, box, *, fg_rgb,
                        width=REEL_W, height=REEL_H, n_times=5, n_cols=7, n_rows=4):
    """The single pixel, across MULTIPLE instants in [start_t, end_t] AND
    multiple rows/columns within `box`, that gives `fg_rgb` the WORST (lowest)
    contrast ratio. Ruling 2: a mean-of-one-instant sample let bare light text
    sit undetected on a bright wall (the mean diluted a small bright patch
    across the whole band); sampling a grid of real points and taking the
    single worst one catches that instead. Returns (rgb, contrast_ratio)."""
    from PIL import Image
    from . import story_overlay as _ov

    y_top, y_bottom = box
    y_top = max(0, min(int(y_top), height - 1))
    y_bottom = max(y_top + 1, min(int(y_bottom), height))

    worst_rgb, worst_c = (0, 0, 0), None
    with tempfile.TemporaryDirectory() as td:
        for i, t in enumerate(_sample_times(start_t, end_t, n_times)):
            png = os.path.join(td, f"sample_{i}.png")
            _extract_frame_png(input_path, t, png)
            img = Image.open(png).convert("RGB")
            if img.size != (width, height):
                img = img.resize((width, height))
            crop = img.crop((0, y_top, width, y_bottom))
            cw, ch = crop.size
            if cw <= 0 or ch <= 0:
                continue
            px = crop.load()
            xs = sorted({min(cw - 1, int(cw * k / (n_cols - 1))) for k in range(n_cols)})
            ys = sorted({min(ch - 1, int(ch * k / (max(2, n_rows) - 1)))
                        for k in range(max(2, n_rows))})
            for x in xs:
                for y in ys:
                    rgb = px[x, y]
                    c = _ov.contrast_ratio(fg_rgb, rgb)
                    if worst_c is None or c < worst_c:
                        worst_c, worst_rgb = c, rgb
    if worst_c is None:
        raise RenderError(
            f"no pixels sampled inside box (top={y_top}, bottom={y_bottom}) across "
            f"t={start_t}..{end_t}; the overlay contrast cannot be sized without a "
            f"real sample, render HELD")
    return worst_rgb, worst_c


def _make_scrim_png(w, h, rgb, alpha, out_path, radius=22):
    """A single RGBA PNG: a rounded rectangle filled with `rgb` at `alpha`
    (0..255), matching the LASSO deck's own card treatment (ruling 2: navy
    scrim, consistent corner radius). ffmpeg's drawbox has no native rounded-
    corner support, so the scrim is composited as an image via `overlay=`
    instead of drawn with drawbox -- this is the ONE place a rounded card is
    produced, so every scrim (hook or ask) looks identical."""
    from PIL import Image, ImageDraw
    w, h = max(1, int(w)), max(1, int(h))
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r = max(0, min(int(radius), w // 2, h // 2))
    fill = (int(rgb[0]), int(rgb[1]), int(rgb[2]), int(max(0, min(255, alpha))))
    draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=fill)
    img.save(out_path)
    return out_path


def burn_overlay_block(input_path, output_path, lines, *, anchor="top",
                       start_t=None, end_t=None, sample_t=None,
                       width=REEL_W, height=REEL_H,
                       fg_rgb=None, scrim_rgb=None, anchor_text=None):
    """
    Burn ONE block of ALREADY-VALIDATED overlay `lines` (ALL-CAPS strings coming
    straight out of story_overlay.py — density-wrapped, <=8 words/line, <=char
    budget/line; never re-wrapped or re-derived here) onto a video, per the Roxx
    overlay standard (as rebuilt 2026-09-01 after the 0/10 pixel-measured proof):

      * a LASSO-branded scrim (navy, rounded corners, cream type by default —
        story_layout / the canonical V3 palette) whose alpha comes from
        story_overlay.scrim_alpha_for(), fed the WORST real sampled backdrop
        pixel across the FULL start_t..end_t display window (ruling 2), never
        one midpoint instant and never a whole-band mean
      * positioned via story_layout's box geometry (hook_box for anchor='top',
        ask_box for anchor='bottom'), inside the safe zone that is itself
        DERIVED from the brand bar's real footprint (ruling 3) — a box that
        would violate it raises RenderError instead of being drawn
      * each line's REAL rendered width (story_layout.measure_text_width, the
        same font ffmpeg's drawtext uses) is checked against the box's usable
        width; a line that still overflows at burn time (both the generation-
        time character cap AND the approval-card cap already having run)
        shrinks the WHOLE block's font size as a failsafe (never below
        story_layout.SHRINK_FONT_FLOOR) and logs an ops alert loudly — this
        firing at all means an upstream cap failed to catch something
      * anchor_text (optional): a small identity-anchor line (city/gym, ruling
        3) burned just below a 'top' block on EVERY hook frame — never on the
        'bottom' (ask) frame, where the identity instead moves into the brand
        bar (see clipper_render.add_brand_frame's identity_text)
      * start_t/end_t (seconds, optional): the block is visible only in that
        window via ffmpeg's 'enable' expression; omitted = the whole clip

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

    # Canonical LASSO V3 palette (ruling 2): navy scrim, cream type. Defaults
    # kept as function params (not hardcoded in the call) so a caller CAN
    # override for a proof/before-after comparison; production never does.
    if fg_rgb is None:
        fg_rgb = tuple(int(_BRAND_CREAM_HEX[i:i + 2], 16) for i in (0, 2, 4))
    if scrim_rgb is None:
        scrim_rgb = tuple(int(_BRAND_NAVY_HEX[i:i + 2], 16) for i in (0, 2, 4))

    box_fn = _sl.hook_box if anchor == "top" else _sl.ask_box
    y_top, y_bottom = box_fn(len(lines), frame_h=height)

    if not _sl.safe_zone_ok((y_top, y_bottom), frame_h=height):
        lo, hi = _sl.safe_zone_bounds(height)
        raise RenderError(
            f"overlay text box (top={y_top}px, bottom={y_bottom}px, "
            f"{len(lines)} line(s)) violates the story safe zone "
            f"(top>={lo}px, bottom<={hi}px, derived from the brand bar's real "
            f"footprint); render HELD, nothing burned")

    anchor_bounds = None
    if anchor_text and anchor == "top":
        anchor_bounds = _sl.anchor_box((y_top, y_bottom), frame_h=height)
        if not _sl.safe_zone_ok(anchor_bounds, frame_h=height):
            raise RenderError(
                f"identity-anchor box (top={anchor_bounds[0]}px, "
                f"bottom={anchor_bounds[1]}px) violates the story safe zone; "
                f"render HELD, nothing burned")

    box_w = width - 2 * _sl.OVERLAY_PAD_X
    # usable_w matches EXACTLY the width story_layout.MAX_CHARS_PER_LINE was
    # measured against (FRAME_W - 2*OVERLAY_PAD_X) -- using a narrower number
    # here would make this failsafe fire on lines the generation-time cap
    # already legitimately cleared.
    usable_w = box_w

    # ---- render-time failsafe: REAL measured width, shrink only if needed ----
    font_size = _sl.OVERLAY_FONT_SIZE
    widest = max((_sl.measure_text_width(ln, font_size=font_size) for ln in lines),
                 default=0)
    if widest > usable_w:
        font_size, fits = _sl.fit_font_size(
            max(lines, key=lambda s: _sl.measure_text_width(s, font_size=_sl.OVERLAY_FONT_SIZE)),
            max_width=usable_w, start_size=_sl.OVERLAY_FONT_SIZE)
        try:
            from . import ops_alerts
            ops_alerts.alert(
                "story overlay shrink-to-fit failsafe fired: a line still "
                f"overflowed at burn time ({widest:.0f}px > {usable_w}px usable) "
                f"despite the generation-time character cap; shrunk font to "
                f"{font_size}px (fits={fits}). Both upstream caps missed "
                f"something -- investigate story_layout.MAX_CHARS_PER_LINE / "
                f"the approval-card editor cap.")
        except Exception:  # noqa: BLE001 - an alert failure must never block the burn
            pass

    line_h = int(font_size * _sl.OVERLAY_LINE_GAP)
    block_h = _sl.OVERLAY_PAD * 2 + line_h * len(lines)
    if anchor == "top":
        y_bottom = y_top + block_h
    else:
        y_top = y_bottom - block_h

    sample_start = start_t if start_t is not None else 0.0
    sample_end = end_t if end_t is not None else sample_t if sample_t is not None else sample_start
    try:
        backdrop, _worst_c = _worst_backdrop_rgb(
            input_path, sample_start, sample_end, (y_top, y_bottom),
            fg_rgb=fg_rgb, width=width, height=height)
    except RenderError:
        raise
    except Exception as e:  # noqa: BLE001 - a sampling failure HOLDS, never guesses
        raise RenderError(f"backdrop sample failed: {type(e).__name__}: {e}") from e
    alpha = _ov.scrim_alpha_for(fg_rgb, backdrop, scrim_rgb=scrim_rgb,
                                target=_CONTRAST_SAFETY_TARGET)

    fg_hex = "".join(f"{c:02X}" for c in fg_rgb)
    enable = f":enable='between(t,{start_t or 0},{end_t})'" if end_t is not None else ""
    fontfile = _esc_ffmpeg(_sl.OVERLAY_FONT_PATH)

    inputs = [input_path]
    filter_stages = []
    cur_label = "0:v"
    next_idx = [1]

    def _overlay_scrim(y0, h0, alpha0, radius=22):
        if alpha0 <= 0:
            return
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            png_path = tf.name
        _make_scrim_png(box_w, h0, scrim_rgb, alpha0, png_path, radius=radius)
        inputs.append(png_path)
        idx = next_idx[0]
        next_idx[0] += 1
        x0 = _sl.OVERLAY_PAD_X
        out_label = f"v{idx}"
        filter_stages.append(
            f"[{cur_label}][{idx}:v]overlay=x={x0}:y={y0}{enable}[{out_label}]")
        return out_label

    scrim_label = _overlay_scrim(y_top, block_h, alpha)
    if scrim_label:
        cur_label = scrim_label

    anchor_scrim_label = None
    if anchor_bounds:
        a_top, a_bottom = anchor_bounds
        try:
            a_backdrop, _ = _worst_backdrop_rgb(
                input_path, sample_start, sample_end, anchor_bounds,
                fg_rgb=fg_rgb, width=width, height=height, n_times=3)
        except RenderError:
            raise
        a_alpha = _ov.scrim_alpha_for(fg_rgb, a_backdrop, scrim_rgb=scrim_rgb,
                                      target=_CONTRAST_SAFETY_TARGET)
        anchor_scrim_label = _overlay_scrim(a_top, a_bottom - a_top, a_alpha, radius=10)
        if anchor_scrim_label:
            cur_label = anchor_scrim_label

    draw_parts = []
    for i, ln in enumerate(lines):
        line_y = y_top + _sl.OVERLAY_PAD + i * line_h
        draw_parts.append(
            f"drawtext=fontsize={font_size}:fontcolor=0x{fg_hex}:"
            f"text='{_esc_ffmpeg(ln)}':x=(w-tw)/2:y={line_y}:"
            f"fontfile='{fontfile}'{enable}")
    if anchor_bounds and anchor_text:
        a_top, _ = anchor_bounds
        anchor_fontfile = _esc_ffmpeg(_sl.ANCHOR_FONT_PATH)
        draw_parts.append(
            f"drawtext=fontsize={_sl.ANCHOR_FONT_SIZE}:fontcolor=0x{fg_hex}:"
            f"text='{_esc_ffmpeg(str(anchor_text))}':x=(w-tw)/2:y={a_top}:"
            f"fontfile='{anchor_fontfile}'{enable}")

    if draw_parts:
        chained = ",".join(draw_parts)
        filter_stages.append(f"[{cur_label}]{chained}[vout]")
        final_label = "vout"
    else:
        final_label = cur_label

    cmd = [_ffmpeg(), "-y"]
    for p in inputs:
        cmd += ["-i", p]
    if filter_stages:
        cmd += ["-filter_complex", ";".join(filter_stages),
               "-map", f"[{final_label}]", "-map", "0:a?"]
    cmd += ["-c:v", "libx264", "-crf", "22", "-preset", "fast", "-c:a", "copy", output_path]
    try:
        _run(cmd, "burn_overlay_block")
    finally:
        for p in inputs[1:]:
            try:
                os.unlink(p)
            except OSError:
                pass
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
