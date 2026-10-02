"""Deterministic, bounded visual moment selection for eligible gym originals.

This is pixel quality/motion analysis, not semantic AI: it cannot identify exercises,
consent, brand fit, or whether a repetition is good. Callers MUST establish raw-media
eligibility and tenant ownership first. A failed inspection holds; it never silently
substitutes the opening seconds. The full <=5 minute source is sampled at 2 Hz.
"""
from __future__ import annotations

import json
import math
import os
import subprocess

MAX_SECONDS = 300.0
MAX_BYTES = 900_000_000
SAMPLE_HZ = 2
WIDTH, HEIGHT = 128, 72
MAX_FRAMES = int(MAX_SECONDS * SAMPLE_HZ) + 1
MIN_WINDOW, MAX_WINDOW = 3.0, 15.0


def _hold(reason, **diagnostics):
    return {"held": True, "hold_reason": reason, "start_ts": None,
            "end_ts": None, "score": None, "diagnostics": diagnostics}


def frame_metrics(frames):
    """Measure grayscale numpy frames. Uses OpenCV's existing project dependency.

    Motion is absolute residual after compensating translation. A static scene or
    camera pan alone therefore does not qualify as activity. Camera shake is measured
    separately from subject movement. Thresholds are conservative engineering
    heuristics, not a human quality grade.
    """
    import cv2
    import numpy as np

    records = []
    previous = None
    previous_shift = None
    for index, pixels in enumerate(frames):
        current = np.asarray(pixels, dtype=np.uint8)
        if current.shape != (HEIGHT, WIDTH):
            raise ValueError("Unexpected analysis frame dimensions")
        mean = float(current.mean()) / 255
        clipped = float(np.mean((current <= 8) | (current >= 247)))
        contrast = float(current.std()) / 255
        sharpness = float(cv2.Laplacian(current, cv2.CV_32F).var())
        motion = 0.0
        shift = (0.0, 0.0)
        shake = False
        confidence = 1.0
        if previous is not None:
            shift, confidence = cv2.phaseCorrelate(previous.astype(np.float32),
                                                   current.astype(np.float32))
            if not all(math.isfinite(v) for v in (*shift, confidence)):
                shift, confidence = (0.0, 0.0), 0.0
            # Unreliable registration cannot turn a frame into apparent stability.
            if confidence < 0.12:
                shake = True
                shift = (0.0, 0.0)
            dx, dy = shift
            aligned = cv2.warpAffine(previous, np.float32([[1, 0, dx], [0, 1, dy]]),
                                     (WIDTH, HEIGHT), borderMode=cv2.BORDER_REFLECT)
            # Exclude the border introduced by camera registration.
            margin = min(16, max(3, int(max(abs(dx), abs(dy))) + 1))
            a = current[margin:-margin, margin:-margin].astype(np.float32)
            b = aligned[margin:-margin, margin:-margin].astype(np.float32)
            motion = float(np.mean(np.abs(a - b))) / 255
            magnitude = math.hypot(dx, dy)
            shake = shake or magnitude > 8
            if previous_shift is not None:
                px, py = previous_shift
                # Direction reversals of the entire frame are camera shake.
                shake = shake or (magnitude > 2 and math.hypot(px, py) > 2
                                  and dx * px + dy * py < -2)
            previous_shift = shift
        records.append({"time": index / SAMPLE_HZ, "mean": mean,
                        "clipped": clipped, "contrast": contrast,
                        "sharpness": sharpness, "motion": motion,
                        "shake": bool(shake), "camera_shift": list(shift),
                        "registration_confidence": float(confidence)})
        previous = current
    return records


def choose_window(metrics, duration, *, window_sec=6.0):
    """Choose a contiguous inspected window. Pure function for offline verification."""
    if (not isinstance(duration, (int, float)) or not math.isfinite(duration)
            or duration < MIN_WINDOW or duration > MAX_SECONDS):
        return _hold("Source duration is outside the 3 second to 5 minute limit")
    if (not isinstance(window_sec, (int, float)) or not math.isfinite(window_sec)
            or not MIN_WINDOW <= window_sec <= MAX_WINDOW):
        return _hold("Requested moment must be between 3 and 15 seconds")
    duration = float(duration)
    window = min(float(window_sec), duration)
    expected = math.ceil(duration * SAMPLE_HZ - 1e-7)
    if len(metrics) < expected or len(metrics) > MAX_FRAMES:
        return _hold("Full clip inspection is incomplete", samples=len(metrics),
                     expected_samples=expected)
    required = ("time", "mean", "clipped", "contrast", "sharpness", "motion")
    for i, row in enumerate(metrics):
        if (any(not isinstance(row.get(k), (int, float)) or
                not math.isfinite(row[k]) for k in required)
                or abs(row["time"] - i / SAMPLE_HZ) > 0.001
                or not isinstance(row.get("shake"), bool)):
            return _hold("Invalid visual inspection evidence")
    candidates = []
    rejection_counts = {"exposure_or_blur": 0, "camera_movement": 0,
                        "insufficient_activity": 0}
    for first in range(len(metrics)):
        start = first / SAMPLE_HZ
        end = start + window
        if end > duration + 1e-7:
            break
        rows = [r for r in metrics[first:] if r["time"] < end - 1e-7]
        # Transition into the first frame lies outside this candidate window.
        transitions = rows[1:]
        exposed = [r for r in rows if 0.10 <= r["mean"] <= 0.90
                   and r["clipped"] <= 0.55 and r["contrast"] >= 0.035
                   and r["sharpness"] >= 12]
        if len(exposed) / len(rows) < 0.9:
            rejection_counts["exposure_or_blur"] += 1
            continue
        if not transitions or any(r["shake"] for r in transitions):
            rejection_counts["camera_movement"] += 1
            continue
        active = [r for r in transitions if 0.008 <= r["motion"] <= 0.28]
        if len(active) / len(transitions) < 0.65:
            rejection_counts["insufficient_activity"] += 1
            continue
        activity = sum(min(r["motion"] / 0.07, 1.0) for r in active) / len(transitions)
        exposure = sum(1 - abs(r["mean"] - 0.5) * 2 for r in rows) / len(rows)
        detail = sum(min(r["sharpness"] / 300, 1.0) for r in rows) / len(rows)
        score = round(100 * (0.60 * activity + 0.25 * exposure + 0.15 * detail), 3)
        candidates.append((score, start, end))
    common = {"method": "local_pixel_motion_v1", "sample_hz": SAMPLE_HZ,
              "samples": len(metrics), "source_duration": duration,
              "inspected_through": metrics[-1]["time"] if metrics else None,
              "window_seconds": window, "rejected_windows": rejection_counts,
              "usable_windows": len(candidates)}
    if not candidates:
        return _hold("No sufficiently exposed, sharp, stable active moment found", **common)
    # Deterministic tie: earlier equally good window wins, no random selection.
    score, start, end = max(candidates, key=lambda c: (c[0], -c[1]))
    chosen = [r for r in metrics if start <= r["time"] < end]
    common["selected_quality"] = {
        "mean_luma": round(sum(r["mean"] for r in chosen) / len(chosen), 4),
        "max_clipped_fraction": round(max(r["clipped"] for r in chosen), 4),
        "min_sharpness": round(min(r["sharpness"] for r in chosen), 3),
        "mean_subject_motion": round(sum(r["motion"] for r in chosen[1:]) / (len(chosen) - 1), 4),
        "camera_shake_frames": sum(r["shake"] for r in chosen[1:]),
    }
    return {"held": False, "hold_reason": "", "start_ts": round(start, 3),
            "end_ts": round(end, 3), "score": score, "diagnostics": common}


def select_moment(source_path, *, window_sec=6.0, runner=None):
    """Probe/sample a real local source and return selected timestamps or a hold.

    runner is injectable for failure and input-cap tests; production uses
    subprocess.run with hard timeouts, byte/frame caps and no shell. A successful
    result includes analysis evidence but makes no claim of semantic understanding.
    """
    run = runner or subprocess.run
    if (not isinstance(window_sec, (int, float)) or not math.isfinite(window_sec)
            or not MIN_WINDOW <= window_sec <= MAX_WINDOW):
        return _hold("Requested moment must be between 3 and 15 seconds")
    try:
        size = os.path.getsize(source_path)
        if size <= 0 or size > MAX_BYTES:
            return _hold("Source size is outside the 900 MB input limit", bytes=size)
        probe = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=codec_type,duration:format=duration",
                     "-of", "json", os.fspath(source_path)], capture_output=True,
                    timeout=20, check=True)
        metadata = json.loads(probe.stdout)
        streams = metadata.get("streams") or []
        if not streams or streams[0].get("codec_type") != "video":
            return _hold("Source has no decodable video stream")
        duration = float(streams[0].get("duration")
                         or metadata.get("format", {}).get("duration") or 0)
        if not math.isfinite(duration) or not MIN_WINDOW <= duration <= MAX_SECONDS:
            return _hold("Source duration is outside the 3 second to 5 minute limit")
        decoded = run(["ffmpeg", "-nostdin", "-v", "error", "-i", os.fspath(source_path),
                       "-map", "0:v:0", "-an", "-t", str(MAX_SECONDS),
                       "-vf", f"fps={SAMPLE_HZ}:start_time=0:round=up,scale={WIDTH}:{HEIGHT}",
                       "-frames:v", str(MAX_FRAMES), "-f", "rawvideo", "-pix_fmt", "gray",
                       "pipe:1"], capture_output=True, timeout=90, check=True)
        raw = decoded.stdout
        frame_bytes = WIDTH * HEIGHT
        if len(raw) > MAX_FRAMES * frame_bytes or len(raw) % frame_bytes:
            return _hold("Invalid decoded frame bounds")
        import numpy as np
        frames = np.frombuffer(raw, dtype=np.uint8).reshape((-1, HEIGHT, WIDTH))
        evidence = frame_metrics(frames)
        result = choose_window(evidence, duration, window_sec=window_sec)
        attempted = [float(window_sec)]
        # Prefer the requested duration, but a shorter genuinely usable moment is
        # better than extending it through a camera whip. Never weaken quality gates.
        shorter = math.ceil(min(float(window_sec), duration)) - 1
        while (result["held"] and result["hold_reason"].startswith("No sufficiently")
               and shorter >= MIN_WINDOW):
            attempted.append(float(shorter))
            result = choose_window(evidence, duration, window_sec=float(shorter))
            shorter -= 1
        result["diagnostics"]["requested_window_seconds"] = window_sec
        result["diagnostics"]["attempted_window_seconds"] = attempted
        result["diagnostics"]["source_bytes"] = size
        return result
    except Exception:  # Includes optional decoder errors; fail closed without logging source paths.
        return _hold("Video inspection failed; source requires review")
