"""
story_classifier.py — raw / finished / ambiguous sorting for the Story Studio lane
(ECHO_STORY_STUDIO_BUILD §0, flag STORY_CLASSIFIER, default ON).

The classifier ONLY sorts. It posts nothing, stages nothing, composes nothing. Its
job is to decide, for a media file that has NO declared lane, whether it is:

  * RAW      raw phone footage Echo may edit into a Story (enters the raw pool);
  * FINISHED an already-edited post to publish as-is (Echo does NOT re-edit it);
  * AMBIGUOUS neither signal is strong enough -> a "Sort these" queue for a human.

THREE layers, the last is always a human (spec §0):
  1. INTENT BEATS INFERENCE. A declared upload lane ("Finished posts" / "Raw
     footage") or a Drive folder mapping OVERRIDES the classifier outright. classify()
     honors a `declared_lane` and never guesses when intent is known.
  2. The classifier below, for everything unmapped. Signals (spec §0.2):
       - OCR on sampled frames finds burned-in text  -> finished
       - 9:16 AND 3..60s                              -> finished
       - 16:9 / 4:3, or > 90s                         -> raw
       - camera-native filename (IMG_/DJI_/GX/PXL_/.MOV off a phone) -> raw
       - high cut density for its length              -> finished
       - content_hash matches one of Echo's own past renders -> finished AND
         blocked from re-ingest (the re-ingest guard, story_ledger)
  3. AMBIGUOUS never auto-posts. It lands in the sort queue (story_sort_queue) and a
     coach-channel digest fires when the queue is non-empty. A silent wrong guess is
     the ONLY unacceptable outcome, so the classifier fails toward the human.

OCR and probe are INJECTABLE so the suite runs offline: a missing OCR reader / probe
degrades to "signal unknown" (never a crash, never a fabricated verdict), which pushes
a borderline file toward AMBIGUOUS rather than a confident wrong guess.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

RAW = "raw"
FINISHED = "finished"
AMBIGUOUS = "ambiguous"

# Declared lanes (spec §0.1: intent beats inference).
LANE_RAW = "raw"
LANE_FINISHED = "finished"

# camera-native filename stems (spec §0.2): a phone/action-cam original.
_CAMERA_NATIVE_RE = re.compile(
    r"^(img[_-]?\d|dji[_-]?\d|gx\d|gopr\d|pxl[_-]?\d|vid[_-]?\d|mov[_-]?\d|"
    r"mvimg|dsc[_-]?\d|clip\d|c\d{3,}|00\d{2})", re.IGNORECASE)
_PHONE_MOV_RE = re.compile(r"\.(mov|3gp)$", re.IGNORECASE)

# a "finished" render's filename often carries an edit-suite export stamp.
_EDITED_NAME_RE = re.compile(
    r"(final|export|edit(ed)?|reel|story|post|capcut|canva|1080x1920|9x16|"
    r"vertical|caption(ed)?|_v\d)", re.IGNORECASE)

# Duration / aspect windows (spec §0.2).
_FINISHED_DUR_MIN = 3.0
_FINISHED_DUR_MAX = 60.0
_RAW_DUR_LONG = 90.0

# aspect: 9:16 ~= 0.5625; 16:9 ~= 1.777; 4:3 ~= 1.333.
_NINE_SIXTEEN = 9 / 16
_ASPECT_TOL = 0.06

# cut density (cuts per second) at/above which a clip reads as an edited cut-up.
_HIGH_CUT_DENSITY = 0.25


@dataclass
class Signals:
    """The raw evidence gathered for one file (each may be None = unknown/not probed)."""
    filename: str = ""
    content_hash: str = ""
    duration_sec: float | None = None
    width: int | None = None
    height: int | None = None
    has_burned_text: bool | None = None   # OCR result on sampled frames
    cut_density: float | None = None       # cuts per second (scene-change probe)
    kind: str = "video"                    # 'video' | 'photo'


@dataclass
class Verdict:
    verdict: str                           # RAW | FINISHED | AMBIGUOUS
    confidence: float                      # 0..1
    reasons: list = field(default_factory=list)   # human-readable signal notes
    finished_score: float = 0.0
    raw_score: float = 0.0
    is_echo_render: bool = False           # re-ingest guard tripped
    declared: bool = False                 # intent beat inference


def _aspect(width, height):
    try:
        w, h = float(width), float(height)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return w / h


def _is_vertical(width, height):
    a = _aspect(width, height)
    return a is not None and abs(a - _NINE_SIXTEEN) <= _ASPECT_TOL


def _is_landscape(width, height):
    a = _aspect(width, height)
    # 4:3 (1.333) or wider counts as landscape/raw.
    return a is not None and a >= (4 / 3) - 1e-6


def camera_native_filename(filename) -> bool:
    """True when the basename looks like an untouched phone/action-cam original."""
    base = os.path.basename(str(filename or "")).strip()
    stem = os.path.splitext(base)[0]
    if _CAMERA_NATIVE_RE.match(stem):
        return True
    # an .MOV / .3gp with NO edit-suite stamp in the name reads as a phone original.
    if _PHONE_MOV_RE.search(base) and not _EDITED_NAME_RE.search(stem):
        return True
    return False


def edited_filename(filename) -> bool:
    """True when the basename carries an edit-suite / export stamp (a finished cut)."""
    stem = os.path.splitext(os.path.basename(str(filename or "")))[0]
    return bool(_EDITED_NAME_RE.search(stem))


def score_signals(sig: Signals):
    """(finished_score, raw_score, reasons) from the gathered signals. Each signal is
    additive and independent; unknown signals contribute nothing (they never fabricate
    a verdict). The re-ingest guard is handled by the caller (classify), not scored
    here, because a hash match is a HARD finished+blocked, not a weighted vote."""
    finished = 0.0
    raw = 0.0
    reasons = []

    # burned-in text (OCR) -> finished (strong).
    if sig.has_burned_text is True:
        finished += 0.55
        reasons.append("burned-in text found (OCR) -> finished")
    elif sig.has_burned_text is False:
        reasons.append("no burned-in text (OCR)")

    # aspect + duration.
    if sig.kind == "video" and sig.duration_sec is not None:
        vertical = _is_vertical(sig.width, sig.height)
        landscape = _is_landscape(sig.width, sig.height)
        if vertical and _FINISHED_DUR_MIN <= sig.duration_sec <= _FINISHED_DUR_MAX:
            finished += 0.45
            reasons.append(
                f"9:16 and {sig.duration_sec:g}s in 3..60s -> finished")
        if landscape:
            raw += 0.5
            reasons.append("16:9 / 4:3 landscape -> raw")
        if sig.duration_sec > _RAW_DUR_LONG:
            raw += 0.5
            reasons.append(f"{sig.duration_sec:g}s > 90s -> raw")

    # camera-native filename -> raw.
    if camera_native_filename(sig.filename):
        raw += 0.45
        reasons.append("camera-native filename -> raw")
    elif edited_filename(sig.filename):
        finished += 0.3
        reasons.append("edit-suite / export filename -> finished")

    # high cut density for its length -> finished (an edited cut-up).
    if sig.cut_density is not None and sig.cut_density >= _HIGH_CUT_DENSITY:
        finished += 0.4
        reasons.append(
            f"high cut density ({sig.cut_density:.2f}/s) -> finished")

    return finished, raw, reasons


# Decision thresholds. A verdict is confident only when its winning score clears the
# floor AND clearly beats the other side; otherwise it is AMBIGUOUS (fail to human).
_DECIDE_FLOOR = 0.5
_DECIDE_MARGIN = 0.25


def classify(sig: Signals, *, declared_lane=None, ledger_lookup=None):
    """Classify ONE file into a Verdict.

    declared_lane: 'raw' | 'finished' (from a portal upload lane or a Drive folder
    mapping). When set, intent BEATS inference: the verdict is the declared lane at
    full confidence and NO signal scoring runs (spec §0.1). The re-ingest guard STILL
    runs even on a declared lane — a declared file whose bytes are Echo's own render is
    still blocked from re-ingest (Echo can never eat its own output, however it is
    declared).

    ledger_lookup: injectable is_echo_render(content_hash) -> bool. Defaults to
    story_ledger.is_echo_render. A hash match is a HARD (FINISHED, blocked) verdict.
    """
    lookup = ledger_lookup
    if lookup is None:
        from . import story_ledger
        lookup = story_ledger.is_echo_render

    # RE-INGEST GUARD first — it overrides everything, declared lane included.
    is_echo = False
    if sig.content_hash:
        try:
            is_echo = bool(lookup(sig.content_hash))
        except Exception as e:  # noqa: BLE001 - a ledger failure must not crash the sort
            print(f"[story-classifier] ledger lookup failed: {type(e).__name__}: {e}")
            is_echo = False
    if is_echo:
        return Verdict(
            verdict=FINISHED, confidence=1.0,
            reasons=["content_hash matches one of Echo's own past renders "
                     "-> finished AND blocked from re-ingest (EP124 guard)"],
            is_echo_render=True)

    # INTENT BEATS INFERENCE: a declared lane wins outright.
    if declared_lane in (LANE_RAW, LANE_FINISHED):
        return Verdict(
            verdict=declared_lane, confidence=1.0,
            reasons=[f"declared lane '{declared_lane}' (intent beats inference)"],
            declared=True)

    finished, raw, reasons = score_signals(sig)
    if finished >= _DECIDE_FLOOR and finished - raw >= _DECIDE_MARGIN:
        return Verdict(verdict=FINISHED, confidence=min(1.0, finished),
                       reasons=reasons, finished_score=finished, raw_score=raw)
    if raw >= _DECIDE_FLOOR and raw - finished >= _DECIDE_MARGIN:
        return Verdict(verdict=RAW, confidence=min(1.0, raw),
                       reasons=reasons, finished_score=finished, raw_score=raw)
    # Neither side is confident enough: fail to a human.
    reasons.append("no confident signal -> AMBIGUOUS (queued for a human)")
    return Verdict(verdict=AMBIGUOUS, confidence=max(finished, raw),
                   reasons=reasons, finished_score=finished, raw_score=raw)


# ---- signal gathering (injectable OCR + cut-density; offline-safe) ----------
def gather_signals(asset, *, local_path=None, ocr_reader=None, cut_probe=None):
    """Build a Signals from a media_asset row (+ an optional downloaded local_path).

    ocr_reader(local_path) -> bool | None: True when burned-in text is found on
    sampled frames, False when none, None when OCR could not run (missing tool).
    cut_probe(local_path)  -> float | None: cuts per second, or None when unavailable.
    Both default to None (not run) so the classifier works from row metadata alone in
    an offline test env; a missing probe leaves that signal UNKNOWN (never fabricated),
    which is exactly what pushes a borderline file to AMBIGUOUS instead of a wrong guess.
    """
    kind = str(asset.get("kind") or "video").strip().lower()
    sig = Signals(
        filename=asset.get("title") or "",
        content_hash=asset.get("content_hash") or "",
        duration_sec=_num(asset.get("duration_sec")),
        width=_int(asset.get("width")),
        height=_int(asset.get("height")),
        kind="photo" if kind == "photo" else "video",
    )
    if local_path and ocr_reader is not None:
        try:
            sig.has_burned_text = ocr_reader(local_path)
        except Exception as e:  # noqa: BLE001
            print(f"[story-classifier] OCR reader failed: {type(e).__name__}: {e}")
            sig.has_burned_text = None
    if local_path and cut_probe is not None and sig.kind == "video":
        try:
            sig.cut_density = cut_probe(local_path)
        except Exception as e:  # noqa: BLE001
            print(f"[story-classifier] cut probe failed: {type(e).__name__}: {e}")
            sig.cut_density = None
    return sig


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


# ---- REAL, LIVE probes (2026-09-01 hardening) --------------------------------
# The proof run found 3 already-finished, captioned clips (2 with a mid-screen
# burned caption on top of a real gym recording, 1 a fully composited fake-SMS
# marketing graphic over dimmed footage) sitting in a gym's raw pool with
# eligible=true. Root cause (confirmed, not guessed): agent/jobs/sync_gym_media.py
# called gather_signals(asset) with NO ocr_reader / cut_probe, so has_burned_text
# and cut_density were ALWAYS None in production — the two signals this module's
# own header promised layer 2 would use never actually ran. These two functions
# are the real, live probes, wired into the sync job's existing budgeted download
# (agent/jobs/sync_gym_media.py sync_source step 5) — no new download path, no new
# provider. OCR reuses the SAME Gemini vision transcription agent/ocr_check.py
# already uses for the headline-quality guard (no new API, no new cost path);
# cut density reuses ffmpeg, already a hard dependency (nixpacks.toml) for the
# HEVC transcode + video probe. Both degrade to None (never a fabricated verdict)
# when ffmpeg or the vision reader is unavailable — exactly the existing
# "unknown signal pushes toward AMBIGUOUS" contract this module already documents.
def _probe_duration(local_path):
    """ffprobe duration in seconds, or None (never raises)."""
    import subprocess
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(local_path)],
            capture_output=True, text=True, timeout=20)
        return float((proc.stdout or "").strip())
    except Exception:  # noqa: BLE001
        return None


def _extract_frame(local_path, ts):
    """One downscaled PNG frame at ts seconds (or the first frame when ts is
    None), as a temp file path the caller must remove. None on any failure."""
    import subprocess
    import tempfile
    fd, out_path = tempfile.mkstemp(suffix=".png", prefix="story_ocr_frame_")
    os.close(fd)
    cmd = ["ffmpeg", "-y"]
    if ts is not None:
        cmd += ["-ss", str(max(0.0, ts))]
    cmd += ["-i", str(local_path), "-frames:v", "1", "-vf", "scale=480:-1", out_path]
    try:
        subprocess.run(cmd, capture_output=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        print(f"[story-classifier] frame extract failed: {type(e).__name__}: {e}")
        try:
            os.remove(out_path)
        except OSError:
            pass
        return None
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path
    try:
        os.remove(out_path)
    except OSError:
        pass
    return None


def _meaningful_text(text):
    """A real threshold, not a bare truthiness check: a stray OCR artifact (one
    stray character, whitespace) must not read as 'burned-in text found'."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", text or "")
    return len(cleaned) >= 4


def default_ocr_reader(local_path):
    """Real burned-in-text detection for a downloaded video file.

    Samples 2 frames (20% and 60% of duration; the first frame when duration is
    unknown) and transcribes each with the SAME Gemini vision reader
    agent/ocr_check.py already uses for the headline quality guard. True when
    either sampled frame carries meaningful rendered text (the P1-1 / P1-3 /
    R2-dark class: a caption, a benefit list, a fake-SMS bubble — all text-heavy),
    False when both come back clean, None when the read could not run at all (no
    ffmpeg, no vision reader armed, every attempt errored) — an unknown signal
    that pushes a borderline file to AMBIGUOUS, never a fabricated verdict.
    """
    from . import ocr_check as _ocr

    try:
        reader = _ocr._default_reader()  # noqa: SLF001 - the same lazy Gemini client
    except Exception as e:  # noqa: BLE001
        print(f"[story-classifier] OCR reader unavailable: {type(e).__name__}: {e}")
        return None
    if reader is None:
        return None
    dur = _probe_duration(local_path)
    fractions = (0.2, 0.6) if dur else (None,)
    ran_any = False
    for frac in fractions:
        ts = None if frac is None else max(0.3, dur * frac)
        frame = _extract_frame(local_path, ts)
        if not frame:
            continue
        ran_any = True
        try:
            text = _ocr.rendered_read(frame, reader=reader)
        except Exception as e:  # noqa: BLE001
            print(f"[story-classifier] OCR frame read failed: {type(e).__name__}: {e}")
            text = ""
        finally:
            try:
                os.remove(frame)
            except OSError:
                pass
        if _meaningful_text(text):
            return True
    return False if ran_any else None


def default_cut_probe(local_path):
    """Real cut-density signal: ffmpeg's scene-change filter counts hard cuts;
    cuts / duration = cuts per second. Backs the ALREADY-SCORED cut_density signal
    in score_signals(), which had no live probe wired to it before this hardening.
    None when ffmpeg is unavailable or the probe fails; never a fabricated number.
    """
    import subprocess
    dur = _probe_duration(local_path)
    if not dur or dur <= 0:
        return None
    try:
        proc = subprocess.run(
            ["ffmpeg", "-i", str(local_path), "-filter:v",
             "select='gt(scene,0.35)',showinfo", "-f", "null", "-"],
            capture_output=True, text=True, timeout=45)
    except Exception as e:  # noqa: BLE001
        print(f"[story-classifier] cut probe failed: {type(e).__name__}: {e}")
        return None
    log = proc.stderr or ""
    if "Parsed_showinfo" not in log and proc.returncode not in (0,):
        return None  # ffmpeg could not read this file at all
    cuts = log.count("Parsed_showinfo")
    return round(cuts / dur, 4)
