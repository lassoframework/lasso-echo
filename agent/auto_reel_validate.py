"""Technical validation of a finished automatic reel before calendar staging.

This checks the encoded artifact, not its visual/editorial quality. Stream durations
are authoritative: a long audio track must never disguise a truncated video stream.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
import subprocess

_VIDEO_TOLERANCE = 0.75
_AUDIO_TOLERANCE = 0.5
_MAX_EXPECTED_SECONDS = 60.0


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _stream_duration(stream):
    seconds = _number(stream.get("duration"))
    if seconds is not None and seconds > 0:
        return seconds
    # Some containers expose only stream ticks. Never fall back to format.duration.
    ticks = _number(stream.get("duration_ts"))
    try:
        numerator, denominator = str(stream.get("time_base") or "").split("/")
        scale = float(numerator) / float(denominator)
        seconds = ticks * scale if ticks is not None else None
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return seconds if seconds is not None and math.isfinite(seconds) and seconds > 0 else None


def validate_output(path, expected_duration, require_audio=True):
    """Return {ok, reason, metadata}; fail closed on missing tools or uncertain bytes.

    ffprobe has a 15s ceiling; full decoding is bounded to 30..180s according to the
    expected reel length. Decoder stderr is deliberately not returned (it can include
    local source paths). No writes, publishing, grading, or external network access.
    """
    metadata = {"expected_duration": None, "decode_checked": False}

    def held(reason):
        return {"ok": False, "reason": reason, "metadata": metadata}

    expected = _number(expected_duration)
    if expected is None or not 0 < expected <= _MAX_EXPECTED_SECONDS:
        return held("Expected reel duration must be finite and between 0 and 60 seconds")
    metadata["expected_duration"] = expected
    try:
        source = Path(path)
        if not source.is_file() or source.stat().st_size <= 0:
            return held("Rendered video file is missing or empty")
        source = source.resolve()
    except (OSError, TypeError, ValueError):
        return held("Rendered video file cannot be read")
    probe, decoder = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not probe or not decoder:
        return held("Video validation requires ffprobe and ffmpeg")
    try:
        result = subprocess.run(
            [probe, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams", "-show_format", "-of", "json", str(source)],
            capture_output=True, text=True, timeout=15, check=False)
        if result.returncode:
            return held("Rendered video could not be probed")
        data = json.loads(result.stdout)
        streams = data.get("streams")
        if not isinstance(streams, list) or not all(isinstance(s, dict) for s in streams):
            return held("Rendered video has no readable stream metadata")
        video = [s for s in streams if s.get("codec_type") == "video"]
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        if len(video) != 1:
            return held("Rendered reel must contain exactly one video stream")
        v = video[0]
        metadata.update(video_codec=v.get("codec_name"), pixel_format=v.get("pix_fmt"),
                        width=v.get("width"), height=v.get("height"),
                        video_duration=_stream_duration(v), audio_streams=len(audio))
        if v.get("codec_name") != "h264" or v.get("pix_fmt") != "yuv420p":
            return held("Rendered reel must use H264 video with yuv420p pixels")
        if v.get("width") != 1080 or v.get("height") != 1920:
            return held("Rendered reel must be 1080 by 1920 pixels")
        if (v.get("disposition") or {}).get("attached_pic"):
            return held("Rendered reel contains cover art instead of a video stream")
        duration = metadata["video_duration"]
        if duration is None or abs(duration - expected) > _VIDEO_TOLERANCE:
            return held("Video stream duration does not match the finished reel")
        if require_audio and not audio:
            return held("Rendered reel is missing its required audio stream")
        if len(audio) > 1:
            return held("Rendered reel must contain at most one mixed audio stream")
        if audio:
            a = audio[0]
            audio_duration = _stream_duration(a)
            metadata.update(audio_codec=a.get("codec_name"), audio_duration=audio_duration,
                            audio_channels=a.get("channels"), audio_sample_rate=a.get("sample_rate"))
            if (_number(a.get("channels")) or 0) <= 0 or (_number(a.get("sample_rate")) or 0) <= 0:
                return held("Rendered audio stream has invalid channels or sample rate")
            if audio_duration is None or abs(audio_duration - duration) > _AUDIO_TOLERANCE:
                return held("Audio and video streams have different durations")
        command = [decoder, "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode",
                   "-protocol_whitelist", "file,pipe", "-i", str(source), "-map", "0:v:0"]
        if audio:
            command.extend(["-map", "0:a:0"])
        command.extend(["-f", "null", "-"])
        decoded = subprocess.run(command, capture_output=True, text=True,
                                 timeout=max(30, math.ceil(expected * 3)), check=False)
        if decoded.returncode:
            return held("Rendered video or audio failed full decoding")
        metadata["decode_checked"] = True
        return {"ok": True, "reason": "", "metadata": metadata}
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError):
        return held("Rendered video validation could not finish reliably")
