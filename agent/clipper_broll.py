"""
Clipper B-roll overlay module.

Creates styled text cards for key moments in a podcast clip and composites
them over the main video using ffmpeg. Gate: AGENT_CLIPPER_BROLL_ENABLED=true
(default OFF per non-negotiable gate rule).

Card design: full-frame navy background, ALL-CAPS white text centered,
LASSO red horizontal accent above text, fade-in/fade-out transitions.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile

from . import config
from .clipper_render import (
    REEL_W, REEL_H,
    _BRAND_NAVY_HEX, _BRAND_RED_HEX, _BRAND_WHITE_HEX,
    RenderError,
    _ffmpeg, _run,
)

BROLL_CARD_DURATION = 2.5   # seconds per B-roll card
BROLL_FADE_DUR = 0.20       # fade in / out duration
BROLL_MIN_GAP = 10.0        # minimum seconds between two cards
BROLL_MIN_OFFSET = 5.0      # earliest card offset from clip start
BROLL_FONT_SIZE = 88        # px — slightly smaller than caption size for cards


def broll_enabled():
    return config.clipper_broll_enabled()


# ---- text card generation ---------------------------------------------------

def _esc_drawtext(text):
    """Minimal escape for ffmpeg drawtext text= option (subprocess, not shell)."""
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _make_broll_card(text, output_path, duration=BROLL_CARD_DURATION):
    """
    Render a full-frame B-roll text card to output_path.
    Navy background, centered white ALL-CAPS text, red accent line,
    fade in/out transitions. Audio: silent AAC track.
    """
    ffmpeg = _ffmpeg()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    from .clipper_render import scrub_onscreen
    safe_text = _esc_drawtext(scrub_onscreen(str(text or "").strip().upper()))
    fade_out_start = max(0.0, float(duration) - BROLL_FADE_DUR)

    # Red accent line: horizontally centered block, 80px from center, 4px tall
    vf = (
        f"drawtext=fontsize={BROLL_FONT_SIZE}:fontcolor=0x{_BRAND_WHITE_HEX}:"
        f"font=Arial:text='{safe_text}':x=(w-tw)/2:y=(h-th)/2,"
        f"drawbox=x=100:y=h/2-80:w=w-200:h=4:"
        f"color=0x{_BRAND_RED_HEX}@1.0:t=fill,"
        f"fade=t=in:st=0:d={BROLL_FADE_DUR},"
        f"fade=t=out:st={fade_out_start:.3f}:d={BROLL_FADE_DUR}"
    )

    cmd = [
        ffmpeg, "-y",
        "-f", "lavfi", "-i",
        f"color=c=0x{_BRAND_NAVY_HEX}:s={REEL_W}x{REEL_H}:r=30",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-vf", vf,
        "-t", str(float(duration)),
        "-c:v", "libx264", "-crf", "22", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RenderError(
            f"broll card render failed: {result.stderr[-400:] if result.stderr else '(no stderr)'}"
        )
    return output_path


# ---- composite --------------------------------------------------------------

def _composite_broll(video_path, broll_clips, output_path):
    """
    Overlay B-roll cards onto video_path at their specified offsets.
    Each card is full-frame and opaque; audio from the main video continues
    uninterrupted. Returns video_path unchanged if broll_clips is empty.
    """
    if not broll_clips:
        return video_path

    ffmpeg = _ffmpeg()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    inputs = ["-i", video_path]
    for bc in broll_clips:
        inputs += ["-i", bc["card_path"]]

    filter_parts = []
    current = "0:v"
    for i, bc in enumerate(broll_clips):
        inp_idx = i + 1
        offset = float(bc["offset"])
        shifted = f"shifted_{i}"
        out_label = f"ov_{i}"
        filter_parts.append(
            f"[{inp_idx}:v]setpts=PTS-STARTPTS+{offset:.3f}/TB[{shifted}]"
        )
        filter_parts.append(
            f"[{current}][{shifted}]overlay=x=0:y=0:eof_action=pass[{out_label}]"
        )
        current = out_label

    cmd = (
        [ffmpeg, "-y"]
        + inputs
        + [
            "-filter_complex", ";".join(filter_parts),
            "-map", f"[{current}]",
            "-map", "0:a",
            "-c:v", "libx264", "-crf", "22", "-preset", "fast",
            "-c:a", "copy",
            output_path,
        ]
    )
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RenderError(
            f"broll composite failed: {result.stderr[-400:] if result.stderr else '(no stderr)'}"
        )
    return output_path


# ---- moment planning --------------------------------------------------------

def _parse_broll_json(raw):
    """Parse LLM B-roll JSON; tolerant of markdown fences."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"(\[.*\])", text, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except Exception:
            return None
    if not isinstance(data, list):
        return None
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        txt = str(item.get("text", "")).strip().upper()
        if not txt:
            continue
        out.append({
            "offset": float(item.get("offset", 0)),
            "text": txt,
            "duration": float(item.get("duration", BROLL_CARD_DURATION)),
        })
    return out or None


def _plan_fallback(seg_words, clip_start, clip_end):
    """Simple heuristic: pick two moments at 25% and 62% of clip duration."""
    dur = clip_end - clip_start
    safe_end = dur - BROLL_MIN_OFFSET
    if safe_end <= BROLL_MIN_OFFSET:
        return []

    moments = []
    for frac in (0.25, 0.62):
        offset = dur * frac
        if offset < BROLL_MIN_OFFSET or offset > safe_end:
            continue
        target_t = clip_start + offset
        nearby = sorted(
            [w for w in seg_words if abs(float(w.get("start", 0)) - target_t) < 4.0],
            key=lambda w: abs(float(w.get("start", 0)) - target_t),
        )[:3]
        if not nearby:
            continue
        text = " ".join(str(w.get("word", "")).strip().upper() for w in nearby)
        if text:
            moments.append({
                "offset": offset,
                "text": text,
                "duration": BROLL_CARD_DURATION,
            })
    return moments


def plan_broll_moments(transcript, clip_start, clip_end, llm=None):
    """
    Pick 2-3 B-roll overlay moments for the clip.
    Returns list of {offset (seconds from clip start), text (ALL CAPS), duration}.
    Uses Claude if llm is provided; falls back to position-based heuristic.
    """
    words = transcript.get("words", [])
    seg_words = [
        w for w in words
        if float(w.get("start", 0)) >= clip_start - 0.1
        and float(w.get("start", 0)) <= clip_end + 0.1
    ]

    if not llm:
        return _plan_fallback(seg_words, clip_start, clip_end)

    dur = clip_end - clip_start
    word_list = " ".join(str(w.get("word", "")) for w in seg_words)
    system = (
        "You select B-roll overlay moments for a social media video clip. "
        "Return only a JSON list of 2-3 objects. "
        "Each object: 'offset' (float, seconds from clip start), "
        "'text' (string, 3-4 words ALL CAPS — the KEY concept at that moment), "
        "'duration' (float, default 2.5)."
    )
    user = (
        f"Clip duration: {dur:.1f}s\n"
        f"Transcript words: {word_list}\n\n"
        f"Rules: offset must be between {BROLL_MIN_OFFSET:.0f} and {dur - BROLL_MIN_OFFSET:.1f}. "
        f"Offsets at least {BROLL_MIN_GAP:.0f}s apart. "
        "text must be 3-4 UPPERCASE words capturing the KEY INSIGHT of that moment. "
        "Never fabricate — only use words from the transcript. "
        "Return only the JSON list, no other text."
    )

    try:
        raw = llm(system, user)
        parsed = _parse_broll_json(raw)
        if parsed:
            # Clamp offsets to safe range
            safe = [
                m for m in parsed
                if BROLL_MIN_OFFSET <= m["offset"] <= dur - BROLL_MIN_OFFSET
            ]
            if safe:
                return safe
    except Exception as exc:
        print(f"[broll] plan_broll_moments error: {exc} — using fallback", flush=True)

    return _plan_fallback(seg_words, clip_start, clip_end)


# ---- orchestrator -----------------------------------------------------------

def add_broll(moment, video_path, transcript, output_dir, llm=None):
    """
    Plan B-roll moments, render text cards, composite onto video.
    Returns the path to the composited video (or the original if nothing to do).
    Errors are logged and re-raised so render_clip can catch and skip gracefully.
    """
    clip_start = float(getattr(moment, "start_ts", 0))
    clip_end = float(getattr(moment, "end_ts", clip_start))

    moments = plan_broll_moments(transcript, clip_start, clip_end, llm=llm)
    if not moments:
        return video_path

    base = f"clip_{int(clip_start):05d}_{int(clip_end):05d}"
    broll_clips = []

    for i, m in enumerate(moments):
        card_path = os.path.join(output_dir, f"{base}_broll_{i:02d}.mp4")
        try:
            _make_broll_card(m["text"], card_path, duration=m.get("duration", BROLL_CARD_DURATION))
            broll_clips.append({"card_path": card_path, "offset": m["offset"]})
            print(f"[broll] card {i}: '{m['text']}' at +{m['offset']:.1f}s", flush=True)
        except Exception as exc:
            print(f"[broll] card {i} failed ({exc}), skipping", flush=True)

    if not broll_clips:
        return video_path

    composited_path = os.path.join(output_dir, base + "_brolled.mp4")
    return _composite_broll(video_path, broll_clips, composited_path)
